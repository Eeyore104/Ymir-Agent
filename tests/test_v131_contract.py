"""v1.3.1 契约回归矩阵（T6.1，docs/13 §2-T6.1 / A9 红线）。

**要防的裂缝**

v1.3.1 往四个地方各加了一个「带默认值的新能力」：``requires_files``（探针侧过滤）、
``symbol_deletions``（符号删除检测）、``provenance``（合入溯源）、
``failure_attribution``（失败归因）。四者都写着「未配置 ⇒ 与 v1.3 逐字节一致」，
但这句话此前只有散落的单点断言（``test_patch.py`` 的 manifest 快照、
``test_evaluation.py`` 的 markdown 对照、``test_incremental_guard.py`` 的冒烟键集合）。
本文件把它升级成一张 **5 组输入 × 4 个产物 = 20 格** 的矩阵（另加 1 格探针缓存键 +
2 格 G6 全集守卫）：**共 23 个测试**（``pytest --collect-only`` 可数）。

为什么是 5 组而不是 4 组：``docs/13`` §8 的 A9（``docs/13-增量设计与任务列表-v1.3.1.md:1001``）
把「未配置时逐字节一致」定义为**五组**输入 —— 第 5 组是「基线已含全部骨架 ⇒
``probe_mode`` 键不出现」（即本文件的 G5）。为什么是 4 个产物而不是 3 个：同一条 A9 把
「**报告 JSON**」与 ``patch_manifest.json``、报告 markdown、``smoke_report.json``
并列为四个产物。

**23 个测试 ≠ 23 个独立断言体（说实话）**

矩阵的形状决定了有些格**共用同一个断言体**：同一种产物、同一份输入，换组只是换
"为什么没配置"的理由，代码路径完全一致。**不写断言体总数**（这个数取决于"怎么数"，
可数的是 23 这个测试数）；各列共用关系见下：

- **manifest 列**：五格的**字节比对体相同**（`merged.manifest_json() == _dumps(字面量)`），
  唯一例外是 G3（`from_scratch` 用 `V13_FRESH_MANIFEST`）。字节比对**之外**各格还有
  自己的定位断言：G1 `"requires_files" not in`；G2 `"symbol_removals"` /
  `"symbol_regression" not in` + `reject` 反证；G3 `provenance is None` +
  `"provenance" not in`；G4 / G5 无额外断言。
- **smoke 列**：G2 ≡ G3 ≡ G4 ≡ G5 **完全同体**（一条 assert）；G1 只是多传了
  `template=`（验"非 `frontend_build` 组不追加 `source`"），断言体也相同。
- **markdown 列**：五格都有「与黄金样本比 bytes」；G3 额外 1 条
  （`count("基线检查：") == 1`）；G5 额外 4 条（`raw == legacy`、`"probe_mode"` /
  `"骨架补缺" not in`、模型级三条键存在性）。
- **报告 JSON 列**：五格都有「与黄金样本比 bytes」；G1 额外 `"skipped_reason" not in`；
  G3 额外 2 条（黄金样本里确有 `null` 探针块、`"provenance" not in`）；
  G4 额外 `"failure_attribution" not in`；G5 额外 3 条（`raw == legacy`、
  `"probe_mode" not in`、`skeleton_filled` 必落键反证）。
- **其它 3 格**：探针缓存键（G1 独有）、G6(a) 键路径全集、G6(b) 显式默认值 == 黄金 ——
  三者互不重复。

**参考值怎么来（不拿被测函数当自己的裁判）**

- ``patch_manifest.json`` / ``smoke_report.json``：**手写字面量**重建，再用与生产同一套
  序列化纪律渲染后比 **bytes**。两份字面量与黄金样本**同源**（已实测逐字节相等，
  见 ``_v13_patch_manifest`` / ``_v13_smoke_bytes`` 的 docstring）。
- 报告 markdown / 报告 JSON：**真黄金样本** ``tests/fixtures/v13_golden/`` 比 bytes。
  样本由 ``scripts/gen_v13_golden.py`` 在 **v1.3 的 worktree**（commit ``9ec20b9``）里生成，
  不是当前代码产出的 —— 这是它唯一的价值来源；出处见
  ``tests/fixtures/v13_golden/PROVENANCE.md``。读数一律 ``Path.read_bytes()``
  （不经平台换行转换，纪律同 ``tests/test_prompts.py:108``）。
  ★ **这一列的边界（原文照抄进 T6.3 的 as-built 与已知限制）**：
  它证明「当前代码对**这三任务输入**产出的 markdown/JSON，与 v1.3 产出的逐字节相同」，
  覆盖 v1.3 的四个渲染分支（基线检查行 / ⚠️ 告警行 / 未执行行 / 未执行计数行）。
  它**不**证明「所有可能的 v1.3 输入形态都一致」，也**不**证明「v1.3 的 markdown
  渲染已被全覆盖」—— 黄金样本只冻结了被 fixture 走到的路径。

  **未覆盖的渲染分支清单（6 条，QA 逐处核过 `report.py`）**：

  1. `BaselineProbeOut.cached=True` ⇒ 「…（缓存命中）」行；
  2. `TaskOut.regression=True` ⇒ 「回归：…」行；
  3. `TaskOut.blocking_failed` 非空 ⇒ 「阻断项失败：…」行；
  4. `TaskOut.skipped_checks` 非空 ⇒ 「已跳过检查（不扣分）：…」行；
  5. `_attribution_lines(failure_attribution)` 有值 ⇒ 归因行；
  6. `probe_mode=skeleton_filled` / `skipped_reason` 非空（**v1.3.1 新增**）。

  前 4 条是 **v1.3 就有**的分支，样本没走到 ⇒ 它们**没有**被样本守到；
  后 2 条是 v1.3.1 新增，而 **A9 只规定「未配置态」**，未覆盖**不算缺陷**。
  换句话说：样本证明的是「这些分支在**默认值**下不渲染」，不等于「这些分支本身正确」。
- 报告 JSON 的**全集守卫**：见 :class:`TestG6ReportJson`。**(a)** 递归键路径全集与手写的
  v1.3 键路径表 ``set`` 相等（唯一能抓到"第四个新键"的守卫，但**随 fixture 变化、
  不是模型不变量**，主守卫是黄金样本）；**(b)** 显式把新字段写成默认值后仍与黄金样本
  逐字节相同（``files()`` 会追加 ``"\n"``，比 ``files()`` 才是落盘字节）。

**为什么改成黄金样本（这段别删）**

本文件上一版有 **9 格**「缺省 vs 显式默认值比字节」。那是**构造性恒真**：
``report.py`` 的默认值就是那些值（``skipped_checks=[]`` / ``failure_attribution=None`` /
``probe_mode=""`` / ``skipped_reason=""``），两侧构造出的是**同一个模型状态**，
``model_dump`` 必然相等，**与 pop 逻辑无关**。QA 用变异 C/D 实证：那两个变异下这些格
一次都没红。现在改成与 v1.3 黄金样本比 bytes，同样的变异会立刻红。
★ 例外：G5 的 markdown/JSON 两格**本来就不恒真**（``raw`` 是非默认值却仍须被 pop），
所以它们保留「``raw`` vs 旧数据」的对照，并额外加上与黄金样本比对。

**纪律**

- 每格独立 ``assert``，失败信息写清「哪一组 × 哪个产物」；
- **不改生产代码**：任何一格拿不到逐字节相等 ⇒ A9 破了，应停下报告，而不是放宽断言；
- 不 mock 被测逻辑：五组都跑真实函数（``merge_trees`` / ``_run_sandbox_smoke`` /
  ``EvaluationReport.build``），只把"当前时刻"这类不可比字段钉成常量。
  尤其 G2：``off``（未配置）时符号检测的**入口条件**为假（``merge.py`` 的
  ``if symbol_deletions != "off" and base_norm``），因此"跑了但放行"在代码上表现为
  "入口不进入"；为证明这份 fixture 真的是符号回归（而不是空转），G2 额外用
  ``reject`` 反证同一输入必抛 :class:`SymbolRegressionConflict`。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ymir.artifacts.merge import SymbolRegressionConflict, merge_trees
from ymir.artifacts.versioning import compute_content_hash
from ymir.evaluation import (
    ARCHITECTURE,
    CONVENTION,
    REQUIREMENT,
    RUNNABILITY,
    EvalCheck,
    EvaluationReport,
    TaskOut,
)
from ymir.evaluation.baseline_probe import (
    PROBE_MODE_SKELETON_FILLED,
    baseline_cache_key,
    missing_requires_files,
)
from ymir.evaluation.report import (
    BaselineCheckOut,
    BaselineProbeOut,
    DimensionOut,
    EvidenceOut,
)
from ymir.evaluation.suite import Thresholds
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.io import StageOutput
from ymir.sandbox.base import ExecResult, NetworkPolicy, SandboxSession
from ymir.templates.models import CheckSpec

# --------------------------------------------------------------------------- 输入样本

#: 两组输入的公共基线：一个源文件 + 一个**引用它**的测试（符号回归的最小形态）。
BASE: dict[str, str] = {
    "app/main.py": "def main() -> None:\n    return None\n",
    "tests/test_main.py": "from app.main import main\n\n\ndef test_main() -> None:\n    main()\n",
}

#: G1 / G4 的普通补丁：改一个文件、保留公共符号 ``main``（无符号回归）。
PLAIN_PATCH: dict[str, str] = {"app/main.py": "def main() -> int:\n    return 1\n"}

#: G2 的补丁：**删掉仍被 ``tests/test_main.py`` 引用的公共符号 ``main``**。
SYMBOL_PATCH: dict[str, str] = {"app/main.py": "X = 1\n"}

#: G3 的 ``from_scratch`` 输入（无基线 ⇒ 无 provenance）。
FRESH: dict[str, str] = {"app/main.py": "app = None\n", "requirements.txt": "pytest>=8.2\n"}

# --------------------------------------------------------------------------- 参考值


def _v13_patch_manifest() -> dict[str, Any]:
    """v1.3 对「``patch`` 模式、改 1/2 个文件」会产出的正文（**字面量**重建）。

    ``change_ratio`` / ``counts`` 是 v1.3 既有字段（v1.3 就设了），因此**在内**；
    v1.3.1 的 ``provenance`` / ``counts.symbol_removals`` **不在内**。
    除 ``base_content_hash`` 外全部字面量 —— 那一个字段是 v1.3 既有、且由输入树算出的
    数据（``patch`` 模式取基线快照指纹），用 v1.3 的哈希工具现算：它不是被测对象，
    钉死反而会让用例随夹具有无关变更而红。

    ★ **与 ``tests/fixtures/v13_golden/patch_manifest.json`` 同源**（已实测逐字节相等）：
    那份黄金样本由 ``scripts/gen_v13_golden.py`` 在 v1.3 的 worktree 里用同一份
    ``BASE`` / ``PLAIN_PATCH`` 产出，所以这里的字面量等价于 v1.3 自己的输出。
    """
    return {
        "base_content_hash": compute_content_hash(BASE),
        "change_ratio": 0.5,
        "changed_paths": ["app/main.py"],
        "conflicts": [],
        "counts": {
            "added": 0,
            "deleted": 0,
            "modified": 1,
            "noop_dropped": 0,
            "reserved_conflicts": 0,
            "skeleton_added": 0,
            "skipped_existing": 0,
        },
        "deleted": [],
        "mode": "patch",
        "reserved_conflicts": [],
        "skeleton_added": [],
        "skipped_existing": [],
    }


#: v1.3 对 ``from_scratch`` 会产出的正文（``change_ratio`` 恒 0 ⇒ 该键整块消失）。
V13_FRESH_MANIFEST: dict[str, Any] = {
    "base_content_hash": "",
    "changed_paths": ["app/main.py", "requirements.txt"],
    "conflicts": [],
    "counts": {
        "added": 2,
        "deleted": 0,
        "modified": 0,
        "noop_dropped": 0,
        "reserved_conflicts": 0,
        "skeleton_added": 0,
        "skipped_existing": 0,
    },
    "deleted": [],
    "mode": "from_scratch",
    "reserved_conflicts": [],
    "skeleton_added": [],
    "skipped_existing": [],
}

#: 冒烟报告里唯一不可比的字段（每次取当前时刻）—— 两侧钉成同一常量。
FIXED_EXECUTED_AT: str = "2026-01-01T00:00:00+00:00"

#: 报告生成时间同理。
FIXED_GENERATED_AT: str = "2026-01-01T00:00:00+00:00"

#: 冒烟命令与固定输出（假会话按此返回 ⇒ 条目内容完全确定）。
SMOKE_COMMAND: str = "python -m pytest -q"
SMOKE_STDOUT: str = "2 passed\n"

#: 报告权重（既有用例同值，只为让 markdown 可渲染）。
WEIGHTS: dict[str, float] = {
    REQUIREMENT: 0.3,
    RUNNABILITY: 0.3,
    CONVENTION: 0.2,
    ARCHITECTURE: 0.2,
}


def _dumps(payload: Any) -> str:
    """与 ``PatchManifest.manifest_json`` 同一套序列化纪律（键排序 + 紧凑分隔符）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _v13_smoke_bytes() -> str:
    """v1.3 形态的 ``smoke_report.json`` 正文（字面量 + 生产同一句 ``dumps``）。

    ★ **与 ``tests/fixtures/v13_golden/smoke_report.json`` 同源**（已实测逐字节相等）：
    黄金样本由 v1.3 的 ``RoleStageExecutor._run_sandbox_smoke`` 产出，唯一被钉死的是
    ``executed_at``（每次取当前时刻，本就不可比）。
    """
    payload: dict[str, Any] = {
        "backend": "fake",
        "network": "none",
        "executed_at": FIXED_EXECUTED_AT,
        "commands": [
            {
                "command": SMOKE_COMMAND,
                "stdout": SMOKE_STDOUT,
                "stderr": "",
                "exit_code": 0,
                "duration_ms": 1,
                "timed_out": False,
            }
        ],
        "all_ok": True,
        "error": None,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- 假沙箱


class _ScriptedSession(SandboxSession):
    """最小假会话：一切命令 exit=0、``duration_ms=1``（冒烟只记录，不判定）。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.executed: list[str] = []

    @property
    def sandbox_id(self) -> str | None:
        return "fake-contract"

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy.NONE

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        self.executed.append(cmd)
        return ExecResult(stdout=SMOKE_STDOUT, stderr="", exit_code=0, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        return None


class _FakeSandboxService:
    """只提供冒烟用到的 ``sandbox`` / ``start`` / ``destroy`` 三件事。"""

    def __init__(self, session: _ScriptedSession) -> None:
        self.sandbox = SimpleNamespace(backend="fake")
        self.session = session

    async def start(self, run_id: str, *, network: Any = None) -> Any:
        return SimpleNamespace(run_id=run_id, network=network, session=self.session)

    async def destroy(self, managed: Any) -> None:
        return None


async def _smoke_bytes(
    *, commands: tuple[str, ...] = (SMOKE_COMMAND,), template: Any = None
) -> str:
    """跑真实 ``_run_sandbox_smoke``，返回**生产口径**的报告正文。"""
    executor = RoleStageExecutor.__new__(RoleStageExecutor)
    executor._sandbox_service = _FakeSandboxService(_ScriptedSession())  # type: ignore[attr-defined]
    executor._sandbox_network = NetworkPolicy.NONE  # type: ignore[attr-defined]
    executor._exec_timeout = 120.0  # type: ignore[attr-defined]
    output = StageOutput(
        files={},
        summary="ok",
        structured={"files": [], "run_commands": list(commands)},
    )
    run = SimpleNamespace(id="run-contract", context={})
    report = await executor._run_sandbox_smoke(  # noqa: SLF001
        run, output, files=dict(BASE), template_spec=template
    )
    # 唯一不可比的字段：钉成常量后再比 bytes（其余字段一个都不动）
    report["executed_at"] = FIXED_EXECUTED_AT
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- 黄金样本

#: v1.3 黄金样本目录。样本由 ``scripts/gen_v13_golden.py`` 在 **v1.3 的 worktree**
#: （commit ``9ec20b9``）里生成，**不是**当前代码产出的；出处见同目录 ``PROVENANCE.md``。
GOLDEN_DIR: Path = Path(__file__).parent / "fixtures" / "v13_golden"


def _golden_bytes(name: str) -> str:
    """读黄金样本的**字节**。

    ★ 用 ``read_bytes()`` 而不是 ``read_text()``：后者会做平台换行转换，
    "逐字节一致"就假了（纪律同 ``tests/test_prompts.py:108``）。
    """
    return (GOLDEN_DIR / name).read_bytes().decode("utf-8")


def _contract_tasks(probe_mode: str = "", *, explicit_defaults: bool = False) -> list[TaskOut]:
    """黄金样本的输入，用**当前代码**的构造器重建（v1.3.1 字段全取默认值）。

    三个任务与 ``scripts/gen_v13_golden.py`` 里的输入逐字段对齐：
    ① 满配（维度 + 证据 + 基线探针 + 告警）② 最简（无探针无维度）③ ``skipped``（未绑定产物）。
    ★ 改动这里必须同步改生成脚本并重新生成样本，否则黄金比对会红 —— 那是**预期**行为。

    Args:
        probe_mode: 塞进任务①探针的 ``probe_mode``（""/"raw"/"skeleton_filled"）。
        explicit_defaults: 为 ``True`` 时**显式**把 v1.3.1 三个可选字段写成默认值
            （``skipped_checks=[]`` / ``failure_attribution=None`` / ``skipped_reason=""``）。
            用于证明「显式写默认值」不改变任何字节。
    """
    extras: dict[str, Any] = (
        {"skipped_checks": [], "failure_attribution": None} if explicit_defaults else {}
    )
    check_extras: dict[str, Any] = {"skipped_reason": ""} if explicit_defaults else {}
    return [
        TaskOut(
            id="t-eval-rich",
            scenario="s",
            project_id="p",
            run_id="r",
            requirement="加一个导出接口",
            artifact_ref={"artifact_id": "a1"},
            dimensions=[
                DimensionOut(
                    name=RUNNABILITY,
                    label="可运行性",
                    score=100.0,
                    weight=0.3,
                    rationale="2/2 检查通过",
                    evidence=[EvidenceOut(kind="command", ref=SMOKE_COMMAND, detail="2 passed")],
                )
            ],
            total_score=70.0,
            runnability=100.0,
            passed=True,
            warnings=["基线不可用 → 分数与基线内运行不可比"],
            baseline_runnability=BaselineProbeOut(
                status="healthy",
                score=100.0,
                passed=True,
                checks=[
                    BaselineCheckOut(
                        command=SMOKE_COMMAND,
                        exit_code=0,
                        output="2 passed",
                        **check_extras,
                    )
                ],
                reason="",
                duration_ms=12,
                cached=False,
                probe_mode=probe_mode,
            ),
            **extras,
        ),
        TaskOut(
            id="t-eval-plain",
            requirement="普通任务",
            total_score=60.0,
            runnability=50.0,
            passed=False,
            **extras,
        ),
        TaskOut(
            id="t-skipped",
            status="skipped",
            skip_reason="未绑定产物",
            project_id="p",
            artifact_ref={"artifact_id": None},
            thresholds={"total": 75.0, "runnability": 60.0},
            **extras,
        ),
    ]


def _report_of(tasks: list[TaskOut]) -> EvaluationReport:
    """把任意任务列表装成报告（``generated_at`` 钉死 = 样本里的常量）。"""
    report = EvaluationReport.build(
        suite_id="default",
        suite_version="1.0",
        project_id="p",
        weights=dict(WEIGHTS),
        thresholds=Thresholds(total=75.0, runnability=60.0),
        tasks=tasks,
        wall_time_ms=1234,
    )
    report.generated_at = FIXED_GENERATED_AT
    return report


def _contract_report(probe_mode: str = "", *, explicit_defaults: bool = False) -> EvaluationReport:
    """黄金样本输入对应的报告。"""
    return _report_of(_contract_tasks(probe_mode, explicit_defaults=explicit_defaults))


def _contract_markdown(probe_mode: str = "") -> str:
    """当前代码对黄金样本输入产出的报告 markdown。"""
    return _contract_report(probe_mode).to_markdown()


def _contract_json(probe_mode: str = "", *, explicit_defaults: bool = False) -> str:
    """当前代码对黄金样本输入产出的**落盘**报告 JSON。

    ★ 取 ``files()`` 而不是 ``to_json()``：``files()`` 在 ``to_json()`` 之后追加了一个
    ``"\\n"``（``report.py:478``），那份才是真正进 ``ArtifactStore`` 的字节。
    """
    return _contract_report(probe_mode, explicit_defaults=explicit_defaults).files()[
        "evaluation_report.json"
    ]


def _json_key_paths(payload: Any, prefix: str = "") -> set[str]:
    """递归收集 JSON 的**全部键路径**（列表按下标展开，空容器只留自身路径）。

    例：``tasks[0].baseline_runnability.checks[0].output``。
    """
    paths: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths |= _json_key_paths(value, path)
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            paths |= _json_key_paths(item, f"{prefix}[{index}]")
    return paths


def _v13_cache_key(files: dict[str, str], checks: list[EvalCheck]) -> str:
    """v1.3 的基线缓存键算法（**字面量独立实现**，不含 ``requires_files`` 块）。

    与 ``baseline_probe.baseline_cache_key`` 的 v1.3 前缀逐字节同构：
    files 块 → checks 块（7 个执行相关字段的 ``repr``）；v1.3 没有骨架块。
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[path].encode("utf-8"))
        digest.update(b"\0")
    for check in checks:
        fingerprint = repr(
            (
                check.type,
                check.run,
                check.weight,
                check.group,
                check.blocking,
                check.requires,
                check.timeout_s,
            )
        )
        digest.update(fingerprint.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _probe(probe_mode: str) -> BaselineProbeOut:
    """v1.3 形态的探针结果 + 一个 ``probe_mode`` 取值（其余字段与 v1.3 一致）。"""
    return BaselineProbeOut(
        status="healthy",
        score=100.0,
        passed=True,
        checks=[],
        reason="",
        duration_ms=0,
        cached=False,
        probe_mode=probe_mode,
    )


# ============================================================ G1 · requires_files 未配置


class TestG1RequiresFilesUnset:
    """G1：模板里有 ``check``，但 ``CheckSpec.requires_files`` **不写**（默认空清单）。

    ``requires_files`` 是**探针侧**概念：它决定"基线里没有该检查依赖的文件 ⇒ 探针跳过"。
    它不进 manifest、不进冒烟、也不进报告正文，因此四个产物的正确结论都是
    「与 v1.3 逐字节相等」—— **这个"不扰动"就是 A9 要求的全部内容**。

    ★ 本组模板 check 刻意取 ``group="backend"``：``frontend_build`` 组会被 T4.1 补进
    冒烟并带上 ``source``（那是**另一个** v1.3.1 能力），混进来就测不清是谁改的字节。
    ★ 缓存键那一格是 G1 **独有**的真正落点（``requires_files`` 不进任何产物，
    所以只有指纹是逐字节可比的）。
    """

    def test_g1_manifest_bytes_identical(self) -> None:
        merged = merge_trees(BASE, PLAIN_PATCH, mode="patch", base_paths=frozenset(BASE))
        actual = merged.manifest_json()

        assert actual == _dumps(_v13_patch_manifest()), (
            "G1 × patch_manifest.json：未配置 requires_files 却改动了 manifest 正文"
        )
        assert "requires_files" not in actual, "G1 × patch_manifest.json：新字段泄漏进正文"

    async def test_g1_smoke_bytes_identical(self) -> None:
        template = SimpleNamespace(checks=[CheckSpec(run=SMOKE_COMMAND, group="backend")])
        actual = await _smoke_bytes(template=template)

        assert actual == _v13_smoke_bytes(), (
            "G1 × smoke_report.json：非 frontend_build 组（或 requires_files 未配置）时，"
            "报告必须与 v1.3 逐字节一致（不多出 source/skipped 等键）"
        )

    def test_g1_markdown_bytes_identical(self) -> None:
        """G1 × 报告 markdown：与 v1.3 黄金样本逐字节相等。

        断言体与 G2/G4 相同（同一份输入、同一列产物，代码路径完全一致）；
        组间差异体现在上面 manifest/smoke 两列各自的入参。
        """
        assert _contract_markdown() == _golden_bytes("evaluation_report.md"), (
            "G1 × 报告 markdown：未配置 requires_files 时渲染结果偏离了 v1.3 黄金样本"
        )

    def test_g1_report_json_bytes_identical(self) -> None:
        """G1 × 报告 JSON：与 v1.3 黄金样本逐字节相等 + ``skipped_reason`` 不得落盘。"""
        actual = _contract_json()

        assert actual == _golden_bytes("evaluation_report.json"), (
            "G1 × 报告 JSON：未配置 requires_files 时落盘正文偏离了 v1.3 黄金样本"
        )
        # 下面这条被上面的字节比对蕴含，保留是为了**失败时一眼定位到具体键**
        assert "skipped_reason" not in actual, (
            "G1 × 报告 JSON：未配置 requires_files 却让 BaselineCheckOut.skipped_reason 落盘"
        )

    def test_g1_probe_cache_key_bytes_identical(self) -> None:
        """G1 的真正落点：``requires_files`` 为空 ⇒ 缓存键与 v1.3 **逐字节**相同。"""
        check = EvalCheck(type="command", run=SMOKE_COMMAND)
        assert check.requires_files == []
        assert missing_requires_files(check, BASE) == ()  # 未配置 ⇒ 不过滤任何检查

        assert baseline_cache_key(BASE, [check]) == _v13_cache_key(BASE, [check]), (
            "G1 × 探针缓存键：空 requires_files 不得追加任何字节"
        )
        # 反证：真的声明了该字段时键必须变（否则上面那条就是空转）
        declared = EvalCheck(type="command", run=SMOKE_COMMAND, requires_files=["requirements.txt"])
        assert baseline_cache_key(BASE, [declared]) != _v13_cache_key(BASE, [declared]), (
            "G1 × 探针缓存键：声明 requires_files 后键必须变（指纹覆盖该字段）"
        )


# ============================================================ G2 · symbol_deletions 未配置


class TestG2SymbolDeletionsUnset:
    """G2：``symbol_deletions`` 不写（默认 ``off``），且补丁里**真的**删了仍被引用的公共符号。

    四个产物都必须与 v1.3 逐字节相等：``off`` 时 ``merge_trees`` 的检测入口条件为假
    （``if symbol_deletions != "off" and base_norm``）⇒ 既不产出 ``counts.symbol_removals``，
    也不产出 ``symbol_regression_ignored`` 告警短码。
    ``report.py`` 里**根本没有**符号渲染分支，所以报告两列的断言体与 G1/G4 相同。

    ★ 为证明这份 fixture 不是空转（"检测没跑" vs "输入压根不是回归"），最后用 ``reject``
    反证同一输入必抛 :class:`SymbolRegressionConflict`。
    """

    def test_g2_manifest_bytes_identical(self) -> None:
        merged = merge_trees(BASE, SYMBOL_PATCH, mode="patch", base_paths=frozenset(BASE))
        actual = merged.manifest_json()

        assert actual == _dumps(_v13_patch_manifest()), (
            "G2 × patch_manifest.json：symbol_deletions 未配置（off）却改动了 manifest 正文"
        )
        assert "symbol_removals" not in actual, "G2 × patch_manifest.json：符号计数泄漏进正文"
        assert "symbol_regression" not in actual, "G2 × patch_manifest.json：符号告警泄漏进正文"

        # 反证：同一输入在 reject 下必抛 ⇒ 它确实是一次符号回归
        with pytest.raises(SymbolRegressionConflict):
            merge_trees(
                BASE,
                SYMBOL_PATCH,
                mode="patch",
                base_paths=frozenset(BASE),
                symbol_deletions="reject",
            )

    async def test_g2_smoke_bytes_identical(self) -> None:
        actual = await _smoke_bytes()

        assert actual == _v13_smoke_bytes(), (
            "G2 × smoke_report.json：符号策略不进冒烟，报告必须与 v1.3 逐字节一致"
        )

    def test_g2_markdown_bytes_identical(self) -> None:
        """G2 × 报告 markdown：与 v1.3 黄金样本逐字节相等。

        ★ 这里**删掉了**原版的 ``"符号" not in`` 断言 —— ``report.py`` 里根本不存在
        符号渲染分支，那条永不失败（QA P2-2）。断言体与 G1/G4 相同，见模块 docstring
        的共用表。
        """
        assert _contract_markdown() == _golden_bytes("evaluation_report.md"), (
            "G2 × 报告 markdown：未配置 symbol_deletions 时渲染结果偏离了 v1.3 黄金样本"
        )

    def test_g2_report_json_bytes_identical(self) -> None:
        """G2 × 报告 JSON：与 v1.3 黄金样本逐字节相等。

        ★ 同样删掉了 ``"symbol" not in`` —— 同理恒真。断言体与 G3 相同。
        """
        assert _contract_json() == _golden_bytes("evaluation_report.json"), (
            "G2 × 报告 JSON：未配置 symbol_deletions 时落盘正文偏离了 v1.3 黄金样本"
        )


# ============================================================ G3 · 无 provenance


class TestG3NoProvenance:
    """G3：无基线（``from_scratch``）⇒ ``PatchManifest.provenance is None``。

    ``provenance`` 挂在「有基线的增量合并」上；``from_scratch`` 构造上取不到基线引用，
    因此键必须**整个消失**（不是空对象），正文与 v1.3 逐字节相等（红线 A9 的核心一条）。
    """

    def test_g3_manifest_bytes_identical(self) -> None:
        merged = merge_trees({}, FRESH, mode="from_scratch")
        actual = merged.manifest_json()

        assert merged.manifest.provenance is None, (
            "G3 × patch_manifest.json：from_scratch 不该有溯源对象"
        )
        assert actual == _dumps(V13_FRESH_MANIFEST), (
            "G3 × patch_manifest.json：无基线的正文必须与 v1.3 逐字节一致"
        )
        assert "provenance" not in actual, "G3 × patch_manifest.json：provenance 键未整块消失"

    async def test_g3_smoke_bytes_identical(self) -> None:
        actual = await _smoke_bytes()

        assert actual == _v13_smoke_bytes(), (
            "G3 × smoke_report.json：溯源信息不进冒烟，报告必须与 v1.3 逐字节一致"
        )

    def test_g3_markdown_bytes_identical(self) -> None:
        """G3 × 报告 markdown：与 v1.3 黄金样本逐字节相等 + 基线检查行只出现一次。

        ★ 这里**替换掉**了原版的 ``"基线检查" not in`` —— 那个输入里压根没有探针，
        那条断言永不失败（QA P2-2）。改成从**黄金样本**取事实：样本里只有
        ``t-eval-rich`` 一个任务有探针，所以「基线检查：」必须恰好 1 行；
        若哪天有人给"无基线"也加了一行渲染，这里立刻红。
        """
        actual = _contract_markdown()

        assert actual == _golden_bytes("evaluation_report.md"), (
            "G3 × 报告 markdown：无基线的任务渲染偏离了 v1.3 黄金样本"
        )
        assert actual.count("基线检查：") == 1, (
            "G3 × 报告 markdown：基线检查行只应出现在有探针的那个任务上（恰好 1 行）"
        )

    def test_g3_report_json_bytes_identical(self) -> None:
        """G3 × 报告 JSON：与 v1.3 黄金样本逐字节相等 + 无基线任务仍是 ``null``。

        ``baseline_runnability`` 在 v1.3 的正文里是 ``null``（**不是整块消失**），
        这条口径从黄金样本里读，不从当前输出里读。
        """
        actual = _contract_json()

        assert actual == _golden_bytes("evaluation_report.json"), (
            "G3 × 报告 JSON：无基线时落盘正文偏离了 v1.3 黄金样本"
        )
        assert '"baseline_runnability": null' in _golden_bytes("evaluation_report.json"), (
            "G3 × 报告 JSON：黄金样本里应存在 null 的探针块（口径参照物本身出错）"
        )
        assert '"provenance"' not in actual, "G3 × 报告 JSON：溯源块泄漏进报告正文"


# ============================================================ G4 · 无 attribution


class TestG4NoAttribution:
    """G4：评测输入不含 probe 结果 / 不含跨命令差异 ⇒ ``failure_attribution is None``。

    归因是**提示**，既不进判据也不进正文：markdown 与报告 JSON 都必须与 v1.3
    逐字节一致（JSON 侧由 ``TaskOut`` 的模型级 serializer 摘键）。
    """

    def test_g4_manifest_bytes_identical(self) -> None:
        merged = merge_trees(BASE, PLAIN_PATCH, mode="patch", base_paths=frozenset(BASE))
        actual = merged.manifest_json()

        assert actual == _dumps(_v13_patch_manifest()), (
            "G4 × patch_manifest.json：归因只影响报告，manifest 必须与 v1.3 逐字节一致"
        )

    async def test_g4_smoke_bytes_identical(self) -> None:
        actual = await _smoke_bytes()

        assert actual == _v13_smoke_bytes(), (
            "G4 × smoke_report.json：归因只影响报告，冒烟必须与 v1.3 逐字节一致"
        )

    def test_g4_markdown_bytes_identical(self) -> None:
        """G4 × 报告 markdown：与 v1.3 黄金样本逐字节相等。

        归因在 v1.3 里根本不存在渲染分支，所以黄金样本就是"归因为默认值"时的正确答案。
        """
        assert _contract_markdown() == _golden_bytes("evaluation_report.md"), (
            "G4 × 报告 markdown：failure_attribution 为默认值时渲染结果偏离了 v1.3 黄金样本"
        )

    def test_g4_report_json_bytes_identical(self) -> None:
        """G4 × 报告 JSON：与 v1.3 黄金样本逐字节相等 + 键**消失**（不是 null）。"""
        actual = _contract_json()

        assert actual == _golden_bytes("evaluation_report.json"), (
            "G4 × 报告 JSON：failure_attribution 为默认值时落盘正文偏离了 v1.3 黄金样本"
        )
        # 被上面的字节比对蕴含，保留是为了失败时一眼定位到具体键
        assert "failure_attribution" not in actual, (
            "G4 × 报告 JSON：无归因时该键必须消失（不是 null）"
        )


# ============================================================ G5 · probe_mode 不落盘


class TestG5ProbeModeNotSerialized:
    """G5（**超出指令、按 A9 补齐**）：``probe_mode="raw"``（基线已含全部骨架 ⇒
    探针树 == 基线原样）⇒ 该键**不落盘**，报告 JSON 与 markdown 与 v1.3 逐字节一致。

    ★ 为什么补这一组：``docs/13`` §8 的 A9（``docs/13-增量设计与任务列表-v1.3.1.md:1001``）
    把「未配置时逐字节一致」定义为**五组**输入，第 5 组正是「基线已含全部骨架
    （``probe_mode`` 键不出现）」；最初指令里的 4 组是它的子集。补上才能把 A9 逐组对上。

    ★ 本组两格**不是**恒真断言（QA 也确认）：``raw`` 是**非默认值**却仍须被 pop，
    删掉 ``BaselineProbeOut`` 的 pop 判断，``raw == legacy`` 立刻红。
    """

    def test_g5_manifest_bytes_identical(self) -> None:
        merged = merge_trees(BASE, PLAIN_PATCH, mode="patch", base_paths=frozenset(BASE))
        actual = merged.manifest_json()

        assert actual == _dumps(_v13_patch_manifest()), (
            "G5 × patch_manifest.json：探针口径只影响报告，manifest 必须与 v1.3 逐字节一致"
        )

    async def test_g5_smoke_bytes_identical(self) -> None:
        actual = await _smoke_bytes()

        assert actual == _v13_smoke_bytes(), (
            "G5 × smoke_report.json：探针口径只影响报告，冒烟必须与 v1.3 逐字节一致"
        )

    def test_g5_markdown_bytes_identical(self) -> None:
        """G5 × 报告 markdown：``raw`` 与 v1.3 旧数据（``""``）**都等于黄金样本**。"""
        raw = _contract_markdown("raw")
        legacy = _contract_markdown("")  # v1.3 旧数据：该字段根本不存在

        assert raw == legacy, (
            "G5 × 报告 markdown：probe_mode=raw 与 v1.3 旧数据必须渲染出同一份 markdown"
        )
        assert raw == _golden_bytes("evaluation_report.md"), (
            "G5 × 报告 markdown：probe_mode=raw 时渲染结果偏离了 v1.3 黄金样本"
        )
        assert "probe_mode" not in raw and "骨架补缺" not in raw, (
            "G5 × 报告 markdown：探针口径泄漏进人读正文"
        )

        # 模型级：raw / 空串都不落键；只有 skeleton_filled 才落（反证键不是永远不出现）
        assert "probe_mode" not in _probe("raw").model_dump_json()
        assert "probe_mode" not in _probe("").model_dump_json()
        assert "probe_mode" in _probe(PROBE_MODE_SKELETON_FILLED).model_dump_json(), (
            "G5 × 报告 JSON：反证 —— 基线真的补了骨架时必须落键（否则键永远不出现＝没实现）"
        )
        # 逐字节：raw 与旧数据的探针 JSON 完全同构
        assert _probe("raw").model_dump_json() == _probe("").model_dump_json()

    def test_g5_report_json_bytes_identical(self) -> None:
        """G5 × 报告 JSON：``raw`` 与旧数据都等于黄金样本；``skeleton_filled`` 必落键。"""
        raw = _contract_json("raw")
        legacy = _contract_json("")

        assert raw == legacy, "G5 × 报告 JSON：probe_mode=raw 与 v1.3 旧数据（空串）必须逐字节一致"
        assert raw == _golden_bytes("evaluation_report.json"), (
            "G5 × 报告 JSON：probe_mode=raw 时落盘正文偏离了 v1.3 黄金样本"
        )
        assert "probe_mode" not in raw, "G5 × 报告 JSON：raw 不得落键"

        # 反证：真补了骨架时必须落键（否则"该出现时出现"也无人守）
        filled = _contract_json(PROBE_MODE_SKELETON_FILLED)
        assert "probe_mode" in filled, "G5 × 报告 JSON：反证 —— probe_mode=skeleton_filled 必须落键"


# ============================================================ G6 · 报告 JSON 键路径全集

#: v1.3 报告 JSON 的**全部键路径**（手写；不是从当前输出导出的）。
#:
#: 骨架 = ``EvaluationReport`` 字段 + 其嵌套模型字段；此处逐条手写，**不含** v1.3.1
#: 的三个可选键（``failure_attribution`` / ``probe_mode`` / ``skipped_reason``），
#: 因为它们在默认值下必须**整键消失**。
#:
#: ★ **定位：次要守卫**。这张表**随 fixture 变化**、不是模型不变量（写死的是
#: ``tasks[0]`` 的单任务形态，fixture 一改就得跟着改）。主守卫是
#: ``tests/fixtures/v13_golden/`` 的黄金样本；本表唯一的不可替代之处是
#: **能抓到「第四个新键」**（黄金样本也能抓到，但失败信息只给"字节不同"）。
V13_REPORT_KEY_PATHS: frozenset[str] = frozenset(
    {
        # ---- EvaluationReport 本层 ----
        "suite_id",
        "suite_version",
        "project_id",
        "run_id",
        "artifact_version",
        "repair_round",
        "verdict_source",
        "generated_at",
        "weights",
        "thresholds",
        "tasks",
        "total_score",
        "runnability",
        "passed",
        "cost_cny",
        "total_tokens",
        "wall_time_ms",
        "executed_count",
        "skipped_count",
        "first_pass_rate",
        "final_pass_rate",
        # ---- weights / thresholds（各自是 dict，两层的路径各算一条）----
        #: 注意 ``weights`` 的键是**维度常量**，不是短名：``REQUIREMENT`` 的值是
        #: ``"requirement_conformance"`` 而不是 ``"requirement"``。
        "weights.requirement_conformance",
        "weights.runnability",
        "weights.convention",
        "weights.architecture",
        "thresholds.total",
        "thresholds.runnability",
        # ---- tasks[0]：TaskOut 全部字段（failure_attribution 已 pop）----
        "tasks[0].id",
        "tasks[0].scenario",
        "tasks[0].status",
        "tasks[0].skip_reason",
        "tasks[0].project_id",
        "tasks[0].run_id",
        "tasks[0].requirement",
        "tasks[0].artifact_ref",
        "tasks[0].dimensions",
        "tasks[0].total_score",
        "tasks[0].runnability",
        "tasks[0].passed",
        "tasks[0].thresholds",
        "tasks[0].wall_time_ms",
        "tasks[0].cost_cny",
        "tasks[0].tokens",
        "tasks[0].warnings",
        "tasks[0].baseline_runnability",
        "tasks[0].regression",
        "tasks[0].regression_reason",
        "tasks[0].blocking_failed",
        "tasks[0].skipped_checks",
        "tasks[0].artifact_ref.artifact_id",
        # ---- 维度 ----
        "tasks[0].dimensions[0].name",
        "tasks[0].dimensions[0].label",
        "tasks[0].dimensions[0].score",
        "tasks[0].dimensions[0].weight",
        "tasks[0].dimensions[0].rationale",
        "tasks[0].dimensions[0].evidence",
        "tasks[0].dimensions[0].cost_cny",
        "tasks[0].dimensions[0].tokens",
        "tasks[0].dimensions[0].degraded",
        # ---- 证据 ----
        "tasks[0].dimensions[0].evidence[0].kind",
        "tasks[0].dimensions[0].evidence[0].ref",
        "tasks[0].dimensions[0].evidence[0].detail",
        # ---- 基线探针（probe_mode 已 pop）----
        "tasks[0].baseline_runnability.status",
        "tasks[0].baseline_runnability.score",
        "tasks[0].baseline_runnability.passed",
        "tasks[0].baseline_runnability.checks",
        "tasks[0].baseline_runnability.reason",
        "tasks[0].baseline_runnability.duration_ms",
        "tasks[0].baseline_runnability.cached",
        # ---- 基线检查条目（skipped_reason 已 pop）----
        "tasks[0].baseline_runnability.checks[0].command",
        "tasks[0].baseline_runnability.checks[0].exit_code",
        "tasks[0].baseline_runnability.checks[0].timed_out",
        "tasks[0].baseline_runnability.checks[0].skipped",
        "tasks[0].baseline_runnability.checks[0].output",
    }
)


def _v13_rich_task() -> TaskOut:
    """v1.3 形态的「满配」任务：四个 v1.3.1 能力全取缺省值，但 v1.3 字段尽量填满。

    刻意的取舍：只填 **v1.3 已有** 的字段（维度 + 证据 + 探针 + 告警），
    这样键路径表既覆盖到嵌套三层，又不会因为 v1.3.1 新字段而模糊"谁多出来的键"。
    """
    return TaskOut(
        id="t",
        scenario="s",
        project_id="p",
        run_id="r",
        requirement="加一个导出接口",
        artifact_ref={"artifact_id": "a1"},
        dimensions=[
            DimensionOut(
                name=RUNNABILITY,
                label="可运行性",
                score=100.0,
                weight=0.3,
                rationale="2/2 检查通过",
                evidence=[EvidenceOut(kind="command", ref=SMOKE_COMMAND, detail="2 passed")],
            )
        ],
        total_score=70.0,
        runnability=50.0,
        passed=False,
        warnings=["基线不可用 → 分数与基线内运行不可比"],
        baseline_runnability=BaselineProbeOut(
            status="healthy",
            score=100.0,
            passed=True,
            checks=[BaselineCheckOut(command=SMOKE_COMMAND, exit_code=0, output="2 passed")],
        ),
    )


class TestG6ReportJson:
    """G6（按 team-lead 裁定补做）：A9 点名的**第 4 个产物**「报告 JSON」。

    为什么**不**手写 40 个键的**值**字面量：值写错时红的是 fixture 而不是红线；
    而"按当前输出对齐值字面量"会让用例退化成自证循环。JSON 的字节一致性由四件事决定
    —— 键集合、嵌套、值的渲染、键序；**本版真正会破的是前两件**（wrap serializer 少
    pop 一个键 / 多带一层），键序只可能被「重排模型字段」破坏，本版没人重排。

    - **(a) 键路径全集相等**（:meth:`test_g6_report_json_key_paths_match_v13_exactly`）：
      递归收集全部键路径，与 :data:`V13_REPORT_KEY_PATHS` 比 ``set``。
      ★ **定位：次要守卫**。它随 fixture 变化、**不是模型不变量**（表里写的是
      ``tasks[0]`` 的单任务形态），**主守卫是黄金样本**（G1–G5 的 JSON 列）。
      保留它的唯一理由：它是**唯一能抓到「第四个新键」**的守卫 —— 既有
      ``test_report_json_omits_v131_keys_when_unconfigured`` 只断言三个**已知**新键不在。
    - **(b) 显式默认值 == 黄金样本**
      （:meth:`test_g6_report_json_defaults_are_byte_identical`）：把 ``probe_mode=""`` /
      ``skipped_reason=""`` / ``failure_attribution=None`` / ``skipped_checks=[]``
      **显式写出来**，落盘 JSON 仍须与 v1.3 黄金样本逐字节相同。
      比的是**外部样本**，不是自己跟自己比 —— 上一版正是"自己跟自己比"，构造性恒真（QA P2-1）。
    """

    def test_g6_report_json_key_paths_match_v13_exactly(self) -> None:
        """(a) 正向全集：报告的键路径集合必须**恰好**等于 v1.3 手写表。"""
        actual = _json_key_paths(json.loads(_report_of([_v13_rich_task()]).to_json()))

        leaked = sorted(actual - V13_REPORT_KEY_PATHS)
        assert not leaked, (
            f"G6 × 报告 JSON：出现了 v1.3 没有的键路径（A9 破了 —— 未配置却是新正文）：{leaked}"
        )
        missing = sorted(V13_REPORT_KEY_PATHS - actual)
        assert not missing, (
            f"G6 × 报告 JSON：v1.3 应有的键路径消失了（不是「多带了键」，是「少带了」）：{missing}"
        )

    def test_g6_report_json_defaults_are_byte_identical(self) -> None:
        """(b) 字节级：显式写出全部默认值后，落盘 JSON 仍与 v1.3 黄金样本逐字节相同。"""
        explicit = _contract_json(explicit_defaults=True)

        assert explicit == _golden_bytes("evaluation_report.json"), (
            'G6 × 报告 JSON：probe_mode="" / skipped_reason="" / failure_attribution=None / '
            "skipped_checks=[] 被显式写出时，落盘正文必须与 v1.3 黄金样本逐字节一致"
        )
