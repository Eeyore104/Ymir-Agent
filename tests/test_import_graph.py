"""依赖护栏定点测试（ARCH-15）：复用 ``scripts/check_import_graph.py`` 的实现。

在 pytest 内执行与 CI 同一套静态检查，保证「分层方向 + 循环依赖」在本机
``uv run pytest`` 即可拦截（CI 另有独立步骤）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_import_graph.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_import_graph", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 注册到 sys.modules：dataclass（含 future annotations）解析注解时按
    # ``cls.__module__`` 反查模块，缺失会抛 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_import_graph_has_no_violations_or_cycles() -> None:
    """src/ymir 无分层违规、无子包级循环依赖。"""
    module = _load_module()
    violations, cycles = module.analyze()
    assert violations == [], f"分层违规: {violations}"
    assert cycles == [], f"循环依赖: {cycles}"


def test_import_graph_detects_injected_violation(tmp_path: Path) -> None:
    """护栏非形同虚设：注入一条逆层 import 应被检出。"""
    module = _load_module()
    fake_root = tmp_path / "ymir"
    (fake_root / "api").mkdir(parents=True)
    (fake_root / "models").mkdir(parents=True)
    (fake_root / "__init__.py").write_text("", encoding="utf-8")
    (fake_root / "api" / "__init__.py").write_text("", encoding="utf-8")
    (fake_root / "models" / "__init__.py").write_text("", encoding="utf-8")
    (fake_root / "models" / "leaky.py").write_text(
        "from ymir.api import router\n", encoding="utf-8"
    )
    # 反向的合法边（api -> models）与之构成子包级环，验证环检测
    (fake_root / "api" / "uses_models.py").write_text(
        "from ymir.models import leaky\n", encoding="utf-8"
    )
    violations, cycles = module.analyze(fake_root)
    assert any(v.src == "models" and v.dst == "api" for v in violations)
    assert cycles, "逆层边应同时构成环"


def test_import_graph_detects_relative_import_violation(tmp_path: Path) -> None:
    """相对导入的逆层违规也必须被检出（QA-15-1）。"""
    module = _load_module()
    fake_root = tmp_path / "ymir"
    (fake_root / "tools").mkdir(parents=True)
    (fake_root / "roles").mkdir(parents=True)
    (fake_root / "__init__.py").write_text("", encoding="utf-8")
    (fake_root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (fake_root / "roles" / "__init__.py").write_text("", encoding="utf-8")
    # tools(rank 3) 用相对导入依赖 roles(rank 2)：逆层，应被检出
    (fake_root / "tools" / "y.py").write_text(
        "from ..roles import schemas\n", encoding="utf-8"
    )
    violations, _cycles = module.analyze(fake_root)
    assert any(v.src == "tools" and v.dst == "roles" for v in violations), violations
