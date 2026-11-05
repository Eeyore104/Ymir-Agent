"""交付树依赖声明 × 镜像预装清单 的**纯函数探针**（v1.5 T03.4，docs/17 §1.2.3）。

**要解决的问题（PRD T1-R2）**：`"因为镜像没装依赖而没跑"` 与 `"代码真的坏了"` 必须
可区分。判据只能是**运行前、与代码无关、可重复**的三条 —— 读交付树的依赖声明，
与镜像预装清单求差集：

```
declared(files) ∩ image_packages  ──差集非空──► 该检查 skipped(requires_missing_deps:<pkg>)
```

**为什么禁止解析报错文本**（产品红线，PRD §3-T1 已否决）：同一棵树两次跑可能给出
不同结论（输出顺序 / 版本差异），那不是判据。

**粒度是"整条检查"**（不是按包、不是按命令）：
一条 ``check`` = 一条命令，"命令级粒度"不存在；``pytest`` 缺 1 个包时是**收集期
ImportError**，"部分通过"没有语义。粒度落在检查这一层，因此 D1 的
``compileall`` / ``ruff`` **照常执行** —— 不会因为缺 1 个包把整棵树判成未验证。

**本模块的纪律**：纯函数、零 IO、绝不抛（坏文件 = 忽略，不是异常）。解析只认
**名字形态**：URL、``-e``、``-r``、``--index-url`` 行跳过不判，并计入
``deps_probe_ignored:<n>``（证据里披露"有多少行没参与判定"，不静默）。

**已知限制（必须写进 docs/04）**：``declared − image ≠ ∅`` 是"声明了但镜像没有"，
**不是**"真的 import 了"。模型声明了不用的包会导致**过度跳过**（方向是宽松），
由 ``unverified`` 与提升留痕兜住 —— 这是"不解析报错文本"这条红线的必然代价。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: 生态名（``requires_deps`` 的通配 token 用同名字面量：``python:*`` / ``node:*``）。
ECOSYSTEM_PYTHON: str = "python"
ECOSYSTEM_NODE: str = "node"

#: 「依赖缺失」跳过原因前缀（与 ``evaluators.base.MISSING_DEPS_PREFIX`` 同值同源）。
MISSING_DEPS_PREFIX: str = "requires_missing_deps:"

#: 「有 N 行依赖声明被忽略」的证据短码前缀（URL / ``-e`` / ``-r`` / ``--index-url``）。
IGNORED_PREFIX: str = "deps_probe_ignored:"

#: PEP 508 名字右边界：``name[extra]>=1.0; python_version>...`` 一律截到名字为止。
_NAME_SPLIT = re.compile(r"[<>=!~\[;(\s]")

#: npm scope 包名（``@types/node`` / ``@types/node@22`` → 截到 scope/name）。
_SCOPED_NAME = re.compile(r"^(@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)")

#: 普通包名（PEP 508 / npm 共用形态）。
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def normalize_dep_name(raw: str) -> str:
    """把一个依赖声明归一成**包名**（去版本/extra/marker；非法 ⇒ ``""``）。

    - ``fastapi>=0.115`` → ``fastapi``；
    - ``uvicorn[standard]`` → ``uvicorn``；
    - ``pytest;python_version<'3.13'`` → ``pytest``；
    - ``@types/node@22`` → ``@types/node``（npm scope 包名，版本后缀截掉）；
    - ``vue-router@3`` → ``vue-router``；
    - ``-e .`` / URL / ``git+…`` → ``""``（不是名字形态，调用方计入 ignored）。

    归一化只做"截到名字"，**不做**大小写归一（PEP 508 名字大小写不敏感，但 npm 不是；
    统一在比对处用 ``casefold()``，避免把 ``@types/Node`` 与 ``@types/node`` 判成两个包）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" in text or text.startswith(("git+", "file:", "-")):
        return ""
    if text.startswith("@"):
        # npm scope 包：``@scope/name[@version]``
        scoped = _SCOPED_NAME.match(text)
        return scoped.group(1) if scoped else ""
    name = _NAME_SPLIT.split(text, 1)[0].split("@", 1)[0].strip()
    return name if _PLAIN_NAME.match(name) else ""


def _is_python_requirements(path: str) -> bool:
    """``requirements.txt`` / ``requirements-dev.txt`` 等（按文件名，任意目录层级）。"""
    base = path.rsplit("/", 1)[-1]
    return base.endswith(".txt") and base.startswith("requirements")


def _is_pyproject(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == "pyproject.toml"


def _is_package_json(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == "package.json"


def declared_deps(files: Mapping[str, str]) -> tuple[dict[str, frozenset[str]], int]:
    """读交付树里的依赖声明，返回 ``(按生态分组的名字集合, 被忽略的行数)``。

    支持的形态（**只解析名字**，不求完整 PEP 508 / semver 语义）：

    - ``requirements*.txt``：逐行；跳过注释 / 空行 / 选项行（``-e`` / ``-r`` /
      ``--index-url`` 等）/ URL 行；
    - ``pyproject.toml`` 的 ``[project].dependencies``（PEP 621 清单）；
    - 任意层级的 ``package.json``（含 ``web/package.json``）的
      ``dependencies`` + ``devDependencies``。

    Args:
        files: 交付树文件集（路径 -> 正文）。

    Returns:
        ``({"python": frozenset(...), "node": frozenset(...)}, ignored)``。
        两个生态**总是**存在（可为空集），调用方不必判 ``KeyError``。

    Note:
        **绝不抛**：TOML / JSON 坏了就当"这个文件没声明"，并把它计入 ignored 之外
        的静默忽略 —— 依赖声明解析失败不该让评测失败（与 ``parse_patch_manifest``
        同纪律）。真正的坏树会由 ``compileall`` / ``pytest`` 抓到。
    """
    python: set[str] = set()
    node: set[str] = set()
    ignored = 0
    for path in sorted(files):
        content = files[path]
        if _is_python_requirements(path):
            names, skipped = _parse_requirements(content)
            python |= names
            ignored += skipped
        elif _is_pyproject(path):
            python |= _parse_pyproject(content)
        elif _is_package_json(path):
            node |= _parse_package_json(content)
    return {ECOSYSTEM_PYTHON: frozenset(python), ECOSYSTEM_NODE: frozenset(node)}, ignored


def _parse_requirements(content: str) -> tuple[set[str], int]:
    """``requirements.txt`` 逐行解析（返回名字集合 + 被忽略行数）。"""
    names: set[str] = set()
    ignored = 0
    for raw_line in str(content or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-") or "://" in line or line.startswith("git+"):
            # ``-e .`` / ``-r other.txt`` / ``--index-url ...`` / 直接写 URL：
            # 都不是"包名形态" ⇒ 跳过不判（数量进 ``deps_probe_ignored:<n>``）
            ignored += 1
            continue
        name = normalize_dep_name(line)
        if name:
            names.add(name)
        else:
            ignored += 1
    return names, ignored


def _parse_pyproject(content: str) -> set[str]:
    """``pyproject.toml`` 的 ``[project].dependencies``（PEP 621）。

    ★ 用标准库 ``tomllib``（Python 3.11+）；解析失败 ⇒ 返回空集（**不抛**）。
    """
    try:
        import tomllib

        payload: Any = tomllib.loads(str(content or ""))
    except Exception:  # noqa: BLE001 - 坏 TOML 只当"没声明"，不让评测失败
        return set()
    if not isinstance(payload, dict):
        return set()
    project = payload.get("project")
    if not isinstance(project, dict):
        return set()
    raw = project.get("dependencies")
    if not isinstance(raw, list):
        return set()
    names = {normalize_dep_name(str(item)) for item in raw}
    names.discard("")
    return names


def _parse_package_json(content: str) -> set[str]:
    """``package.json`` 的 ``dependencies`` + ``devDependencies``（键名即包名）。"""
    try:
        payload: Any = json.loads(str(content or "{}"))
    except (TypeError, ValueError):
        return set()
    if not isinstance(payload, dict):
        return set()
    names: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        block = payload.get(section)
        if not isinstance(block, dict):
            continue
        for raw_name in block:
            name = normalize_dep_name(str(raw_name))
            if name:
                names.add(name)
    return names


def missing_deps_by_language(
    declared: Mapping[str, Any],
    image_packages: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    """``declared − image`` 按生态分组（保序去重；``image_packages`` 缺该生态 ⇒ 不判）。

    ★ 为什么 ``image_packages`` 缺某生态时**不判**（返回空元组）而不是"全缺"：
    空表意味着"我不知道镜像里有什么"（例如调用方没注入），此时把一切都判缺失
    会让**整棵树变成永久未验证**。加载期校验 25 已经从源头禁止了"空表 + 声明"，
    运行期这一侧宁可"少跳过"也不"全军覆没"。
    """
    result: dict[str, tuple[str, ...]] = {}
    if not image_packages:
        return result
    for language, names in declared.items():
        table = {str(item).casefold() for item in (image_packages.get(language) or ())}
        if not table:
            continue
        missing: list[str] = []
        for name in sorted({str(item) for item in names}):
            if str(name).casefold() not in table:
                missing.append(str(name))
        result[str(language)] = tuple(missing)
    return result


def missing_image_deps(
    declared: Mapping[str, Any],
    image_packages: Mapping[str, Sequence[str]] | None,
) -> tuple[str, ...]:
    """``declared − image`` 的**扁平**形态（保序去重，供告警/证据文案）。

    与 :func:`missing_deps_by_language` 同一份事实，只是丢掉生态标签 ——
    ``requires_deps`` 的判定必须用**按生态**那份（见 ``check_requires_deps``：
    否则 ``web/package.json`` 里新增的 ``vue-router`` 会把后端 ``pytest`` 一起 skip）。
    """
    flat: list[str] = []
    grouped = missing_deps_by_language(declared, image_packages)
    for language in (ECOSYSTEM_PYTHON, ECOSYSTEM_NODE):
        for name in grouped.get(language, ()):
            if name not in flat:
                flat.append(name)
    return tuple(flat)


def missing_deps_for(
    files: Mapping[str, str],
    image_packages: Mapping[str, Sequence[str]] | None,
) -> tuple[dict[str, tuple[str, ...]], int]:
    """一次性把「树 -> 缺失依赖（按生态）」算完（三个执行点的公共入口）。

    Returns:
        ``(按生态的缺失清单, 被忽略的声明行数)``。
    """
    declared, ignored = declared_deps(files)
    return missing_deps_by_language(declared, image_packages), ignored


def image_package_set(
    image_packages: Mapping[str, Sequence[str]] | None, language: str = ""
) -> frozenset[str]:
    """把镜像包表压成**大小写不敏感**的集合（具体包名的运行期兜底比对用）。"""
    tables: list[Sequence[str]] = []
    if image_packages:
        if language:
            tables.append(image_packages.get(language) or ())
        else:
            tables.extend(image_packages.values())
    return frozenset(str(item).casefold() for table in tables for item in table)


def check_requires_deps(
    requires_deps: Sequence[str] | None,
    *,
    missing_deps: Mapping[str, Sequence[str]] | None = None,
    image_packages: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """判定一条检查的 ``requires_deps`` 是否满足；不满足 ⇒ 返回**跳过原因**。

    Args:
        requires_deps: 该检查声明的 token（``python:*`` / ``node:*`` / 具体包名）。
        missing_deps: 交付树声明但镜像没有的包（按生态，:func:`missing_deps_for` 的产物）。
        image_packages: 镜像包表（按生态）。具体包名用它的**并集**做兜底比对
            （运行期镜像可能被手工改动：宁可 skip 也不误判 fail）。

    Returns:
        ``""``（可执行）或 ``requires_missing_deps:<pkg>``（该检查应 ``skipped``）。

    Note:
        ``image_packages`` 为空 ⇒ **不做任何依赖判定**（镜像表未知，见
        ``missing_deps_by_language`` 的说明）。这条保住了"直接构造 ``EvalTask``"
        的老调用方：它们的检查本来也不声明 ``requires_deps``。
    """
    tokens = [str(item or "").strip() for item in (requires_deps or ())]
    tokens = [token for token in tokens if token]
    if not tokens or not image_packages:
        return ""
    missing = missing_deps or {}
    known = image_package_set(image_packages)
    for token in tokens:
        language = _wildcard_language(token)
        if language is not None:
            item = missing.get(language) or ()
            if item:
                return f"{MISSING_DEPS_PREFIX}{item[0]}"
            continue
        if token.casefold() not in known:
            return f"{MISSING_DEPS_PREFIX}{token}"
    return ""


def _wildcard_language(token: str) -> str | None:
    """``python:*`` → ``python``；``node:*`` → ``node``；其它 ⇒ ``None``。

    ★ 字面量与 ``templates.models.REQUIRES_DEPS_WILDCARDS`` 同源。此处**刻意复写**：
    ``evaluation``(rank 1) 可以 import ``templates``(2)，但 ``deps`` 是纯函数模块，
    保持"只吃纯数据"能让它被任意层复用（含 ``orchestrator`` 的冒烟段）。
    """
    text = str(token or "").strip().casefold()
    if text.endswith(":*"):
        language = text[: -len(":*")]
        if language in (ECOSYSTEM_PYTHON, ECOSYSTEM_NODE):
            return language
    return None


def describe_ignored(ignored: int) -> str:
    """``deps_probe_ignored:<n>`` 证据短码（``0`` ⇒ 空串，不进证据）。"""
    return f"{IGNORED_PREFIX}{ignored}" if ignored else ""
