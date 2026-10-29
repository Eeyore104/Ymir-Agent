# v1.3 黄金样本来源

这四个文件**不是**手工写的，也不是当前工作树产出的 —— 它们由 `scripts/gen_v13_golden.py`
在 **v1.3 的 commit** 上生成，用于 A9 红线「未配置 ⇒ 与 v1.3 逐字节一致」的比对。

## 出处

| 项 | 值 |
|---|---|
| commit | `9ec20b96da4574ff3004963bce1793d71163ff81` |
| commit 标题 | docs(readme): 补 v1.3 验收结果与三条能力边界 |
| `ymir.evaluation.report.__file__` | `D:/PyCharm 2026.2.1/project/ymir-agent/.workbuddy/tmp/v13wt/src/ymir/evaluation/report.py` |
| 生成脚本 | `scripts/gen_v13_golden.py`（入库，可复现） |
| 生成时刻 | `2026-01-01T00:00:00+00:00`（钉死常量，非真实时间） |

## 完整命令

```bash
git worktree add .workbuddy/tmp/v13wt 9ec20b96da4574ff3004963bce1793d71163ff81
PYTHONPATH=.workbuddy/tmp/v13wt/src .venv/Scripts/python.exe \
    scripts/gen_v13_golden.py --worktree .workbuddy/tmp/v13wt
git worktree remove .workbuddy/tmp/v13wt
```

## 输入

- `patch_manifest.json`：`merge_trees(BASE, PLAIN_PATCH, mode="patch", base_paths=BASE)`
  - `BASE` = `app/main.py`（`def main() -> None`）+ `tests/test_main.py`（引用 `main`）
  - `PLAIN_PATCH` = `app/main.py` 改为 `def main() -> int`
- `smoke_report.json`：`RoleStageExecutor._run_sandbox_smoke`，一条命令
  `python -m pytest -q`，脚本化假会话（`exit_code=0` / `stdout="2 passed\n"` / `duration_ms=1`）
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
