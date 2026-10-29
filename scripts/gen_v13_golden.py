#!/usr/bin/env python
"""生成 **v1.3 黄金样本**（``tests/fixtures/v13_golden/``），供 A9 红线做逐字节比对。

★★★ **本脚本只能在 v1.3 的 worktree 里跑，不能对当前工作树跑** ★★★

对当前工作树重跑本脚本会得到 **v1.3.1** 的输出，那样样本就退化成自证 ——
"当前代码等于当前代码"，永远为真、永远抓不到回归。样本的价值完全来自
「它出自一件**没有** v1.3.1 那四个新能力的代码」，所以只能用 v1.3 的 commit 生成。

正确用法（仓库根目录）::

    git worktree add .workbuddy/tmp/v13wt 9ec20b9
    PYTHONPATH=.workbuddy/tmp/v13wt/src .venv/Scripts/python.exe \\
        scripts/gen_v13_golden.py --worktree .workbuddy/tmp/v13wt
    git worktree remove .workbuddy/tmp/v13wt

脚本自带四重护栏（任一不满足即中止，**不写任何文件**）：

1. ``ymir.evaluation.report`` 的实际来源文件不得位于本仓库的 ``src/`` 下
   （editable install 抢先时最容易踩这个坑）；
2. ``report`` 模块必须落在 ``--worktree`` 指定的目录下；
3. ``git -C <worktree> status --porcelain`` 必须**为空**。脏工作树会产出"被改过的
   v1.3"，而本脚本写出的 ``PROVENANCE.md`` 仍会声称「未做任何编辑」—— 那是
   **假 provenance**（QA P3-8 实测：改一行 markdown 标题即可静默写出 1255 字节的脏样本）；
4. 三个 v1.3.1 字段必须**不存在**于 v1.3 的模型里
   （``TaskOut.failure_attribution`` / ``BaselineProbeOut.probe_mode`` /
   ``BaselineCheckOut.skipped_reason``）—— 只要有一个存在，说明跑的不是 v1.3。

此外还会打印 ``report.__file__`` 与 ``git rev-parse HEAD``，供人工留证（也要贴进报告）。

四个产物与 A9 原文一一对应：``patch_manifest.json``、``evaluation_report.json``、
``evaluation_report.md``、``smoke_report.json``。

**不可比字段**：``executed_at``（每次取当前时刻）与 ``generated_at`` 由本脚本钉成常量后
再落盘 —— 否则样本每天都不一样。除这两个字段外，其余字节原样来自 v1.3 的产出函数。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

#: 钉死的"当前时刻"（冒烟报告 / 评测报告各一处不可比字段）。
FIXED_NOW: str = "2026-01-01T00:00:00+00:00"

#: 黄金样本的输入：一个 `patch` 模式的普通补丁（无符号回归）。
BASE: dict[str, str] = {
    "app/main.py": "def main() -> None:\n    return None\n",
    "tests/test_main.py": "from app.main import main\n\n\ndef test_main() -> None:\n    main()\n",
}
PLAIN_PATCH: dict[str, str] = {"app/main.py": "def main() -> int:\n    return 1\n"}

#: 冒烟命令与固定输出（假会话按此返回 ⇒ 条目内容完全确定）。
SMOKE_COMMAND: str = "python -m pytest -q"
SMOKE_STDOUT: str = "2 passed\n"

#: 报告权重（与 v1.3 既有用例同值）。
WEIGHTS: dict[str, float] = {
    "requirement_conformance": 0.3,
    "runnability": 0.3,
    "convention": 0.2,
    "architecture": 0.2,
}


def _abort(message: str) -> None:
    print(f"[ABORT] {message}", file=sys.stderr)
    raise SystemExit(1)


def _assert_running_on_v13(worktree: Path) -> None:
    """三重护栏：确认当前 import 到的确实是 worktree 里的 v1.3。"""
    import ymir
    import ymir.evaluation.report as report_module
    from ymir.evaluation.report import BaselineCheckOut, BaselineProbeOut, TaskOut

    report_file = Path(report_module.__file__).resolve()
    print(f"ymir.evaluation.report.__file__ = {report_file}")
    print(f"ymir.__file__                      = {Path(ymir.__file__).resolve()}")

    repo_src = (Path(__file__).resolve().parent.parent / "src").resolve()
    if repo_src in report_file.parents:
        _abort(
            f"import 到的是本仓库工作树的 src（{report_file}），不是 v1.3 worktree。\n"
            "        用 PYTHONPATH=<worktree>/src 前缀跑，别直接 uv run。"
        )
    if worktree.resolve() not in report_file.parents:
        _abort(f"report 模块不在指定的 worktree {worktree} 下：{report_file}")

    # 护栏 3：worktree 必须**干净**。脏工作树会产出"被改过的 v1.3"，
    # 而 PROVENANCE.md 仍会写「其余字节原样来自 v1.3 产出函数，未做任何编辑」——
    # 那就是**假 provenance**（QA P3-8 实测：改一行 markdown 标题即可静默写出脏样本）。
    dirty = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        preview = "\n".join(f"          {line}" for line in dirty.splitlines()[:10])
        _abort(
            f"worktree 不干净（{len(dirty.splitlines())} 处改动），样本可能是被改过的：\n"
            f"{preview}\n"
            "        先 `git -C <worktree> status` 看一眼，清理干净（checkout/stash）再生成。"
        )

    for model, field in (
        (TaskOut, "failure_attribution"),
        (BaselineProbeOut, "probe_mode"),
        (BaselineCheckOut, "skipped_reason"),
    ):
        if field in model.model_fields:
            _abort(
                f"{model.__name__}.{field} 存在 ⇒ 这不是 v1.3（v1.3.1 才有该字段）。"
                "样本会退化成自证。"
            )

    sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"git -C {worktree} rev-parse HEAD  = {sha}")


class _ScriptedSession:
    """只用于冒烟的最小假会话：一切命令 exit=0、``duration_ms=1``。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    @property
    def sandbox_id(self) -> str | None:
        return "v13-golden"

    @property
    def network_policy(self) -> Any:
        from ymir.sandbox.base import NetworkPolicy

        return NetworkPolicy.NONE

    async def exec(self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None) -> Any:
        from ymir.sandbox.base import ExecResult

        return ExecResult(stdout=SMOKE_STDOUT, stderr="", exit_code=0, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: Any) -> None:
        return None


class _FakeSandboxService:
    """冒烟只用到 ``sandbox`` / ``start`` / ``destroy`` 三件事。"""

    def __init__(self, session: _ScriptedSession) -> None:
        self.sandbox = SimpleNamespace(backend="fake")
        self.session = session

    async def start(self, run_id: str, *, network: Any = None) -> Any:
        return SimpleNamespace(run_id=run_id, network=network, session=self.session)

    async def destroy(self, managed: Any) -> None:
        return None


async def _smoke_report_bytes() -> str:
    """v1.3 的 ``smoke_report.json`` 正文（钉住 ``executed_at`` 后落盘口径）。"""
    from ymir.orchestrator.role_executor import RoleStageExecutor
    from ymir.roles.io import StageOutput
    from ymir.sandbox.base import NetworkPolicy

    executor = RoleStageExecutor.__new__(RoleStageExecutor)
    executor._sandbox_service = _FakeSandboxService(_ScriptedSession())
    executor._sandbox_network = NetworkPolicy.NONE
    executor._exec_timeout = 120.0
    output = StageOutput(
        files={},
        summary="ok",
        structured={"files": [], "run_commands": [SMOKE_COMMAND]},
    )
    report = await executor._run_sandbox_smoke(  # noqa: SLF001
        SimpleNamespace(id="run-v13-golden", context={}), output, files=dict(BASE)
    )
    report["executed_at"] = FIXED_NOW  # 唯一不可比字段
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def _patch_manifest_bytes() -> str:
    """v1.3 的 ``patch_manifest.json`` 正文。"""
    from ymir.artifacts.merge import merge_trees

    merged = merge_trees(BASE, PLAIN_PATCH, mode="patch", base_paths=frozenset(BASE))
    return merged.manifest_json()


def _evaluation_report_files() -> dict[str, str]:
    """v1.3 的评测报告两件套（markdown + JSON，均含落盘口径的换行）。"""
    from ymir.evaluation.report import (
        BaselineCheckOut,
        BaselineProbeOut,
        DimensionOut,
        EvaluationReport,
        EvidenceOut,
        TaskOut,
    )
    from ymir.evaluation.suite import Thresholds

    tasks = [
        # ① 满配：维度 + 证据 + 基线探针 + 告警（覆盖 v1.3 全部渲染分支）
        TaskOut(
            id="t-eval-rich",
            scenario="s",
            project_id="p",
            run_id="r",
            requirement="加一个导出接口",
            artifact_ref={"artifact_id": "a1"},
            dimensions=[
                DimensionOut(
                    name="runnability",
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
                checks=[BaselineCheckOut(command=SMOKE_COMMAND, exit_code=0, output="2 passed")],
                reason="",
                duration_ms=12,
                cached=False,
            ),
        ),
        # ② 最简：无探针、无维度（覆盖"无基线检查行"的分支）
        TaskOut(
            id="t-eval-plain",
            requirement="普通任务",
            total_score=60.0,
            runnability=50.0,
            passed=False,
        ),
        # ③ 未执行：覆盖未执行行与"已执行/未执行"计数行
        TaskOut(
            id="t-skipped",
            status="skipped",
            skip_reason="未绑定产物",
            project_id="p",
            artifact_ref={"artifact_id": None},
            thresholds={"total": 75.0, "runnability": 60.0},
        ),
    ]
    report = EvaluationReport.build(
        suite_id="default",
        suite_version="1.0",
        project_id="p",
        weights=dict(WEIGHTS),
        thresholds=Thresholds(total=75.0, runnability=60.0),
        tasks=tasks,
        wall_time_ms=1234,
    )
    report.generated_at = FIXED_NOW  # 唯一不可比字段
    return report.files()


_PROVENANCE_TEMPLATE = """# v1.3 黄金样本来源

这四个文件**不是**手工写的，也不是当前工作树产出的 —— 它们由 `scripts/gen_v13_golden.py`
在 **v1.3 的 commit** 上生成，用于 A9 红线「未配置 ⇒ 与 v1.3 逐字节一致」的比对。

## 出处

| 项 | 值 |
|---|---|
| commit | `{sha}` |
| commit 标题 | {subject} |
| `ymir.evaluation.report.__file__` | `{report_file}` |
| 生成脚本 | `scripts/gen_v13_golden.py`（入库，可复现） |
| 生成时刻 | `{generated_at}`（钉死常量，非真实时间） |

## 完整命令

```bash
git worktree add .workbuddy/tmp/v13wt {sha}
PYTHONPATH=.workbuddy/tmp/v13wt/src .venv/Scripts/python.exe \\
    scripts/gen_v13_golden.py --worktree .workbuddy/tmp/v13wt
git worktree remove .workbuddy/tmp/v13wt
```

## 输入

- `patch_manifest.json`：`merge_trees(BASE, PLAIN_PATCH, mode="patch", base_paths=BASE)`
  - `BASE` = `app/main.py`（`def main() -> None`）+ `tests/test_main.py`（引用 `main`）
  - `PLAIN_PATCH` = `app/main.py` 改为 `def main() -> int`
- `smoke_report.json`：`RoleStageExecutor._run_sandbox_smoke`，一条命令
  `python -m pytest -q`，脚本化假会话（`exit_code=0` / `stdout="2 passed\\n"` / `duration_ms=1`）
- `evaluation_report.md` / `.json`：`EvaluationReport.build(...)`，三个任务
  ① 满配（维度 + 证据 + 基线探针 + 告警）② 最简（无探针无维度）③ `skipped`（未绑定产物）

## 不可比字段（已在生成时钉死，故样本里是常量）

| 字段 | 为什么不可比 |
|---|---|
| `smoke_report.json.executed_at` | `datetime.now(UTC).isoformat()`，每次不同 |
| `evaluation_report.*.generated_at` | 同上 |

除这两个字段，其余字节**原样**来自 v1.3 的产出函数，未做任何编辑。

## ⚠️ 护栏

对**当前工作树**重跑 `scripts/gen_v13_golden.py` 会得到 **v1.3.1** 的输出，
样本立刻退化成自证（"当前代码等于当前代码"）。脚本内置三重中止条件：

1. `ymir.evaluation.report` 不得来自本仓库 `src/`；
2. `TaskOut.failure_attribution` / `BaselineProbeOut.probe_mode` /
   `BaselineCheckOut.skipped_reason` 三个字段必须**都不存在**；
3. `ymir.evaluation.report.__file__` 必须落在 `--worktree` 指定的目录下。

要重新生成（例如 v1.3 有了新 commit），必须重新开 worktree，不要就地跑。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 v1.3 黄金样本")
    parser.add_argument(
        "--worktree",
        default=".workbuddy/tmp/v13wt",
        help="v1.3 的 worktree 路径（默认 .workbuddy/tmp/v13wt）",
    )
    parser.add_argument(
        "--out",
        default="tests/fixtures/v13_golden",
        help="黄金样本输出目录（默认 tests/fixtures/v13_golden）",
    )
    args = parser.parse_args()

    worktree = Path(args.worktree).resolve()
    if not worktree.is_dir():
        _abort(f"worktree 不存在：{worktree}（先 git worktree add）")

    _assert_running_on_v13(worktree)

    # 默认值自检：v1.3 里这些字段不存在，构造时也就不会有它们。
    import asyncio

    import ymir.evaluation.report as report_module

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    artifacts: dict[str, str] = {
        "patch_manifest.json": _patch_manifest_bytes(),
        "smoke_report.json": asyncio.run(_smoke_report_bytes()),
    }
    artifacts.update(_evaluation_report_files())

    for name, text in artifacts.items():
        path = out_dir / name
        # 用 bytes 写：不经平台换行转换，"逐字节"才可比（同 tests/test_prompts.py:108 纪律）
        path.write_bytes(text.encode("utf-8"))
        print(f"[WRITE] {path}  ({len(text.encode('utf-8'))} bytes)")

    sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subject = subprocess.run(
        ["git", "-C", str(worktree), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    provenance = _PROVENANCE_TEMPLATE.format(
        sha=sha,
        subject=subject,
        report_file=Path(report_module.__file__).resolve().as_posix(),
        generated_at=FIXED_NOW,
    )
    (out_dir / "PROVENANCE.md").write_bytes(provenance.encode("utf-8"))
    print(f"[WRITE] {out_dir / 'PROVENANCE.md'}")


if __name__ == "__main__":
    main()
