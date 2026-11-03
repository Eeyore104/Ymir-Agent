"""合入溯源与「以合入结果为新基线」（v1.3.1 T5.1 / T5.2，docs/13 §7.3-7）。

**要解决的问题（W6 / R9④ 的补落）**

增量 run 的产物只存**增量**（``patch_manifest.json`` + 变更文件），基线是另一个
不可变产物。用户想让"这次合入的结果"成为**新基线**时，服务端必须能从这两份纯数据
**确定性地重建**出当时的评测树 —— 重建得出来才敢落盘为新基线，否则宁可拒绝。

因此本模块做两件事：

1. :class:`MergeProvenance`：把"重建所需的全部输入"写进产物自描述
   （基线引用 + 补丁指纹 + 模板指纹 + 合并树指纹 + **本次实际生效的参数**）；
   其中 ``max_file_bytes`` / ``symbol_policy`` **参与重建**，``protected_policy``
   仅作审计（重建不传 ``protected_paths`` ⇒ 无分支可触发，见步骤 6 的注释）；
2. :func:`promote_merged_to_baseline`：读回来，**六道校验**逐条比对，
   全部通过后用 ``merge_trees`` 重建，再与当时的合并树指纹逐字节比对，最后落成
   新基线版本（体积上限在 ``store.save`` **之前**拦，绝不落半成品）。

**六道校验各自的拒绝语义（业务码零新增，只用 1001/1002/1003）**

====================  ==================================================
校验                  拒绝原因（1003 = 状态冲突，409）
====================  ==================================================
不是增量产物          1001（``mode != "patch"`` / 无 manifest）
无溯源（老产物）      1003（v1.3.1 之前产生的产物，无法安全重建）
基线引用 / 指纹不符    1003（基线被替换或版本目录丢失）
补丁指纹不符          1003（产物内容不完整）
模板指纹不符          1003（模板已变更，含注释）
重建树指纹不符        1003（重建结果与当时评测树不一致）
超体积上限            1001（在 ``store.save`` 之前拒）
====================  ==================================================

**v1.4 追加：提升前的「合并树健康度」闸门（T2.1，docs/15 §3.2 / ★3）**

坏补丁被提升成新基线 ⇒ **喂给下一轮 run**，质量传染是语义必然（docs/14 §3.2-★3：
v1.3.1 真跑 R-c1 的前端构建就是坏的，v2 因此也坏）。因此提升前必须过一道闸门：

| 合并树健康度 | ``force=False``（默认） | ``force=True`` |
|---|---|---|
| ``healthy`` | 正常提升 | 正常提升（``force`` 无副作用） |
| ``unhealthy`` | **``BaselineUnhealthyError``（1003）** | 放行 + 不健康留痕短码 |
| ``skipped`` | 放行（"跑不了"≠"跑坏了"） | 放行 |
| 未知（``""``） | 见下 | 放行 + 未知留痕短码 |

**未知态：默认放行，HTTP 主路径改为 fail-closed（v1.4 QA 返修 P1-3）**

"未知"有两种来源，处置**必须分开**：

1. **探针路径**（``health_probe`` 注入 / 探针自己抛异常）：未知 ⇒ **放行**。理由不变 ——
   因探针故障把提升打死比拦不下来更贵；
2. **HTTP 主路径**（``require_known_health=True``）：未知 ⇒ **抛
   ``BaselineHealthUnknownError``（1003 / 409）**。理由：API 请求里
   ``health_probe=None``（现起沙箱最长 120s，不划算），于是 `known_health` 就是**唯一**
   的健康结论来源；若它拿不到还放行，闸门等于"只要不评测就能绕过"——比没闸门更糟
   （v1.4 首版就是这个形态）。真要让用户越过，``force=true`` 保留人工通道并留痕
   ``baseline_promoted_unknown:1``。

**健康度怎么来（成本从零到一次，绝不多花）**

1. **优先复用 run 已有的结论**：调用方（API 层）从该 run 已有的
   ``evaluation_report.json`` 里读出**合并树**的健康结论（``blocking_failed`` /
   ``regression``，见 ``api.routers.artifacts._report_health_status``）传进
   ``known_health`` ⇒ **零额外沙箱调用**；
2. 否则用调用方注入的 ``health_probe`` 对**合并树**跑一次探针（纯数据进、状态出，
   零模型调用）。探针函数由 rank ≤1 的调用方传入（``evaluation.baseline_probe``），
   因为 ``artifacts``(3) 不能 import ``evaluation``(1)（逆层，CI 拦）；
3. 两者都没有 ⇒ 状态未知 ⇒ 按上面两条规则处置。**不做"猜一个 unhealthy"**：
   猜坏会误拦，而误拦比漏拦更容易掩盖真问题。

★ **留痕为什么不写进 ``patch_manifest.json``**：新基线的 ``content_hash`` 必须等于
``merged_files_hash``（步骤 7 已逐字节校验），往文件树里塞任何字节都会让"确定性重建"
当场破功。因此留痕落在**版本行的 ``message``**（落盘、可查询、可追溯）与响应体的
``warning`` 字段上，短码字面量见 :data:`BASELINE_PROMOTED_UNHEALTHY_CODE` 与
:data:`BASELINE_PROMOTED_UNKNOWN_CODE`。

**两条铁律（docs/09 §4.3，W6 沿用的同一条）**
1. **禁止「拿最新基线顶上」**：只认 ``base_ref`` 里的 ``artifact_id@version``，
   绝不回退到该 artifact 的 ``current_version``；
2. **指纹不符不得继续 merge**：``base_ref["content_hash"]`` ==
   ``prov.base_content_hash`` == **加载后现算** ``compute_content_hash(files)``
   三方相等。第三方不能省：``ArtifactStore._read_tree``（``store.py:257``）在版本
   目录丢失时**静默返回 ``{}``**，只比元数据会让"空树重建"通过校验。

**簿记文件必须剔除（6b/6c，写错必然校验失败）**

``patch_files_hash`` / ``merged_files_hash`` 都**不含** ``patch_manifest.json`` 与
``smoke_report.json``。原因：编排层写溯源时冒烟**还没跑**（``_execute_with_patch_contract``
返回之前），而产物落盘时 ``smoke_report.json`` 已在 ``output.files`` 里 —— 若把它
算进补丁文件，重建就会在合并树里凭空多出该文件，``merged_files_hash`` 必不符。
同理基线里也不该有它（它是运行簿记，不是源码）。

**为什么 ``MergeProvenance`` 定义在 ``artifacts.merge``**

它是 ``PatchManifest`` 的字段类型，而 ``merge`` 是**纯函数模块**（零 IO / 零 DB）。
让纯模块 import 本服务模块会形成 ``merge ↔ provenance`` 循环导入，方向也正好反了；
因此唯一定义点在 ``merge.py``，本模块**再导出** ——
``from ymir.artifacts.provenance import MergeProvenance`` 依然可用。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ymir.artifacts.merge import (
    BOOKKEEPING_FILES,
    DEFAULT_MAX_CHANGE_RATIO,
    DEFAULT_MAX_FILE_BYTES,
    PATCH_MANIFEST_NAME,
    MergeProvenance,
    merge_trees,
    parse_patch_manifest,
)
from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore, LoadedVersion, SavedVersion
from ymir.artifacts.versioning import compute_content_hash
from ymir.common.errors import InvalidParamError, NotFoundError, StateConflictError
from ymir.models.entities import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_ARTIFACT_NAME",
    "BASELINE_PROMOTED_UNHEALTHY_CODE",
    "BASELINE_PROMOTED_UNKNOWN_CODE",
    "HEALTH_HEALTHY",
    "HEALTH_SKIPPED",
    "HEALTH_UNHEALTHY",
    "BaselineHealthUnknownError",
    "BaselineUnhealthyError",
    "MergedHealth",
    "MergeProvenance",
    "PromoteResult",
    "promote_merged_to_baseline",
    "resolve_merged_health",
    "strip_bookkeeping",
    "template_yaml_hash",
]

#: 合并树健康度三态（字面量与 ``evaluation.baseline_probe`` **同源**）。
#:
#: ★ 为什么不 import 那边的常量：``artifacts``(3) -> ``evaluation``(1) 是逆层
#: （CI 用 AST 断言拦）。两处字面量由本条注释互指，与 ``BASELINE_ARTIFACT_NAME``
#: 的处理方式一致。
HEALTH_HEALTHY: str = "healthy"
HEALTH_UNHEALTHY: str = "unhealthy"
HEALTH_SKIPPED: str = "skipped"

#: 「不健康的合并树被强制提升」的留痕短码（v1.4 T2.1，A4：不静默）。
BASELINE_PROMOTED_UNHEALTHY_CODE: str = "baseline_promoted_unhealthy:1"

#: 「没有健康结论却被强制提升」的留痕短码（v1.4 返修 P1-3）。
#:
#: ★ 与上一条**分开**：一个说"探针说它坏了"，一个说"没人说它好"。混成同一个字面量
#: 会让排障时无法区分"真坏"与"没评测"，而这两件事的处置完全不同（前者要修代码，
#: 后者要补评测）。
BASELINE_PROMOTED_UNKNOWN_CODE: str = "baseline_promoted_unknown:1"


class BaselineUnhealthyError(StateConflictError):
    """合并树**自身**判定不健康，拒绝提升为基线（v1.4 T2.1，docs/15 §3.2）。

    判据是这棵合并树的：``blocking_failed`` 非空（有阻断项失败）或
    ``regression=True``（相对基线出现回归）。**不是** ``baseline_runnability``
    —— 那一条探的是上一轮的基线（``health(M_{N-1})``），拿它判断 ``M_N`` 会让
    "基线健康、合并树坏"的补丁一路放行（v1.4 QA 返修 P0-1 已否定的口径）。

    业务码沿用 ``1003``（HTTP 409）**不新增**：语义就是"状态冲突"——
    这次提升会把一个已知不健康的树变成下一轮的起点。

    ``details`` 固定为 ``{"reason": "baseline_unhealthy", "status": ...,
    "message": ...}``，供 API 层 / 前端直接消费（前端据此展示红色警告并走
    `force` 二次确认）。

    ★ v1.4 第二次返修（P2-C）：文案此前写"合并树探针不健康（baseline_runnability=
    unhealthy）"，经 ``promoteErrorText`` 原样上屏，把 P0-1 已否定的口径重新教给
    用户（用户会去查基线探针，而真正该改的是合并树里的阻断项失败 / 回归）。
    """

    def __init__(self, status: str = HEALTH_UNHEALTHY, message: str = "") -> None:
        text = message or (
            "合并树存在阻断项失败（blocking_failed）或相对基线出现回归（regression），"
            "提升为新基线会把坏补丁喂给下一轮（health="
            f"{status or 'unknown'}）。"
            "如确认可以接受，请带 force=true 重试"
        )
        self.reason: str = "baseline_unhealthy"
        self.status = status
        super().__init__(
            text,
            details={"reason": self.reason, "status": status, "message": text},
        )


class BaselineHealthUnknownError(StateConflictError):
    """拿不到合并树健康结论，拒绝提升为基线（v1.4 QA 返修 P1-3，fail-closed）。

    为什么需要它（v1.4 首版的漏洞）：HTTP 主路径固定 ``health_probe=None``，全仓没有
    任何注入方；``known_health=""`` 又按"未知 ⇒ 放行"处理 ⇒ **不跑评测即可绕过整个
    ★3 闸门**（先不评测，再提升）。这比没有闸门更糟：它给出"已校验"的错觉。

    业务码同样沿用 ``1003``（HTTP 409）。``details`` 形状与
    :class:`BaselineUnhealthyError` **同族**（``reason`` / ``status`` / ``message``），
    只是 ``reason`` 为 ``baseline_health_unknown``、``status`` 为 ``unknown``，
    前端据此换文案（"没有健康结论" ≠ "探针不健康"）。
    """

    def __init__(self, message: str = "") -> None:
        text = message or (
            "该运行还没有可用的合并树健康结论（评测报告缺失、版本/产物不匹配或不可读）："
            "闸门无法判断这棵树是否跑得起来。请先对这一版完成评测"
            "（控制台「产物库 → 发起四维评测」，无需额外参数），"
            "或带 force=true 明确接受「未评测的树被提升为基线」的风险"
        )
        self.reason: str = "baseline_health_unknown"
        self.status: str = "unknown"
        super().__init__(
            text,
            details={"reason": self.reason, "status": self.status, "message": text},
        )


@dataclass(frozen=True)
class MergedHealth:
    """「待提升合并树」的健康度结论（纯数据）。

    Attributes:
        status: ``healthy`` / ``unhealthy`` / ``skipped`` / ``""``（未知）。
        source: 结论来源，仅供日志与排障：``run``（复用 run 已有结论）/
            ``probe``（对合并树现跑一次探针）/ ``unknown``。
    """

    status: str = ""
    source: str = "unknown"

    @property
    def unhealthy(self) -> bool:
        """合并树自身就失败（提升将污染下一轮基线）。"""
        return self.status == HEALTH_UNHEALTHY

    @property
    def known(self) -> bool:
        """是否有**可用的**健康结论（``healthy`` / ``unhealthy`` / ``skipped``）。

        ★ ``skipped`` 算"已知"：那是探针明确回报"这个环境跑不了检查"，是结论而不是
        缺失。只有 ``""`` 才是"什么都没拿到"。
        """
        return self.status in {HEALTH_HEALTHY, HEALTH_UNHEALTHY, HEALTH_SKIPPED}


async def resolve_merged_health(
    files: Mapping[str, str],
    *,
    known_status: str = "",
    probe: Callable[[Mapping[str, str]], Awaitable[str]] | None = None,
) -> MergedHealth:
    """判定「待提升合并树」的健康度（成本从零到一次探针，绝不多花）。

    Args:
        files: 重建出来的合并树（**已剔除簿记文件**，与落盘口径一致）。
        known_status: 调用方已知的结论（通常来自 run 已有的
            ``evaluation_report.json`` 里**合并树自己**的判据：
            ``blocking_failed`` 非空 / ``regression=true`` ⇒ ``unhealthy``）。
        probe: 现跑探针的可调用对象（``files -> status``）；``None`` ⇒ 不跑。

    Returns:
        :class:`MergedHealth`。**绝不抛异常**：探针自身报错一律降级成"未知"
        （拦不下来是产品风险，因探针故障把提升打死更糟）。

    Note:
        优先级：``known_status`` > ``probe`` > 未知。理由：探针有成本
        （一次沙箱会话），而 run 已有结论是**评测时真的跑过**的同一件事。
    """
    normalized = str(known_status or "").strip().lower()
    if normalized:
        return MergedHealth(status=normalized, source="run")
    if probe is None:
        return MergedHealth(status="", source="unknown")
    try:
        status = str(await probe(files) or "").strip().lower()
    except Exception:  # noqa: BLE001 - 探针故障不得阻断提升（降级为"未知"）
        logger.warning("合并树健康度探针异常，按未知处理（不阻断提升）", exc_info=True)
        return MergedHealth(status="", source="unknown")
    return MergedHealth(status=status, source="probe")


#: 基线产物名（与 ``api.routers.artifacts.BASELINE_ARTIFACT_NAME`` 同一字面量）。
#:
#: 定义在这里是因为**写入方**是本模块（``promote_merged_to_baseline`` 直接建新基线
#: 版本），API 层只读它。``artifacts``(3) -> ``api``(0) 是逆层，不能反向 import，
#: 故由本模块持有唯一字面量、API 层复用之。
BASELINE_ARTIFACT_NAME: str = "baseline"

#: 模板文件后缀（``templates.loader`` 的目录约定：``<id>.yaml``）。
_TEMPLATE_SUFFIX: str = ".yaml"


def strip_bookkeeping(files: Mapping[str, str]) -> dict[str, str]:
    """剔除运行簿记文件（``patch_manifest.json`` / ``smoke_report.json``）。

    写溯源与重建两侧**必须调同一个函数**，否则"生成时算的哈希"与"重建时算的哈希"
    口径不同 —— 那正是最隐蔽的一类失败：两边都自认为对，只是永远对不上。
    """
    return {path: content for path, content in files.items() if path not in BOOKKEEPING_FILES}


def template_yaml_hash(templates_dir: Path | str, template_id: str) -> str:
    """模板 YAML **原始字节**的 sha256（v1.3.1 §7.3-7b）。

    ★ 为什么不用 ``TemplateSpec.version``：那是模板作者**手写**的契约版本
    （``python-cli.yaml`` 里 ``version: "1.0"``），改 YAML **不必然 bump** ⇒
    拿它做「同一输入」的证明是**假的**。

    ★ 为什么目录是**入参**而不是写死：``artifacts``(3) 不能 import ``templates``(2)
    （逆层，CI 拦）。调用方（``role_executor`` / API 层，rank ≤ 1）把
    ``templates.loader.DEFAULT_TEMPLATES_DIR`` 传进来即可 —— 路径知识留在能合法
    依赖 ``templates`` 的那一侧，本模块只做哈希。

    ★ 已知代价（保守方向，必须说清）：**只改注释 / 格式也会被拒**。对外文案因此写
    「模板文件有改动（含注释）」，不能写「模板版本变了」——后者会让用户找不到原因。
    真实使用中若频繁被拒，v1.4 换成「规范化 spec 指纹」即可（切换只改这一个函数）。

    Args:
        templates_dir: 模板目录（通常 ``DEFAULT_TEMPLATES_DIR``）。
        template_id: 模板 id（= YAML 文件名主干）。空串 ⇒ 返回 ``""``（无模板）。

    Returns:
        十六进制 sha256；模板 id 为空或文件不可读 ⇒ ``""``。

    Note:
        文件不可读**不抛**：产物正文里存 ``""`` 表示"当时没有模板指纹"，重建时
        与同样为 ``""`` 的现算值相等 ⇒ 通过；一旦后来模板出现/变化，两侧不等 ⇒
        按「模板已变更」拒绝（宁严不松）。
    """
    text = str(template_id or "").strip()
    if not text:
        return ""
    path = Path(templates_dir) / f"{text}{_TEMPLATE_SUFFIX}"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        logger.warning("模板文件不可读，模板指纹留空：%s", path, exc_info=True)
        return ""


@dataclass(frozen=True)
class PromoteResult:
    """「以合入结果为新基线」的结果（API 响应体的纯数据来源）。"""

    artifact_id: str
    version: int
    content_hash: str
    file_count: int
    size_bytes: int
    #: ``True`` = 重建结果与当前基线最新版**内容相同**，未新建版本（幂等，步骤 9）
    reused: bool
    #: 来源增量产物（审批 / 追溯用）
    source_artifact_id: str
    source_version: int
    #: 强制提升留痕（v1.4 T2.1，A4）：合并树 ``unhealthy`` 且 ``force=True`` 时为
    #: ``baseline_promoted_unhealthy:1``，其余情况为空串。**不静默**是这一项的纪律。
    warning: str = ""
    #: 本次提升用到的合并树健康度（``healthy`` / ``unhealthy`` / ``skipped`` / ``""``）
    health_status: str = ""
    #: 健康度结论来源：``run``（复用已有结论）/ ``probe``（现跑探针）/ ``unknown``
    health_source: str = "unknown"


async def promote_merged_to_baseline(
    store: ArtifactStore,
    *,
    project_id: str,
    artifact_id: str,
    version: int,
    base_ref: Mapping[str, object],
    skeleton: Mapping[str, str],
    template_id: str,
    template_hash: str,
    run_id: str,
    max_files: int = 300,
    max_bytes: int = 5 * 1024 * 1024,
    max_change_ratio: float = DEFAULT_MAX_CHANGE_RATIO,
    tenant_id: str = DEFAULT_TENANT_ID,
    force: bool = False,
    known_health: str = "",
    health_probe: Callable[[Mapping[str, str]], Awaitable[str]] | None = None,
    strict_health: bool = False,
) -> PromoteResult:
    """把「增量产物的合入结果」落成**新基线版本**（v1.3.1 T5.2，docs/13 §7.3-7）。

    十步与拒绝语义见模块 docstring 的表。实现上**每一步都在下一步之前拒绝**，
    尤其体积上限在 ``store.save`` 之前 —— 绝不落半成品基线。

    Args:
        store: 产物库。
        project_id: 项目 id（新基线落在该项目下）。
        artifact_id: 增量产物 id（``code``）。
        version: 增量产物版本（**显式给**，不复用"最新"）。
        base_ref: ``run.context["baseline"]`` 的**纯数据**（含 ``artifact_id`` /
            ``version`` / ``content_hash``）。API 层解析后传入。
        skeleton: ``TemplateSpec.skeleton_files()``（纯数据）；无模板传 ``{}``。
        template_id: 模板 id（无模板传 ``""``）。
        template_hash: **现算**的模板 YAML 指纹（:func:`template_yaml_hash`）。
        run_id: 仅用于新基线版本的 ``message`` 追溯。
        max_files: 基线上限（``settings.baseline_max_files``）。
        max_bytes: 基线上限（``settings.baseline_max_bytes``）。
        max_change_ratio: 重建时的改动面阈值（**只影响** ``manifest.warnings``，
            **不改变树**、不进 ``merged_files_hash``）。
        tenant_id: 租户。
        force: 合并树 ``unhealthy`` 时是否放行（v1.4 T2.1）。``False``（默认）⇒ 抛
            :class:`BaselineUnhealthyError`（1003 / 409）；``True`` ⇒ 放行并留痕
            ``baseline_promoted_unhealthy:1``（进版本 ``message`` 与
            :attr:`PromoteResult.warning`）。
        known_health: 调用方已知的合并树健康度（通常复用 run 已有评测报告的
            **合并树**结论）⇒ **零额外沙箱调用**。
        health_probe: 现跑探针（``files -> status``）。仅在 ``known_health`` 为空时
            被调用；``None`` ⇒ 不跑（状态未知）。
        strict_health: 未知态是否 fail-closed（v1.4 返修 P1-3）。
            ``False``（默认）⇒ 未知放行（探针路径的既有纪律）；
            ``True`` ⇒ 未知且 ``force=False`` 时抛
            :class:`BaselineHealthUnknownError`（1003 / 409）。**HTTP 主路径必传
            ``True``**：那里 ``health_probe=None``，不 fail-closed 就等于
            "不评测即可绕过闸门"。

    Returns:
        :class:`PromoteResult`（``reused=True`` 时指向已存在的最新基线版本）。

    Raises:
        NotFoundError: 增量产物 / 基线版本不存在（1002）。★ 基线**版本**不存在时
            按 1003 处置（见下）—— 因为版本行在而目录丢失也是 1003，两者同一类事故。
        InvalidParamError: 非增量产物 / 超体积上限（1001）。
        StateConflictError: 无溯源 / 基线或补丁或模板或重建树指纹不符（1003）。
        BaselineUnhealthyError: 合并树 ``unhealthy`` 且 ``force=False``（1003 / 409，
            ``details={"reason": "baseline_unhealthy", ...}``）。
        BaselineHealthUnknownError: ``strict_health`` 且拿不到健康结论且
            ``force=False``（1003 / 409，
            ``details={"reason": "baseline_health_unknown", ...}``）。
    """
    loaded = await store.load(artifact_id, version, tenant_id=tenant_id)
    artifact_name = loaded.artifact.name

    # ---- 步骤 1：必须是增量产物（否则纯 merge 无从谈起）----
    manifest = parse_patch_manifest(loaded.files.get(PATCH_MANIFEST_NAME))
    if manifest is None or manifest.mode != "patch":
        raise InvalidParamError(
            "该版本不是增量产物，无 patch_manifest.json；如需以此为基线，请使用上传基线接口"
        )

    # ---- 步骤 2：必须有溯源（v1.3.1 之前的老产物无法安全重建）----
    prov = manifest.provenance
    if prov is None:
        raise StateConflictError(
            "该产物缺少合入溯源信息（v1.3.1 之前产生的产物），无法安全重建合并树"
        )
    gaps = prov.missing()
    if gaps:
        raise StateConflictError(
            "该产物的合入溯源信息不完整（缺 " + "、".join(gaps) + "），无法安全重建合并树"
        )

    # ---- 步骤 3：基线校验（两条铁律：不拿最新顶上 / 三方指纹相等）----
    base_files = await _load_pinned_baseline(
        store,
        base_ref=base_ref,
        prov=prov,
        tenant_id=tenant_id,
    )

    # ---- 步骤 4：补丁指纹（剔除两个簿记文件，与写侧同源）----
    patch_files = strip_bookkeeping(loaded.files)
    if compute_content_hash(patch_files) != prov.patch_files_hash:
        raise StateConflictError("产物内容不完整（补丁文件指纹与记录不一致）")

    # ---- 步骤 5：模板指纹（改注释也算改，保守方向）----
    if template_id != prov.template_id or template_hash != prov.template_hash:
        raise StateConflictError(
            f"模板已变更（{_short(prov.template_hash)}→{_short(template_hash)}），"
            "合并树无法按原样重建；请用当前模板重新跑一次"
        )

    # ---- 步骤 6：重建（按「当时实际生效的参数」重建；四个入参的性质**各不相同**）----
    #
    # ① ``max_file_bytes``：**参与重建**。传小了会把补丁文件判成 ``too_large`` 丢弃
    #    ⇒ 树不同（fail-closed，方向安全）。``0`` 视为"未知"，退回落差值。
    #
    # ② ``symbol_deletions``：**参与重建，但只单向**。原 run 用 ``off`` 时，"模型删掉
    #    仍被其它文件引用的公共符号"是**放行**的，产物补丁集里已是删后内容；此时重建
    #    若按 ``ignore`` ⇒ 该文件被回滚成基线版（树不同 ⇒ 1003）、按 ``reject`` ⇒ 直接抛
    #    :class:`~ymir.artifacts.merge.SymbolRegressionConflict`，两种都拿不到当时的树。
    #    **反向无害**：原 run 为 ``ignore`` 的产物里**根本没有那个被回滚的文件**（回滚 ⇒
    #    模型内容被丢弃 ⇒ 不进补丁集），改成 ``off`` 也无从检出 —— 所以护栏**只有一个
    #    方向**，而那正是"篡改 manifest 放行一次符号回归"的攻击面（钉子：
    #    ``tests/test_baseline_promote.py::TestSymbolPolicyIsLoadBearing``）。
    #
    # ③ ``max_change_ratio``：**不参与重建**，只影响 ``manifest.warnings``（改动面告警），
    #    不进 ``merged_files_hash``、不改变一个字节的树。重建时传它只为让"同一输入两次
    #    合并"在告警口径上也一致，与"树是否相同"无关。
    #
    # ④ ``protected_paths`` / ``on_protected_hit``：**审计字段，不参与重建**。这里
    #    **刻意不传** ``protected_paths``（默认空集合 ⇒ ``merge_trees`` 永不抛），
    #    且产物补丁集在**写侧**已 sanitize（受保护路径的模型内容已被丢弃、进不了
    #    补丁集）⇒ 构造上没有可命中的分支可触发。保留 ``protected_policy`` 只为回答
    #    "当时到底用了什么策略"；将来若把原始模型输出也存进产物（v1.4 候选），
    #    届时**同时**改「重建传 protected_paths」才能让它变成 load-bearing。
    rebuilt = merge_trees(
        base_files,
        patch_files,
        mode="patch",
        skeleton=skeleton,
        base_paths=frozenset(base_files),
        deleted=manifest.deleted,
        max_file_bytes=(prov.max_file_bytes or DEFAULT_MAX_FILE_BYTES),
        # 审计值：无 protected_paths ⇒ 无分支可触发。保留传参是为了"策略哪天变成
        # load-bearing 时这里已经传对"，真正的开关是上面那句「不传 protected_paths」。
        on_protected_hit=(prov.protected_policy or "reject"),  # type: ignore[arg-type]
        # ★ 真护栏：删掉这个实参 ⇒ "off → ignore" 的篡改会静默重建出另一棵树。
        symbol_deletions=(prov.symbol_policy or "off"),  # type: ignore[arg-type]
        # 只影响 warnings：不进 merged_files_hash、不改变树（见上面 ③）。
        max_change_ratio=max_change_ratio,
    )
    merged_files = strip_bookkeeping(rebuilt.files)

    # ---- 步骤 7：重建树必须与当时评测树逐字节一致 ----
    merged_hash = compute_content_hash(merged_files)
    if merged_hash != prov.merged_files_hash:
        raise StateConflictError("重建结果与当时评测树不一致（合并树指纹不符），拒绝据此新建基线")

    # ---- 步骤 8：体积上限（在 store.save 之前，绝不落半成品）----
    size_bytes = sum(len(content.encode("utf-8")) for content in merged_files.values())
    if len(merged_files) > max_files or size_bytes > max_bytes:
        raise InvalidParamError(
            f"合并结果超出基线上限（{len(merged_files)} 文件 / {size_bytes} 字节；"
            f"上限 {max_files} / {_human_bytes(max_bytes)}）"
        )

    # ---- 步骤 8b：合并树健康度闸门（v1.4 T2.1，docs/15 §3.2 / ★3）----
    # ★ 位置是刻意的：放在**便宜**的校验（指纹 / 体积）之后、幂等与落盘之前。
    #   探针最贵，前面能拒的就别白跑一次沙箱。
    health = await resolve_merged_health(
        merged_files, known_status=known_health, probe=health_probe
    )
    warning = ""
    if health.unhealthy:
        if not force:
            # 1003 / 409：默认拦住。质量传染（坏补丁变坏基线）是语义必然，
            # 不是"偶发失败"，必须在闸门处挡；force 保留人工越权通道。
            raise BaselineUnhealthyError(health.status)
        # force：放行但**留痕**（A4：不静默）。写不进文件树（会破坏 content_hash =
        # merged_files_hash 的逐字节契约），因此落在版本 message + 响应体 warning。
        warning = BASELINE_PROMOTED_UNHEALTHY_CODE
        logger.warning(
            "以不健康的合并树提升基线（force=true，已留痕）：project=%s run=%s status=%s",
            project_id,
            run_id,
            health.status,
        )
    elif strict_health and not health.known:
        # ---- v1.4 QA 返修 P1-3：未知态 fail-closed ----
        # ★ 只在 ``strict_health`` 下生效：HTTP 主路径没有 ``health_probe``，
        #   ``known_health`` 是**唯一**结论来源，拿不到还放行 ⇒ "不评测即可绕过"。
        #   探针路径（``health_probe`` 注入）继续"未知 ⇒ 放行"：因探针故障把提升
        #   打死比拦不下来更贵，而那条路径本来就有别的证据（真跑过探针）。
        if not force:
            raise BaselineHealthUnknownError()
        warning = BASELINE_PROMOTED_UNKNOWN_CODE
        logger.warning(
            "以「没有健康结论」的合并树提升基线（force=true，已留痕）："
            "project=%s run=%s source=%s",
            project_id,
            run_id,
            health.source,
        )

    # ---- 步骤 9：幂等（store.save 本身不去重，连点两次不该产生 vN+1）----
    reused = await _find_same_latest(
        store, project_id=project_id, content_hash=merged_hash, tenant_id=tenant_id
    )
    if reused is not None:
        return PromoteResult(
            artifact_id=reused.artifact.id,
            version=reused.version,
            content_hash=merged_hash,
            file_count=len(merged_files),
            size_bytes=size_bytes,
            reused=True,
            source_artifact_id=artifact_id,
            source_version=version,
            warning=warning,
            health_status=health.status,
            health_source=health.source,
        )

    # ---- 步骤 10：落盘为新基线版本 ----
    message = (
        f"from-merge: run={run_id} base=v{prov.base_version} (← {artifact_name} v{version})"
    )
    if warning:
        # ★ 留痕必须进**落盘**正文（版本行 message）：只写日志等于没写。
        message = f"{message} [{warning}]"
    saved = await store.save(
        project_id=project_id,
        name=BASELINE_ARTIFACT_NAME,
        artifact_type=BASELINE_TYPE,
        files=merged_files,
        run_id=run_id,
        created_by="user",
        message=message,
        tenant_id=tenant_id,
    )
    logger.info(
        "合入结果已沉淀为新基线：project=%s baseline=v%s ← %s v%s (run=%s)",
        project_id,
        saved.version,
        artifact_name,
        version,
        run_id,
    )
    return _result_from_saved(
        saved,
        reused=False,
        artifact_id=artifact_id,
        version=version,
        warning=warning,
        health=health,
    )


def _result_from_saved(
    saved: SavedVersion,
    *,
    reused: bool,
    artifact_id: str,
    version: int,
    warning: str = "",
    health: MergedHealth | None = None,
) -> PromoteResult:
    """:class:`SavedVersion` → :class:`PromoteResult`（字段一一对应，不猜）。"""
    resolved = health if health is not None else MergedHealth()
    return PromoteResult(
        artifact_id=saved.artifact_id,
        version=saved.version,
        content_hash=saved.content_hash,
        file_count=saved.file_count,
        size_bytes=saved.size_bytes,
        reused=reused,
        source_artifact_id=artifact_id,
        source_version=version,
        warning=warning,
        health_status=resolved.status,
        health_source=resolved.source,
    )


async def _load_pinned_baseline(
    store: ArtifactStore,
    *,
    base_ref: Mapping[str, object],
    prov: MergeProvenance,
    tenant_id: str,
) -> dict[str, str]:
    """按 ``base_ref`` 的 ``artifact_id@version`` 加载基线**正文**（铁律 ①②）。

    ★ **绝不** ``store.load(artifact_id)``（缺省 = current_version）：项目里可能已经
    上传了更新的基线（v2/v3），拿最新顶上会把"别人那一版"当成这次 run 的输入 ——
    R9 明令禁止，且会静默产出一棵与评测无关的树。

    Raises:
        StateConflictError: 引用缺失 / 与溯源不一致 / 指纹三方不等（1003）。
    """
    ref_id = str(base_ref.get("artifact_id") or "").strip()
    raw_version = base_ref.get("version")
    ref_version = raw_version if isinstance(raw_version, int) else None
    ref_hash = str(base_ref.get("content_hash") or "").strip()
    if not ref_id:
        raise StateConflictError("无法确定基线引用（该 run 不是增量运行）")
    if ref_version is None or not ref_hash:
        # v1.3.1（T6.3-C）：两种原因必须分开说 —— 原来"字段没写全"与"根本没有基线引用"
        # 抛同一句「该 run 不是增量运行」，把 CLI 少写字段误诊成"这不是增量 run"，
        # 排查的人会去查 run 模式，而真正该查的是**谁写的 context**。
        absent = "version" if ref_version is None else "content_hash"
        raise StateConflictError(
            f"无法确定基线引用：run 的基线引用缺少 `{absent}`"
            "（CLI 发起时可能未写全），无法做三方比对"
        )
    if ref_id != prov.base_artifact_id or ref_version != prov.base_version:
        raise StateConflictError(
            "基线引用与产物记录不一致（"
            f"run 记录 {ref_id[:8]}@v{ref_version}，"
            f"产物记录 {prov.base_artifact_id[:8]}@v{prov.base_version}）"
        )
    if ref_hash != prov.base_content_hash:
        raise StateConflictError("基线版本内容与产物记录不一致（基线被替换或版本目录丢失）")

    try:
        base_loaded = await store.load(ref_id, ref_version, tenant_id=tenant_id)
    except NotFoundError as exc:
        raise StateConflictError(
            "基线版本内容与产物记录不一致（基线被替换或版本目录丢失）"
        ) from exc

    # ★ 现算：``_read_tree`` 在版本目录丢失时静默返回 ``{}``，只比元数据会放过空树
    computed = compute_content_hash(base_loaded.files)
    if computed != prov.base_content_hash:
        raise StateConflictError("基线版本内容与产物记录不一致（基线被替换或版本目录丢失）")
    return dict(base_loaded.files)


async def _find_same_latest(
    store: ArtifactStore,
    *,
    project_id: str,
    content_hash: str,
    tenant_id: str,
) -> LoadedVersion | None:
    """当前基线最新版是否已与重建结果**内容相同**（步骤 9 的幂等判据）。

    比的是**版本行记录的** ``content_hash``（入库时现算），而不是重读文件树：
    目录丢失时 ``_read_tree`` 会返回 ``{}``，那会把"内容缺失"误判成"内容相同"。
    """
    latest = await store.latest_by_name(project_id, BASELINE_ARTIFACT_NAME, tenant_id=tenant_id)
    if latest is None:
        return None
    if latest.version_row.content_hash != content_hash:
        return None
    return latest


def _short(value: str) -> str:
    """指纹前 8 位（对外文案只给短前缀：够定位、不刷屏）。"""
    text = str(value or "")
    return text[:8] if text else "无模板"


def _human_bytes(value: int) -> str:
    """字节数的可读形态（``5 MB``；文案里出现的上限值）。"""
    if value and value % (1024 * 1024) == 0:
        return f"{value // (1024 * 1024)} MB"
    return f"{value} 字节"
