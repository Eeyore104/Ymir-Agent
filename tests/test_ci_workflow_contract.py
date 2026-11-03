"""CI 工作流契约：沙箱镜像元数据步骤必须对 ``Labels`` 判空。

背景（2026-11-03）：该步骤原写 ``{{index .Config.Labels "..."}}``，
而镜像**完全没有 Labels 键**时 docker 的 Go 模板直接报
``map has no entry for key "Labels"`` ⇒ 步骤 exit 1 ⇒ nightly 自 v1.3 起长期红
（run 41431980817，build 步骤是 success，只有这一步挂）。
``{{if .Config.Labels}}`` 救不了 —— 访问即失败，必须先 ``index .Config "Labels"``
判空（缺键返回零值，不报错）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = ROOT / "docker" / "sandbox.Dockerfile"


def _inspect_step_run() -> str:
    data = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = data["jobs"]["docker-build"]["steps"]
    runs = [s["run"] for s in steps if s.get("name") == "Inspect image metadata"]
    assert runs, "ci.yml 里找不到 'Inspect image metadata' 步骤（改名请同步本用例）"
    return runs[0]


def test_inspect_step_guards_missing_labels_key() -> None:
    run = _inspect_step_run()
    assert 'index .Config "Labels"' in run, '判空必须走 index .Config "Labels"'
    assert ".Config.Labels" not in run, (
        "镜像没有 Labels 键时 `.Config.Labels` 会 template parsing error（不判空时不要用）"
    )


def test_sandbox_image_declares_base_name_label() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "org.opencontainers.image.base.name" in text, (
        "沙箱镜像需声明 base.name 标签，否则 CI 输出的 base= 恒为 unspecified"
    )
