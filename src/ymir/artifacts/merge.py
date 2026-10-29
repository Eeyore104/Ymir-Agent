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

**v1.3 追加：受保护路径（T1.1）与改动面告警（T1.3）**

- ``protected_paths`` 命中时按 ``on_protected_hit`` 处置：``reject`` 抛
  :class:`ProtectedPathConflict`（唯一抛异常场景，**应用补丁前整批抛**）；
  ``ignore`` 丢弃模型内容、保留基线版本并记 ``conflicts`` / ``counts.protected_hits``；
- ``change_ratio`` / ``warnings`` 只进 manifest，**不改变** ``files``；
- ``manifest_json()`` 采「非默认值才序列化」，未触发新能力时与 v1.2 **逐字节一致**（A4）。

**v1.3.1 追加：引用感知的符号删除检测（T3.2）**

- ``symbol_deletions="off"``（默认）⇒ 检测代码路径**完全不进入** ⇒ 行为与 v1.3
  **逐字节一致**（红线 A9）；
- ``reject`` ⇒ 抛 :class:`SymbolRegressionConflict`（``ProtectedPathConflict`` 的
  子类 ⇒ 编排层三段式零改动复用），同样**应用补丁前整批抛**；
- ``ignore`` ⇒ 丢弃模型内容 / 不执行删除、保留基线版本，记
  ``conflicts(reason="symbol_regression")`` + ``counts.symbol_removals`` +
  短码 ``symbol_regression_ignored:<n>``；
- 受保护路径已命中的路径**短路**符号检测（S4：同一路径只记一条冲突、只烧一次重试）。

**v1.3.1 追加：合入溯源（T5.1）**

- :class:`MergeProvenance`（本模块定义、``artifacts.provenance`` 再导出）随
  ``PatchManifest.provenance`` 落进 ``patch_manifest.json``；
- ``provenance is None`` ⇒ ``manifest_json()`` pop 掉该键 ⇒ 无基线 / ``from_scratch``
  产物与 v1.3 **逐字节一致**（A9）。本模块仍**零 IO**：溯源里的哈希由编排层算好传入。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet  # typing.AbstractSet 已弃用（UP035）
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ymir.artifacts.symbols import SymbolRemoval, detect_symbol_removals
from ymir.artifacts.versioning import compute_content_hash
from ymir.common.contracts import SymbolPolicy
from ymir.common.errors import InvalidParamError, StateConflictError
from ymir.common.paths import normalize_rel_path, normalize_scope_path

#: 单文件大小上限（超过即记 ``too_large`` 并跳过，不阻断整次合并）。
DEFAULT_MAX_FILE_BYTES: int = 512 * 1024

#: ``patch_manifest.json`` 体积**软预算**：仅由测试盯住（v1.4 再谈硬截断）。``src/`` 里零读取点。
MANIFEST_MAX_BYTES: int = 2048

#: 机器簿记文件名（产物内自描述，docs/09 §4.2.2）。编排层写、评测层读，
#: 因此常量放在这里由双方共用，避免两处字符串漂移。
PATCH_MANIFEST_NAME: str = "patch_manifest.json"

#: 冒烟报告文件名（运行簿记，v1.3.1 T5.1）。
#:
#: ★ 与 :data:`PATCH_MANIFEST_NAME` **同待遇**：两个都不是工程源码，计算
#: 「补丁/合并树内容哈希」时必须剔除（``provenance`` 的口径），否则重建会凭空
#: 多出一个文件、哈希必然对不上。字面量与 ``role_executor._SMOKE_REPORT_NAME``
#: 同源（那边是私有常量，``artifacts``(3) -> ``orchestrator``(1) 是逆层，不能 import）。
SMOKE_REPORT_NAME: str = "smoke_report.json"

#: 计算「补丁文件 / 合并树」内容哈希时**必须剔除**的簿记文件（v1.3.1 T5.1 §7.3-7 6b）。
BOOKKEEPING_FILES: frozenset[str] = frozenset({PATCH_MANIFEST_NAME, SMOKE_REPORT_NAME})

#: 冲突原因（``delete_conflict`` 为本实现的补充值：docs/09 §4.2.2 未定义
#: "同一路径既在 ``deleted`` 又有补丁正文" 的处置，此处定为**删除优先**并显式记账）。
#:
#: v1.3（docs/11 §7.2.3）**追加**第 7 个值 ``protected_path``：命中模板声明的
#: 受保护路径（只增不改，历史 manifest 仍可解析）。
#:
#: v1.3.1（docs/13 §7.2.4）**追加**第 8 个值 ``symbol_regression``：删掉了仍被
#: 未改动基线文件引用的公共顶层符号（引用感知的符号删除检测，W3-B′）。
ConflictReason = Literal[
    "delete_missing",
    "path_type_conflict",
    "too_large",
    "invalid_path",
    "reserved",
    "delete_conflict",
    "protected_path",
    "symbol_regression",
]

#: 受保护路径命中策略（v1.3，docs/11 §7.2.3）。
#:
#: 与 ``templates.models.ProtectedSpec.on_hit`` **同构**，但两者各写一份字面量而
#: **不跨层 import**（``artifacts``(3) -> ``templates``(2) 是逆层，CI 拦）——
#: 与 :data:`MergeMode` / ``RunMode`` 的既有做法一致。
ProtectedPolicy = Literal["reject", "ignore"]

#: 符号删除检测策略（v1.3.1 T3.2，docs/13 §7.2.4）。
#:
#: ★ 与 :data:`ProtectedPolicy` 不同：这一份**从 ``common.contracts`` 导入**而不是
#: 本地复写 —— 因为 ``templates``(2) 的 ``ProtectedSpec.symbol_deletions`` 也要用它，
#: 两边都只能向下依赖 common(5)，不会造出逆层边（``ProtectedPolicy`` 只有
#: ``artifacts`` 一侧消费，本地复写反而更省一条边）。取值语义见
#: :data:`ymir.common.contracts.SymbolPolicy`。

#: 改动面告警阈值缺省值（v1.3 T1.3，docs/11 §7.2.3）。
#:
#: 数值与 ``templates.models.ProtectedSpec.max_change_ratio`` 的默认值**同源**
#: （两处注释互指），但同样不跨层 import。
DEFAULT_MAX_CHANGE_RATIO: float = 0.5

#: ``warnings`` 短码前缀（≤3 条、每条 ≤64 字节，docs/11 §7.5-2）。
CHANGE_RATIO_WARNING_PREFIX: str = "change_ratio_exceeded:"
PROTECTED_HIT_WARNING_PREFIX: str = "protected_path_ignored:"
#: 符号删除被 ``ignore`` 兜底时的短码前缀（v1.3.1 T3.3 ④，docs/13 §7.5-1）。
SYMBOL_REGRESSION_WARNING_PREFIX: str = "symbol_regression_ignored:"

#: ``warnings`` 条数与单条字节上限（体积硬约束，docs/11 §7.5-2）。
WARNINGS_MAX_ITEMS: int = 3
WARNING_MAX_BYTES: int = 64

#: 合并模式：``from_scratch`` 骨架全量注入 / ``patch`` 仅补缺失 / ``merge`` 纯基线合并。
MergeMode = Literal["from_scratch", "patch", "merge"]


@dataclass(frozen=True)
class Conflict:
    """一条冲突（跳过而非报错：一个坏路径不该毁掉整次生成，docs/09 §4.2.3）。"""

    path: str
    reason: ConflictReason


class ProtectedPathConflict(StateConflictError):
    """模型输出命中了模板声明的**受保护路径**（v1.3 T1.1，docs/11 §7.3-1）。

    业务码沿用 ``1003``（HTTP 409），**不新增 1008**：语义上它就是「状态冲突」——
    基线版本不可被静默覆盖。``details`` 固定为
    ``{"reason": "protected_path", "paths": [...]}``，供 API 层与审批卡片直接消费。

    ★ 本异常是 :func:`merge_trees` **唯一**抛异常的场景，且命中集合在应用补丁
    **之前**一次性算出并整批抛出（便于编排层把全部路径一次回喂模型）。
    """

    def __init__(
        self,
        paths: Sequence[str],
        message: str = "",
        *,
        reason: str = "protected_path",
    ) -> None:
        unique = tuple(dict.fromkeys(str(path) for path in paths))
        self.paths = unique
        self.reason = reason
        text = message or (
            "命中受保护路径（由基线提供、不允许修改）："
            + "、".join(unique)
            + "；请改为新建文件实现目标"
        )
        super().__init__(text, details={"reason": reason, "paths": list(unique)})


class SymbolRegressionConflict(ProtectedPathConflict):
    """模型删掉了**仍被未改动基线文件引用**的公共顶层符号（v1.3.1 T3.2，docs/13 §7.2.4）。

    ★ **刻意继承** :class:`ProtectedPathConflict`（它的 ``__init__`` 已接受 ``reason``）：
    编排层既有的「``reject`` → 1 次定向重试 → 兜底 ``ignore``」三段式
    （``role_executor._execute_with_patch_contract``）里那个
    ``except ProtectedPathConflict`` 因此**零改动复用**；业务码仍是 ``1003``（409），
    ``details`` 变成 ``{"reason": "symbol_regression", "paths": [...]}``。

    ★ 回喂文案**不在这里**：``role_executor._protected_feedback`` 按 ``reason`` 分支
    （M2）—— 同一条「请改为新建文件」的文案对"删了 ``run``"是错误的引导，模型会
    把子命令搬到新文件、入口留空壳，比不拦更糟。
    """

    def __init__(
        self,
        paths: Sequence[str],
        message: str = "",
        *,
        reason: str = "symbol_regression",
    ) -> None:
        unique = tuple(dict.fromkeys(str(path) for path in paths))
        text = message or (
            "删除了仍被引用的公共符号（这些文件里的公共函数/类仍被其它文件引用）："
            + "、".join(unique)
            + "；请恢复这些符号，或同步更新所有引用后再删除"
        )
        super().__init__(unique, text, reason=reason)


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
    #: 被拦截的受保护路径条数（v1.3 T1.1）：``ignore`` 策略下记数；
    #: ``reject`` 策略直接抛 :class:`ProtectedPathConflict`（不产出 manifest），故不记。
    protected_hits: int = 0
    #: 被拦截的「符号删除」文件数（v1.3.1 T3.2）：同样只在 ``ignore`` 策略下记数
    #: （``reject`` 直接抛 :class:`SymbolRegressionConflict`，不产出 manifest）。
    symbol_removals: int = 0


@dataclass(frozen=True)
class MergeProvenance:
    """合入溯源（v1.3.1 T5.1，docs/13 §7.3-7）：W6 确定性校验的**唯一**依据。

    五元组（基线引用 / 补丁指纹 / 模板指纹）+ 合并树指纹，再加三项「本次实际生效的
    参数」，共 10 个字段（文档口径记作 9 项——``template_id`` 与 ``template_hash``
    合并计一项；但重建时**两者都比**，故字段是 10 个）；
    此外重建还收一个入参 ``max_change_ratio``（**不是本类字段**）。
    **它们不是同一性质**，四分法：``max_file_bytes`` 与 ``symbol_policy`` **参与重建**
    （后者仅单向：``off`` 产物被改成 ``ignore`` / ``reject`` 才会改树或抛），
    ``protected_policy`` **仅作审计**（重建不传 ``protected_paths``，构造上无分支可触发），
    ``max_change_ratio`` **只影响告警**（不改树、不进 ``merged_files_hash``）。
    一刀切既会漏掉真护栏（``symbol_policy``），也会把审计字段误当护栏去写测试
    （``protected_policy`` ⇒ 断言"篡改它必然 1003"**不成立**）；逐条见下方。

    为何要第 6 项 ``merged_files_hash``：``store._read_tree`` 在版本目录丢失时
    **静默返回 ``{}``**（``store.py:257-263``），只校验前五项无法发现"重建出一棵
    空树/残树"；``merged_files_hash`` 是唯一能证明"重建树 == 当时评测树"的字段
    （docs/12 只要求五元组，此为**加强**，不是替代）。

    为何要第 7–9 项（``protected_policy`` / ``symbol_policy`` / ``max_file_bytes``）：
    **三者性质不同，不要一刀切**（v1.3.1 QA 反馈后的实测口径，逐条见下）。

    - ``max_file_bytes``：**参与重建**。传小了会把补丁文件判成 ``too_large`` 丢弃
      ⇒ 树不同（fail-closed，方向安全）。
    - ``symbol_policy``：**参与重建，但只单向** —— 原 run 用 ``off`` 时"删掉仍被
      引用的公共符号"是放行的，产物补丁集里已是删后内容；重建若按 ``ignore`` ⇒
      该文件被回滚成基线版（树不同 ⇒ 1003）、按 ``reject`` ⇒ 直接抛
      :class:`SymbolRegressionConflict`，两种都拿不到当时的树。**反向无害**：
      原 run 为 ``ignore`` 的产物里**根本没有那个被回滚的文件**（回滚 ⇒ 模型内容被
      丢弃 ⇒ 不进补丁集），改成 ``off`` 也无从检出。
      ★ **不要**把 ``symbol_deletions`` 从重建调用里删掉：它是真护栏，删了以后
      "off → ignore"的 manifest 篡改会静默重建出另一棵树。钉子用例：
      ``tests/test_baseline_promote.py::TestSymbolPolicyIsLoadBearing``。
    - ``protected_policy``：**审计字段，不参与重建**。重建**不传**
      ``protected_paths``（``merge_trees`` 默认空集合 ⇒ 永不抛），且产物补丁集在
      写侧已 sanitize（受保护路径的模型内容已被丢弃、进不了补丁集）⇒ 构造上无分支
      可触发。保留它只为"当时用了什么策略"可审计；将来若把原始模型输出也存进产物
      （v1.4 候选），需**同时**改「重建传 ``protected_paths``」才能让它 load-bearing。
      as-built 用例：``TestProtectedPolicyIsAuditOnly``。

    ★ 另有 :func:`~ymir.artifacts.provenance.promote_merged_to_baseline` 的
    ``max_change_ratio`` 入参：它**不是**本类字段（故不在此列），且**不参与重建**
    —— 只影响 ``manifest.warnings``（改动面告警），不进 ``merged_files_hash``、
    不改变一个字节的树。重建时传它只为让"同一输入两次合并"在告警口径上也一致。

    ★ 定义在 ``merge.py`` 而不是 ``provenance.py``：它是 :class:`PatchManifest` 的
    字段类型，而 ``merge`` 是**纯函数模块**（零 IO / 零 DB）—— 让纯模块 import
    服务模块会形成 ``merge ↔ provenance`` 循环，方向也正好反了。
    ``artifacts.provenance`` **再导出**本类，调用方仍可
    ``from ymir.artifacts.provenance import MergeProvenance``。

    ★ 序列化纪律：``PatchManifest.provenance is None``（无基线 / ``from_scratch``）
    时 :meth:`MergeResult.manifest_json` 把该键整个 pop 掉 ⇒ 与 v1.3 **逐字节一致**（A9）。
    """

    #: 基线产物引用（``run.context["baseline"]`` 的 ``artifact_id@version``）
    base_artifact_id: str = ""
    #: ``None`` = 未知（**不许**据此回退到「当前版本」，R9 铁律）
    base_version: int | None = None
    #: 基线快照指纹（= ``PatchManifest.base_content_hash``）
    base_content_hash: str = ""
    #: 补丁文件内容哈希（**不含** ``patch_manifest.json`` / ``smoke_report.json``）
    patch_files_hash: str = ""
    template_id: str = ""
    #: 模板 YAML **原始字节** sha256（§7.3-7b：不用 ``template.version``，那条证明是假的）
    template_hash: str = ""
    #: 合并树内容哈希（同样剔除两个簿记文件）
    merged_files_hash: str = ""
    #: 本次**实际生效**的受保护路径策略：``reject`` | ``ignore``（兜底后可能是 ``ignore``）
    protected_policy: str = ""
    #: 本次**实际生效**的符号删除策略：``off`` | ``reject`` | ``ignore``
    symbol_policy: str = ""
    #: 本次合并实际用的单文件上限（``0`` = 未知 ⇒ 重建时用 ``DEFAULT_MAX_FILE_BYTES``）
    max_file_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的纯字典（键序固定，便于人读与 diff）。"""
        return {
            "base_artifact_id": self.base_artifact_id,
            "base_version": self.base_version,
            "base_content_hash": self.base_content_hash,
            "patch_files_hash": self.patch_files_hash,
            "template_id": self.template_id,
            "template_hash": self.template_hash,
            "merged_files_hash": self.merged_files_hash,
            "protected_policy": self.protected_policy,
            "symbol_policy": self.symbol_policy,
            "max_file_bytes": self.max_file_bytes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> MergeProvenance:
        """从字典还原（字段缺失取默认值，**绝不抛**）。

        宽松解析的理由：本类会被写进产物正文，读到它的可能是旧版本代码 / 手工构造的
        测试数据。缺字段应当降级成"该项未知"，由 :meth:`missing` 与六道校验去拒，
        而不是在反序列化阶段炸掉整条链路（``parse_patch_manifest`` 的既有纪律）。
        """
        data = payload if isinstance(payload, Mapping) else {}
        raw_version = data.get("base_version")
        raw_bytes = data.get("max_file_bytes")

        def _text(key: str) -> str:
            value = data.get(key)
            return "" if value is None else str(value)

        return cls(
            base_artifact_id=_text("base_artifact_id"),
            base_version=raw_version if isinstance(raw_version, int) else None,
            base_content_hash=_text("base_content_hash"),
            patch_files_hash=_text("patch_files_hash"),
            template_id=_text("template_id"),
            template_hash=_text("template_hash"),
            merged_files_hash=_text("merged_files_hash"),
            protected_policy=_text("protected_policy"),
            symbol_policy=_text("symbol_policy"),
            max_file_bytes=raw_bytes if isinstance(raw_bytes, int) else 0,
        )

    def missing(self) -> tuple[str, ...]:
        """返回**缺口**字段名（供 1003 文案说清"到底缺什么"）。

        判据是"重建必需但为空"：基线引用三项 + 两个内容哈希 + 两个生效策略。
        ``template_id`` / ``template_hash`` 为空是**合法**状态（无模板的增量运行），
        因此只有声明了模板却缺哈希时才算缺口。
        """
        gaps: list[str] = []
        if not self.base_artifact_id:
            gaps.append("base_artifact_id")
        if self.base_version is None:
            gaps.append("base_version")
        if not self.base_content_hash:
            gaps.append("base_content_hash")
        if not self.patch_files_hash:
            gaps.append("patch_files_hash")
        if self.template_id and not self.template_hash:
            gaps.append("template_hash")
        if not self.merged_files_hash:
            gaps.append("merged_files_hash")
        if not self.protected_policy:
            gaps.append("protected_policy")
        if not self.symbol_policy:
            gaps.append("symbol_policy")
        return tuple(gaps)


class PatchManifest(BaseModel):
    """产物内的自描述簿记（零迁移：不新增表/列，docs/09 §4.2.2）。

    无时间戳字段是**刻意**的：``generated_at`` 会让"同一输入两次合并字节一致"
    不可成立（T2.1 DoD ③），而时间信息由产物落盘的 ``created_at`` 承担。

    v1.3（T1.3）追加两个**带默认值**的字段，并由 :meth:`MergeResult.manifest_json`
    做「非默认值才序列化」—— 未触发新能力时 ``patch_manifest.json`` 与 v1.2
    **逐字节一致**（契约 A4，docs/11 §7.2.3）。
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
    #: 改动面 = ``len(changed_paths) / max(1, 基线文件数)``，保留 2 位小数（T1.3）。
    #: 无基线（``from_scratch``）恒为 ``0.0``。
    change_ratio: float = 0.0
    #: 机器短码告警（≤3 条、每条 ≤64 字节）。已知取值：
    #: ``change_ratio_exceeded:<ratio>`` / ``protected_path_ignored:<n>``。
    warnings: list[str] = Field(default_factory=list)
    #: 合入溯源（v1.3.1 T5.1，docs/13 §7.3-7）。**默认 ``None``** ⇒ 无基线 /
    #: ``from_scratch`` 产物的正文与 v1.3 **逐字节一致**（A9）；
    #: 有增量合并时由 ``role_executor`` 在返回前写入（冒烟之前）。
    provenance: MergeProvenance | None = None


@dataclass(frozen=True)
class MergeResult:
    """合并结果：合并后的树 + 可序列化的机器簿记（``PatchManifest``）。"""

    files: dict[str, str]
    manifest: PatchManifest
    #: ``symbol_deletions="ignore"`` 时被拦下的符号删除明细（v1.3.1 T3.2）。
    #:
    #: ★ 这是**纯内存**的附加字段（默认空元组 ⇒ 与 v1.3 逐字节/逐行为一致）：
    #: manifest 只记条数（``counts.symbol_removals``），而审批卡片需要知道
    #: "到底是哪几个函数没了"。放在这里而不是重新检测一遍，是因为
    #: ``detect_symbol_removals`` 的结论依赖"补丁是否触及"这一**调用时**状态，
    #: 事后重算会得到可能不同的答案（且多一次全树 AST 解析）。
    #: **不参与任何序列化**：``manifest_json()`` 只读 ``manifest``。
    symbol_removals: tuple[SymbolRemoval, ...] = ()

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
        """``patch_manifest.json`` 正文（键排序 + 紧凑分隔符 ⇒ 字节稳定）。

        ★★ **A4 字节级契约**：v1.3 新增字段采用「**非默认值才序列化**」——
        ``change_ratio == 0.0`` / ``warnings`` 为空 / ``counts.protected_hits == 0``
        / ``counts.symbol_removals == 0``
        时把对应键 pop 掉，使未触发新能力的输入产出的正文与 v1.2 **逐字节相同**。

        ★ 删除下面任何一个 ``pop`` 判断都等于**破坏 A4 契约**：
        历史批次报告与前端 diff 会立刻出现"看起来改过、实际没改"的噪音
        （docs/11 §7.7-R6，回归用例见 ``tests/test_patch.py`` 的逐字节快照）。
        """
        payload = self.manifest.model_dump(mode="json")
        if self.manifest.change_ratio == 0.0:
            payload.pop("change_ratio", None)
        if not self.manifest.warnings:
            payload.pop("warnings", None)
        # v1.3.1 T5.1：无溯源（无基线 / from_scratch）时该键必须整个消失，
        # 否则「未配置 ⇒ 与 v1.3 逐字节一致」（红线 A9）立刻破功。
        if self.manifest.provenance is None:
            payload.pop("provenance", None)
        counts = payload.get("counts")
        if isinstance(counts, dict):
            if not counts.get("protected_hits"):
                counts.pop("protected_hits", None)
            # v1.3.1 T3.2：``symbol_deletions="off"``（默认）时该键必须整个消失，
            # 否则「未配置 ⇒ 与 v1.3 逐字节一致」（红线 A9）立刻破功。
            if not counts.get("symbol_removals"):
                counts.pop("symbol_removals", None)
        return json.dumps(
            payload,
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


# ------------------------------------------------------- 受保护路径 / 改动面（v1.3 T1.1/T1.3）


def detect_protected_hits(
    patch_paths: AbstractSet[str],
    protected_paths: AbstractSet[str],
) -> tuple[str, ...]:
    """返回命中的受保护路径（排序、去重）。纯函数，无 IO。

    匹配规则（与 ``layout.allowed_prefixes`` 的尾斜杠约定同源）：

    - ``protected`` 以 ``/`` 结尾 ⇒ **目录前缀**：``p == d`` / ``p.startswith(d)`` /
      ★ ``p == d.rstrip("/")``；
    - 否则 ⇒ **精确文件**：``p == protected``。

    ★ 第三条（F6，QA P3-2）解决"**无扩展名文件**无法被保护"的漏洞：
    ``normalize_scope_path`` 会把 ``Dockerfile``（末段无扩展名）当目录补成
    ``Dockerfile/``，而 ``Dockerfile == "Dockerfile/"`` 与
    ``"Dockerfile".startswith("Dockerfile/")`` **都不成立** ⇒ 改写完全不拦且无报错。
    加上 ``p == d.rstrip("/")`` 后，``Dockerfile`` 这种声明既能保护**该文件本身**，
    也继续保护以它为名的目录内容（``Dockerfile/extra``）。

    返回的是**补丁侧**的路径（而不是保护声明的字面量），因为回喂模型时必须
    给出它实际写过的文件名（``tests/conftest.py``），不能只给目录。

    ★ 本函数只做**路径匹配**；「内容是否真的变了」由调用方过滤（QA F2：
    ``merge_trees`` 只把 ``patch[p] != base[p]`` 的路径传给本函数，原样回显交给
    noop 语义吃掉）。
    """
    hits: set[str] = set()
    prefixes = tuple(item for item in protected_paths if item.endswith("/"))
    # 目录项的「本名」形态：`Dockerfile/` → `Dockerfile`（保护无扩展名单文件，F6）
    prefix_names = frozenset(item.rstrip("/") for item in prefixes)
    exact = frozenset(item for item in protected_paths if not item.endswith("/"))
    for path in patch_paths:
        if path in exact or path in prefix_names:
            hits.add(path)
            continue
        if any(path.startswith(item) for item in prefixes):
            hits.add(path)
    return tuple(sorted(hits))


def compute_change_ratio(changed_paths: Sequence[str], base_file_count: int) -> float:
    """改动面 = ``len(changed_paths) / max(1, base_file_count)``，四舍五入 2 位。

    无基线（``base_file_count <= 0``，即 ``from_scratch``）返回 ``0.0`` —— 从零生成
    没有"改动面"这一概念，报一个非零值只会误导人审。
    """
    if base_file_count <= 0:
        return 0.0
    unique = len(set(changed_paths))
    return round(unique / max(1, base_file_count), 2)


def _normalize_protected(paths: AbstractSet[str]) -> frozenset[str]:
    """归一化受保护路径（目录前缀保留 / 自动补尾斜杠）。

    与模板加载期校验 18 **同一实现**（:func:`ymir.common.paths.normalize_scope_path`）：
    ``tests`` / ``tests/`` 都归一为 ``tests/``，``tests/conftest.py`` 保持精确匹配。

    非法路径**静默丢弃**而不是抛异常：模板加载期校验 18 已保证合法，这里的
    归一化只是防御（``merge_trees`` 的既有纪律是"模型笔误不毁整次生成"）。
    """
    normalized: set[str] = set()
    for raw in paths:
        text = str(raw).strip()
        if not text:
            continue
        try:
            normalized.add(normalize_scope_path(text))
        except InvalidParamError:
            continue
    return frozenset(normalized)


def _bounded_warnings(codes: Sequence[str]) -> list[str]:
    """裁剪告警短码（≤3 条、每条 ≤64 字节，且去重保序）。"""
    result: list[str] = []
    for raw in codes:
        code = str(raw)
        encoded = code.encode("utf-8")
        if len(encoded) > WARNING_MAX_BYTES:
            code = encoded[:WARNING_MAX_BYTES].decode("utf-8", "ignore")
        if code and code not in result:
            result.append(code)
        if len(result) >= WARNINGS_MAX_ITEMS:
            break
    return result


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
    protected_paths: AbstractSet[str] = frozenset(),
    on_protected_hit: ProtectedPolicy = "reject",
    max_change_ratio: float = DEFAULT_MAX_CHANGE_RATIO,
    symbol_deletions: SymbolPolicy = "off",
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
        protected_paths: 受保护路径（v1.3 T1.1；纯数据，元素语义同
            ``layout.allowed_prefixes``：尾斜杠 = 目录前缀，经
            ``common.paths.normalize_scope_path`` 归一化 —— ``tests`` 与 ``tests/``
            等价）。**空集合 ⇒ 永不抛出**，v1.0–v1.2 行为逐字节不变。
            ★ **本函数不看 ``mode``**：``from_scratch`` 是否启用保护由**编排层**决定
            （``role_executor._apply_patch_contract`` 的 ``and base_files``）——
            纯函数只回答"给了这组参数怎么合"，不猜调用方意图（QA 澄清项）。
        on_protected_hit: ``reject``（默认）= 抛 :class:`ProtectedPathConflict`；
            ``ignore`` = 丢弃模型内容、保留基线版本、记 manifest。
        max_change_ratio: 改动面告警阈值（v1.3 T1.3，``(0, 1]``）。超阈值只记
            ``warnings``，**不阻断**合并。
        symbol_deletions: 符号删除检测策略（v1.3.1 T3.2）。``off``（默认）⇒
            检测代码路径完全不进入 ⇒ 行为与 v1.3 **逐字节一致**；``reject`` ⇒
            抛 :class:`SymbolRegressionConflict`；``ignore`` ⇒ 丢弃模型内容、
            保留基线版本、记 ``conflicts`` / ``counts.symbol_removals``。
            ★ 与 ``protected_paths`` 同纪律：**只要命中就在应用补丁之前抛出**。

    Returns:
        :class:`MergeResult`：``files``（合并后树）+ ``manifest``（可序列化记账）。

    Raises:
        InvalidParamError: ``mode`` 非法 / ``max_file_bytes`` 非正；
            v1.3 追加：``max_change_ratio`` 不在 ``(0, 1]`` / ``on_protected_hit`` 非法；
            v1.3.1 追加：``symbol_deletions`` 非法。
        ProtectedPathConflict: 命中 ``protected_paths`` 且策略为 ``reject``
            （1003 / HTTP 409，``details={"reason":"protected_path","paths":[...]}``）。
        SymbolRegressionConflict: 命中「删掉仍被引用的公共符号」且策略为 ``reject``
            （``ProtectedPathConflict`` 的子类，1003 / HTTP 409，
            ``details={"reason":"symbol_regression","paths":[...]}``）。

    Note:
        ``base`` / ``patch`` / ``skeleton`` **不会被原地修改**（内部先复制）。

        v1.3 三段式处置（docs/11 §7.3-4）：本函数只负责**第一段**（``reject`` 抛出）；
        「1 次定向重试」与「兜底 ``ignore``」由编排层
        （``orchestrator.role_executor._execute_with_patch_contract``）承担。

        v1.3.1（S4）：``protected_paths`` 已命中的路径**短路**符号检测 —— 同一路径
        只记一条冲突、只消耗一次定向重试；``protected_path`` 语义更强（整文件不可动），
        优先。
    """
    if mode not in ("from_scratch", "patch", "merge"):
        raise InvalidParamError(f"非法合并模式: {mode!r}")
    if max_file_bytes <= 0:
        raise InvalidParamError(f"max_file_bytes 必须为正数: {max_file_bytes}")
    if on_protected_hit not in ("reject", "ignore"):
        raise InvalidParamError(f"非法受保护路径策略: {on_protected_hit!r}")
    if symbol_deletions not in ("off", "reject", "ignore"):
        raise InvalidParamError(f"非法符号删除策略: {symbol_deletions!r}")
    if not (0 < max_change_ratio <= 1):
        raise InvalidParamError(
            f"max_change_ratio 必须在 (0, 1] 区间: {max_change_ratio}"
        )

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

    # ---- 受保护路径判定（v1.3 T1.1）：应用补丁**之前**一次性算全量并整批处置 ----
    # ★ F2（QA P2）：**只有内容真的变了才算命中**。
    # 模型把基线文件原样回显（``patch[p] == base[p]``）属于「未修改」，v1.2 的 noop
    # 语义本就吃掉它；若在此判成命中，则 reject 会白烧一次全量模型重试、ignore 会产出
    # ``protected_path_ignored:1`` 的假噪音。**这不是放松约束**：内容一字节没变，
    # 受保护文件就没有被改动。
    # ``deleted`` 通道**无条件命中**：删除是不可逆的破坏，不能靠"内容相同"豁免。
    protected = _normalize_protected(protected_paths)
    changed_patch_paths = frozenset(
        path
        for path, content in patch_norm.items()
        if path not in base_norm or base_norm[path] != content
    )
    protected_hits: frozenset[str] = frozenset(
        detect_protected_hits(
            changed_patch_paths | frozenset(deleted_paths), protected
        )
    )
    if protected_hits and on_protected_hit == "reject":
        # 整批抛出（不是"抛第一个"）：回喂模型时必须给出全部命中路径。
        raise ProtectedPathConflict(sorted(protected_hits))
    for path in sorted(protected_hits):
        conflicts.append(Conflict(path=path, reason="protected_path"))

    known_base_paths: frozenset[str] = (
        frozenset(base_paths) if base_paths else frozenset(base_norm)
    )
    reserved: frozenset[str] = frozenset(reserved_paths)
    deleted_set = set(deleted_paths)

    files: dict[str, str] = dict(base_norm)
    counts = PatchCounts()
    counts.protected_hits = len(protected_hits)
    added_paths: list[str] = []
    modified_paths: list[str] = []
    applied_deleted: list[str] = []
    skeleton_added: list[str] = []
    reserved_conflicts: list[str] = []
    skipped_existing: list[str] = []

    # ---- 骨架决策（优先级表 1/2/3；mode="merge" 一律不注入）----
    inject: dict[str, str] = {}
    drop_model: set[str] = set(protected_hits)

    # ---- 符号删除检测（v1.3.1 T3.2）：与受保护路径**同一纪律**，应用补丁之前处置 ----
    # ★ 短码**不在**这里新建 list：只算出 ``symbol_warning`` 字符串，在后面的
    #   「改动面与告警」段与 ``change_ratio`` 短码一起并入 ``warning_codes``
    #   （最终顺序：forced_ignore 由编排层前置 → symbol → change_ratio），
    #   即 ``warning_codes`` 仍只有那**一个**初始化点。
    symbol_warning: str = ""
    symbol_removals: list[SymbolRemoval] = []
    #: 符号删除命中（``ignore`` 策略）的路径。**独立于 ``drop_model``** 的原因是
    #: 删除通道也要用它：``drop_model`` 里还混着 ``reserved`` 冲突的路径，复用会把
    #: "reserved 文件同时在 deleted 里" 从 ``delete_conflict`` 静默改成"跳过删除"
    #: ⇒ 改变 v1.3 既有行为。只让符号命中影响删除，既有语义零变化。
    symbol_hit_paths: set[str] = set()
    if symbol_deletions != "off" and base_norm:
        detected = detect_symbol_removals(base_norm, patch_norm, deleted_paths)
        # ★ S4：受保护路径已命中的路径不再算符号冲突 —— 同一路径只记一条冲突、
        #   只消耗一次定向重试。protected 优先（它语义更强：整文件不可动）。
        symbol_removals = [item for item in detected if item.path not in protected_hits]
        if symbol_removals:
            symbol_paths = tuple(item.path for item in symbol_removals)
            if symbol_deletions == "reject":
                # 整批抛出（不是"抛第一个"）：回喂模型时必须给出全部命中路径
                raise SymbolRegressionConflict(sorted(symbol_paths))
            for path in sorted(symbol_paths):
                # ignore：丢弃模型内容、保留基线版本（``files`` 里已有基线正文）
                conflicts.append(Conflict(path=path, reason="symbol_regression"))
                drop_model.add(path)
                symbol_hit_paths.add(path)
            counts.symbol_removals = len(symbol_paths)
            symbol_warning = f"{SYMBOL_REGRESSION_WARNING_PREFIX}{len(symbol_paths)}"

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
        if path in protected_hits:
            # ignore 策略：丢弃模型内容、保留基线版本（files 里已有 base_norm 的内容）
            continue
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
        if path in protected_hits:
            # 受保护路径不可删除：与"改内容"同口径，保留基线版本
            continue
        if path in symbol_hit_paths:
            # 符号删除被 ignore 兜底：删除**同样**不生效（否则"保留基线版本"不成立，
            # 引用方的 import 仍会 ImportError）。docs/13 §7.3-4 的示例代码只写了
            # ``drop_model.add``，漏掉了删除通道 —— 此处补齐。
            continue
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

    # ---- 改动面与告警（v1.3 T1.3）：只写 manifest，**不改变** files ----
    change_ratio = compute_change_ratio(changed_paths, len(base_norm))
    warning_codes: list[str] = []
    if symbol_warning:
        # v1.3.1 T3.2：符号短码排在 change_ratio 之前（它是更强的降级信号），
        # 且排在编排层前置的 forced_ignore 之后 —— 见本函数「符号删除检测」段的说明。
        warning_codes.append(symbol_warning)
    if protected_hits and on_protected_hit == "ignore":
        warning_codes.append(f"{PROTECTED_HIT_WARNING_PREFIX}{len(protected_hits)}")
    if len(base_norm) > 0 and change_ratio > max_change_ratio:
        # 超阈值只提示、不阻断（T1.3 DoD ④：提示而非拒绝）
        warning_codes.append(f"{CHANGE_RATIO_WARNING_PREFIX}{change_ratio:.2f}")

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
        change_ratio=change_ratio,
        warnings=_bounded_warnings(warning_codes),
    )
    return MergeResult(
        files=files,
        manifest=manifest,
        symbol_removals=tuple(symbol_removals),
    )
