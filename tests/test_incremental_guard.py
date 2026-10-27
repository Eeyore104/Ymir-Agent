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
from ymir.common.errors import BizCode, InvalidParamError
from ymir.evaluation.baseline_probe import (
    BASELINE_HEALTHY,
    BASELINE_SKIPPED,
    BASELINE_UNHEALTHY,
    BaselineProbeCache,
    baseline_cache_key,
    probe_baseline,
)
from ymir.evaluation.evaluators.base import CheckRun, EvalCheck, EvalTask
from ymir.evaluation.gate import HarnessEvaluationGate
from ymir.evaluation.harness import BASELINE_UNHEALTHY_WARNING, EvaluationHarness
from ymir.evaluation.suite import EvalSuite
from ymir.orchestrator.role_executor import (
    PROTECTED_FORCED_IGNORE_PREFIX,
    RoleStageExecutor,
)
from ymir.roles.io import StageInput, StageOutput
from ymir.sandbox.base import ExecResult, NetworkPolicy, SandboxSession
from ymir.storage.repositories import ProjectRepository

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
    """

    def __init__(
        self, *, fail_marker: str = "", missing_bins: tuple[str, ...] = ()
    ) -> None:
        self.files: dict[str, str] = {}
        self.fail_marker = fail_marker
        self.missing_bins = tuple(missing_bins)
        self.executed: list[str] = []

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
        if cmd.startswith("command -v "):
            name = cmd[len("command -v ") :].strip()
            if name in self.missing_bins:
                return ExecResult(stdout="", stderr="", exit_code=127, duration_ms=1)
            return ExecResult(
                stdout=f"/usr/bin/{name}", stderr="", exit_code=0, duration_ms=1
            )
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
        self, *, fail_marker: str = "", missing_bins: tuple[str, ...] = ()
    ) -> None:
        self.fail_marker = fail_marker
        self.missing_bins = missing_bins
        self.sessions: list[_ScriptedSession] = []

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        session = _ScriptedSession(
            fail_marker=self.fail_marker, missing_bins=self.missing_bins
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
) -> tuple[ArtifactStore, str, str]:
    """铺一个增量项目：baseline 产物 + 带 ``patch_manifest.json`` 的 code 产物。"""
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


def _template(*, on_hit: str = "reject") -> Any:
    """最小 ``TemplateSpec`` 替身（只提供 ``_apply_patch_contract`` 用到的接口）。"""
    return SimpleNamespace(
        protected=SimpleNamespace(on_hit=on_hit, max_change_ratio=0.5),
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
