"""``common`` 下沉收口测试（v1.2 r4 架构收口）。

背景：``normalize_rel_path`` 与密钥指纹原本分别被困在 ``artifacts``(rank 3) 与
``templates``(rank 2) 里，导致 T3.1 推送过滤（``artifacts/exporter.py``）无法合法
复用。两者下沉到 ``common``(rank 5) 后：

- **单一真相**：``templates.models.SECRET_FINGERPRINTS`` 与
  ``common.secrets.SECRET_FINGERPRINTS`` 必须是**同一对象**（不是"内容相等"）；
- **re-export 未走样**：``artifacts.versioning.normalize_rel_path`` 与
  ``common.paths.normalize_rel_path`` 必须是同一对象（既有 439 条用例依赖旧路径）；
- **防复发**：``src/ymir/templates/**`` 不得再出现任何 ``ymir.artifacts`` 的 import。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ymir.common.secrets as common_secrets
import ymir.templates.models as templates_models
from ymir.artifacts.versioning import normalize_rel_path as versioning_normalize
from ymir.common.errors import InvalidParamError
from ymir.common.paths import normalize_rel_path
from ymir.common.secrets import SECRET_FINGERPRINTS, scan_secret

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "ymir"


# ------------------------------------------------------------------ 单一真相断言


def test_secret_fingerprints_single_source_of_truth() -> None:
    """密钥指纹只有一份：``templates.models`` 是 ``common.secrets`` 的同一对象。"""
    assert templates_models.SECRET_FINGERPRINTS is common_secrets.SECRET_FINGERPRINTS
    assert templates_models.SECRET_FINGERPRINTS is SECRET_FINGERPRINTS
    assert len(SECRET_FINGERPRINTS) == 5


def test_normalize_rel_path_reexport_is_same_object() -> None:
    """``artifacts.versioning`` 的 re-export 未走样（同一对象 + 行为不变）。"""
    assert versioning_normalize is normalize_rel_path
    assert normalize_rel_path("./a//b/./c.py") == "a/b/c.py"
    with pytest.raises(InvalidParamError):
        versioning_normalize("../escape.py")


# ----------------------------------------------------------------- scan_secret 三例


def test_scan_secret_detects_aws_key_returns_rule_only() -> None:
    """命中 AWS Access Key：返回**规则字符串**，绝不返回密钥正文。"""
    rule = scan_secret('aws_access_key_id = "AKIAIOSFODNN7EXAMPLE"\n')
    assert rule is not None
    assert rule == SECRET_FINGERPRINTS[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in rule


def test_scan_secret_detects_openai_style_key() -> None:
    """命中 ``sk-`` 样本：返回规则而非匹配文本（防日志二次泄露）。"""
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0kLmN"
    rule = scan_secret(f"OPENAI_API_KEY={secret}\n")
    assert rule is not None
    assert rule.startswith("sk-")
    assert secret not in rule


def test_scan_secret_clean_text_returns_none() -> None:
    """干净文本（含 ``.env.example`` 形态的空值）→ ``None``。"""
    assert scan_secret("APP_ENV=dev\nDATABASE_URL=\n") is None
    assert scan_secret("") is None


# --------------------------------------------------------------- 防复发：分层断言


def test_templates_package_has_zero_artifacts_imports() -> None:
    """``src/ymir/templates/**/*.py`` 不含任何 ``ymir.artifacts`` 的 import。

    这是"templates(2) 不得依赖 artifacts(3)"的 AST 级防复发条款：
    ``scripts/check_import_graph.py`` 只按 rank 判定（2 -> 3 恰好合法），
    无法表达"这条边在架构上不允许"，因此单列一条静态断言。
    """
    offenders: list[str] = []
    for path in sorted((SRC_ROOT / "templates").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for module in modules:
                if module == "ymir.artifacts" or module.startswith("ymir.artifacts."):
                    offenders.append(f"{path.relative_to(SRC_ROOT).as_posix()}:{node.lineno}")
    assert offenders == [], f"templates 包出现 artifacts 依赖: {offenders}"


def test_common_paths_is_only_path_validator() -> None:
    """路径安全实现只有一处：``common.paths`` 之外不得再出现盘符正则定义。"""
    pattern = "WINDOWS_DRIVE_RE"
    hits = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts and pattern in path.read_text(encoding="utf-8")
    ]
    assert hits == ["common/paths.py"]


def test_no_duplicate_definitions_in_src() -> None:
    """源码里不得有**同一作用域内的重名定义**（QA-复验发现的静默覆盖类缺陷）。

    ``ruff`` 不报这类问题：后一份定义会静默覆盖前一份，前一份里的自递归/死代码
    永远跑不到，直到有人删掉后一份才炸。本文件用 AST 做全局兜底（含类内方法）。
    实际案例：``artifacts/export_service.py::_resolve_artifact`` 曾定义两遍，
    第一份结尾是无限自递归。
    """
    import ast
    from collections import Counter

    duplicates: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.Module, ast.ClassDef)):
                continue
            names = Counter(
                child.name
                for child in scope.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            )
            for name, count in names.items():
                if count > 1:
                    duplicates.append(
                        f"{path.relative_to(SRC_ROOT).as_posix()}:"
                        f"{getattr(scope, 'name', '<module>')}::{name} x{count}"
                    )
    assert duplicates == [], f"同一作用域出现重名定义（后者静默覆盖前者）: {duplicates}"
