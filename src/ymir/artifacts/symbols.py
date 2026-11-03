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

**v1.4 追加：符号级「回退」（``revert``，docs/15 §3.1）**

:func:`revert_symbols` 是本模块的第二个纯函数：给定基线 / 补丁 / 引用感知的删除清单，
把**被删的公共顶层符号的源码**从基线搬回补丁文件，**保留模型对同文件其它部分的编辑**。

为什么需要它：v1.3.1 的三段式兜底把 ``symbol_deletions`` 降级成 ``ignore``，而
``ignore`` 的语义是「丢弃模型内容、保留基线版本」⇒ **整文件回退**。模型若在同一个
``.py`` 里既做了正当重构、又误删了一个仍被引用的公共符号，兜底会把那处重构一起吃掉
（v1.3.1 真跑 R-b / R-c1 / R-c2 三个 run 全部命中，docs/14 §3.2-★2）。回退粒度必须
是**符号**而不是文件。

边界（写清而不假装没有）：

- 还原「定义节点」本身（``def`` / ``async def`` / ``class``，含其装饰器行），
  并按**同文件依赖闭包**连带还原它引用的其它顶层 ``def`` / ``class``（v1.4 返修 3，
  见下一节）；模块级**赋值**、``import`` 别名**不**还原；
- **只处理补丁里仍有正文的文件**：文件被整份删除时没有正文可改，取消删除即可
  （由 ``merge_trees`` 的删除通道负责，见 ``merge.py`` 的 ``revert`` 分支）；
- 插回位置**按基线顶层定义顺序锚定**（v1.4 返修，见下一节）；
- 补丁侧已有同名顶层定义 / 赋值 ⇒ **不插入**（不重复定义）；
- 沿用三条纪律：纯函数、零 IO、绝不抛异常。

**插回位置：为什么不再「一律追加到文件末尾」（v1.4 QA 返修 P1-2）**

v1.4 首版把还原的定义**固定追加到文件末尾**，实测三种比「整文件回退」（v1.3.1）更糟
的退化 —— 还原"成功"了，树却仍是坏的：

1. 基线以 ``if __name__ == "__main__": main()`` 结尾、被还原的 ``main`` 被它调用
   ⇒ 追加到末尾后 ``NameError: name 'run' is not defined``；
2. ``@app.get("/tasks")`` 装饰的路由函数被还原到文件末尾，而文件末尾的
   ``app.include_router(...)`` **早已执行完** ⇒ 路由**静默丢失**（``AssertionError:
   路由缺失``）。真实产出的 ``app/main.py`` 正是这个形态；
3. 基线 ``__all__`` 也删了该符号 ⇒ 只搬 ``def`` 不补 ``__all__`` ⇒
   ``from mod import *`` 仍然失败，而短码却记 ``reverted:1``（**假证据**）。

锚定规则（逐个缺失符号求「插入到补丁的第几行」，行号 0 基）：

1. 在基线顶层 body 里找该符号**前面最近的定义**，若它在**补丁里仍以「同名 + 同节点
   类型」存在** ⇒ 插到它在补丁里的 ``end_lineno`` 之后；
2. 前面没有存活定义 ⇒ 找**后面最近的存活定义**，插到它在补丁里的**起始行之前**
   （起始行含装饰器首行，否则 ``@app.get`` 会与定义分家）；
3. 前后都没有 ⇒ 插到补丁里第一个「**模块级副作用语句**」之前
   （``Expr(Call)`` / ``If`` / ``With`` / ``For`` / ``Try`` / ``While`` / ``Match``；
   **右值含 ``ast.Call`` 的赋值 / ``AnnAssign``** 也算 —— ``VALUE = new_anchor()`` /
   ``ROUTES = build()`` 会在定义处立即求值调用；``Import`` / 纯常量赋值 / 文档字符串
   **不算**）—— 插入点因此必然落在 ``app = FastAPI()`` 之后、``include_router(...)``
   与 ``if __name__ == "__main__":`` 之前；
4. 连副作用语句都没有（纯定义文件）⇒ 退回追加到文件末尾。

**P1-B（第二次返修）：后锚不得跨过模块级副作用，装饰器依赖绑定必须已就位**

规则 2 首版直接 ``return spans[key][0] - 1``，没有与规则 3 的副作用边界取 min。缺失符号
前面的定义被改名 / 删除时，插入点会落到 ``app.include_router(...)`` / ``VALUE =
new_anchor()`` / 模块级 ``assert`` **之后**：装饰器注册晚于 ``include_router`` ⇒ 路由
静默丢失；模块级赋值先调用被还原的 ``helper`` ⇒ ``NameError``（短码却仍记
``reverted:1``，假证据）。因此三条一起修：

1. **任何**插入点都不得晚于第一个模块级副作用（与候选锚点取 ``min``）；
2. 副作用集合加入 **右值含 ``ast.Call`` 的 ``Assign`` / ``AnnAssign``**；
3. 但「**装饰器依赖绑定**」不算边界：``app = FastAPI()`` 绑定的名字出现在被还原定义
   的装饰器里，插入点必须落在它**之后**（否则 ``@app.get`` 先执行会 ``NameError``）。
   实现：先收集被还原定义的装饰器 / 默认值 / 基类里 ``Name(Load)`` 的名字集合，
   跳过赋值目标与之相交的语句继续往后找边界；最后再取
   ``max(候选点, 依赖绑定的结束行)``。

多个符号**按基线出现顺序**插入，且**从后往前**改行号（先插后面的锚点，前面的行号
才不会被挤歪），因此「同一输入 ⇒ 逐字节同一输出」的契约不变；符号已经回来 ⇒
无缺失 ⇒ 不插入（幂等）。

**``__all__`` 也要一起补回**：基线 ``__all__`` 里列了该符号、且补丁里仍有
``__all__ = [...]`` **字面量列表**时，把名字按基线顺序插回该列表（AST 定位，保留原有
的引号 / 缩进 / 单行或多行风格）。不补这一步，``from mod import *`` 仍会失败，
而短码已经记了 ``reverted:1`` —— 那是假证据。

**返修 3（v1.4 真跑暴露）：同文件依赖闭包 —— 只搬回被删的那个符号还不够**

v1.4 真跑把"不追依赖"这条假设打穿了。两例（``data/v14/INDEX.md`` §3.4 / §5.1）：

- R-a（run ``29f52b8f``）：还原 ``create_app`` 后，它引用的 ``lifespan`` **也被模型
  一并删了** ⇒ 合并树 ``ImportError`` / ``NameError``；
- R-c（run ``c5174125``）：还原 ``render_stats`` 后，它引用的 ``StatsResult``（class）
  与 ``_sorted_tags``（def）也被删 ⇒ ``import`` 即 ``NameError``，pytest 收集期 3 errors。

⇒ "依赖通常还在"是错的。被删符号的依赖必须**一起还原**，否则还原是**假证据**
（短码记 ``symbol_regression_reverted:1``，树却仍然起不来）。

实现（:func:`_dependency_closure`，**有界闭包 BFS**，不是全量依赖分析）：

1. 种子 = 本文件本次要还原的符号（即"仍被外部引用"的 ``missing``）；
2. 展开：在**基线顶层**找同名 ``def`` / ``async def`` / ``class``，收集它的全部
   ``Name(Load)`` —— 函数体、装饰器、参数默认值、注解、基类、类关键字都覆盖
   （``ast.walk`` 整棵定义子树，天然含这些位置）；
3. 过滤（四种情况**不**加入）：补丁里已有顶层绑定（``def``/``class``/``Assign``/
   ``AnnAssign``/``import``）、基线里是 ``import`` 别名、内建名、基线顶层不是
   ``def``/``class``（赋值 / 工厂实例 — 见已知限制）；
4. 收敛：加入集合后继续展开（BFS），**上限 64 个符号**（``_MAX_CLOSURE_SYMBOLS``）；
   触顶即停止展开，已收集的照旧还原 —— 目的是防止一次误删把半个模块搬回来；
5. 插回顺序与 ``__all__`` 口径不变：blocks 仍按**基线 body 顺序**生成；
   ``__all__`` 仍只补**原始 missing**（私有依赖``_sorted_tags`` 之类**不得**进导出面）。

已知限制（有界闭包的代价，写清而不假装没有）：

- **不做作用域分析**：局部同名（如推导式变量 ``[x for x in items]`` 里的 ``x``）若
  恰好与某个基线顶层 ``def`` 同名，会带来**少量多余还原**（多搬一个用不上的定义）；
- **不还原模块级赋值**（``CONST = ...`` / ``ENGINE = create_engine()``）与
  ``import`` 别名：后者不是"被删符号"，前者只是数据、且本版没有"赋值节点"的插回
  锚定规则。R-a 的 ``CORSMiddleware`` / ``tasks_router`` 就是这一类 —— 按设计**不**
  还原，因此 revert 只保证 ``def`` / ``class`` 依赖闭合，**不保证整棵树可运行**；
- 上限截断后可能仍有缺口（闭包不完整），此时短码仍按命中路径数记 —— 与"降级不谎报
  成功"的既有口径一致（见 ``merge.py`` 的 ``revert`` 分支注释）。

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
import builtins
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


# --------------------------------------------------- 符号级回退（v1.4 T1.1）


#: 可被「搬回」的定义节点类型（与 :func:`_public_top_level_symbols` 同口径）。
_DEFINITION_NODES: tuple[type[ast.stmt], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)

#: 插回时相邻顶层定义之间的空行（PEP 8：顶层定义之间两个空行 ⇒ 3 个换行符）。
_SYMBOL_SEPARATOR: str = "\n\n\n"

#: 「模块级副作用语句」：执行顺序敏感，插回的定义必须落在它们**之前**
#: （装饰器 ``@app.get`` 只有在 ``include_router`` 之前执行才会被注册进路由表）。
#:
#: ★ ``Import`` / **纯常量赋值** / 文档字符串（``Expr(Constant)``）**不在**此列：它们
#: 没有"注册 / 副作用"语义，插在它们后面是安全的。**右值含 ``ast.Call`` 的赋值**
#: （``VALUE = new_anchor()`` / ``ROUTES = build()``）算副作用 —— 它在定义处立即求值
#: 调用，插在它之后就等于让调用发生在被还原的 ``helper`` 定义之前（``NameError``）。
#: 见 :func:`_first_side_effect_line` / :func:`_is_call_assignment`。
_SIDE_EFFECT_NODES: tuple[type[ast.stmt], ...] = (
    ast.If,
    ast.With,
    ast.AsyncWith,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.TryStar,
    ast.Match,
)

#: ``__all__`` 的字面量名（星号导入的"白名单"，还原符号时必须同步补回）。
_DUNDER_ALL: str = "__all__"

#: 依赖闭包的单文件符号上限（返修 3）。
#:
#: 有界而非全量：删除一个"枢纽"符号时，无界 BFS 会把半个模块搬回来（把模型的重构
#: 大面积吃掉，比 `ignore` 还糟）；64 足够覆盖真实模块的"一棵依赖子树"，触顶后停止
#: 展开但保留已收集项（宁可少还原，不可失控扩面）。
_MAX_CLOSURE_SYMBOLS: int = 64

#: 内建名集合（依赖闭包的过滤项之一：`len` / `dict` 之类没有可还原的定义）。
_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))

#: 一对**单引号 / 双引号**（渲染 ``__all__`` 时沿用原有风格）。
_QUOTES: tuple[str, str] = ('"', "'")


@dataclass(frozen=True)
class _DefinitionBlock:
    """一个待插回的定义：它在**基线顶层 body** 里的下标 + 原始源码片段（纯数据）。

    Attributes:
        base_index: 该定义在 ``ast.Module.body`` 里的下标（用于在基线里找前后锚点）。
        segment: 原始源码片段（含装饰器行，逐字节取自基线）。
    """

    base_index: int
    segment: str


def revert_symbols(
    base: Mapping[str, str],
    patch: Mapping[str, str],
    removals: Sequence[SymbolRemoval],
) -> dict[str, str]:
    """把「被删的公共顶层符号」从基线搬回补丁文件（**绝不抛异常**）。

    这是 ``symbol_deletions="revert"`` 的实现体（v1.4 T1.1，docs/15 §3.1）：
    回退粒度从**整文件**细化到**符号** —— 模型在同一文件里的正当重构得以保留，
    只有"被删且仍被引用"的 ``def`` / ``async def`` / ``class`` 被搬回来。

    Args:
        base: 基线文件集（``path -> content``），只读不改。
        patch: 补丁文件集（模型产出），只读不改。
        removals: :func:`detect_symbol_removals` 的结论（``(path, symbols)`` 序列）。

    Returns:
        ``path -> 还原后的正文``，**只含真正被改动的路径**；``{}`` 表示无需还原
        （没有可搬的符号 / 补丁写坏 / 路径不在补丁里）。

    Note:
        - **只处理补丁里仍有正文的路径**：文件被整份删除时没有正文可插，调用方
          应直接取消该删除（``merge_trees`` 的 ``revert`` 分支就是这么做的）；
        - 插回位置按基线顶层定义顺序**锚定**（模块 docstring 的四条规则），
          不再一律追加到文件末尾；
        - **同文件依赖闭包**（v1.4 返修 3）：被还原定义引用的其它顶层 ``def`` /
          ``class`` 若也被删，一并还原（有界 64 个，见 :func:`_dependency_closure`）；
          模块级赋值 / ``import`` 别名不在其中；
        - 基线 ``__all__`` 列了被还原的符号时，同步把名字补回补丁的 ``__all__``
          （只补**原始**被删符号，依赖闭包带来的私有名字不进导出面）；
        - 补丁侧已有同名顶层定义 / 赋值 ⇒ 不插入（``不重复定义``）；
        - 解析失败（基线或补丁语法错误）⇒ 该路径跳过，**绝不抛**；
        - 纯函数：同一输入两次调用返回逐字节相同的结果，且不修改入参。
    """
    try:
        return _revert(base, patch, removals)
    except Exception:  # noqa: BLE001 - 兜底纪律：还原失败也不许毁掉整次合并
        return {}


def _revert(
    base: Mapping[str, str],
    patch: Mapping[str, str],
    removals: Sequence[SymbolRemoval],
) -> dict[str, str]:
    """``revert_symbols`` 的实现体（异常由外层兜住）。"""
    result: dict[str, str] = {}
    for item in removals:
        path = str(item.path)
        if path not in patch:
            continue  # 整份删除：没有正文可插（由调用方取消删除）
        base_source = str(base.get(path, ""))
        patch_source = str(patch.get(path, ""))
        if not base_source:
            continue
        try:
            base_tree = ast.parse(base_source)
            patch_tree = ast.parse(patch_source)
        except (SyntaxError, ValueError):
            continue  # 任一侧写坏语法：不猜（宁可漏还原，不可崩）
        wanted = tuple(str(name) for name in item.symbols if str(name))
        if not wanted:
            continue
        present = _top_level_binding_names(patch_tree)
        missing = tuple(name for name in wanted if name not in present)
        if not missing:
            continue  # 符号还在 ⇒ 无需插入（不重复定义）
        # ★ 返修 3：连同**同文件依赖**一起还原（`create_app` ⇒ `lifespan`、
        #   `render_stats` ⇒ `StatsResult` + `_sorted_tags`）。只搬种子的话，还原
        #   出来的定义引用未定义名 ⇒ 树仍起不来，而短码已经记了 reverted（假证据）。
        restore = _dependency_closure(base_tree, patch_tree, frozenset(missing))
        blocks = _definition_blocks(base_source, base_tree, restore)
        if not blocks:
            continue
        text = _insert_definitions(patch_source, patch_tree, base_tree, blocks)
        # ★ ``__all__`` 在插入定义**之后**再处理：插入会移动行号，届时重新解析。
        # ★ 只补**原始 missing**：私有依赖（`_sorted_tags`）不得进导出面。
        text = _restore_all_names(base_tree, text, frozenset(missing))
        if text != patch_source:
            result[path] = text
    return result


def _dependency_closure(
    base_module: ast.Module,
    patch_module: ast.Module,
    seeds: AbstractSet[str],
    *,
    limit: int = _MAX_CLOSURE_SYMBOLS,
) -> frozenset[str]:
    """求「要还原的顶层定义」的**有界依赖闭包**（返修 3，见模块 docstring）。

    种子是本文件本次要还原的符号；对每个种子，在**基线顶层**找同名 ``def`` /
    ``async def`` / ``class``，收集它引用到的 ``Name(Load)``，其中仍是"基线顶层
    def/class 且补丁里已不存在"的名字加入集合并继续展开（BFS）。

    Args:
        base_module: 基线 AST（定义与依赖的唯一来源）。
        patch_module: 补丁 AST（判断"补丁里是否已有绑定"）。
        seeds: 本次要还原的符号名（来自 ``detect_symbol_removals``）。
        limit: 单文件闭包上限（默认 :data:`_MAX_CLOSURE_SYMBOLS`）。

    Returns:
        需要一并还原的符号名集合（**含种子本身**，仅当它在基线顶层是
        ``def`` / ``async def`` / ``class``）；顺序信息不在这里 —— 插回顺序由
        :func:`_definition_blocks` 按基线 body 顺序给出。

    Note:
        - **纯函数**：只读两个 AST，不修改入参、无 IO、不抛（调用方外层还有兜底）；
        - **不做作用域分析**：局部同名可能带来少量多余还原（见模块 docstring 的
          「已知限制」），方向是"多还原一点"而非"少还原"（少还原 ⇒ 坏树 + 假证据）；
        - 依赖是模块级赋值 / ``import`` 别名 / 内建名 ⇒ **跳过**（本版不还原它们）；
        - 触达 ``limit`` 即停止展开（已收集项保留），不会把整模块搬回来。
    """
    definitions = _definition_map(base_module)
    #: 补丁里**已有**的绑定：定义 / 赋值（不重复插入）+ import 别名（避免同名双绑）。
    bound_in_patch = _top_level_binding_names(patch_module) | _import_bound_names(patch_module)
    #: 基线里由 ``import`` 绑定的名字：导入缺失不在本版范围（不还原 import 语句）。
    imported_in_base = _import_bound_names(base_module)
    collected: list[str] = []
    seen: set[str] = set()
    # ★ 种子按名字排序 + 依赖按名字排序 ⇒ 遍历顺序确定（输出本身再由基线顺序决定，
    #   这里排序只为"同一输入 ⇒ 同一收集集合"，避免 set 迭代顺序渗进结果）。
    queue: list[str] = sorted(name for name in seeds if name in definitions)
    cursor = 0
    while cursor < len(queue) and len(collected) < limit:
        name = queue[cursor]
        cursor += 1
        if name in seen:
            continue
        seen.add(name)
        if name in bound_in_patch or name in imported_in_base or name in _BUILTIN_NAMES:
            continue  # 补丁里已有 / 基线是 import / 内建 ⇒ 无可还原的 def 或 class
        node = definitions.get(name)
        if node is None:
            continue  # 基线顶层不是 def/class（模块级赋值、未定义）：本版不还原
        collected.append(name)
        for dependency in sorted(_loaded_names(node)):
            if dependency not in seen:
                queue.append(dependency)
    return frozenset(collected)


def _definition_map(module: ast.Module) -> dict[str, ast.stmt]:
    """基线顶层**定义节点**索引 ``name -> node``（同名只保留第一个）。"""
    mapping: dict[str, ast.stmt] = {}
    for node in module.body:
        if not isinstance(node, _DEFINITION_NODES):
            continue
        if node.name and node.name not in mapping:
            mapping[node.name] = node
    return mapping


def _import_bound_names(module: ast.Module) -> frozenset[str]:
    """顶层 ``import`` / ``from ... import ...`` **绑定的名字**（不含星号导入）。

    ``import a.b`` 绑定 ``a``（不是 ``a.b``）；``import a.b as ab`` 绑定 ``ab``；
    ``from x import y as z`` 绑定 ``z``。``from x import *`` 不产生具体名字 ⇒ 忽略
    （此时无法判断某个名字是否已被补丁绑定，按"没绑定"处理，方向是多还原）。
    """
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name:
                    continue
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == STAR_IMPORT or not alias.name:
                    continue
                names.add(alias.asname or alias.name)
    return frozenset(names)


def _top_level_binding_names(module: ast.Module) -> frozenset[str]:
    """补丁文件里**已存在**的顶层绑定名（定义 + 赋值，用于"不重复插入"）。

    比 :func:`_public_top_level_symbols` 多算赋值：模型若把 ``run`` 改写成
    ``run = make_runner()``，再插一个 ``def run`` 会**遮蔽**它 —— 那不是"还原"，
    而是制造一处静默的行为替换。
    """
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, _DEFINITION_NODES):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_assignment_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_assignment_names(node.target))
    return frozenset(names)


def _assignment_names(target: ast.expr) -> tuple[str, ...]:
    """赋值目标的裸名字（``a`` / ``a = b = 1`` / ``a, b = ...``；属性、下标不算）。"""
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in target.elts:
            names.extend(_assignment_names(element))
        return tuple(names)
    return ()


def _definition_blocks(
    source: str,
    module: ast.Module,
    names: frozenset[str],
) -> tuple[_DefinitionBlock, ...]:
    """取出指定符号的**原始源码片段**（含装饰器行，按基线出现顺序）。

    为什么不用 ``ast.get_source_segment``：对带装饰器的定义它从 ``def`` 行开始，
    **装饰器会被丢掉** —— ``@app.get("/x")`` 没了，路由就真的消失了。
    """
    lines = source.splitlines()
    blocks: list[_DefinitionBlock] = []
    for index, node in enumerate(module.body):
        if not isinstance(node, _DEFINITION_NODES) or node.name not in names:
            continue
        start = node.lineno
        for decorator in getattr(node, "decorator_list", ()):  # noqa: B009 - ast 属性
            start = min(start, decorator.lineno)
        end = getattr(node, "end_lineno", None) or start
        segment = "\n".join(lines[max(0, start - 1) : end])
        if segment.strip():
            blocks.append(_DefinitionBlock(base_index=index, segment=segment))
    return tuple(blocks)


def _insert_definitions(
    source: str,
    module: ast.Module,
    base_module: ast.Module,
    blocks: Sequence[_DefinitionBlock],
) -> str:
    """把待插回的定义按**锚定行号**插进补丁正文（从后往前改，保持偏移有效）。

    Args:
        source: 补丁正文（未插入任何东西）。
        module: ``source`` 的 AST（**插入位置**的行号来源）。
        base_module: 基线 AST（**锚点**的查找范围 —— 顺序信息只在基线里）。
        blocks: 待插入的定义（已按基线顺序排列）。

    Returns:
        插入后的正文；末尾换行形态与入参保持一致（原本有换行就保留一个）。
    """
    lines = source.splitlines()
    spans = _definition_spans(module)
    # ★ P1-B：装饰器 / 默认值里用到的名字 ⇒ 它们的模块级绑定必须已执行（插入点在其后）。
    dependency_names = _definition_dependency_names(base_module, blocks)
    fallback = _first_side_effect_line(module, dependency_names=dependency_names)
    dependency_end = _dependency_binding_end(module, dependency_names)
    grouped: dict[int, list[str]] = {}
    for block in blocks:
        index = _anchor_line(
            base_module,
            block,
            spans,
            fallback,
            dependency_end=dependency_end,
            total=len(lines),
        )
        grouped.setdefault(index, []).append(block.segment)
    for index in sorted(grouped, reverse=True):
        lines = _splice_definitions(lines, index, grouped[index])
    # ★ 行尾风格跟原文件走：``splitlines`` 会把 CRLF 一起吃掉，若这里一律用 ``"\n"``
    #   拼接，就等于把整份 CRLF 文件改写成 LF —— 那是"插入一个符号"之外的字节变更，
    #   违反"不重排模型代码"（且会让下游 diff 面目全非）。
    newline = "\r\n" if "\r\n" in source else "\n"
    text = newline.join(lines)
    return f"{text}{newline}" if source.endswith(("\n", "\r")) and text else text


def _definition_spans(
    module: ast.Module,
) -> dict[tuple[str, type[ast.stmt]], tuple[int, int]]:
    """顶层定义在**补丁**里的 ``(起始行, 结束行)``（1 基，起始行含装饰器）。

    键是 ``(名字, 节点类型)``：``def`` / ``async def`` / ``class`` 三者互不混淆
    —— 模型把 ``def run`` 改成 ``async def run`` 属于"该定义已被改写"，
    不能再当锚点（否则插入点会贴着一段形状不同的代码）。
    """
    spans: dict[tuple[str, type[ast.stmt]], tuple[int, int]] = {}
    for node in module.body:
        key = _definition_key(node)
        if key is None or key in spans:
            continue
        start = node.lineno
        for decorator in getattr(node, "decorator_list", ()):  # noqa: B009 - ast 属性
            start = min(start, decorator.lineno)
        end = getattr(node, "end_lineno", None) or node.lineno
        spans[key] = (start, end)
    return spans


def _definition_key(node: ast.stmt) -> tuple[str, type[ast.stmt]] | None:
    """定义节点的锚点键 ``(名字, 节点类型)``；非定义节点返回 ``None``。"""
    if not isinstance(node, _DEFINITION_NODES):
        return None
    return (node.name, type(node))


def _first_side_effect_line(
    module: ast.Module,
    *,
    dependency_names: AbstractSet[str] = frozenset(),
) -> int | None:
    """补丁里第一个**模块级副作用语句**的行号（1 基）；没有返回 ``None``。

    副作用 = ``Expr(Call)``（``include_router(...)`` / ``main()``）/ 控制流语句 /
    **右值含调用的赋值**（``VALUE = new_anchor()`` / ``ROUTES = build()``）。

    ★ P1-B 例外：赋值目标若是被还原定义的**装饰器依赖绑定**（``app = FastAPI()`` 之于
    ``@app.get(...)``），它**不算**边界 —— 插入点必须落在它**之后**，否则装饰器在
    ``app`` 定义之前执行会 ``NameError``。这类绑定由 :func:`_dependency_binding_end`
    单独兜住；这里跳过它继续往后找边界。

    ``Import`` / 纯常量赋值 / 文档字符串不算副作用：``@app.get`` 这类装饰器只要排在
    ``app = FastAPI()`` 之后、``include_router(...)`` 之前就已经正确。
    """
    skip = set(dependency_names)
    for node in module.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return node.lineno
        if isinstance(node, _SIDE_EFFECT_NODES):
            return node.lineno
        if _is_call_assignment(node) and not (_assignment_target_names(node) & skip):
            return node.lineno
    return None


def _is_call_assignment(node: ast.stmt) -> bool:
    """模块级赋值且**右值里出现调用**（``VALUE = new_anchor()`` / ``ROUTES = build()``）。

    ★ 为什么只认含 ``ast.Call`` 的赋值：``VERSION = "1.0"`` 这类纯常量赋值没有执行
    顺序依赖（插在它前后行为不变），若也算副作用，插入点会被无谓地推到文件开头，
    反而把符号放到别的定义之前。
    """
    if isinstance(node, ast.Assign):
        value: ast.expr | None = node.value
    elif isinstance(node, ast.AnnAssign):
        value = node.value
    else:
        return False
    if value is None:
        return False
    return any(isinstance(child, ast.Call) for child in ast.walk(value))


def _assignment_target_names(node: ast.stmt) -> frozenset[str]:
    """顶层赋值语句绑定的裸名字集合（``a = ...`` / ``a = b = ...`` / ``a, b = ...``）。"""
    if isinstance(node, ast.Assign):
        names: set[str] = set()
        for target in node.targets:
            names.update(_assignment_names(target))
        return frozenset(names)
    if isinstance(node, ast.AnnAssign):
        return frozenset(_assignment_names(node.target))
    return frozenset()


def _definition_dependency_names(
    base_module: ast.Module,
    blocks: Sequence[_DefinitionBlock],
) -> frozenset[str]:
    """被还原定义在**执行时求值**的自由名字（装饰器 / 默认值 / 基类 / 类关键字）。

    这些名字对应的模块级绑定（``app = FastAPI()``）必须排在插入点之前：装饰器
    ``@app.get(...)`` 在定义处立即执行，``app`` 尚未定义就是 ``NameError``；
    默认值 ``def f(x: int = LIMIT)`` 同理。
    """
    names: set[str] = set()
    for block in blocks:
        node = base_module.body[block.base_index]
        for decorator in getattr(node, "decorator_list", ()):  # noqa: B009 - ast 属性
            names |= _loaded_names(decorator)
        arguments = getattr(node, "args", None)  # noqa: B009 - ast 属性
        if isinstance(arguments, ast.arguments):
            for default in (*arguments.defaults, *arguments.kw_defaults):
                if default is not None:
                    names |= _loaded_names(default)
        for base in getattr(node, "bases", ()):  # noqa: B009 - ast 属性
            names |= _loaded_names(base)
        for keyword in getattr(node, "keywords", ()):  # noqa: B009 - ast 属性
            if keyword.value is not None:
                names |= _loaded_names(keyword.value)
    return frozenset(names)


def _loaded_names(node: ast.AST) -> frozenset[str]:
    """``node`` 子树里所有 ``Name(Load)`` 的名字（只用于装饰器 / 默认值等求值位置）。"""
    return frozenset(
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    )


def _dependency_binding_end(module: ast.Module, names: AbstractSet[str]) -> int:
    """补丁里「装饰器依赖绑定」的**最后结束行**（1 基）；没有返回 ``0``。

    插入点（0 基下标）必须 ``>=`` 该值，即落在这些绑定**之后**（``app = FastAPI()``
    必须先于 ``@app.get`` 执行）。
    """
    if not names:
        return 0
    wanted = set(names)
    end = 0
    for node in module.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if not (_assignment_target_names(node) & wanted):
            continue
        end = max(end, getattr(node, "end_lineno", None) or node.lineno)
    return end


def _anchor_line(
    base_module: ast.Module,
    block: _DefinitionBlock,
    spans: Mapping[tuple[str, type[ast.stmt]], tuple[int, int]],
    fallback: int | None,
    *,
    dependency_end: int = 0,
    total: int,
) -> int:
    """求该定义在**补丁**里的插入行号（0 基，插到该下标对应的行之前）。

    规则见模块 docstring（前锚 / 后锚 / 副作用语句 / 文件末尾）+ P1-B 两条护栏：
    候选点与副作用边界取 ``min``，再与装饰器依赖绑定的结束行取 ``max``。
    """
    body = base_module.body
    candidate: int | None = None
    # ① 前面最近的、在补丁里仍存活的定义 ⇒ 插到它结束行之后
    for node in reversed(body[: block.base_index]):
        key = _definition_key(node)
        if key is not None and key in spans:
            candidate = spans[key][1]  # end_lineno 是 1 基闭区间 ⇒ 正好是 0 基的"下一行"
            break
    # ② 后面最近的、在补丁里仍存活的定义 ⇒ 插到它起始行之前
    if candidate is None:
        for node in body[block.base_index + 1 :]:
            key = _definition_key(node)
            if key is not None and key in spans:
                candidate = max(0, spans[key][0] - 1)
                break
    if candidate is None:
        # ③ 第一个模块级副作用语句之前；④ 纯定义文件 ⇒ 追加末尾
        candidate = max(0, fallback - 1) if fallback is not None else total
    elif fallback is not None:
        # ★ P1-B 护栏 1：任何插入点都不得晚于第一个模块级副作用 —— 后锚跨过
        #   ``include_router(...)`` / ``VALUE = new_anchor()`` 会让路由静默丢失、
        #   或让模块级调用先于被还原的 ``helper`` 执行（NameError）。
        candidate = min(candidate, max(0, fallback - 1))
    # ★ P1-B 护栏 2：必须落在装饰器依赖绑定之后（``app = FastAPI()`` 先于 ``@app.get``）
    if dependency_end:
        candidate = max(candidate, dependency_end)
    return candidate


def _splice_definitions(lines: Sequence[str], index: int, segments: Sequence[str]) -> list[str]:
    """把若干定义插到 ``lines[:index]`` 与 ``lines[index:]`` 之间（PEP 8 两个空行）。

    接缝处的空行按"恰好两个空行"重建，其余内容（含缩进、行尾注释）原样保留。
    """
    texts = [segment.strip("\n") for segment in segments if segment.strip("\n")]
    if not texts:
        return list(lines)
    head = list(lines[:index])
    tail = list(lines[index:])
    while head and not head[-1].strip():
        head.pop()
    while tail and not tail[0].strip():
        tail.pop(0)
    block = _SYMBOL_SEPARATOR.join(texts).split("\n")
    merged = list(head)
    if merged:
        merged.extend(["", ""])
    merged.extend(block)
    if tail:
        merged.extend(["", ""])
        merged.extend(tail)
    return merged


def _restore_all_names(
    base_tree: ast.Module,
    patch_source: str,
    names: frozenset[str],
) -> str:
    """把被还原的符号名补回补丁的 ``__all__`` 字面量列表（**不猜、不动拼接式**）。

    Args:
        base_tree: 基线 AST（``__all__`` 的顺序来源）。
        patch_source: 已插回定义的补丁正文。
        names: 本次被还原的符号名。

    Returns:
        处理后的正文；无需 / 无法处理时**原样返回** ``patch_source``。

    Note:
        只处理 ``__all__ = [...]`` / ``__all__: list[str] = [...]`` 这种**字面量**；
        ``__all__ = _A + _B`` 之类的拼接形态一律不动（算不出插入点，宁可不改）。
    """
    base_order = _all_names(base_tree)
    if not base_order:
        return patch_source
    wanted = [name for name in base_order if name in names]
    if not wanted:
        return patch_source
    try:
        patch_tree = ast.parse(patch_source)
    except (SyntaxError, ValueError):
        return patch_source
    assignment = _all_assignment(patch_tree)
    if assignment is None:
        return patch_source  # 补丁里没有 __all__：不新建（那会改变模块导出面）
    current = _all_names(patch_tree)
    if current is None:
        return patch_source  # 非字面量列表：不猜
    final = _merge_all_order(base_order, current, wanted)
    if final == list(current):
        return patch_source
    return _render_all(patch_source, assignment, final)


def _all_assignment(module: ast.Module) -> ast.stmt | None:
    """顶层 ``__all__`` 赋值节点（``Assign`` / 带值的 ``AnnAssign``）；没有返回 ``None``。"""
    for node in module.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and targets[0].id == _DUNDER_ALL
            ):
                return node
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == _DUNDER_ALL
                and node.value is not None
            ):
                return node
    return None


def _all_names(module: ast.Module) -> tuple[str, ...] | None:
    """``__all__`` 里的字符串名（按源码顺序）；不是**字面量列表 / 元组** ⇒ ``None``。"""
    node = _all_assignment(module)
    if node is None:
        return None
    values = getattr(node, "value", None)
    if not isinstance(values, (ast.List, ast.Tuple)):
        return None
    names: list[str] = []
    for element in values.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        names.append(element.value)
    return tuple(names)


def _merge_all_order(
    base_order: Sequence[str],
    current: Sequence[str],
    wanted: Sequence[str],
) -> list[str]:
    """把 ``wanted`` 按**基线顺序**插进 ``current``（不重排补丁里已有的名字）。"""
    position_of = {name: index for index, name in enumerate(base_order)}
    final = list(current)
    for name in wanted:
        if name in final:
            continue
        position = 0
        for index, existing in enumerate(final):
            if existing in position_of and position_of[existing] < position_of[name]:
                position = index + 1
        final.insert(position, name)
    return final


def _render_all(source: str, assignment: ast.stmt, names: Sequence[str]) -> str:
    """重渲染 ``__all__`` 的**列表字面量**部分（保留原有引号 / 缩进 / 多行风格）。"""
    values = getattr(assignment, "value", None)
    if not isinstance(values, (ast.List, ast.Tuple)):
        return source
    list_like = isinstance(values, ast.List)
    opener, closer = ("[", "]") if list_like else ("(", ")")
    quote = _quote_style(source, values)
    start = _char_offset(source, values.lineno, values.col_offset)
    end = _char_offset(source, values.end_lineno, values.end_col_offset)
    if start < 0 or end < start:
        return source
    rendered_items = [f"{quote}{name}{quote}" for name in names]
    multiline = values.lineno != values.end_lineno
    if multiline:
        indent = _element_indent(source, values)
        closing = _line_indent(source, values.lineno)
        body = "\n".join(f"{indent}{item}," for item in rendered_items)
        rendered = f"{opener}\n{body}\n{closing}{closer}" if rendered_items else f"{opener}{closer}"
    else:
        rendered = f"{opener}{', '.join(rendered_items)}{closer}"
    return f"{source[:start]}{rendered}{source[end:]}"


def _quote_style(source: str, values: ast.List | ast.Tuple) -> str:
    """沿用原有引号风格（看第一个**字符串元素**的首字符）；识别不出用双引号。"""
    if values.elts:
        first = values.elts[0]
        if isinstance(first, ast.Constant):
            offset = _char_offset(source, first.lineno, first.col_offset)
            if 0 <= offset < len(source) and source[offset] in _QUOTES:
                return source[offset]
    return _QUOTES[0]


def _element_indent(source: str, values: ast.List | ast.Tuple) -> str:
    """多行 ``__all__`` 里元素的缩进（取第一个元素的行首空白）。"""
    if values.elts:
        first = values.elts[0]
        if first.lineno != values.lineno:
            line = source.splitlines()[first.lineno - 1]
            return line[: len(line) - len(line.lstrip())]
    return f"{_line_indent(source, values.lineno)}    "


def _line_indent(source: str, lineno: int) -> str:
    """第 ``lineno`` 行（1 基）的行首空白。"""
    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        line = lines[lineno - 1]
        return line[: len(line) - len(line.lstrip())]
    return ""


def _char_offset(source: str, lineno: int, byte_col: int) -> int:
    """AST 的 ``(lineno, col_offset)`` → 正文的**字符**偏移。

    ★ ``col_offset`` / ``end_col_offset`` 是 **UTF-8 字节**偏移：源码里出现中文
    （``__all__ = ["中文"]``）时，字节数大于字符数，直接当索引会切在半个字符上。
    这里按行内"字节前缀解码"换算成字符数。
    """
    lines = source.splitlines(keepends=True)
    if lineno < 1 or lineno > len(lines):
        return -1
    prefix = sum(len(line) for line in lines[: lineno - 1])
    line = lines[lineno - 1]
    head = line.encode("utf-8")[: max(0, byte_col)]
    return prefix + len(head.decode("utf-8", "ignore"))


__all__ = [
    "INIT_STEM",
    "PRIVATE_PREFIX",
    "PYTHON_SUFFIX",
    "STAR_IMPORT",
    "SymbolRemoval",
    "detect_symbol_removals",
    "revert_symbols",
]
