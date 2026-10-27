"""模板加载与加载期校验（v1.3：**21 条**；v1.2 为 17 条，docs/11 §7.2.7）。

目录约定：``config/templates/<id>.yaml``，``id`` 必须等于文件名主干。

**21 条加载期校验**（违反任一条 → :class:`TemplateDefinitionError`，业务码 1001）：

===========  ================================================================
#            校验
===========  ================================================================
1            ``id`` 非空且等于文件名主干（重复 id 在 ``list_templates`` 检出）
2            ``version`` 非空
3            ``stack.language ∈ ALLOWED_LANGUAGES``（v1.3：``python`` / ``typescript``）
4            ``stack.dependencies ⊆ 该语言的镜像清单``（按语言选表，v1.3 语义细化）
5            ``layout`` 各路径字段过 ``normalize_rel_path``
6            ``conventions[].path`` 过 ``normalize_rel_path`` 且互不重复
7            ``skeleton.files[].path`` 过 ``normalize_rel_path`` 且互不重复
8            ``skeleton.files[].content`` 非空（纯空白也算空）
9            ``skeleton.reserved_paths ⊆ skeleton.files`` 的路径集合
10           ``len(files) ≤ max_files`` 且总字节 ≤ ``max_bytes``
11           骨架内容不得命中密钥指纹
12           ``skeleton`` 与 ``conventions`` 路径不得交叉
13           ``checks`` 非空且至少 1 条 ``type == "command"``
14           ``checks[].run`` 不得含 ``http://``/``https://``/``pip install``/``npm install``
15           ``checks[].weight > 0``
16           ``supports`` 非空且 ⊆ ``{from_scratch, patch}``
17           ``prompting`` 的键必须是已注册角色 id
18           ``protected.paths`` 归一化 + 去重 + **不得与 skeleton / conventions 交叉**
19           ``protected.on_hit ∈ {reject, ignore}``（显式复核，防默认值被放宽）
20           ``0 < protected.max_change_ratio ≤ 1``
21           ``scoring`` 配分完整性（值 > 0、键非空；声明前端配分必须有 ``layout.frontend``）
===========  ================================================================

设计取舍（与 ``sop/loader.py`` 同构）：

- **加载期 fail-fast，不做静默修正**：配置错误必须显式报出来（``extra="forbid"``
  兜住拼错字段，正则/集合校验兜住语义错误）；
- **坏模板不拖垮注册表**：:func:`list_templates` 把单模板错误收进
  ``TemplateEntry.loading_error``，只有 :func:`load_template` 才抛异常。

v1.3 追加校验 18–21（T1.1 / Q3），17 条原有语义**全部不变**。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.common.paths import normalize_rel_path, normalize_scope_path
from ymir.common.secrets import scan_secret
from ymir.config import PROJECT_ROOT
from ymir.templates.models import (
    ALLOWED_LANGUAGES,
    SANDBOX_IMAGE_NODE_PACKAGES,
    SANDBOX_IMAGE_PACKAGES,
    CheckSpec,
    ConventionFile,
    LayoutSpec,
    ProtectedSpec,
    SkeletonFile,
    SkeletonSpec,
    TemplateEntry,
    TemplateSpec,
)

#: 模板目录（平台配置，不是用户数据：docs/09 §4.4.1「模板库不要表」）。
DEFAULT_TEMPLATES_DIR: Path = PROJECT_ROOT / "config" / "templates"

#: 角色定义目录（校验 17 的缺省来源：文件名主干即角色 id）。
DEFAULT_ROLES_DIR: Path = PROJECT_ROOT / "config" / "roles"

#: 校验 14：检查命令禁止联网/安装（离线可复现）。
_OFFLINE_FORBIDDEN: tuple[str, ...] = ("http://", "https://", "pip install", "npm install")

#: 校验 19：受保护路径策略的合法取值（与 ``artifacts.merge.ProtectedPolicy`` 同构）。
_PROTECTED_POLICIES: frozenset[str] = frozenset({"reject", "ignore"})

#: 校验 21：内置「静态组」名字（该组无对应 check，由前端结构规则打分）。
FRONTEND_STATIC_GROUP: str = "frontend_static"

#: 依赖名的合法形式：``fastapi`` / ``fastapi>=0.115`` / ``uvicorn[standard]>=0.30``
_DEP_NAME_SPLIT = re.compile(r"[<>=!~\[;\s]")


class TemplateDefinitionError(InvalidParamError):
    """模板定义非法（1001，与 ``SopDefinitionError`` 同级）。"""


def _error(template_id: str, check_no: int, message: str) -> TemplateDefinitionError:
    """构造带「校验编号」的模板错误（便于贡献者对照 docs/09 §4.1.5 定位）。"""
    return TemplateDefinitionError(f"模板定义非法（{template_id}，校验 {check_no}）: {message}")


def image_packages_for(language: str) -> frozenset[str]:
    """校验 4 的镜像包清单（按语言选表；未知语言返回空表 = 任何依赖都不合法）。

    空表不是"漏洞"：某语言的包表若为空（例如将来新增一种语言但镜像还没装依赖），
    声明任何依赖都会被**明确拒绝**，而不是"能加载但沙箱跑不通"。
    ``typescript`` 的 node 包表已随 v1.3 与 ``docker/sandbox.Dockerfile`` 同批填充（B14）。
    """
    normalized = str(language).strip().lower()
    if normalized == "python":
        return SANDBOX_IMAGE_PACKAGES
    if normalized == "typescript":
        return SANDBOX_IMAGE_NODE_PACKAGES
    return frozenset()


def dependency_name(raw: str) -> str:
    """从依赖声明里取出归一化包名（去版本约束/ extras，小写）。"""
    return _DEP_NAME_SPLIT.split(raw.strip(), maxsplit=1)[0].strip().lower()


def default_role_ids(roles_dir: Path | None = None) -> set[str]:
    """已注册角色 id 集合（``config/roles/*.yaml`` 文件名主干）。

    直接用目录扫描而不是构造 ``RoleRegistry``：模板校验只关心"角色 id 是否已注册"，
    不需要加载角色正文（避免校验模板时因某个角色 YAML 非法而连带失败）。
    目录不存在时返回空集合 —— 此时校验 17 **跳过**（宁可少校验，不可让模板在
    裁掉角色目录的环境里全部不可用）。
    """
    directory = roles_dir or DEFAULT_ROLES_DIR
    if not directory.is_dir():
        return set()
    return {path.stem for path in directory.glob("*.yaml")}


def check_reserved_subset(
    template_id: str, reserved: Iterable[str], all_paths: Iterable[str]
) -> None:
    """校验 9：``reserved_paths ⊆ skeleton.files`` 的路径集合。

    独立成函数是为了让这条校验**可定点测试**（否则在正常模型下它是恒真不变量）。
    违反通常意味着调用方把"声明式 reserved 清单"与"文件清单"分两处维护而漂移。
    """
    extra = sorted(set(reserved) - set(all_paths))
    if extra:
        raise _error(
            template_id, 9, f"skeleton.reserved_paths 不在 skeleton.files 中: {extra}"
        )


def _normalize_path_field(template_id: str, check_no: int, field: str, raw: str) -> str:
    """单路径归一化（失败转成带校验编号的模板错误）。"""
    try:
        return normalize_rel_path(raw)
    except InvalidParamError as exc:
        raise _error(template_id, check_no, f"{field} 路径非法（{raw!r}）: {exc}") from exc


def _normalize_path_list(
    template_id: str, check_no: int, field: str, raw: Sequence[str]
) -> list[str]:
    return [_normalize_path_field(template_id, check_no, field, item) for item in raw]


def _check_unique(template_id: str, check_no: int, field: str, paths: Sequence[str]) -> None:
    """同一清单内归一化后不得重复（重复即静默覆盖，必须拒绝）。"""
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            raise _error(template_id, check_no, f"{field} 路径重复: {path}")
        seen.add(path)


def _protected_overlaps(
    protected_paths: Sequence[str], other_paths: Iterable[str]
) -> set[str]:
    """校验 18：受保护路径与其它清单的交叉项（目录前缀按前缀语义判定）。

    返回**交叉到的另一侧路径**（便于错误信息直接指出冲突文件）。
    """
    hits: set[str] = set()
    prefixes = [item for item in protected_paths if item.endswith("/")]
    exact = {item for item in protected_paths if not item.endswith("/")}
    for path in other_paths:
        if path in exact:
            hits.add(path)
            continue
        if any(path.startswith(prefix) for prefix in prefixes):
            hits.add(path)
    return hits


def _normalize_prefix_paths(
    template_id: str, check_no: int, field: str, raw: Sequence[str]
) -> list[str]:
    """归一化「可带尾斜杠的前缀路径」清单（目录语义保留）。

    ★ QA F4：改用 :func:`ymir.common.paths.normalize_scope_path` —— **末段无扩展名的
    路径自动补尾斜杠**。否则模板作者写 ``paths: [tests]``（本意是目录）会退化成
    "精确匹配名为 tests 的文件"，目录保护**静默失效且无报错**。
    显式尾斜杠仍是权威写法；无扩展名文件的边界见 ``looks_like_file_path``。
    """
    normalized: list[str] = []
    for item in raw:
        try:
            normalized.append(normalize_scope_path(str(item)))
        except InvalidParamError as exc:
            raise _error(
                template_id, check_no, f"{field} 路径非法（{item!r}）: {exc}"
            ) from exc
    return normalized


def _validate(spec: TemplateSpec, stem: str, known_roles: set[str] | None) -> TemplateSpec:
    """执行 21 条校验，返回**路径已归一化**的模板（顺序即 docs/11 §7.2.7 表）。"""
    template_id = spec.id or stem

    # 1 · id 必须等于文件名主干
    if not spec.id.strip():
        raise _error(stem, 1, "id 不能为空")
    if spec.id != stem:
        raise _error(template_id, 1, f"id 必须等于文件名主干（{spec.id!r} != {stem!r}）")

    # 2 · version 非空
    if not spec.version.strip():
        raise _error(template_id, 2, "version 不能为空")

    # 3 · 语言必须在允许集合内（v1.3：python | typescript）
    language = spec.stack.language.strip().lower()
    if language not in ALLOWED_LANGUAGES:
        raise _error(
            template_id,
            3,
            f"stack.language 必须为 {sorted(ALLOWED_LANGUAGES)} 之一"
            f"（当前 {spec.stack.language!r}）",
        )

    # 4 · 依赖 ⊆ 该语言的镜像清单（v1.3 语义细化：按 language 选表，python 行为不变）
    image_packages = image_packages_for(language)
    unknown = sorted(
        dep for dep in spec.stack.dependencies if dependency_name(dep) not in image_packages
    )
    if unknown:
        raise _error(
            template_id,
            4,
            f"stack.dependencies 不在 {language} 镜像清单内: "
            f"{unknown}（允许：{sorted(image_packages)}）",
        )

    # 5 · layout 路径字段过 normalize_rel_path
    normalized_layout = LayoutSpec(
        required_dirs=_normalize_path_list(
            template_id, 5, "layout.required_dirs", spec.layout.required_dirs
        ),
        required_files=_normalize_path_list(
            template_id, 5, "layout.required_files", spec.layout.required_files
        ),
        any_of_files=[
            _normalize_path_list(template_id, 5, "layout.any_of_files", group)
            for group in spec.layout.any_of_files
        ],
        forbidden_dirs=_normalize_path_list(
            template_id, 5, "layout.forbidden_dirs", spec.layout.forbidden_dirs
        ),
        snake_case_py=spec.layout.snake_case_py,
        max_file_lines=spec.layout.max_file_lines,
        # allowed_prefixes 是**目录前缀**：归一化会丢掉尾部 "/"，这里补回，
        # 否则 "app" 会误匹配 "application/x.py"（前缀语义必须带分隔符）。
        allowed_prefixes=[
            f"{path.rstrip('/')}/"
            for path in _normalize_path_list(
                template_id, 5, "layout.allowed_prefixes", spec.layout.allowed_prefixes
            )
        ],
        frontend=spec.layout.frontend,
    )

    # 6 · conventions 路径归一化 + 去重
    convention_paths = _normalize_path_list(
        template_id, 6, "conventions[].path", [item.path for item in spec.conventions]
    )
    _check_unique(template_id, 6, "conventions[].path", convention_paths)
    normalized_conventions = [
        ConventionFile(path=path, required=item.required)
        for path, item in zip(convention_paths, spec.conventions, strict=True)
    ]

    # 7 · skeleton 路径归一化 + 去重
    skeleton_paths = _normalize_path_list(
        template_id, 7, "skeleton.files[].path", [item.path for item in spec.skeleton.files]
    )
    _check_unique(template_id, 7, "skeleton.files[].path", skeleton_paths)

    # 8 · 骨架正文非空（纯空白同样视为空；pydantic 只拦得住空串）
    for item, path in zip(spec.skeleton.files, skeleton_paths, strict=True):
        if not item.content.strip():
            raise _error(template_id, 8, f"skeleton 文件正文为空: {path}")

    normalized_skeleton = SkeletonSpec(
        max_files=spec.skeleton.max_files,
        max_bytes=spec.skeleton.max_bytes,
        files=[
            SkeletonFile(path=path, content=item.content, reserved=item.reserved)
            for item, path in zip(spec.skeleton.files, skeleton_paths, strict=True)
        ],
    )

    # 9 · reserved_paths ⊆ skeleton.files 的路径集合
    check_reserved_subset(template_id, normalized_skeleton.reserved_paths, skeleton_paths)

    # 10 · 文件数与总字节上限
    if len(normalized_skeleton.files) > normalized_skeleton.max_files:
        raise _error(
            template_id,
            10,
            f"skeleton 文件数 {len(normalized_skeleton.files)} 超上限 "
            f"{normalized_skeleton.max_files}",
        )
    if normalized_skeleton.total_bytes > normalized_skeleton.max_bytes:
        raise _error(
            template_id,
            10,
            f"skeleton 总字节 {normalized_skeleton.total_bytes} 超上限 "
            f"{normalized_skeleton.max_bytes}",
        )

    # 11 · 骨架内容不得命中密钥指纹（.env.example 只写空值）
    for item in normalized_skeleton.files:
        hit = scan_secret(item.content)
        if hit is not None:
            raise _error(
                template_id,
                11,
                f"骨架文件疑似含真实密钥（命中规则 {hit}）: {item.path}",
            )

    # 12 · 骨架与 conventions 不得交叉（分工：我们提供 vs 模型必须产出）
    overlap = sorted(normalized_skeleton.all_paths & set(convention_paths))
    if overlap:
        raise _error(
            template_id,
            12,
            f"路径同时出现在 skeleton 与 conventions: {overlap}（分工冲突，二选一）",
        )

    # 13 · checks 非空且至少 1 条 command
    if not spec.checks:
        raise _error(template_id, 13, "checks 不能为空（至少 1 条 type=command）")
    if not any(check.type == "command" for check in spec.checks):
        raise _error(template_id, 13, "checks 至少需要 1 条 type=command")

    # 14 · 检查命令离线可复现
    for check in spec.checks:
        lowered = check.run.lower()
        for token in _OFFLINE_FORBIDDEN:
            if token in lowered:
                raise _error(
                    template_id, 14, f"checks[].run 不得含 {token!r}（离线可复现）: {check.run}"
                )

    # 15 · weight > 0（pydantic 已拦，此处显式复核以免默认值被放宽）
    for check in spec.checks:
        if check.weight <= 0:
            raise _error(template_id, 15, f"checks[].weight 必须 > 0: {check.run}")

    # 16 · supports 非空且取值合法
    if not spec.supports:
        raise _error(template_id, 16, "supports 不能为空")
    illegal = sorted(set(spec.supports) - {"from_scratch", "patch"})
    if illegal:
        raise _error(template_id, 16, f"supports 取值非法: {illegal}")

    # 17 · prompting 的键必须是已注册角色 id（known_roles 为空时跳过）
    if known_roles:
        unknown_roles = sorted(set(spec.prompting) - known_roles)
        if unknown_roles:
            raise _error(
                template_id,
                17,
                f"prompting 键不是已注册角色: {unknown_roles}（已注册：{sorted(known_roles)}）",
            )

    # 18 · protected.paths 归一化 + 去重 + 不得与 skeleton / conventions 交叉
    protected_paths = _normalize_prefix_paths(
        template_id, 18, "protected.paths", spec.protected.paths
    )
    _check_unique(template_id, 18, "protected.paths", protected_paths)
    normalized_protected = ProtectedSpec(
        paths=protected_paths,
        on_hit=spec.protected.on_hit,
        max_change_ratio=spec.protected.max_change_ratio,
    )
    overlap_skeleton = sorted(
        _protected_overlaps(protected_paths, normalized_skeleton.all_paths)
    )
    overlap_conventions = sorted(_protected_overlaps(protected_paths, convention_paths))
    if overlap_skeleton or overlap_conventions:
        # 交叉即分工矛盾：骨架已由 reserved 机制保护；conventions 是"模型必须产出"
        raise _error(
            template_id,
            18,
            "protected.paths 与 skeleton/conventions 交叉: "
            f"skeleton={overlap_skeleton} conventions={overlap_conventions}（分工冲突）",
        )

    # 19 · on_hit 取值（pydantic Literal 已拦，此处显式复核防默认值被放宽）
    if normalized_protected.on_hit not in _PROTECTED_POLICIES:
        raise _error(
            template_id, 19, f"protected.on_hit 取值非法: {normalized_protected.on_hit!r}"
        )

    # 20 · max_change_ratio 区间（pydantic gt/le 已拦，同样显式复核）
    ratio = normalized_protected.max_change_ratio
    if not (0 < ratio <= 1):
        raise _error(
            template_id, 20, f"protected.max_change_ratio 必须在 (0, 1] 区间: {ratio}"
        )

    # 21 · scoring 配分完整性（否则配分无法兑现，属"碰巧能跑"的隐性错误）
    points = dict(spec.scoring.runnability_points)
    for group, value in points.items():
        if not str(group).strip():
            raise _error(template_id, 21, "scoring.runnability_points 的组名不能为空")
        if value <= 0:
            raise _error(
                template_id, 21, f"scoring.runnability_points[{group}] 必须 > 0（当前 {value}）"
            )
    needs_frontend = (
        FRONTEND_STATIC_GROUP in points or spec.scoring.frontend_scored_in_runnability
    )
    if needs_frontend and spec.layout.frontend is None:
        raise _error(
            template_id,
            21,
            "声明 frontend_static 配分/frontend_scored_in_runnability 时"
            "必须声明 layout.frontend（否则配分无法兑现）",
        )

    return spec.model_copy(
        update={
            "layout": normalized_layout,
            "conventions": normalized_conventions,
            "skeleton": normalized_skeleton,
            "checks": [
                CheckSpec(
                    type=check.type,
                    run=check.run,
                    weight=check.weight,
                    group=check.group,
                    blocking=check.blocking,
                    requires=check.requires,
                    timeout_s=check.timeout_s,
                )
                for check in spec.checks
            ],
            "protected": normalized_protected,
        }
    )


def load_template_file(path: Path, *, known_roles: set[str] | None = None) -> TemplateSpec:
    """加载并校验单个模板文件；违反任一条校验 → ``TemplateDefinitionError``（1001）。"""
    stem = path.stem
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # YAML 语法错误与结构错误同为 1001
        raise _error(stem, 1, f"YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise _error(stem, 1, "模板文件顶层必须是映射（key: value）")
    try:
        spec = TemplateSpec.model_validate(raw)
    except ValidationError as exc:
        # 拼错字段（如 checks[].kind）/ 类型不符 / 空 content 都在这里被拒
        raise _error(stem, 1, f"模板定义结构非法: {exc}") from exc
    return _validate(spec, stem, known_roles)


def load_template(
    template_id: str,
    *,
    templates_dir: Path | None = None,
    known_roles: set[str] | None = None,
) -> TemplateSpec:
    """按 id 加载模板（不存在 → ``NotFoundError`` 1002；非法 → 1001）。"""
    directory = templates_dir or DEFAULT_TEMPLATES_DIR
    path = directory / f"{template_id}.yaml"
    if not path.exists():
        raise NotFoundError(f"模板不存在: {template_id}")
    if known_roles is None:
        known_roles = default_role_ids()
    return load_template_file(path, known_roles=known_roles)


def list_templates(
    *,
    templates_dir: Path | None = None,
    known_roles: set[str] | None = None,
) -> list[TemplateEntry]:
    """扫描模板目录，返回全部条目（按 id 排序）。

    **坏模板不让整体失败**：其 ``spec`` 为 ``None``、``loading_error`` 带错误原因；
    重复 id（大小写不敏感文件系统上的 ``a.yaml`` / ``A.yaml``）同样记为坏条目。
    """
    directory = templates_dir or DEFAULT_TEMPLATES_DIR
    if known_roles is None:
        known_roles = default_role_ids()
    entries: list[TemplateEntry] = []
    seen: dict[str, str] = {}
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.yaml")):
        stem = path.stem
        previous = seen.get(stem.lower())
        if previous is not None:
            entries.append(
                TemplateEntry(
                    id=stem,
                    spec=None,
                    loading_error=f"模板 id 重复: {stem}（已由 {previous} 占用）",
                )
            )
            continue
        seen[stem.lower()] = path.name
        try:
            spec = load_template_file(path, known_roles=known_roles)
        except InvalidParamError as exc:
            entries.append(TemplateEntry(id=stem, spec=None, loading_error=str(exc)))
            continue
        entries.append(TemplateEntry(id=spec.id, spec=spec))
    return entries
