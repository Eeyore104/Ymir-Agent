"""增量合并纯函数（v1.2，docs/09 §4.2.3）。

**纪律（违反即 CI 失败或设计失效）**

- **纯函数**：无 IO、无 DB、无网络、无时钟 —— 同一输入两次合并字节一致；
- **只收纯数据**：``Mapping[str, str]`` / ``AbstractSet[str]`` / ``Sequence[str]``；
  **禁止** ``from ymir.templates import ...``（``artifacts``(rank 3) -> ``templates``(rank 2)
  是逆层，docs/09 §11-R12 用 AST 断言拦截）；
- **永不写用户磁盘**：基线不可变、补丁不可变、合并树是内存派生（docs/09 §3.2-K3）。

**骨架注入的优先级（唯一答案，docs/09 §4.1.3 优先级表）**

1. ``p ∈ base_paths``（基线已有该文件）→ 骨架完全不参与（用户文件优先），
   记 ``skipped_existing``；
2. ``p ∈ reserved_paths`` 且 ``p ∉ base_paths`` 且模型输出该路径 → 丢弃模型内容、
   保留骨架，记 ``reserved_conflicts``（warning，**不阻断**；该路径同时计入
   ``skeleton_added``，因为它确实由骨架落位）；
3. ``p ∈ skeleton``（seed）且未经 1/2 且模型未输出 → 补齐骨架，记 ``skeleton_added``；
4. 其它模型输出路径 → 按 §4.2.2 推导 ``added`` / ``modified`` / ``noop``。

判定**只在**本函数内部完成（依据只有 ``base_paths`` 与 ``reserved_paths`` 两个纯参数）：
不在 ``templates``（模板不该知道基线状态）、不在 ``role_executor``（编排只解数据不做策略）、
不在 ``store``（store 只负责不可变落盘）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet  # typing.AbstractSet 已弃用（UP035）
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ymir.artifacts.versioning import compute_content_hash
from ymir.common.errors import InvalidParamError
from ymir.common.paths import normalize_rel_path

#: 单文件大小上限（超过即记 ``too_large`` 并跳过，不阻断整次合并）。
DEFAULT_MAX_FILE_BYTES: int = 512 * 1024

#: ``patch_manifest.json`` 体积硬约束（它是机器簿记文件，会被下游提示词当普通文件注入）。
MANIFEST_MAX_BYTES: int = 2048

#: 机器簿记文件名（产物内自描述，docs/09 §4.2.2）。编排层写、评测层读，
#: 因此常量放在这里由双方共用，避免两处字符串漂移。
PATCH_MANIFEST_NAME: str = "patch_manifest.json"

#: 冲突原因（``delete_conflict`` 为本实现的补充值：docs/09 §4.2.2 未定义
#: "同一路径既在 ``deleted`` 又有补丁正文" 的处置，此处定为**删除优先**并显式记账）。
ConflictReason = Literal[
    "delete_missing",
    "path_type_conflict",
    "too_large",
    "invalid_path",
    "reserved",
    "delete_conflict",
]

#: 合并模式：``from_scratch`` 骨架全量注入 / ``patch`` 仅补缺失 / ``merge`` 纯基线合并。
MergeMode = Literal["from_scratch", "patch", "merge"]


@dataclass(frozen=True)
class Conflict:
    """一条冲突（跳过而非报错：一个坏路径不该毁掉整次生成，docs/09 §4.2.3）。"""

    path: str
    reason: ConflictReason


class ManifestConflict(BaseModel):
    """``patch_manifest.json`` 里的一条冲突（可 ``model_dump`` 往返）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    reason: ConflictReason


class PatchCounts(BaseModel):
    """各类计数的汇总（进审批卡片摘要，docs/09 §13「报告纪律」）。"""

    model_config = ConfigDict(extra="forbid")

    added: int = 0
    modified: int = 0
    noop_dropped: int = 0
    deleted: int = 0
    skeleton_added: int = 0
    reserved_conflicts: int = 0
    skipped_existing: int = 0


class PatchManifest(BaseModel):
    """产物内的自描述簿记（零迁移：不新增表/列，docs/09 §4.2.2）。

    无时间戳字段是**刻意**的：``generated_at`` 会让"同一输入两次合并字节一致"
    不可成立（T2.1 DoD ③），而时间信息由产物落盘的 ``created_at`` 承担。
    """

    model_config = ConfigDict(extra="forbid")

    mode: MergeMode = "merge"
    base_content_hash: str = Field(default="", description="基线快照指纹（无基线时为空）")
    counts: PatchCounts = Field(default_factory=PatchCounts)
    changed_paths: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    conflicts: list[ManifestConflict] = Field(default_factory=list)
    skeleton_added: list[str] = Field(default_factory=list)
    reserved_conflicts: list[str] = Field(default_factory=list)
    skipped_existing: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class MergeResult:
    """合并结果：合并后的树 + 可序列化的机器簿记（``PatchManifest``）。"""

    files: dict[str, str]
    manifest: PatchManifest

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """变更路径（新增 + 修改 + 删除 + 骨架补齐），排序后不可变序列。"""
        return tuple(self.manifest.changed_paths)

    @property
    def deleted(self) -> tuple[str, ...]:
        """实际生效的删除路径。"""
        return tuple(self.manifest.deleted)

    @property
    def conflicts(self) -> tuple[Conflict, ...]:
        """冲突清单（``Conflict`` 视图，便于编排层直接消费）。"""
        return tuple(
            Conflict(path=item.path, reason=item.reason) for item in self.manifest.conflicts
        )

    def manifest_json(self) -> str:
        """``patch_manifest.json`` 正文（键排序 + 紧凑分隔符 ⇒ 字节稳定）。"""
        return json.dumps(
            self.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def parse_patch_manifest(raw: str | None) -> PatchManifest | None:
    """解析产物内的 ``patch_manifest.json`` 正文。

    缺失 / 非法 JSON / 结构不符一律返回 ``None``（下游据此走"非增量"路径）。
    刻意**不抛异常**：旧产物（v1.0/v1.1）根本没有这个文件，评测与导出都不能因此失败。
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return PatchManifest.model_validate(payload)
    except ValueError:
        return None


def _normalize_lenient(files: Mapping[str, str]) -> tuple[dict[str, str], list[Conflict]]:
    """逐路径归一化（非法路径记冲突而不是抛异常）。

    与 :func:`ymir.artifacts.versioning.normalize_files` 的区别：那份用于**入库**
    （必须 fail-fast），本份用于**合并**（模型笔误不该毁掉整次生成）。归一化本身
    仍复用 ``normalize_rel_path``，不另写路径校验（docs/09 §13「路径安全」）。
    """
    normalized: dict[str, str] = {}
    conflicts: list[Conflict] = []
    for raw_path in sorted(files):
        content = files[raw_path]
        try:
            path = normalize_rel_path(raw_path)
        except InvalidParamError:
            conflicts.append(Conflict(path=str(raw_path), reason="invalid_path"))
            continue
        if path in normalized:
            # 归一化后撞名（如 a/./b 与 a/b）：保留先出现者，记结构冲突
            conflicts.append(Conflict(path=path, reason="path_type_conflict"))
            continue
        normalized[path] = content
    return normalized, conflicts


def _normalize_deleted(deleted: Sequence[str]) -> tuple[list[str], list[Conflict]]:
    """删除清单归一化（非法路径记冲突）。"""
    paths: list[str] = []
    conflicts: list[Conflict] = []
    seen: set[str] = set()
    for raw_path in deleted:
        try:
            path = normalize_rel_path(raw_path)
        except InvalidParamError:
            conflicts.append(Conflict(path=str(raw_path), reason="invalid_path"))
            continue
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return sorted(paths), conflicts


def _prefix_conflict(tree: AbstractSet[str], path: str) -> str | None:
    """文件与目录同名检测（复用 ``versioning.py`` 的前缀冲突口径）。

    返回冲突对方路径；无冲突返回 ``None``。``tree`` 可同时包含"已知文件"与
    "仅知存在的基线路径"（后者用于阶段一：只有路径清单、没有基线正文）。
    """
    parts = path.split("/")
    for index in range(1, len(parts)):
        ancestor = "/".join(parts[:index])
        if ancestor in tree:
            return ancestor
    prefix = f"{path}/"
    for existing in tree:
        if existing.startswith(prefix):
            return existing
    return None


def merge_trees(
    base: Mapping[str, str],
    patch: Mapping[str, str],
    *,
    mode: MergeMode,
    skeleton: Mapping[str, str] | None = None,
    reserved_paths: AbstractSet[str] = frozenset(),
    base_paths: AbstractSet[str] = frozenset(),
    deleted: Sequence[str] = (),
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> MergeResult:
    """合并基线树与补丁树（纯函数），并按优先级表处理骨架注入。

    Args:
        base: 基线文件集（``from_scratch`` / 阶段一传 ``{}``）。
        patch: 模型产出的补丁文件集（新增/修改给全文）。
        mode: ``from_scratch`` 骨架全量注入；``patch`` 骨架仅补缺失；
            ``merge`` 纯基线合并（**不注入骨架**）。
        skeleton: 骨架文件集（纯数据，由 rank 1 从 ``TemplateSpec`` 解出）。
        reserved_paths: 模板固定提供的路径（模型写同路径一律丢弃）。
        base_paths: "基线是否已有该路径"的判定依据；缺省 = ``set(base)``。
            阶段一只给路径清单而不给正文时显式传入。
        deleted: 要从基线删除的相对路径（仅 ``patch`` / ``merge`` 有意义）。
        max_file_bytes: 单文件字节上限，超限记 ``too_large`` 并跳过。

    Returns:
        :class:`MergeResult`：``files``（合并后树）+ ``manifest``（可序列化记账）。

    Raises:
        InvalidParamError: ``mode`` 非法 / ``max_file_bytes`` 非正。

    Note:
        ``base`` / ``patch`` / ``skeleton`` **不会被原地修改**（内部先复制）。
    """
    if mode not in ("from_scratch", "patch", "merge"):
        raise InvalidParamError(f"非法合并模式: {mode!r}")
    if max_file_bytes <= 0:
        raise InvalidParamError(f"max_file_bytes 必须为正数: {max_file_bytes}")

    base_norm, base_conflicts = _normalize_lenient(base)
    patch_norm, patch_conflicts = _normalize_lenient(patch)
    skeleton_norm, skeleton_conflicts = _normalize_lenient(skeleton or {})
    deleted_paths, deleted_conflicts = _normalize_deleted(deleted)

    conflicts: list[Conflict] = [
        *base_conflicts,
        *patch_conflicts,
        *skeleton_conflicts,
        *deleted_conflicts,
    ]

    known_base_paths: frozenset[str] = (
        frozenset(base_paths) if base_paths else frozenset(base_norm)
    )
    reserved: frozenset[str] = frozenset(reserved_paths)
    deleted_set = set(deleted_paths)

    files: dict[str, str] = dict(base_norm)
    counts = PatchCounts()
    added_paths: list[str] = []
    modified_paths: list[str] = []
    applied_deleted: list[str] = []
    skeleton_added: list[str] = []
    reserved_conflicts: list[str] = []
    skipped_existing: list[str] = []

    # ---- 骨架决策（优先级表 1/2/3；mode="merge" 一律不注入）----
    inject: dict[str, str] = {}
    drop_model: set[str] = set()
    if mode != "merge":
        for path in sorted(skeleton_norm):
            content = skeleton_norm[path]
            if path in known_base_paths:
                # 优先级 1：基线已有该文件 —— 骨架完全不参与（用户文件优先）
                skipped_existing.append(path)
                continue
            if path in reserved and path in patch_norm:
                # 优先级 2：reserved 被模型写 —— 丢弃模型内容、保留骨架（warning）
                drop_model.add(path)
                reserved_conflicts.append(path)
                inject[path] = content
                continue
            # 优先级 3：seed 只在"模型没输出"时补齐（模型输出则用模型的）
            inject[path] = content

    # ---- 补丁应用（新增 / 修改 / noop / 冲突）----
    for path in sorted(patch_norm):
        content = patch_norm[path]
        if path in deleted_set:
            # 同一路径既声明删除又给了正文：删除优先（显式删除意图最强）
            conflicts.append(Conflict(path=path, reason="delete_conflict"))
            continue
        if path in drop_model:
            continue  # reserved 保护：模型内容已丢弃，骨架稍后落位
        if len(content.encode("utf-8")) > max_file_bytes:
            conflicts.append(Conflict(path=path, reason="too_large"))
            continue
        if path in base_norm and base_norm[path] == content:
            counts.noop_dropped += 1  # 与基线逐字节相同：自动退化为增量，不报错
            continue
        tree_view = set(files) | known_base_paths
        blocker = _prefix_conflict(tree_view, path)
        if blocker is not None:
            conflicts.append(Conflict(path=path, reason="path_type_conflict"))
            continue
        files[path] = content
        if path in base_norm or path in known_base_paths:
            modified_paths.append(path)
            counts.modified += 1
        else:
            added_paths.append(path)
            counts.added += 1

    # ---- 删除应用（指向不存在路径只记账，不报错）----
    for path in deleted_paths:
        if path not in base_norm and path not in known_base_paths:
            conflicts.append(Conflict(path=path, reason="delete_missing"))
            continue
        applied_deleted.append(path)
        counts.deleted += 1
        files.pop(path, None)

    # ---- 骨架落位（只补"基线没有且模型没给"的路径）----
    for path in sorted(inject):
        if path in files:
            continue  # 基线已有（优先级 1）或模型已输出（seed 被覆盖）
        content = inject[path]
        if len(content.encode("utf-8")) > max_file_bytes:
            conflicts.append(Conflict(path=path, reason="too_large"))
            continue
        blocker = _prefix_conflict(set(files), path)
        if blocker is not None:
            conflicts.append(Conflict(path=path, reason="path_type_conflict"))
            continue
        files[path] = content
        # 骨架内容确实进了产物 → 一律计入 skeleton_added（含"模型写了 reserved
        # 被丢弃后由骨架落位"的路径，避免"树里有、manifest 没提"的隐性路径）
        skeleton_added.append(path)
        counts.skeleton_added += 1

    changed_paths = sorted(
        {
            *added_paths,
            *modified_paths,
            *applied_deleted,
            *skeleton_added,
        }
    )
    counts.reserved_conflicts = len(reserved_conflicts)
    counts.skipped_existing = len(skipped_existing)

    manifest = PatchManifest(
        mode=mode,
        base_content_hash=compute_content_hash(base_norm) if base_norm else "",
        counts=counts,
        changed_paths=changed_paths,
        deleted=sorted(applied_deleted),
        conflicts=[
            ManifestConflict(path=item.path, reason=item.reason) for item in conflicts
        ],
        skeleton_added=sorted(skeleton_added),
        reserved_conflicts=sorted(reserved_conflicts),
        skipped_existing=sorted(skipped_existing),
    )
    return MergeResult(files=files, manifest=manifest)
