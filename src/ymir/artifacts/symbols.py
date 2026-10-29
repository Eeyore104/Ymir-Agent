"""引用感知的「符号删除」检测（v1.3.1 · T3.2a，docs/13 §2-T3.2 / §7.3-4）。

**解决什么问题**：``protected.paths`` 只能逐个列文件，列不全就被绕过 —— 真实事故
（docs/12 §7-L1）里模型把 ``ymir_cli/cli.py`` 的 ``run`` 函数删掉、把子命令搬到
新文件，入口留空壳：``import ymir_cli`` 仍成功，``tests/`` 里的
``from ymir_cli.cli import run`` 却全线 ImportError。逐个列文件既脆弱（新模板要
重新枚举）又漏（模型换个文件删符号就绕开）。本模块提供**不靠白名单**的通用判据。

**判据（误伤面的全部来源，逐条对应 docs/13 §7.3-4 的判定链）**

1. 只看 ``.py``；只认**顶层** ``def`` / ``async def`` / ``class``（嵌套符号、
   模块级变量、``__all__`` 一概不看）；
2. 符号名**不得**以 ``_`` 开头 —— 私有不保护，重构最常动的就是它；
3. ``removed = names(基线) - names(补丁)``；``deleted`` 里的文件视为**全部符号被删**；
4. ``referenced_only=True``（默认）时，只保留**仍被「本补丁未触及的基线文件」引用**
   的符号。引用形态三种（AST 分析，**不用正则猜**）：

   - ``from <module> import <name>``（含 ``as`` 别名）；
   - ``import <module>`` 后 ``<module>.<name>``（含 ``import m as alias`` 后 ``alias.name``）；
   - ``from <module> import *`` ⇒ 该 module 的**全部符号**视为被引用（保守，宁拦勿漏）；

5. 任一文件解析失败（语法错误的旧文件 / 模型写坏的补丁）⇒ **该文件跳过，绝不抛异常**。

**为什么是「收紧而非放松」**：删掉一个没人引用的符号（正常重构）不命中；同步改完
所有引用的重构不命中（引用方也在补丁里 ⇒ 不进引用索引）；删掉仍被引用的公共符号
必命中 —— 这正是事故形态。

**三条纪律**

- **纯函数、零 IO**：只吃 ``Mapping[str, str]``，不读盘、无时钟、无日志；
- **绝不抛异常**：``artifacts.merge.merge_trees`` 的既有纪律是"不因一个坏文件毁掉
  整次生成"，检测失败一律返回空元组（宁可漏拦，不可崩）；
- **不跨层 import**：本模块只依赖标准库（``artifacts``(rank 3) 不得 import
  ``templates``(2)，那是逆层，CI 拦）。

**Known limitations（写清而不假装没有）**

- **相对导入**按「引用文件自身位置 + ``level``」解析成**具体文件路径**（见
  :func:`_resolve_module_paths`），因此 ``from .cli import run`` 只会落到同包的
  ``<pkg>/cli.py``；跨层相对（``from ..pkg.cli import run``）按 level 上溯同样支持，
  但「重命名后再导入」（``import x as y`` 之后用 ``y`` 的间接别名）不覆盖；
- ``import a.b`` 后写 ``a.b.run`` **已识别**（v1.3.1 修复：按**最长前缀**把点链
  解析成模块，见 :func:`_resolve_attribute_imports`）—— 这是一条**收紧方向**的
  行为变更：更多引用被识别 ⇒ 更可能命中（不再是漏拦）；
- ``__init__.py`` 里 ``from .cli import run`` 若 ``__init__.py`` 自身就在补丁里被改
  ⇒ 它算「已触及」，它的引用不参与判定（引用方必须是**未触及的基线文件**）；
- **``import a`` + ``a.b.run()``（未显式 ``import a.b``）不识别**：只写了 ``import a``
  时 ``a.b`` 可能指子模块、也可能指 ``a`` 里的对象属性，AST 层面无法区分；即使
  ``a/__init__.py`` 里写了 ``from . import b`` 也不识别（那需要把 ``__init__`` 的
  再导出链一路展开，成本远高于收益）。方向是**假阴性**（漏拦，安全）——
  刻意不为它加复杂度：真要保护这种形态，用 ``protected.paths`` 显式声明即可。

**v1.3.1 修复：引用索引的键是「文件路径」而不是「模块名字符串」**

旧实现把 ``ymir_cli/cli.py`` 映射成候选模块名 ``{cli, .cli, ymir_cli.cli, …}``，
其中**裸末段** ``cli`` 与**相对形态** ``.cli`` 会把「同名 basename 的不同文件」
压成同一个键 ⇒ 实测假阳性：

- 根级 ``utils.py`` + ``app/utils.py``，``app/__init__.py`` 里
  ``from .utils import get_db``；模型只重构**根级** ``utils.py::get_db``（根级没有
  任何引用方）⇒ 旧实现命中根级 ``utils.py``；
- ``a/cli.py`` 与 ``b/cli.py`` 各有 ``run``，引用方写 ``from cli import run``
  ⇒ 旧实现同时命中两个文件。

★ 这不是"多拦一次"那么轻：``reject`` 会回喂「恢复这些符号或同步改引用」，但模型
**已经把真实引用全改完了**，无从满足 ⇒ 第二次仍 reject ⇒ 第三段 forced ignore
⇒ **模型那处正当重构被静默回滚成基线版**，阶段照常继续。悄悄让模型的正确动作
消失，比拦错更糟。因此引用必须**解析到文件**，不能靠名字片段匹配。
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet  # typing.AbstractSet 已弃用（UP035）
from dataclasses import dataclass

#: 只检测 Python 源码（其它语言的「符号」无统一定义，强行猜会误伤）。
PYTHON_SUFFIX: str = ".py"

#: 星号导入的字面量。
STAR_IMPORT: str = "*"

#: 包入口文件名主干（``pkg/__init__.py`` 的模块名是 ``pkg``，不是 ``pkg.__init__``）。
INIT_STEM: str = "__init__"

#: 私有符号前缀（**不保护**：重构最常动的就是它）。
PRIVATE_PREFIX: str = "_"


@dataclass(frozen=True)
class SymbolRemoval:
    """一个文件里「被删掉、且仍被别处引用」的公共顶层符号（纯数据）。

    Attributes:
        path: 被改 / 被删的文件（相对路径，与 ``patch_manifest`` 同一口径）。
        symbols: 该文件里被删的公共顶层符号名（**排序、去重**）。
    """

    path: str
    symbols: tuple[str, ...]


def detect_symbol_removals(
    base: Mapping[str, str],
    patch: Mapping[str, str],
    deleted: Sequence[str] = (),
    *,
    referenced_only: bool = True,
) -> tuple[SymbolRemoval, ...]:
    """检出「删掉了仍被别处引用的公共顶层符号」的文件（**绝不抛异常**）。

    Args:
        base: 基线文件集（``path -> content``），只读不改。
        patch: 补丁文件集（模型产出）。**内容与基线逐字节相同**的路径不算"触及"
            （见 :func:`_touched_paths` 的说明）。
        deleted: 要从基线删除的路径；其中的 ``.py`` 文件视为"全部符号被删"。
        referenced_only: ``True``（默认）⇒ 只报告仍被未触及基线文件引用的符号；
            ``False`` ⇒ 报告全部删除（调试 / 单测用，勿在生产接线里关掉）。

    Returns:
        按 ``path`` 升序排列的 :class:`SymbolRemoval` 元组；无命中返回 ``()``。

    Note:
        解析失败的文件直接跳过（不抛）；``patch`` / ``deleted`` 里的路径应以
        调用方的归一化结果为准（``merge_trees`` 已归一化），本函数不再做路径校验。
    """
    try:
        return _detect(base, patch, deleted, referenced_only=referenced_only)
    except Exception:  # noqa: BLE001 - 检测失败必须降级为空，不能毁掉整次合并
        return ()


def _detect(
    base: Mapping[str, str],
    patch: Mapping[str, str],
    deleted: Sequence[str],
    *,
    referenced_only: bool,
) -> tuple[SymbolRemoval, ...]:
    """``detect_symbol_removals`` 的实现体（异常由此层之上兜住）。"""
    removed_by_path = _removed_symbols(base, patch, deleted)
    if not removed_by_path:
        return ()
    if not referenced_only:
        return tuple(
            SymbolRemoval(path=path, symbols=removed_by_path[path])
            for path in sorted(removed_by_path)
        )

    direct, star = _reference_index(base, _touched_paths(base, patch, deleted))
    removals: list[SymbolRemoval] = []
    for path in sorted(removed_by_path):
        # ★ 键是**文件路径**：引用索引在记账前就把模块名解析成了"树里真实存在的
        #   那个文件"，因此这里不需要任何名字候选匹配（旧实现的 `_module_candidates`
        #   正是假阳性的来源，v1.3.1 已退休）。
        referenced = set(direct.get(path, frozenset()))
        if path in star:
            # 星号导入 ⇒ 该文件的全部符号都"可能被用到"，保守视为全被引用
            referenced |= set(removed_by_path[path])
        hit = tuple(name for name in removed_by_path[path] if name in referenced)
        if hit:
            removals.append(SymbolRemoval(path=path, symbols=hit))
    return tuple(removals)


def _removed_symbols(
    base: Mapping[str, str],
    patch: Mapping[str, str],
    deleted: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """算出每个 ``.py`` 文件被删掉的公共顶层符号（解析失败的文件跳过）。"""
    deleted_paths = {str(item).strip() for item in deleted if str(item).strip()}
    removed: dict[str, tuple[str, ...]] = {}
    for path in sorted(base):
        if not _is_python(path):
            continue
        base_names = _public_top_level_symbols(base.get(path, ""))
        if base_names is None:
            continue  # 基线里的坏文件：跳过（不猜）
        if path in deleted_paths:
            names = base_names
        elif path in patch:
            patch_names = _public_top_level_symbols(patch.get(path, ""))
            if patch_names is None:
                continue  # 补丁写坏了语法：无法判断删了什么，跳过（不猜）
            names = base_names - patch_names
        else:
            continue
        if names:
            removed[path] = tuple(sorted(names))
    return removed


def _touched_paths(
    base: Mapping[str, str],
    patch: Mapping[str, str],
    deleted: Sequence[str],
) -> frozenset[str]:
    """「本补丁真的动过」的基线路径集合。

    ★ 判据是**内容是否变了**，而不是"``patch`` 里有没有这个键"：模型在 patch 模式下
    经常把整棵树原样回显（含未改动文件），若按"键存在"判定，引用索引会被清空 ⇒
    检测静默失效（漏拦，且没有任何报错）。内容逐字节相同 ⇒ 该文件的引用仍然有效。
    """
    touched = {
        path for path, content in patch.items() if path in base and str(base[path]) != str(content)
    }
    touched |= {str(item).strip() for item in deleted if str(item).strip()}
    return frozenset(touched)


def _reference_index(
    base: Mapping[str, str],
    touched: frozenset[str],
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """构建「未触及基线文件」的引用索引（**键是树里的文件路径**）。

    三种引用形态全部先**解析成目标文件**再记账（``in base`` 才记）：

    1. ``from <module> import <name>`` / ``from .<module> import <name>``
       ⇒ :func:`_resolve_module_paths`；
    2. ``import <dotted>`` （含 ``as``）后 ``<dotted>.<name>``
       ⇒ :func:`_resolve_attribute_imports`（最长前缀解析）；
    3. ``from <module> import *`` ⇒ 记进 ``star``（该文件的符号全视为被引用）。

    Returns:
        ``(direct, star)``：``direct[path]`` = 被引用的符号名集合；
        ``star`` = 被 ``import *`` 命中的文件路径集合。

    ★ 只对 ``base`` 里**真实存在**的路径记账：解析不出来就不记。这是
    假阳性的根治点 —— 名字对不上文件时**宁可不记**（漏拦方向），绝不错记到
    另一个同名文件上（误伤方向）。
    """
    direct: dict[str, set[str]] = {}
    star: set[str] = set()
    known = frozenset(base)
    for path in sorted(base):
        if not _is_python(path) or path in touched:
            continue
        try:
            module = ast.parse(str(base.get(path, "")))
        except (SyntaxError, ValueError):
            continue  # 坏文件跳过（引用可能漏，方向是漏拦而非误伤）

        # ---- 形态①/③：from ... import ...（含相对导入）----
        for node in ast.walk(module):
            if not isinstance(node, ast.ImportFrom):
                continue
            targets = _resolve_module_paths(path, node, known)
            if not targets:
                continue
            for alias in node.names:
                if alias.name == STAR_IMPORT:
                    star.update(targets)
                elif alias.name:
                    for target in targets:
                        direct.setdefault(target, set()).add(alias.name)

        # ---- 形态②：import <dotted> 后 <dotted>.<name> ----
        aliased: dict[str, str] = {}
        dotted: set[str] = set()
        for node in ast.walk(module):
            if not isinstance(node, ast.Import):
                continue
            for alias in node.names:
                if not alias.name:
                    continue
                dotted.add(alias.name)
                if alias.asname:
                    aliased[alias.asname] = alias.name
        if not aliased and not dotted:
            continue
        for node in ast.walk(module):
            if not isinstance(node, ast.Attribute):
                continue
            resolved = _resolve_attribute_imports(node, aliased=aliased, dotted=dotted)
            if resolved is None:
                continue
            target_module, symbol = resolved
            for target in _path_candidates(target_module.split("."), known):
                direct.setdefault(target, set()).add(symbol)
    return {key: frozenset(value) for key, value in direct.items()}, frozenset(star)


def _resolve_module_paths(
    source_path: str,
    node: ast.ImportFrom,
    known: frozenset[str],
) -> tuple[str, ...]:
    """把 ``from ... import ...`` 的模块解析成**树里真实存在的文件路径**。

    解析规则（``level == 0`` 绝对导入 / ``level >= 1`` 相对导入）：

    - 绝对：模块点分名从**树根**解析，``ymir_cli.cli`` → ``ymir_cli/cli.py`` 或
      ``ymir_cli/cli/__init__.py``；``from ymir_cli import run`` 的模块是
      ``ymir_cli`` ⇒ 目标是 ``ymir_cli/__init__.py``（**不**猜 ``ymir_cli/run.py``）；
    - 相对：引用文件所在**包目录** ``P``（``b/__init__.py`` ⇒ ``P = ["b"]``；
      ``b/x.py`` ⇒ ``P = ["b"]``），``level=1`` 用 ``P``，``level=2`` 用 ``P`` 的
      父目录，即 ``P[: len(P) - (level - 1)]``。``b/__init__.py`` 的
      ``from .cli import run`` 因此**只会**落到 ``b/cli.py``（这正是假阳性的修复点）。

    Returns:
        命中 ``known`` 的路径元组（可能为空 / 极少情况下两个都存在）。
    """
    module_parts = [item for item in (node.module or "").split(".") if item]
    if node.level:
        package = _package_dir(source_path)
        drop = node.level - 1
        if drop >= len(package) + 1:
            return ()  # 上溯越界（坏补丁 / 树外引用）：不猜
        base_parts = package[: len(package) - drop] if drop else list(package)
    else:
        base_parts = []
    return _path_candidates([*base_parts, *module_parts], known)


def _resolve_attribute_imports(
    node: ast.Attribute,
    *,
    aliased: Mapping[str, str],
    dotted: AbstractSet[str],
) -> tuple[str, str] | None:
    """把 ``<alias>.<name>`` / ``a.b.<name>`` 解析成 ``(模块点分名, 符号名)``。

    两种写法都要认：

    - ``import a.b as ab`` 后 ``ab.run`` ⇒ 别名直接给出模块 ``a.b``，符号 ``run``；
    - ``import a.b`` 后 ``a.b.run`` ⇒ **最长前缀**解析：先试 ``a.b``（在 ``dotted``
      里）⇒ 模块 ``a.b``、符号 ``run``；试不到再退 ``a``（模块 ``a``、符号 ``b``）。

    ★ 第二条是 v1.3.1 的**行为变更（收紧方向）**：旧实现只认一级属性
    （``alias.name``），``import a.b`` + ``a.b.run`` 这种形态完全漏掉。现在识别出来
    只会**增加**命中可能，不会放松任何既有判据。

    Returns:
        ``(module, symbol)``；无法解析返回 ``None``（宁可不记，绝不猜）。
    """
    chain = _attribute_chain(node)
    if len(chain) < 2:
        return None
    head = chain[0]
    alias_module = aliased.get(head)
    if alias_module is not None:
        return alias_module, chain[1]
    for size in range(len(chain) - 1, 0, -1):
        candidate = ".".join(chain[:size])
        if candidate in dotted:
            return candidate, chain[size]
    return None


def _attribute_chain(node: ast.Attribute) -> tuple[str, ...]:
    """把 ``a.b.c`` 形式的属性链拍平成 ``("a", "b", "c")``（首段非 ``Name`` 则空）。"""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ()
    parts.append(current.id)
    return tuple(reversed(parts))


def _package_dir(path: str) -> tuple[str, ...]:
    """文件所在**包目录**的路径片段（``b/__init__.py`` 与 ``b/x.py`` 都返回 ``("b",)``）。"""
    return tuple(item for item in str(path).split("/")[:-1] if item)


def _path_candidates(parts: Sequence[str], known: frozenset[str]) -> tuple[str, ...]:
    """模块路径片段 → 树里**真实存在**的文件路径（``X.py`` / ``X/__init__.py``）。"""
    modules = [str(item) for item in parts if str(item)]
    if not modules:
        return ()
    stem = "/".join(modules)
    candidates = (f"{stem}{PYTHON_SUFFIX}", f"{stem}/{INIT_STEM}{PYTHON_SUFFIX}")
    return tuple(item for item in candidates if item in known)


def _public_top_level_symbols(source: str) -> frozenset[str] | None:
    """顶层公共符号名（``def`` / ``async def`` / ``class``，名字非 ``_`` 开头）。

    Returns:
        符号名集合；**解析失败返回 ``None``**（与"空文件 ⇒ 空集合"区分开）。
    """
    try:
        module = ast.parse(str(source or ""))
    except (SyntaxError, ValueError):
        return None
    names: set[str] = set()
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = node.name
        if name and not name.startswith(PRIVATE_PREFIX):
            names.add(name)
    return frozenset(names)


def _is_python(path: str) -> bool:
    """是否 Python 源码（只认后缀，不看内容）。"""
    return str(path).endswith(PYTHON_SUFFIX)


__all__ = [
    "INIT_STEM",
    "PRIVATE_PREFIX",
    "PYTHON_SUFFIX",
    "STAR_IMPORT",
    "SymbolRemoval",
    "detect_symbol_removals",
]
