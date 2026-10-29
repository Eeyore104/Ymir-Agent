"""增量质量硬化回归用例（v1.3 · T1.4 / QA F1–F5，docs/11 §7.4 / §7.6）。

把 v1.2 真跑暴露的两类失败（场景② fastapi-vue3 / 场景③ python-cli：模型重写
``tests/conftest.py`` 导致基线既有测试集体失效）固化为**回归测试**。

四类用例（T1.4 DoD）：

1. **受保护路径 · reject 策略**：模型改 ``tests/conftest.py`` ⇒ 抛
   :class:`ProtectedPathConflict`（1003 / ``details.reason=protected_path``）；
2. **受保护路径 · ignore 策略**：保留基线版本 + manifest 记
   ``protected_path`` 冲突 + ``counts.protected_hits``；
3. **改动面告警**：``change_ratio`` 超阈值产生 ``warnings``，且**不阻断**；
4. **基线独立执行**：``healthy`` / ``unhealthy`` / ``skipped`` 三态各一例 +
   ``regression`` 判定（基线通过而合并树失败）。

另外覆盖 Q1 三段式（编排层）：reject → 1 次定向重试 → 兜底 ignore 不整轮失败，
以及 QA 回修项：

- **F1**：``protected_path_forced_ignore`` 必须出现在**落盘正文**（不是只在内存对象）；
- **F2**：原样回显受保护文件 = 未修改（走 noop），不误判命中、不白烧重试；
- **F3**：基线探测缓存键覆盖 ``requires`` / ``group`` / ``blocking`` / ``timeout_s``；
- **F4**：``tests``（无尾斜杠）等价于 ``tests/``，目录保护不静默失效；
- **F5**：``env_missing`` 降级结果**不进**缓存。

v1.3.1 追加（batch2 · T4 证据链，docs/13 §7.3-5 / §7.3-6）：

- **T4.1**：模板 ``frontend_build`` 命令进冒烟 —— 只有模板条目带 ``source``
  （无模板时报告逐字节不变）、与模型命令去重、``requires`` 缺失即跳过、
  ``all_ok`` 三态（全跳过 ⇒ ``None`` 而不是 ``True``）；
- **T4.2**：QA「基线已知事实」—— 事实**只**取本 stage 上游那一版 ``code``
  产物（``_baseline_facts`` 结构性不碰 store），三种缺料只降级不抛。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.merge import (
    CHANGE_RATIO_WARNING_PREFIX,
    DEFAULT_MAX_CHANGE_RATIO,
    PATCH_MANIFEST_NAME,
    PROTECTED_HIT_WARNING_PREFIX,
    ProtectedPathConflict,
    compute_change_ratio,
    detect_protected_hits,
    merge_trees,
)
from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore
from ymir.common.contracts import UpstreamArtifact
from ymir.common.errors import BizCode, InvalidParamError
from ymir.evaluation.baseline_probe import (
    BASELINE_HEALTHY,
    BASELINE_SKIPPED,
    BASELINE_UNHEALTHY,
    PROBE_MODE_RAW,
    PROBE_MODE_SKELETON_FILLED,
    SKIP_BASELINE_INCOMPLETE,
    SKIP_FILE_MISSING_PREFIX,
    BaselineProbeCache,
    baseline_cache_key,
    build_probe_tree,
    missing_requires_files,
    probe_baseline,
)
from ymir.evaluation.evaluators.base import CheckRun, EvalCheck, EvalTask
from ymir.evaluation.gate import HarnessEvaluationGate
from ymir.evaluation.harness import (
    BASELINE_INCOMPLETE_WARNING,
    BASELINE_UNHEALTHY_WARNING,
    EvaluationHarness,
)
from ymir.evaluation.run_task import build_task_for_run, resolve_checks
from ymir.evaluation.suite import EvalSuite
from ymir.orchestrator.role_executor import (
    PROTECTED_FORCED_IGNORE_PREFIX,
    RoleStageExecutor,
)
from ymir.roles.io import BaselineContext, StageInput, StageOutput
from ymir.sandbox.base import ExecResult, NetworkPolicy, SandboxSession
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates import load_template
from ymir.templates.models import CheckSpec
from ymir.tools.read_artifact import read_artifacts

# --------------------------------------------------------------- 基线 / 补丁样本

CONFTEST = """import pytest


@pytest.fixture()
def client():
    return {"ok": True}
"""

BASE: dict[str, str] = {
    "pyproject.toml": "[project]\nname = 'demo'\n",
    "app/__init__.py": "",
    "app/main.py": "def main() -> None:\n    return None\n",
    "tests/conftest.py": CONFTEST,
    "tests/test_main.py": (
        "from conftest import client\n\n\ndef test_ok(client):\n    assert client\n"
    ),
    "README.md": "# demo\n",
}

#: 模型"想加新测试 → 顺手重写 conftest"的典型输出（场景②③的根因形态）
CONFTEST_REWRITE = """import pytest


@pytest.fixture()
def db():
    return None
"""


# --------------------------------------------------------------- 假沙箱（脚本化）


class _ScriptedSession(SandboxSession):
    """按「工作区里是否出现 marker」决定退出码的假会话（确定性、零 Docker）。

    ``missing_bins``：``command -v <bin>`` 返回 127（模拟镜像缺工具链），
    用于覆盖 ``requires`` → ``skipped`` → ``env_missing`` 的降级链（F5 用例）。
    ``fail_commands``：命令含该子串 ⇒ exit 1（v1.3.1：用于「合并树侧缺目录仍
    failed」与「probe 跳过但合并树失败不判 regression」两条定点用例）。
    ``timeouts``：按调用顺序记录每次 ``exec`` 的 ``timeout`` —— T4.1 用它断言
    「模板命令用 ``check.timeout_s``、缺省回落 ``_exec_timeout``、``requires`` 探测
    用短超时」，避免为了看超时再造一个会话替身。
    """

    def __init__(
        self,
        *,
        fail_marker: str = "",
        missing_bins: tuple[str, ...] = (),
        fail_commands: tuple[str, ...] = (),
    ) -> None:
        self.files: dict[str, str] = {}
        self.fail_marker = fail_marker
        self.missing_bins = tuple(missing_bins)
        self.fail_commands = tuple(fail_commands)
        self.executed: list[str] = []
        self.timeouts: list[float] = []

    @property
    def sandbox_id(self) -> str | None:
        return "fake-guard-1"

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy.NONE

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        self.executed.append(cmd)
        self.timeouts.append(timeout)
        if cmd.startswith("command -v "):
            name = cmd[len("command -v ") :].strip()
            if name in self.missing_bins:
                return ExecResult(stdout="", stderr="", exit_code=127, duration_ms=1)
            return ExecResult(
                stdout=f"/usr/bin/{name}", stderr="", exit_code=0, duration_ms=1
            )
        if any(fragment in cmd for fragment in self.fail_commands):
            return ExecResult(stdout="", stderr="failed", exit_code=1, duration_ms=1)
        if self.fail_marker and self.fail_marker in self.files:
            return ExecResult(
                stdout="", stderr="AssertionError: broken", exit_code=1, duration_ms=1
            )
        return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        return None


class _ScriptedProvider:
    """每次进入都给一个**新会话**（基线探测与合并树评测互不污染）。"""

    def __init__(
        self,
        *,
        fail_marker: str = "",
        missing_bins: tuple[str, ...] = (),
        fail_commands: tuple[str, ...] = (),
    ) -> None:
        self.fail_marker = fail_marker
        self.missing_bins = missing_bins
        self.fail_commands = fail_commands
        self.sessions: list[_ScriptedSession] = []

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        session = _ScriptedSession(
            fail_marker=self.fail_marker,
            missing_bins=self.missing_bins,
            fail_commands=self.fail_commands,
        )
        self.sessions.append(session)
        yield session


# ============================================================ ① 受保护路径 · reject


class TestProtectedPathReject:
    """DoD ①：``reject`` 策略下命中受保护路径必抛冲突（1003 / 409）。"""

    def test_reject_raises_conflict_with_biz_code_and_reason(self) -> None:
        with pytest.raises(ProtectedPathConflict) as excinfo:
            merge_trees(
                BASE,
                {"tests/conftest.py": CONFTEST_REWRITE},
                mode="patch",
                protected_paths=frozenset({"tests/conftest.py"}),
            )
        error = excinfo.value
        assert error.biz_code == BizCode.STATE_CONFLICT
        assert int(error.biz_code) == 1003
        assert error.http_status == 409
        assert error.reason == "protected_path"
        assert error.paths == ("tests/conftest.py",)
        assert error.details == {
            "reason": "protected_path",
            "paths": ["tests/conftest.py"],
        }

    def test_reject_raises_before_applying_any_patch(self) -> None:
        """整批抛出：命中集合在应用补丁**之前**算出（便于一次回喂全部路径）。"""
        patch = {
            "app/api/items.py": "x = 1\n",  # 合法改动
            "tests/conftest.py": CONFTEST_REWRITE,  # 受保护
        }
        with pytest.raises(ProtectedPathConflict) as excinfo:
            merge_trees(
                BASE,
                patch,
                mode="patch",
                protected_paths=frozenset({"tests/conftest.py"}),
            )
        assert excinfo.value.paths == ("tests/conftest.py",)

    def test_default_policy_is_reject(self) -> None:
        """默认策略为 ``reject``（Q1 定案 A）：不显式传 ``on_protected_hit`` 也会抛。"""
        with pytest.raises(ProtectedPathConflict):
            merge_trees(
                BASE,
                {"tests/conftest.py": CONFTEST_REWRITE},
                mode="patch",
                protected_paths=frozenset({"tests/conftest.py"}),
            )

    def test_empty_protected_paths_never_raises(self) -> None:
        """兼容性红线：未配置 ``protected_paths`` 时行为与 v1.2 完全一致。"""
        merged = merge_trees(BASE, {"tests/conftest.py": CONFTEST_REWRITE}, mode="patch")
        assert merged.files["tests/conftest.py"] == CONFTEST_REWRITE
        assert merged.manifest.counts.protected_hits == 0
        assert merged.manifest.warnings == []

    def test_directory_without_trailing_slash_is_treated_as_prefix(self) -> None:
        """F4（QA P3）：``tests``（无尾斜杠）等价于 ``tests/``，目录保护不静默失效。"""
        with pytest.raises(ProtectedPathConflict):
            merge_trees(
                BASE,
                {"tests/fixtures/a.json": "{}\n"},
                mode="patch",
                protected_paths=frozenset({"tests"}),  # 作者本意是目录
            )

    def test_extensionless_file_can_be_protected(self) -> None:
        """F6（QA P3-2）：``protected: ["Dockerfile"]`` 必须拦住对 ``Dockerfile`` **本体**的改写。

        归一化会把 ``Dockerfile``（末段无扩展名）当目录补成 ``Dockerfile/``；
        只按 ``p == d or p.startswith(d)`` 匹配时，``Dockerfile`` 两条都不满足 ⇒
        保护静默失效。目录项现在额外匹配「去掉尾斜杠后的本名」。
        """
        base = {"Dockerfile": "FROM python:3.12-slim\n", "app/main.py": "x = 1\n"}
        protected = frozenset({"Dockerfile"})  # 加载期会归一成 Dockerfile/

        with pytest.raises(ProtectedPathConflict) as excinfo:
            merge_trees(
                base, {"Dockerfile": "FROM evil\n"}, mode="patch", protected_paths=protected
            )
        assert excinfo.value.paths == ("Dockerfile",)

        merged = merge_trees(
            base,
            {"Dockerfile": "FROM evil\n"},
            mode="patch",
            protected_paths=protected,
            on_protected_hit="ignore",
        )
        assert merged.files["Dockerfile"] == base["Dockerfile"]  # 保留基线
        assert merged.manifest.counts.protected_hits == 1
        # 原样回显仍走 noop（F2 语义不被 F6 破坏）
        echoed = merge_trees(
            base, {"Dockerfile": base["Dockerfile"]}, mode="patch", protected_paths=protected
        )
        assert echoed.manifest.counts.protected_hits == 0
        assert echoed.manifest.counts.noop_dropped == 1

    def test_extensionless_protection_boundaries(self) -> None:
        """F6 边界：``Dockerfile.bak`` 不拦（不是同名/子路径），``Dockerfile/extra`` 拦。"""
        base = {"Dockerfile": "FROM base\n", "app/main.py": "x = 1\n"}
        protected = frozenset({"Dockerfile"})

        # Dockerfile.bak：不在保护范围 ⇒ 不抛、不改
        merged = merge_trees(
            base,
            {"Dockerfile.bak": "FROM copy\n"},
            mode="patch",
            protected_paths=protected,
        )
        assert merged.files["Dockerfile.bak"] == "FROM copy\n"
        assert merged.manifest.counts.protected_hits == 0

        # Dockerfile/extra：目录语义仍生效 ⇒ 命中
        with pytest.raises(ProtectedPathConflict):
            merge_trees(
                base,
                {"Dockerfile/extra": "x\n"},
                mode="patch",
                protected_paths=protected,
            )

    def test_directory_prefix_protection(self) -> None:
        """尾斜杠 = 目录前缀（与 ``layout.allowed_prefixes`` 同口径）。"""
        assert detect_protected_hits(
            {"tests/fixtures/a.json", "tests/conftest.py"},
            frozenset({"tests/fixtures/"}),
        ) == ("tests/fixtures/a.json",)
        with pytest.raises(ProtectedPathConflict):
            merge_trees(
                BASE,
                {"tests/fixtures/a.json": "{}\n"},
                mode="patch",
                protected_paths=frozenset({"tests/fixtures/"}),
            )

    def test_delete_of_protected_path_is_also_blocked(self) -> None:
        """删除同样被拦：否则模型可用 ``deleted`` 绕过硬约束。"""
        with pytest.raises(ProtectedPathConflict):
            merge_trees(
                BASE,
                {},
                mode="patch",
                deleted=["tests/conftest.py"],
                protected_paths=frozenset({"tests/conftest.py"}),
            )

    def test_invalid_params_rejected(self) -> None:
        with pytest.raises(InvalidParamError):
            merge_trees(BASE, {}, mode="patch", on_protected_hit="block")  # type: ignore[arg-type]
        with pytest.raises(InvalidParamError):
            merge_trees(BASE, {}, mode="patch", max_change_ratio=0)
        with pytest.raises(InvalidParamError):
            merge_trees(BASE, {}, mode="patch", max_change_ratio=1.5)


# ============================================================ ② 受保护路径 · ignore


class TestProtectedPathIgnore:
    """DoD ②：``ignore`` 策略保留基线版本，并把拦截记进 manifest。"""

    def test_ignore_keeps_baseline_content(self) -> None:
        merged = merge_trees(
            BASE,
            {"tests/conftest.py": CONFTEST_REWRITE, "app/api/items.py": "x = 1\n"},
            mode="patch",
            protected_paths=frozenset({"tests/conftest.py"}),
            on_protected_hit="ignore",
        )
        # 基线版本原样保留（模型的 conftest 被丢弃）
        assert merged.files["tests/conftest.py"] == CONFTEST
        # 其它改动照常合并
        assert merged.files["app/api/items.py"] == "x = 1\n"
        # 冲突可见（审批卡片必须看得到）
        assert [(item.path, item.reason) for item in merged.conflicts] == [
            ("tests/conftest.py", "protected_path")
        ]
        assert merged.manifest.counts.protected_hits == 1
        assert merged.manifest.warnings == [f"{PROTECTED_HIT_WARNING_PREFIX}1"]
        # 受保护路径不进 changed_paths（内容没变）
        assert "tests/conftest.py" not in merged.manifest.changed_paths

    def test_ignore_records_manifest_json(self) -> None:
        merged = merge_trees(
            BASE,
            {"tests/conftest.py": CONFTEST_REWRITE},
            mode="patch",
            protected_paths=frozenset({"tests/conftest.py"}),
            on_protected_hit="ignore",
        )
        payload = json.loads(merged.manifest_json())
        assert payload["counts"]["protected_hits"] == 1
        assert payload["warnings"] == [f"{PROTECTED_HIT_WARNING_PREFIX}1"]
        assert payload["conflicts"] == [
            {"path": "tests/conftest.py", "reason": "protected_path"}
        ]

    def test_identical_echo_is_noop_not_hit(self) -> None:
        """F2（QA P2）：**原样回显**受保护文件 = 未修改 ⇒ 不算命中、不烧重试。

        v1.2 的 noop 语义本就该吃掉它；若判成命中，reject 会白烧一次全量模型重试、
        ignore 会产生 ``protected_path_ignored:1`` 的假噪音。
        """
        for policy in ("reject", "ignore"):
            merged = merge_trees(
                BASE,
                {"tests/conftest.py": CONFTEST, "app/extra.py": "x = 1\n"},
                mode="patch",
                protected_paths=frozenset({"tests/conftest.py"}),
                on_protected_hit=policy,  # type: ignore[arg-type]
            )
            assert merged.manifest.counts.protected_hits == 0, policy
            assert merged.manifest.counts.noop_dropped == 1, policy
            assert merged.manifest.warnings == [], policy
            assert merged.conflicts == (), policy
            assert merged.files["tests/conftest.py"] == CONFTEST, policy
            # 正常改动照常合入
            assert merged.files["app/extra.py"] == "x = 1\n", policy

    def test_real_modification_still_hits(self) -> None:
        """F2 对照：**真有改动**（哪怕只差一个字节）仍必须命中。"""
        tweaked = CONFTEST + "# 多一行\n"
        with pytest.raises(ProtectedPathConflict):
            merge_trees(
                BASE,
                {"tests/conftest.py": tweaked},
                mode="patch",
                protected_paths=frozenset({"tests/conftest.py"}),
            )
        merged = merge_trees(
            BASE,
            {"tests/conftest.py": tweaked},
            mode="patch",
            protected_paths=frozenset({"tests/conftest.py"}),
            on_protected_hit="ignore",
        )
        assert merged.manifest.counts.protected_hits == 1
        assert merged.files["tests/conftest.py"] == CONFTEST  # 保留基线版本

    def test_delete_still_hits_even_if_content_unchanged(self) -> None:
        """F2 对照：``deleted`` 通道**无条件命中**（删除不可逆，不能靠"内容相同"豁免）。"""
        with pytest.raises(ProtectedPathConflict):
            merge_trees(
                BASE,
                {"tests/conftest.py": CONFTEST},  # 原样回显 + 同时声明删除
                mode="patch",
                deleted=["tests/conftest.py"],
                protected_paths=frozenset({"tests/conftest.py"}),
            )

    def test_ignore_keeps_protected_file_on_delete(self) -> None:
        merged = merge_trees(
            BASE,
            {},
            mode="patch",
            deleted=["tests/conftest.py"],
            protected_paths=frozenset({"tests/conftest.py"}),
            on_protected_hit="ignore",
        )
        assert merged.files["tests/conftest.py"] == CONFTEST
        assert merged.manifest.counts.deleted == 0
        assert merged.manifest.counts.protected_hits == 1


# ================================================================ ③ 改动面告警


class TestChangeRatioWarning:
    """DoD ③：``change_ratio`` 超阈值只记 warning，**不阻断**合并。"""

    def test_change_ratio_over_threshold_warns_but_merges(self) -> None:
        # 基线 6 个文件，改 4 个 ⇒ 0.67 > 0.5
        patch = {
            "app/main.py": "def main() -> int:\n    return 1\n",
            "app/extra.py": "x = 1\n",
            "app/more.py": "y = 2\n",
            "tests/test_extra.py": "def test_x():\n    assert True\n",
        }
        merged = merge_trees(BASE, patch, mode="patch")
        assert merged.manifest.change_ratio == pytest.approx(0.67)
        assert merged.manifest.warnings == [f"{CHANGE_RATIO_WARNING_PREFIX}0.67"]
        # 不阻断：改动全部生效
        for path in patch:
            assert merged.files[path] == patch[path]

    def test_change_ratio_within_threshold_is_silent(self) -> None:
        merged = merge_trees(BASE, {"app/main.py": "x = 1\n"}, mode="patch")
        assert merged.manifest.change_ratio == pytest.approx(0.17)
        assert merged.manifest.warnings == []

    def test_threshold_is_configurable(self) -> None:
        patch = {"app/main.py": "x = 1\n", "app/b.py": "y = 2\n"}
        # 阈值放宽到 1.0 ⇒ 不再告警
        quiet = merge_trees(BASE, patch, mode="patch", max_change_ratio=1.0)
        assert quiet.manifest.warnings == []
        # 阈值收紧到 0.2 ⇒ 告警
        loud = merge_trees(BASE, patch, mode="patch", max_change_ratio=0.2)
        assert loud.manifest.warnings == [f"{CHANGE_RATIO_WARNING_PREFIX}0.33"]

    def test_from_scratch_has_zero_ratio(self) -> None:
        merged = merge_trees({}, {"app/main.py": "x = 1\n"}, mode="from_scratch")
        assert merged.manifest.change_ratio == 0.0
        assert merged.manifest.warnings == []
        assert compute_change_ratio(["a.py"], 0) == 0.0
        assert DEFAULT_MAX_CHANGE_RATIO == 0.5


# ============================================================ ④ 基线独立执行三态


class TestBaselineProbe:
    """DoD ④：``healthy`` / ``unhealthy`` / ``skipped`` 三态。"""

    CHECKS = [EvalCheck(run="python -m pytest -q", weight=1.0)]

    async def test_healthy_when_all_checks_pass(self) -> None:
        provider = _ScriptedProvider()
        probe = await probe_baseline(
            files=BASE, checks=self.CHECKS, sandbox_provider=provider
        )
        assert probe.status == BASELINE_HEALTHY
        assert probe.passed is True
        assert probe.score == 100.0
        assert probe.reason == ""
        # 独立会话：探测只写基线文件，不碰合并树
        assert provider.sessions[0].files == BASE

    async def test_unhealthy_when_baseline_itself_fails(self) -> None:
        provider = _ScriptedProvider(fail_marker="app/main.py")
        probe = await probe_baseline(
            files=BASE, checks=self.CHECKS, sandbox_provider=provider
        )
        assert probe.status == BASELINE_UNHEALTHY
        assert probe.passed is False
        assert probe.score == 0.0
        assert "python -m pytest -q" in probe.reason

    async def test_skipped_when_sandbox_missing(self) -> None:
        probe = await probe_baseline(
            files=BASE, checks=self.CHECKS, sandbox_provider=None
        )
        assert probe.status == BASELINE_SKIPPED
        assert probe.reason == "no_sandbox"

    async def test_skipped_when_baseline_or_checks_missing(self) -> None:
        provider = _ScriptedProvider()
        empty_files = await probe_baseline(
            files={}, checks=self.CHECKS, sandbox_provider=provider
        )
        assert empty_files.status == BASELINE_SKIPPED
        assert empty_files.reason == "baseline_unavailable"
        no_checks = await probe_baseline(
            files=BASE, checks=[], sandbox_provider=provider
        )
        assert no_checks.status == BASELINE_SKIPPED
        assert no_checks.reason == "no_checks"
        assert provider.sessions == []  # 降级路径不启会话

    async def test_cache_key_covers_all_check_fields(self) -> None:
        """F3（QA P3）：``requires`` / ``group`` / ``blocking`` / ``timeout_s`` 都进指纹。"""
        command = "npx tsc --noEmit"
        variants = [
            EvalCheck(run=command, weight=1.0),
            EvalCheck(run=command, weight=2.0),
            EvalCheck(run=command, weight=1.0, requires="npx"),
            EvalCheck(run=command, weight=1.0, requires="node"),
            EvalCheck(run=command, weight=1.0, group="frontend_build"),
            EvalCheck(run=command, weight=1.0, blocking=True),
            EvalCheck(run=command, weight=1.0, timeout_s=300),
        ]
        keys = {baseline_cache_key(BASE, [variant]) for variant in variants}
        # 7 个互不相同的字段组合 ⇒ 7 个不同指纹（少拼任一字段就会撞成同一个）
        assert len(keys) == 7
        # 基线内容变了，键也必须变
        assert baseline_cache_key({**BASE, "x.py": "y = 1\n"}, [variants[0]]) not in keys

    async def test_env_missing_is_not_cached(self) -> None:
        """F5（QA P3）：``env_missing`` 不进缓存（否则"后来装了 node"在本进程永久失效）。"""
        checks = [EvalCheck(run="npx tsc --noEmit", weight=1.0, requires="npx")]
        provider = _ScriptedProvider(missing_bins=("npx",))
        cache = BaselineProbeCache()
        key = baseline_cache_key(BASE, checks)
        first = await probe_baseline(
            files=BASE, checks=checks, sandbox_provider=provider, cache=cache, cache_key=key
        )
        second = await probe_baseline(
            files=BASE, checks=checks, sandbox_provider=provider, cache=cache, cache_key=key
        )
        assert first.reason == "env_missing" and second.reason == "env_missing"
        assert first.cached is False and second.cached is False
        assert len(provider.sessions) == 2  # 两次都真跑（未命中缓存）
        assert len(cache) == 0  # 缓存里没有这条降级结果

    async def test_cache_reuses_probe(self) -> None:
        provider = _ScriptedProvider()
        cache = BaselineProbeCache()
        key = baseline_cache_key(BASE, self.CHECKS)
        first = await probe_baseline(
            files=BASE,
            checks=self.CHECKS,
            sandbox_provider=provider,
            cache=cache,
            cache_key=key,
        )
        second = await probe_baseline(
            files=BASE,
            checks=self.CHECKS,
            sandbox_provider=provider,
            cache=cache,
            cache_key=key,
        )
        assert first.cached is False
        assert second.cached is True
        assert len(provider.sessions) == 1  # 只真跑一次


# ============================================================ regression 判定（整链路）


async def _seed_incremental_project(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    baseline_files: dict[str, str],
    patch_files: dict[str, str],
    extra_files: dict[str, str] | None = None,
) -> tuple[ArtifactStore, str, str]:
    """铺一个增量项目：baseline 产物 + 带 ``patch_manifest.json`` 的 code 产物。

    Args:
        baseline_files: 基线文件树。
        patch_files: 模型补丁（经 ``merge_trees`` 合并后只存**改动**的文件）。
        extra_files: 额外塞进 code 产物的文件（v1.3.1 T4.2 用它放
            ``smoke_report.json``，构造「事实来源」用例）；缺省 ``None`` ⇒ 与
            既有用例的产物内容完全一致。
    """
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="guard-incremental")
        await session.commit()
        project_id = project.id

    baseline = await store.save(
        project_id=project_id,
        name="baseline",
        artifact_type=BASELINE_TYPE,
        files=baseline_files,
        created_by="user",
    )
    merged = merge_trees(baseline_files, patch_files, mode="patch")
    stored = {
        path: merged.files[path]
        for path in merged.manifest.changed_paths
        if path in merged.files
    }
    stored[PATCH_MANIFEST_NAME] = merged.manifest_json()
    if extra_files:
        stored.update(extra_files)
    await store.save(
        project_id=project_id, name="code", artifact_type="code", files=stored
    )
    return store, project_id, baseline.artifact_id


class TestRegressionJudgement:
    """三态之三：基线通过 + 合并树失败 ⇒ ``regression``（而不是"模型能力差"）。"""

    CHECKS = [EvalCheck(run="python -m pytest -q", weight=1.0)]

    def _task(self, baseline_id: str) -> EvalTask:
        return EvalTask(
            id="t-guard",
            artifact_name="code",
            checks=list(self.CHECKS),
            baseline_ref={"artifact_id": baseline_id, "version": 1},
        )

    async def test_regression_when_merged_tree_fails_but_baseline_healthy(
        self, session_factory, tmp_path: Path
    ) -> None:
        # 补丁新增 app/broken.py ⇒ 只有合并树里出现该 marker（基线没有）
        store, project_id, baseline_id = await _seed_incremental_project(
            session_factory,
            tmp_path,
            baseline_files=BASE,
            patch_files={"app/broken.py": "BROKEN = True\n"},
        )
        harness = EvaluationHarness(
            store=store, sandbox_provider=_ScriptedProvider(fail_marker="app/broken.py")
        )
        outcome = await harness.evaluate_task(
            self._task(baseline_id), project_id=project_id
        )

        assert outcome.baseline_runnability is not None
        assert outcome.baseline_runnability.status == BASELINE_HEALTHY
        assert outcome.regression is True
        assert "app/broken.py" not in outcome.regression_reason  # 原因给的是失败命令
        assert "python -m pytest -q" in outcome.regression_reason
        # 通过判定不受影响（回归不改 passed 语义，只做归因）
        assert BASELINE_UNHEALTHY_WARNING not in outcome.warnings

    async def test_unhealthy_baseline_degrades_verdict_to_unknown(
        self, session_factory, tmp_path: Path
    ) -> None:
        """基线自身就坏 ⇒ 分数不可比：``unknown(baseline_unhealthy)``，**不阻断**。"""
        broken_base = {**BASE, "tests/test_broken.py": "def test_bad():\n    assert 0\n"}
        store, project_id, baseline_id = await _seed_incremental_project(
            session_factory,
            tmp_path,
            baseline_files=broken_base,
            patch_files={"tests/test_broken.py": "def test_bad():\n    assert 0\n"},
        )
        harness = EvaluationHarness(
            store=store,
            sandbox_provider=_ScriptedProvider(fail_marker="tests/test_broken.py"),
        )
        outcome = await harness.evaluate_task(
            self._task(baseline_id), project_id=project_id
        )

        assert outcome.baseline_runnability is not None
        assert outcome.baseline_runnability.status == BASELINE_UNHEALTHY
        assert outcome.regression is False  # 基线不健康 ⇒ 不归因为回归
        assert BASELINE_UNHEALTHY_WARNING in outcome.warnings

        gate = HarnessEvaluationGate(harness=harness, store=None)
        verdict = gate._to_verdict(EvalSuite.load("default"), outcome, 0)  # noqa: SLF001
        assert verdict.status == "unknown"
        assert verdict.failure_class == "baseline_unhealthy"
        assert verdict.degraded is True
        assert verdict.baseline_status == BASELINE_UNHEALTHY
        assert verdict.regression is False

    async def test_no_probe_without_baseline_ref(
        self, session_factory, tmp_path: Path
    ) -> None:
        """兼容性红线：无 ``baseline_ref`` ⇒ 不探测、新字段保持默认值。"""
        store, project_id, _baseline_id = await _seed_incremental_project(
            session_factory,
            tmp_path,
            baseline_files=BASE,
            patch_files={"app/items.py": "x = 1\n"},
        )
        harness = EvaluationHarness(store=store, sandbox_provider=_ScriptedProvider())
        task = EvalTask(id="t-plain", artifact_name="code", checks=list(self.CHECKS))
        # 无 baseline_ref 时 harness 用指纹匹配仍能解析基线，但探测需要显式引用 ——
        # 这里直接断言"没有引用就不探测"这一关闭条件。
        outcome = await harness.evaluate_task(task, project_id=project_id)
        assert outcome.baseline_runnability is None
        assert outcome.regression is False
        assert outcome.blocking_failed == []
        assert outcome.skipped_checks == []


# ============================================================ Q1 三段式（编排层）


def _output(files: dict[str, str], *, cost: float = 0.01) -> StageOutput:
    return StageOutput(
        files=dict(files),
        summary="ok",
        structured={"files": []},
        provider="fake",
        model="fake",
        prompt_tokens=100,
        completion_tokens=50,
        cost_cny=cost,
        schema_attempts=1,
    )


class _FakeRuntime:
    """脚本化 runtime：每次 ``execute`` 弹出一个预置产出，并记录入参。"""

    def __init__(self, outputs: list[StageOutput]) -> None:
        self._outputs = list(outputs)
        self.inputs: list[StageInput] = []

    async def execute(
        self, role_id: str, stage_input: StageInput, *, run_id: str, stage_id: str
    ) -> StageOutput:
        self.inputs.append(stage_input)
        index = min(len(self.inputs) - 1, len(self._outputs) - 1)
        return self._outputs[index]


def _executor(runtime: _FakeRuntime) -> RoleStageExecutor:
    """最小装配：只用 ``_execute_with_patch_contract`` 需要的那两个属性。"""
    executor = RoleStageExecutor.__new__(RoleStageExecutor)
    executor._runtime = runtime  # type: ignore[attr-defined]
    executor._max_patch_file_bytes = 512 * 1024  # type: ignore[attr-defined]
    return executor


def _stage() -> Any:
    return SimpleNamespace(id="coding", role="engineer")


def _run() -> Any:
    return SimpleNamespace(id="run-guard", context={})


def _template(*, on_hit: str = "reject", symbol_deletions: str = "off") -> Any:
    """最小 ``TemplateSpec`` 替身（只提供 ``_apply_patch_contract`` 用到的接口）。

    v1.3.1（T3.2）等价更新：``protected`` 多带一个 ``symbol_deletions`` ——
    真实 ``ProtectedSpec`` 一定有该字段（默认 ``off``），替身缺它会让
    ``_execute_with_patch_contract`` 抛 ``AttributeError``。默认仍取 ``off``，
    因此本节所有既有断言（受保护路径三段式）语义**一字未变**。
    """
    return SimpleNamespace(
        protected=SimpleNamespace(
            on_hit=on_hit,
            max_change_ratio=0.5,
            symbol_deletions=symbol_deletions,
        ),
        protected_paths=lambda: frozenset({"tests/conftest.py"}),
        skeleton_files=lambda: {},
        reserved_paths=lambda: frozenset(),
    )


class TestProtectedThreeStage:
    """Q1：reject → 1 次定向重试 → 兜底 ignore（**不整轮报废**）。"""

    async def _call(
        self,
        first: StageOutput,
        retries: list[StageOutput],
        *,
        on_hit: str = "reject",
        base_files: dict[str, str] | None = None,
    ) -> tuple[StageOutput, Any, dict[str, Any] | None, _FakeRuntime]:
        runtime = _FakeRuntime(retries)
        executor = _executor(runtime)
        combined, merged, retry_info = await executor._execute_with_patch_contract(  # noqa: SLF001
            run=_run(),
            stage=_stage(),
            stage_input=StageInput(requirement="加一个导出接口"),
            output=first,
            template_spec=_template(on_hit=on_hit),  # type: ignore[arg-type]
            base_files=dict(BASE) if base_files is None else dict(base_files),
        )
        return combined, merged, retry_info, runtime

    async def test_retry_success_accumulates_cost(self) -> None:
        """第二段：1 次定向重试生效 + 成本/tokens 两次相加（不漏计）。"""
        combined, merged, retry_info, runtime = await self._call(
            _output({"tests/conftest.py": CONFTEST_REWRITE}),
            [_output({"app/export.py": "def export() -> None:\n    pass\n"})],
        )
        assert merged is not None
        assert merged.manifest.counts.protected_hits == 0  # 重试后不再命中
        assert merged.manifest.warnings == []
        assert merged.files["tests/conftest.py"] == CONFTEST  # 基线原样
        assert merged.files["app/export.py"] == "def export() -> None:\n    pass\n"
        # 成本 / tokens 两次相加
        assert combined.cost_cny == pytest.approx(0.02)
        assert combined.prompt_tokens == 200
        assert combined.completion_tokens == 100
        assert retry_info == {"paths": ["tests/conftest.py"], "attempts": 1}
        assert len(runtime.inputs) == 1  # 恰好 1 次重试

    async def test_retry_receives_feedback_and_attempt(self) -> None:
        """第二段：纠正指令经 ``feedback`` 回喂（复用既有通道，不新增通道）。"""
        _combined, _merged, _retry, runtime = await self._call(
            _output({"tests/conftest.py": CONFTEST_REWRITE}),
            [_output({"app/export.py": "x = 1\n"})],
        )
        assert len(runtime.inputs) == 1
        retry_input = runtime.inputs[0]
        assert retry_input.attempt == 2
        assert retry_input.feedback and "受保护路径不可修改" in retry_input.feedback[0]
        assert "tests/conftest.py" in retry_input.feedback[0]
        assert "新建文件" in retry_input.feedback[0]  # 必须给替代路径（R1 ②）

    async def test_forced_ignore_warning_reaches_serialized_manifest(self) -> None:
        """**F1（QA P2）**：``protected_path_forced_ignore`` 必须进**落盘正文**。

        这条用例刻意读 ``output.files[PATCH_MANIFEST_NAME]`` 的**字符串**（不是内存对象）：
        历史缺陷正是"内存有、落盘没有"—— ``_apply_patch_contract`` 先序列化，第三段
        兜底事后才追加短码，只改了内存。
        """
        stubborn = _output({"tests/conftest.py": CONFTEST_REWRITE})
        combined, merged, retry_info, _runtime = await self._call(stubborn, [stubborn])
        assert merged is not None
        # 内存对象
        assert any(
            item.startswith(PROTECTED_FORCED_IGNORE_PREFIX)
            for item in merged.manifest.warnings
        )
        # 落盘正文（关键断言：读字符串，不读对象）
        assert PATCH_MANIFEST_NAME in combined.files
        on_disk = combined.files[PATCH_MANIFEST_NAME]
        assert PROTECTED_FORCED_IGNORE_PREFIX in on_disk, on_disk
        payload = json.loads(on_disk)
        assert payload["warnings"] == merged.manifest.warnings  # 内存与落盘**完全一致**
        assert payload["counts"]["protected_hits"] == 1
        assert payload["conflicts"] == [
            {"path": "tests/conftest.py", "reason": "protected_path"}
        ]
        assert retry_info is not None and retry_info["forced_ignore"] == [
            "tests/conftest.py"
        ]

    async def test_forced_ignore_fallback_does_not_fail_the_stage(self) -> None:
        """第三段：两次都撞墙 ⇒ ``ignore`` 兜底，阶段继续（不整轮报废）。"""
        stubborn = _output({"tests/conftest.py": CONFTEST_REWRITE})
        combined, merged, retry_info, runtime = await self._call(stubborn, [stubborn])
        assert merged is not None
        # 基线版本保留（模型内容被丢弃）
        assert merged.files["tests/conftest.py"] == CONFTEST
        assert merged.manifest.counts.protected_hits == 1
        assert retry_info is not None
        assert retry_info["forced_ignore"] == ["tests/conftest.py"]
        assert combined.cost_cny == pytest.approx(0.02)
        assert len(runtime.inputs) == 1

    async def test_forced_ignore_code_is_prepended_and_bounded(self) -> None:
        """P3-4（QA）：forced 短码**前置**且走统一裁剪（≤3 条、≤64 字节）。

        历史实现是 ``[*warnings, code][:3]`` —— 先到的 ``change_ratio`` /
        ``protected_path_ignored`` 会把最重要的「被迫降级」信号挤掉，且将来加第 3 类
        短码时会被静默截断。
        """
        stubborn = _output(
            {
                "tests/conftest.py": CONFTEST_REWRITE,
                "app/a.py": "a = 1\n",
                "app/b.py": "b = 2\n",
                "app/c.py": "c = 3\n",
            }
        )
        _combined, merged, _retry, _runtime = await self._call(stubborn, [stubborn])
        assert merged is not None
        warnings = merged.manifest.warnings
        assert warnings[0].startswith(PROTECTED_FORCED_IGNORE_PREFIX), warnings
        assert len(warnings) <= 3
        assert all(len(item.encode("utf-8")) <= 64 for item in warnings), warnings

    async def test_template_ignore_policy_needs_no_retry(self) -> None:
        """模板声明 ``ignore`` 时一次合并即可（不调第二次模型）。"""
        _combined, merged, retry_info, runtime = await self._call(
            _output({"tests/conftest.py": CONFTEST_REWRITE}),
            [_output({"unused.py": "x = 1\n"})],
            on_hit="ignore",
        )
        assert merged is not None
        assert merged.files["tests/conftest.py"] == CONFTEST
        assert retry_info is None
        assert runtime.inputs == []

    async def test_from_scratch_does_not_protect(self) -> None:
        """``from_scratch``（无基线）不启用保护：模型创建 conftest 是必需的。"""
        _combined, merged, retry_info, runtime = await self._call(
            _output({"tests/conftest.py": CONFTEST}),
            [_output({"unused.py": "x = 1\n"})],
            base_files={},
        )
        assert merged is not None
        assert merged.files["tests/conftest.py"] == CONFTEST
        assert retry_info is None
        assert runtime.inputs == []


def test_check_run_three_states() -> None:
    """``CheckRun`` 三态语义（skipped 既不通过也不失败，不进分母）。"""
    passed = CheckRun(command="pytest", status="passed", exit_code=0)
    failed = CheckRun(command="pytest", status="failed", exit_code=1)
    skipped = CheckRun(
        command="fe-build web", status="skipped", skipped_reason="requires_missing:node"
    )
    assert passed.passed and not passed.failed
    assert failed.failed and not failed.passed
    assert not skipped.passed and not skipped.failed
    assert skipped.to_dict()["skipped_reason"] == "requires_missing:node"


# ============================================================ v1.3.1 · T1.1 探针同尺

#: 探针树用例的骨架样本（含一条会补齐的 ``web/package.json``：基线没有 ``web/``）
SKELETON: dict[str, str] = {
    "web/package.json": '{"name": "web"}\n',
    "ruff.toml": "line-length = 100\n",
}


class TestProbeTreeSameRuler:
    """T1.1 DoD ①②③④：探针树 = 骨架补缺后的基线，且与合并树**同一个函数**。"""

    def test_raw_when_no_skeleton(self) -> None:
        """无骨架 ⇒ ``raw`` + 空补缺 + 树与基线逐字节一致（v1.3 行为）。"""
        tree, mode, added = build_probe_tree(BASE)
        assert tree == BASE
        assert mode == PROBE_MODE_RAW
        assert added == ()

    def test_skeleton_filled_matches_merge_trees(self) -> None:
        """有骨架 ⇒ ``skeleton_filled``，且树**等于** ``merge_trees(mode="patch")`` 结果。

        DoD ④ 的字面断言：用的是 ``artifacts.merge.merge_trees`` **这个**函数，
        不是第二套等价实现（``build_probe_tree`` 的模块全局里指向同一个对象）。
        """
        tree, mode, added = build_probe_tree(BASE, SKELETON)
        assert mode == PROBE_MODE_SKELETON_FILLED
        assert added == ("ruff.toml", "web/package.json")
        expected = merge_trees(
            BASE, {}, mode="patch", skeleton=SKELETON, base_paths=frozenset(BASE)
        )
        assert tree == expected.files
        assert set(added) == set(expected.manifest.skeleton_added)
        assert build_probe_tree.__globals__["merge_trees"] is merge_trees

    def test_raw_when_baseline_already_has_all_skeleton(self) -> None:
        """基线已含全部骨架 ⇒ ``raw``（报告里连 ``probe_mode`` 键都不出现）。"""
        base = {**BASE, **SKELETON}
        tree, mode, added = build_probe_tree(base, SKELETON)
        assert mode == PROBE_MODE_RAW
        assert added == ()
        assert tree == base

    async def test_probe_baseline_writes_filled_tree_into_sandbox(self) -> None:
        """DoD ①：写入沙箱的是**探针树**（含骨架），不是基线原样。"""
        provider = _ScriptedProvider()
        probe = await probe_baseline(
            files=BASE,
            checks=[EvalCheck(run="python -m pytest -q")],
            sandbox_provider=provider,
            skeleton=SKELETON,
        )
        assert probe.status == BASELINE_HEALTHY
        assert probe.probe_mode == PROBE_MODE_SKELETON_FILLED
        assert probe.skeleton_added == ("ruff.toml", "web/package.json")
        assert provider.sessions[0].files == build_probe_tree(BASE, SKELETON)[0]
        assert "web/package.json" in provider.sessions[0].files

    async def test_probe_mode_raw_when_skeleton_absent(self) -> None:
        """无骨架 ⇒ 内部 ``probe_mode="raw"``（报告层按 M7 ① 不序列化它）。"""
        provider = _ScriptedProvider()
        probe = await probe_baseline(
            files=BASE,
            checks=[EvalCheck(run="python -m pytest -q")],
            sandbox_provider=provider,
        )
        assert probe.probe_mode == PROBE_MODE_RAW
        assert probe.skeleton_added == ()
        assert provider.sessions[0].files == BASE


# ============================================================ v1.3.1 · T1.2 requires_files


class TestRequiresFiles:
    """T1.2 DoD ①②③⑤：缺文件 ⇒ skipped（不是 unhealthy）；合并树侧不放水。"""

    BUILD_CHECK = EvalCheck(
        run="fe-build web", weight=2.0, requires_files=["web/"]
    )

    def test_missing_requires_files_semantics(self) -> None:
        """元素语义：尾斜杠目录 = 该前缀下 ≥1 文件；否则精确文件。"""
        check = EvalCheck(
            run="fe-build web",
            requires_files=["web/", "apps/app/main.py", "web/"],
        )
        assert missing_requires_files(check, BASE) == ("web/", "apps/app/main.py")
        assert missing_requires_files(check, {"web/index.html": ""}) == (
            "apps/app/main.py",
        )
        assert missing_requires_files(check, {"apps/app/main.py": "x"}) == ("web/",)
        # 声明为空 ⇒ 永不跳过（v1.3 行为不变）
        assert missing_requires_files(EvalCheck(run="pytest"), BASE) == ()

    async def test_all_missing_yields_baseline_incomplete(self) -> None:
        """DoD ②③：全部 skipped ⇒ ``status="skipped"`` + ``baseline_incomplete``。"""
        provider = _ScriptedProvider()
        probe = await probe_baseline(
            files=BASE,
            checks=[self.BUILD_CHECK],
            sandbox_provider=provider,
        )
        assert probe.status == BASELINE_SKIPPED
        assert probe.reason == SKIP_BASELINE_INCOMPLETE
        assert probe.passed is False
        assert provider.sessions == []  # 不启会话、零等待
        assert len(probe.checks) == 1
        assert probe.checks[0].skipped is True
        assert probe.checks[0].skipped_reason == f"{SKIP_FILE_MISSING_PREFIX}web/"

    async def test_partial_skip_denominator_counts_executed_only(self) -> None:
        """DoD ④：部分 skipped ⇒ 分母只算**实际执行**的命令（与 env_missing 同口径）。"""
        provider = _ScriptedProvider()
        probe = await probe_baseline(
            files=BASE,
            checks=[
                EvalCheck(run="python -m pytest -q", weight=2.0),
                self.BUILD_CHECK,
            ],
            sandbox_provider=provider,
        )
        assert probe.status == BASELINE_HEALTHY
        assert probe.score == 100.0  # 不是 50.0（skipped 不进分母）
        assert [item.skipped for item in probe.checks] == [False, True]
        assert probe.checks[1].skipped_reason == f"{SKIP_FILE_MISSING_PREFIX}web/"
        # 会话只收到那一条真正要跑的命令
        assert provider.sessions[0].executed == ["python -m pytest -q"]

    async def test_skipped_result_is_not_cached(self) -> None:
        """``baseline_incomplete`` 与 ``env_missing`` 同理：不写缓存（基线会变）。"""
        provider = _ScriptedProvider()
        cache = BaselineProbeCache()
        key = baseline_cache_key(BASE, [self.BUILD_CHECK])
        first = await probe_baseline(
            files=BASE,
            checks=[self.BUILD_CHECK],
            sandbox_provider=provider,
            cache=cache,
            cache_key=key,
        )
        second = await probe_baseline(
            files=BASE,
            checks=[self.BUILD_CHECK],
            sandbox_provider=provider,
            cache=cache,
            cache_key=key,
        )
        assert first.reason == SKIP_BASELINE_INCOMPLETE == second.reason
        assert first.cached is False and second.cached is False
        assert len(cache) == 0

    def test_resolve_checks_maps_requires_files(self) -> None:
        """``resolve_checks`` 必须把模板声明的 ``requires_files`` 映射过去。"""
        spec = load_template("fastapi-vue3")
        checks = resolve_checks(spec, None)
        build = [check for check in checks if check.run == "fe-build web"]
        assert len(build) == 1
        assert build[0].requires_files == ["web/"]
        # 未声明的检查保持空清单（不引入"默认声明"的隐性行为）
        assert [check.requires_files for check in checks if check.run != "fe-build web"] == [
            []
        ] * 4

    def test_build_task_for_run_injects_baseline_skeleton(self) -> None:
        """T1.1：骨架经 ``EvalTask.baseline_skeleton`` 纯数据跨层（唯一注入点）。"""
        run = SimpleNamespace(
            id="run-skel",
            project_id="p-skel",
            context={
                "requirement": "写一个待办 API",
                "template": {"id": "fastapi-vue3"},
                "baseline": {"artifact_id": "base-1", "version": 1},
                "benchmark": {"eval_suite": "default", "eval_task": "task-todo-api"},
            },
        )
        task, _suite = build_task_for_run(run)  # type: ignore[arg-type]
        spec = load_template("fastapi-vue3")
        assert task.baseline_skeleton == spec.skeleton_files()
        assert len(task.baseline_skeleton) == 5
        # S3：刻意**不新增** baseline_reserved（patch={} 下 reserved 分支不可达）
        assert "baseline_reserved" not in EvalTask.model_fields


# ============================================================ v1.3.1 · T1.3 缓存键


class TestProbeCacheKeySkeletonAndRequiresFiles:
    """T1.3 DoD ①②③：两条独立入口（改骨架 / 改 requires_files）都必须换键。"""

    CHECKS = [EvalCheck(run="python -m pytest -q")]

    def test_key_changes_with_skeleton_only(self) -> None:
        """仅改骨架（基线没变）⇒ 键必须不同（否则复现 F3 幽灵缓存）。"""
        plain = baseline_cache_key(BASE, self.CHECKS)
        assert baseline_cache_key(BASE, self.CHECKS, skeleton=SKELETON) != plain
        # 纪律：空骨架 / None **不掺字节** ⇒ 与 v1.3 的键逐字节相等
        assert baseline_cache_key(BASE, self.CHECKS, skeleton={}) == plain
        assert baseline_cache_key(BASE, self.CHECKS, skeleton=None) == plain

    def test_key_changes_with_requires_files_only(self) -> None:
        """仅改 ``requires_files`` ⇒ 键必须不同（否则改了声明仍命中旧探测）。"""
        plain = baseline_cache_key(BASE, [EvalCheck(run="fe-build web")])
        with_dir = baseline_cache_key(
            BASE, [EvalCheck(run="fe-build web", requires_files=["web/"])]
        )
        with_file = baseline_cache_key(
            BASE,
            [EvalCheck(run="fe-build web", requires_files=["web/package.json"])],
        )
        assert len({plain, with_dir, with_file}) == 3


# ============================================================ v1.3.1 · T1.4 regression 收紧


class TestRegressionTightening:
    """T1.4（C2）：只有「probe 侧 executed 且 exit=0」的命令才计入 regression。

    场景：基线**没有** ``web/``（v1.2 时代的补丁产物形态），模板的 ``fe-build``
    声明 ``requires_files: ["web/"]`` ⇒ 探针 ``skipped(baseline_incomplete)``；
    而合并树侧仍会**真的执行**该命令并失败。这正是「基线不完整」与「模型回归」
    必须分开的那一刻：不收紧判据就会冤枉模型。
    """

    async def _outcome(self, session_factory, tmp_path: Path):  # noqa: ANN202
        store, project_id, baseline_id = await _seed_incremental_project(
            session_factory,
            tmp_path,
            baseline_files=BASE,  # 基线没有 web/
            patch_files={"app/broken.py": "BROKEN = True\n"},
        )
        task = EvalTask(
            id="t-tighten",
            artifact_name="code",
            checks=[
                EvalCheck(
                    run="fe-build web",
                    weight=2.0,
                    blocking=True,
                    requires_files=["web/"],
                )
            ],
            baseline_ref={"artifact_id": baseline_id, "version": 1},
        )
        harness = EvaluationHarness(
            store=store,
            sandbox_provider=_ScriptedProvider(fail_commands=("fe-build",)),
        )
        return harness, await harness.evaluate_task(task, project_id=project_id)

    async def test_regression_excludes_probe_skipped_checks(
        self, session_factory, tmp_path: Path
    ) -> None:
        harness, outcome = await self._outcome(session_factory, tmp_path)

        # 探针侧：基线不完整 ⇒ skipped（不是 unhealthy）
        assert outcome.baseline_runnability is not None
        assert outcome.baseline_runnability.status == BASELINE_SKIPPED
        assert outcome.baseline_runnability.reason == SKIP_BASELINE_INCOMPLETE
        # 合并树侧：阻断项真的失败（否则"不判 regression"没有意义）
        assert outcome.blocking_failed == ["fe-build web"]
        # ★ 收紧点：probe 跳过的命令失败 ⇒ **不算回归**
        assert outcome.regression is False
        assert outcome.regression_reason == ""
        # 落到 check_failed（而不是 regression）—— 判据链的终点必须是"检查失败"
        gate = HarnessEvaluationGate(harness=harness, store=None)
        verdict = gate._to_verdict(EvalSuite.load("default"), outcome, 0)  # noqa: SLF001
        assert verdict.failure_class == "check_failed"
        assert verdict.regression is False
        assert verdict.baseline_status == BASELINE_SKIPPED

    async def test_baseline_incomplete_emits_warning(
        self, session_factory, tmp_path: Path
    ) -> None:
        """docs/13 §7.4：基线**整份**因缺依赖文件跳过时，报告必须有可见警示。"""
        _harness, outcome = await self._outcome(session_factory, tmp_path)

        assert outcome.baseline_runnability is not None
        assert outcome.baseline_runnability.status == BASELINE_SKIPPED
        assert outcome.baseline_runnability.reason == SKIP_BASELINE_INCOMPLETE
        assert BASELINE_INCOMPLETE_WARNING in outcome.warnings
        # 警示是披露，不改判据：不因此判回归
        assert outcome.regression is False

    async def test_merged_side_ignores_requires_files(
        self, session_factory, tmp_path: Path
    ) -> None:
        """Q1 不对称：``requires_files`` **不外泄**到合并树侧 —— 交付树缺目录仍 failed。"""
        _harness, outcome = await self._outcome(session_factory, tmp_path)
        assert outcome.skipped_checks == []  # 合并树侧不因缺 web/ 而跳过
        assert outcome.passed is False
        runnability = outcome.dimension("runnability")
        assert runnability is not None
        assert any(
            item.ref == "fe-build web" and "exit=1" in item.detail
            for item in runnability.evidence
        ), [item.ref for item in runnability.evidence]
        # M7 ①：``probe_mode="raw"`` 时该键不进 JSON（旧报告逐字节一致）
        assert outcome.baseline_runnability is not None
        assert "probe_mode" not in outcome.baseline_runnability.model_dump()
        assert outcome.baseline_runnability.checks[0].skipped_reason == (
            f"{SKIP_FILE_MISSING_PREFIX}web/"
        )

    async def test_attribution_unknown_when_probe_skipped(
        self, session_factory, tmp_path: Path
    ) -> None:
        """T2.2 装配：探针跳过 ⇒ 归因 ``unknown`` / ``probe_skipped``（绝不猜）。"""
        _harness, outcome = await self._outcome(session_factory, tmp_path)

        assert outcome.failure_attribution is not None
        assert outcome.failure_attribution.verdict == "unknown"
        assert outcome.failure_attribution.basis == "probe_skipped"
        # 归因不参与判据：阻断项仍然生效
        assert outcome.passed is False


# ============================================================ v1.3.1 · T4.1 模板命令进冒烟


class _FakeSandboxService:
    """最小 ``SandboxService`` 替身：只提供冒烟用到的 ``sandbox`` / ``start`` / ``destroy``。

    刻意**不**复用 ``_ScriptedProvider``（那是探测/评测侧的 ``session()`` 上下文管理器
    接口）；冒烟走的是 ``start(run_id, network=...) -> managed.session`` 这条路径，
    两者形状不同，硬套会让用例读不懂在测谁。
    """

    def __init__(self, session: _ScriptedSession, *, backend: str = "fake") -> None:
        self.sandbox = SimpleNamespace(backend=backend)
        self.session = session
        self.started: list[Any] = []
        self.destroyed: list[Any] = []

    async def start(self, run_id: str, *, network: Any = None) -> Any:
        managed = SimpleNamespace(run_id=run_id, network=network, session=self.session)
        self.started.append(managed)
        return managed

    async def destroy(self, managed: Any) -> None:
        self.destroyed.append(managed)


def _smoke_executor(service: _FakeSandboxService) -> RoleStageExecutor:
    """最小装配：只用 ``_run_sandbox_smoke`` 需要的那三个属性。"""
    executor = RoleStageExecutor.__new__(RoleStageExecutor)
    executor._sandbox_service = service  # type: ignore[attr-defined]
    executor._sandbox_network = NetworkPolicy.NONE  # type: ignore[attr-defined]
    executor._exec_timeout = 120.0  # type: ignore[attr-defined]
    return executor


def _smoke_output(*commands: str, files: dict[str, str] | None = None) -> StageOutput:
    """构造带 ``run_commands`` 的模型产出（冒烟只读 ``structured.run_commands``）。"""
    return StageOutput(
        files=dict(files or {}),
        summary="ok",
        structured={"files": [], "run_commands": list(commands)},
        provider="fake",
        model="fake",
        prompt_tokens=10,
        completion_tokens=5,
        cost_cny=0.0,
        schema_attempts=1,
    )


def _smoke_template(*checks: CheckSpec) -> Any:
    """最小 ``TemplateSpec`` 替身（冒烟只用 ``checks``）。"""
    return SimpleNamespace(checks=list(checks))


class TestSmokeTemplateCommands:
    """T4.1（docs/13 §7.3-6）：模板 ``frontend_build`` 命令进冒烟。

    四条必须钉住的语义：

    1. **只有模板条目带 ``source``** ⇒ 无模板时 ``smoke_report.json`` 与 v1.3
       **逐字节一致**（红线 A9）；
    2. 与模型自报命令按字符串去重（省时、避免同一条跑两遍）；
    3. ``requires`` 缺失 ⇒ ``skipped`` + ``reason``，**不执行**主命令、零等待；
    4. ``all_ok`` **三态**：``skipped`` 不进分母；**全部 skipped ⇒ ``None``**
       （``all(空集) == True`` 会让报告显示"冒烟全过"而实际一条没跑）。
    """

    #: 模型条目的键集合（v1.3 形态）—— 多一个键就是破坏了逐字节一致
    V13_ENTRY_KEYS = {
        "command",
        "stdout",
        "stderr",
        "exit_code",
        "duration_ms",
        "timed_out",
    }

    async def _report(
        self,
        *,
        commands: list[str],
        template: Any = None,
        session: _ScriptedSession | None = None,
    ) -> tuple[dict[str, Any], _ScriptedSession, _FakeSandboxService]:
        session = _ScriptedSession() if session is None else session
        service = _FakeSandboxService(session)
        executor = _smoke_executor(service)
        report = await executor._run_sandbox_smoke(  # noqa: SLF001
            _run(), _smoke_output(*commands), files=dict(BASE), template_spec=template
        )
        return report, session, service

    async def test_template_commands_appended_with_source_field(self) -> None:
        """模板 → 冒烟：只取 ``frontend_build`` 组、≤2 条，且**仅这些条目**带 ``source``。"""
        template = _smoke_template(
            CheckSpec(run="python -m pytest -q", group="backend"),
            CheckSpec(run="fe-build web", group="frontend_build"),
            CheckSpec(run="fe-build web", group="frontend_build"),  # 模板内重复
            CheckSpec(run="fe-lint web", group="frontend_build"),
            CheckSpec(run="fe-test web", group="frontend_build"),  # 超上限：不进
        )
        report, session, service = await self._report(
            commands=["python -m pytest -q"], template=template
        )

        entries = report["commands"]
        assert [item["command"] for item in entries] == [
            "python -m pytest -q",
            "fe-build web",
            "fe-lint web",
        ]
        # ① 模型条目：v1.3 形态，一个键都不多
        assert set(entries[0]) == self.V13_ENTRY_KEYS
        # ② 模板条目：必有 source，且**只有它**有
        assert [item.get("source") for item in entries[1:]] == ["template", "template"]
        assert entries[1]["exit_code"] == 0
        assert report["all_ok"] is True
        assert report["error"] is None
        assert len(service.started) == 1 and service.destroyed == service.started

    async def test_template_command_without_source_leaves_v13_bytes_intact(self) -> None:
        """无模板 ⇒ 条目键集合与 v1.3 完全一致（A9 红线：不加 ``source`` 空值）。"""
        report, _session, _service = await self._report(commands=["python -m pytest -q"])

        assert set(report["commands"][0]) == self.V13_ENTRY_KEYS
        assert "source" not in report["commands"][0]
        assert report["all_ok"] is True
        # 报告顶层键集合也不因本批改动而变化（后端 / 网络 / 时间戳是 v1.3 既有键）
        assert set(report) == {
            "backend",
            "network",
            "executed_at",
            "commands",
            "all_ok",
            "error",
        }

    async def test_template_command_already_reported_by_model_is_not_rerun(self) -> None:
        """模型自报过同一条命令 ⇒ 模板那条不再执行（按命令串去重）。"""
        template = _smoke_template(CheckSpec(run="fe-build web", group="frontend_build"))
        report, session, _service = await self._report(commands=["fe-build web"], template=template)

        assert [item["command"] for item in report["commands"]] == ["fe-build web"]
        assert "source" not in report["commands"][0]  # 保留模型条目原形
        assert session.executed == ["fe-build web"]  # 只跑一次

    async def test_template_requires_probe_runs_before_the_command(self) -> None:
        """``requires`` 先探测、用短超时；缺失 ⇒ ``skipped`` 且**不执行**主命令。"""
        session = _ScriptedSession(missing_bins=("npm",))
        template = _smoke_template(
            CheckSpec(run="fe-build web", group="frontend_build", requires="npm")
        )
        report, session, _service = await self._report(
            commands=["python -m pytest -q"], template=template, session=session
        )

        assert report["commands"][1] == {
            "command": "fe-build web",
            "source": "template",
            "skipped": True,
            "reason": "requires_missing:npm",
        }
        # 只跑了探测那一条：主命令零执行、零等待
        assert session.executed == ["python -m pytest -q", "command -v npm"]
        assert session.timeouts[-1] == 10.0

    async def test_skipped_template_command_is_not_counted_in_all_ok(self) -> None:
        """``skipped`` 不进分母：模型那条过了 ⇒ ``all_ok`` 仍为 ``True``。"""
        session = _ScriptedSession(missing_bins=("npm",))
        template = _smoke_template(
            CheckSpec(run="fe-build web", group="frontend_build", requires="npm")
        )
        report, _s, _service = await self._report(
            commands=["python -m pytest -q"], template=template, session=session
        )

        assert report["all_ok"] is True
        assert report["error"] is None  # 有执行过的命令 ⇒ 不写 error

    async def test_all_skipped_yields_none_all_ok_with_explicit_error(self) -> None:
        """★ M6：**全部** skipped ⇒ ``all_ok is None`` + ``skipped: ...`` 文案。

        ``all(空集) == True``，若直接 ``all(...)`` 会把"一条没跑"渲染成"冒烟全过"；
        这种形态在"无 node 镜像 + 只有 frontend_build 组"时必然踩到。
        """
        session = _ScriptedSession(missing_bins=("npm",))
        template = _smoke_template(
            CheckSpec(run="fe-build web", group="frontend_build", requires="npm")
        )
        report, _s, _service = await self._report(commands=[], template=template, session=session)

        assert report["all_ok"] is None
        assert report["error"] == "skipped: 全部命令因环境缺失跳过"
        assert [item["skipped"] for item in report["commands"]] == [True]

    async def test_no_commands_at_all_keeps_v13_error_text(self) -> None:
        """既无模型命令、也无模板命令 ⇒ v1.3 原文案（语义不变，且不启会话）。"""
        report, _session, service = await self._report(commands=[], template=None)

        assert report["error"] == "skipped: 无自测命令"
        assert report["all_ok"] is None
        assert report["commands"] == []
        assert service.started == []

    async def test_template_failure_makes_all_ok_false(self) -> None:
        """模板命令失败 ⇒ ``all_ok is False``（模板命令与模型命令同权参与判定）。"""
        session = _ScriptedSession(fail_commands=("fe-build",))
        template = _smoke_template(CheckSpec(run="fe-build web", group="frontend_build"))
        report, _s, _service = await self._report(
            commands=["python -m pytest -q"], template=template, session=session
        )

        assert report["all_ok"] is False
        assert report["commands"][1]["exit_code"] == 1

    async def test_template_command_exception_is_recorded_not_raised(self) -> None:
        """⑥：模板命令执行抛异常 ⇒ 只记条目（``error`` + ``exit_code=None``），不阻断。"""

        class _BoomSession(_ScriptedSession):
            async def exec(
                self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
            ) -> ExecResult:
                if cmd == "fe-build web":
                    raise RuntimeError("沙箱炸了")
                return await super().exec(cmd, timeout=timeout, workdir=workdir)

        template = _smoke_template(CheckSpec(run="fe-build web", group="frontend_build"))
        report, _session, _service = await self._report(
            commands=[], template=template, session=_BoomSession()
        )

        assert report["commands"] == [
            {
                "command": "fe-build web",
                "source": "template",
                "error": "沙箱炸了",
                "exit_code": None,
            }
        ]
        # 条目没被标 skipped ⇒ 进分母；拿不到 exit=0 ⇒ 不是"全过"
        assert report["all_ok"] is False

    async def test_template_timeout_uses_check_timeout_then_exec_default(self) -> None:
        """``timeout_s`` 优先；缺省回落 ``_exec_timeout``（不篡改既有超时口径）。"""
        template = _smoke_template(
            CheckSpec(run="fe-build web", group="frontend_build", timeout_s=300),
            CheckSpec(run="fe-lint web", group="frontend_build"),
        )
        report, session, _service = await self._report(commands=[], template=template)

        assert [item["command"] for item in report["commands"]] == [
            "fe-build web",
            "fe-lint web",
        ]
        assert session.timeouts == [300.0, 120.0]

    def test_real_template_frontend_build_is_picked(self) -> None:
        """真实模板（fastapi-vue3）里 ``fe-build web`` 确实进冒烟清单。"""
        from ymir.orchestrator.role_executor import _smoke_template_checks

        picked = _smoke_template_checks(load_template("fastapi-vue3"))
        assert [check.run for check in picked] == ["fe-build web"]
        assert picked[0].requires == "node"
        # 无模板（通用模式）⇒ 空清单：A9 逐字节一致的入口条件
        assert _smoke_template_checks(None) == []


# ============================================================ v1.3.1 · T4.2 QA 基线事实


class _ForbiddenStore:
    """任何属性访问都失败：证明 ``_baseline_facts`` **不查 store**（结构性保证）。

    这正是 T4.2 的核心纪律 —— 事实只能来自 ``stage_input.upstream``。若实现里
    出现"顺手查一下项目最新的 code 产物"，本替身会立刻炸掉。
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_baseline_facts 不应访问 store.{name}")


def _facts_executor(store: Any = None) -> RoleStageExecutor:
    """最小装配：``_baseline_facts`` 理论上只需要 ``run.id`` 打日志。"""
    executor = RoleStageExecutor.__new__(RoleStageExecutor)
    executor._store = _ForbiddenStore() if store is None else store  # type: ignore[attr-defined]
    return executor


def _facts_baseline() -> BaselineContext:
    return BaselineContext(
        artifact_id="base-1",
        version=1,
        content_hash="0123456789abcdef",
        file_count=len(BASE),
        paths=tuple(sorted(BASE)),
    )


def _manifest(changed: list[str], skeleton: list[str] | None = None) -> str:
    """最小 ``patch_manifest.json`` 正文（只放事实取数用到的键）。"""
    payload: dict[str, Any] = {"mode": "patch", "changed_paths": changed}
    if skeleton:
        payload["skeleton_added"] = skeleton
    return json.dumps(payload, ensure_ascii=False)


def _smoke_json(*entries: dict[str, Any]) -> str:
    return json.dumps({"all_ok": True, "commands": list(entries)}, ensure_ascii=False)


class TestQaBaselineFacts:
    """T4.2（docs/13 §7.3-5）：三块事实的取数纪律与降级面。

    ★ 最要紧的一条：**事实来源 = 本 stage 上游 ``code`` 产物这一版**，
    不是"项目最新"。项目里可能已存在更晚的版本（重跑 / 并行 run / 人工补跑），
    取最新会把别人那一版的事实塞进本次 QA —— 事实与结论来自不同产物，正是
    v1.3 F7/F8 的同一类错误。
    """

    async def test_facts_come_from_stage_upstream_version_not_project_latest(
        self, session_factory, tmp_path: Path
    ) -> None:
        """④ 构造「项目里存在更新的 code 版本」，断言事实仍取**本次那一版**。

        构造方式（按真实时序复刻 F7/F8 的裂缝）：

        1. 用 ``_seed_incremental_project`` 产出 code **v1**（改动 ``app/first.py``，
           带 1 条 exit=0 的冒烟记录）；
        2. 此刻调 :func:`read_artifacts` 拿到"本 stage 的 ``stage.inputs`` 解析结果"
           —— 这就是 QA 阶段手上那一版（``version=1``）；
        3. **之后**项目里再存一版 code **v2**（改动 ``app/second.py`` + 骨架补缺 +
           1 条 exit=1 的冒烟记录）⇒ ``store.latest_by_name`` 已指向 v2；
        4. 把 (2) 的 upstream 交给 ``_baseline_facts``：三块事实必须全部来自 v1。
        """
        store, project_id, _baseline_id = await _seed_incremental_project(
            session_factory,
            tmp_path,
            baseline_files=BASE,
            patch_files={"app/first.py": "first = 1\n"},
            extra_files={
                "smoke_report.json": _smoke_json({"command": "python -m pytest -q", "exit_code": 0})
            },
        )
        upstream = await read_artifacts(store, project_id, ["code"])
        assert [(item.name, item.version) for item in upstream] == [("code", 1)]

        newer = await store.save(
            project_id=project_id,
            name="code",
            artifact_type="code",
            files={
                "app/second.py": "second = 2\n",
                PATCH_MANIFEST_NAME: _manifest(["app/second.py"], ["web/index.html"]),
                "smoke_report.json": _smoke_json(
                    {"command": "fe-build web", "exit_code": 1},
                    {"command": "python -m pytest -q", "exit_code": 0},
                    {"command": "ruff check .", "exit_code": 0},
                ),
            },
        )
        assert newer.version == 2
        latest = await store.latest_by_name(project_id, "code")
        assert latest is not None and latest.version == 2  # 朴素实现会取到这一版

        facts = await _facts_executor()._baseline_facts(  # noqa: SLF001
            _run(),
            _facts_baseline(),
            StageInput(requirement="评审这次交付", upstream=upstream),
        )

        assert facts is not None
        # ① 补丁改动 = v1 的（v2 是 app/second.py）
        assert facts.patch_changed_paths == ("app/first.py",)
        assert "app/second.py" not in facts.patch_changed_paths
        # ② 骨架补缺 = v1 的（v1 没补任何骨架）
        assert facts.skeleton_added == ()
        # ③ 冒烟摘要 = v1 的（v2 有 3 条命令、首个非 0 是 fe-build web）
        assert facts.smoke_summary == "命令 1 条 / exit=0 1 条 / 首个非 0 命令：无"
        assert "fe-build web" not in facts.smoke_summary

    async def test_no_upstream_code_artifact_degrades_to_untouched_baseline(self) -> None:
        """⑤-a：无上游 ``code`` 产物 ⇒ 原样返回（不造空事实、不抛）。"""
        baseline = _facts_baseline()
        facts = await _facts_executor()._baseline_facts(  # noqa: SLF001
            _run(), baseline, StageInput(requirement="评审")
        )

        assert facts is baseline  # 同一个对象：一个字都没改
        assert facts is not None
        assert facts.patch_changed_paths == ()
        assert facts.smoke_summary == ""

    async def test_missing_patch_manifest_degrades_to_empty(self) -> None:
        """⑤-b：上游有文件但缺 ``patch_manifest.json`` ⇒ 两块路径事实为空。"""
        upstream = [UpstreamArtifact(name="code", version=3, files={"app/main.py": "x = 1\n"})]
        facts = await _facts_executor()._baseline_facts(  # noqa: SLF001
            _run(), _facts_baseline(), StageInput(requirement="评审", upstream=upstream)
        )

        assert facts is not None
        assert facts.patch_changed_paths == ()
        assert facts.skeleton_added == ()
        assert facts.smoke_summary == ""

    async def test_missing_smoke_report_degrades_to_empty_string(self) -> None:
        """⑤-c：有清单但没有 ``smoke_report.json`` ⇒ 只冒烟摘要为空，路径事实照留。"""
        upstream = [
            UpstreamArtifact(
                name="code",
                version=2,
                files={PATCH_MANIFEST_NAME: _manifest(["app/a.py"], ["web/index.html"])},
            )
        ]
        facts = await _facts_executor()._baseline_facts(  # noqa: SLF001
            _run(), _facts_baseline(), StageInput(requirement="评审", upstream=upstream)
        )

        assert facts is not None
        assert facts.patch_changed_paths == ("app/a.py",)
        assert facts.skeleton_added == ("web/index.html",)
        assert facts.smoke_summary == ""

    async def test_broken_json_degrades_to_empty_without_raising(self) -> None:
        """坏 JSON / 非 dict 正文同样只降级（取证材料坏了不该让整轮 run 失败）。"""
        upstream = [
            UpstreamArtifact(
                name="code",
                version=2,
                files={
                    PATCH_MANIFEST_NAME: "{not json",
                    "smoke_report.json": "oops",
                },
            )
        ]
        facts = await _facts_executor()._baseline_facts(  # noqa: SLF001
            _run(), _facts_baseline(), StageInput(requirement="评审", upstream=upstream)
        )

        assert facts is not None
        assert facts.patch_changed_paths == ()
        assert facts.smoke_summary == ""

    async def test_no_baseline_returns_none(self) -> None:
        """无基线引用 ⇒ ``None``（不凭空造一个 ``BaselineContext``）。"""
        upstream = [
            UpstreamArtifact(
                name="code", version=1, files={PATCH_MANIFEST_NAME: _manifest(["a.py"])}
            )
        ]
        facts = await _facts_executor()._baseline_facts(  # noqa: SLF001
            _run(), None, StageInput(requirement="评审", upstream=upstream)
        )

        assert facts is None

    async def test_unexpected_error_is_swallowed_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """任何意外异常 ⇒ 只 ``logger.warning``，返回原 baseline（绝不阻断阶段）。"""
        from ymir.orchestrator import role_executor as role_executor_module

        def boom(_raw: str) -> Any:
            raise RuntimeError("清单解析炸了")

        monkeypatch.setattr(role_executor_module, "parse_patch_manifest", boom)
        baseline = _facts_baseline()
        upstream = [
            UpstreamArtifact(
                name="code", version=1, files={PATCH_MANIFEST_NAME: _manifest(["a.py"])}
            )
        ]
        facts = await _facts_executor()._baseline_facts(  # noqa: SLF001
            _run(), baseline, StageInput(requirement="评审", upstream=upstream)
        )

        assert facts is baseline

    async def test_execute_enriches_baseline_for_qa_only(
        self, session_factory, tmp_path: Path
    ) -> None:
        """装配点：``execute()`` 只给 **qa** 补事实（engineer 的入参逐字节不变）。"""
        store, project_id, baseline_id = await _seed_incremental_project(
            session_factory,
            tmp_path,
            baseline_files=BASE,
            patch_files={"app/first.py": "first = 1\n"},
            extra_files={
                "smoke_report.json": _smoke_json({"command": "python -m pytest -q", "exit_code": 0})
            },
        )
        upstream = await read_artifacts(store, project_id, ["code"])
        baseline = BaselineContext(
            artifact_id=baseline_id,
            version=1,
            content_hash="0123456789abcdef",
            file_count=len(BASE),
            paths=tuple(sorted(BASE)),
        )
        # 产物版本表有 run_id 外键 ⇒ 必须铺一条真实 run（否则保存产物直接 FK 报错）
        async with session_factory() as session:
            run_row = await RunRepository(session).create(
                project_id=project_id, requirement="评审这次交付", mode="batch"
            )
            await session.commit()
            run_id = run_row.id

        qa_view = await self._capture_baseline(
            session_factory,
            store,
            role="qa",
            project_id=project_id,
            run_id=run_id,
            baseline=baseline,
            upstream=upstream,
        )
        engineer_view = await self._capture_baseline(
            session_factory,
            store,
            role="engineer",
            project_id=project_id,
            run_id=run_id,
            baseline=baseline,
            upstream=upstream,
        )

        assert qa_view is not None
        assert qa_view.patch_changed_paths == ("app/first.py",)
        assert qa_view.smoke_summary.startswith("命令 1 条")
        # engineer 侧：连字段都没被填（T4.2 明令 engineer 段与 v11 快照一位不动）
        assert engineer_view is not None
        assert engineer_view.patch_changed_paths == ()
        assert engineer_view.smoke_summary == ""

    async def _capture_baseline(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        *,
        role: str,
        project_id: str,
        run_id: str,
        baseline: BaselineContext,
        upstream: list[UpstreamArtifact],
    ) -> BaselineContext | None:
        """跑一遍 ``execute()``，回传 runtime 真正看到的 ``baseline``（模型入参）。"""
        runtime = _FakeRuntime([_output({"app/new.py": "new = 1\n"})])
        executor = RoleStageExecutor(
            session_factory=session_factory,
            store=store,
            runtime=runtime,  # type: ignore[arg-type]
        )

        async def fake_collect(_run: Any, _stage: Any, _attempt: int, **_kwargs: Any) -> StageInput:
            return StageInput(
                requirement="评审这次交付", upstream=list(upstream), baseline=baseline
            )

        async def fake_load_baseline(_run: Any) -> tuple[BaselineContext, dict[str, str]]:
            return baseline, dict(BASE)

        executor._collect_input = fake_collect  # type: ignore[method-assign]
        executor._load_baseline = fake_load_baseline  # type: ignore[method-assign]
        stage = SimpleNamespace(
            id=f"{role}-stage",
            role=role,
            inputs=["code"],
            outputs=["review"],
            use_sandbox=False,
        )
        run = SimpleNamespace(id=run_id, project_id=project_id, context={})
        await executor.execute(run=run, stage=stage, attempt=1)
        return runtime.inputs[0].baseline
