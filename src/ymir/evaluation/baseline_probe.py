"""基线测试独立执行（v1.3 T1.2，docs/11 §7.3-5）。

**解决什么问题**：增量模式下评测工作区是「基线 ⊕ 补丁」的合并树。合并树跑失败时，
只看合并树无法区分两种完全不同的原因：

- **模型改坏了既有功能**（回归）—— 该扣分、该回边；
- **基线本身就是坏的** —— 分数不可比，不该据此判模型失败。

所以评测前先在**独立沙箱会话**里对基线自身跑一遍模板 ``checks``，把两者分开。

三条纪律：

1. **独立会话**（B15）：沙箱只有 ``exec/read_file/write_file/list_files``，**没有删除
   API**；复用主会话会残留基线文件、污染合并树评测，因此必须另起会话；
2. **只跑 checks**：不跑 judge、不跑另外三个维度 —— 增量约等于 1 个 pytest 时长
   （docs/11 §7.7-R2 ①），不是"评测耗时翻倍"；
3. **降级优先、永不阻断**：环境缺失 / 会话异常一律 ``skipped`` 并给原因，
   **不算 unhealthy**（绝不因环境问题把好代码判 fail）。

v1.3.1 增量（docs/13 §7.3-1/1b/1c/2/3）—— **探针与合并树同尺**（L9 修复）：

- 写进沙箱的不再是「基线原样」，而是 :func:`build_probe_tree` 的产物：它调用
  **合并树同一个函数** ``artifacts.merge.merge_trees(..., mode="patch", patch={})``，
  于是骨架补缺规则（优先级表 1/2/3）与合并树**逐条一致**；
- 新增第 6 个降级原因 ``baseline_incomplete``：``checks[].requires_files`` 声明的
  文件基线里**一个都没有** ⇒ 该检查不执行、记 ``skipped``；全部 skipped ⇒
  整体 ``skipped``（"基线不完整" ≠ "基线不健康"）；
- 合并树侧语义**一行不改**：缺文件仍 ``failed``（Q1 的不对称，刻意如此）。

分层（docs/11 §7.5-6）：``evaluation``(rank 1) 只依赖 ``sandbox``(3) / ``common``(5) /
``artifacts``(3, ``merge_trees`` 纯函数) 与本包；**禁止** import ``templates``(2) ——
检查项与骨架都以纯数据传入。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from ymir.artifacts.merge import DEFAULT_MAX_FILE_BYTES, merge_trees
from ymir.evaluation.evaluators.base import EvalCheck, run_checks

#: 基线三态（唯一口径，docs/11 §7.5-1）。
BASELINE_HEALTHY: str = "healthy"
BASELINE_UNHEALTHY: str = "unhealthy"
BASELINE_SKIPPED: str = "skipped"

BaselineStatus = Literal["healthy", "unhealthy", "skipped"]

#: 降级原因短码（唯一口径）。
SKIP_BASELINE_UNAVAILABLE: str = "baseline_unavailable"
SKIP_NO_SANDBOX: str = "no_sandbox"
SKIP_NO_CHECKS: str = "no_checks"
SKIP_ENV_MISSING: str = "env_missing"
SKIP_SANDBOX_ERROR: str = "sandbox_error"
#: ★ v1.3.1 新增第 6 值：基线缺少该检查依赖的文件（**不是** unhealthy）。
SKIP_BASELINE_INCOMPLETE: str = "baseline_incomplete"

#: 探针树口径（内部字段；**只有** ``skeleton_filled`` 会进报告 JSON）。
PROBE_MODE_RAW: str = "raw"
PROBE_MODE_SKELETON_FILLED: str = "skeleton_filled"

#: 探针侧单条检查跳过原因前缀（与合并树侧 ``requires_missing:<bin>`` **刻意不同**：
#: 一个是"文件不在树里"，一个是"镜像没这个程序"）。
SKIP_FILE_MISSING_PREFIX: str = "requires_file_missing:"

#: 进程内 LRU 容量（同批次同一基线多次评测只付一次探测成本）。
DEFAULT_CACHE_SIZE: int = 8


@dataclass(frozen=True)
class BaselineCheck:
    """基线侧单条检查的执行明细（报告形态）。"""

    command: str
    exit_code: int | None = None
    timed_out: bool = False
    skipped: bool = False
    output: str = ""
    #: 跳过原因（v1.3.1）：唯一取值 ``requires_file_missing:<path>``。
    skipped_reason: str = ""


@dataclass(frozen=True)
class BaselineProbe:
    """基线健康度探测结果（``TaskOut.baseline_runnability`` 的数据来源）。"""

    status: BaselineStatus = BASELINE_SKIPPED
    score: float = 0.0
    passed: bool = False
    checks: tuple[BaselineCheck, ...] = ()
    reason: str = ""
    duration_ms: int = 0
    cached: bool = False
    #: 探针树口径（v1.3.1）：``raw`` / ``skeleton_filled`` / ``""``（旧数据 = v1.3 口径）。
    #: ★ 内部**总是**被赋值（供日志与测试断言），但报告层只在 ``skeleton_filled``
    #: 时序列化它（见 ``report.BaselineProbeOut``）。
    probe_mode: str = ""
    #: 探针树**真的补进**的骨架路径（排序）；空 ⇒ ``probe_mode="raw"``。
    skeleton_added: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        """基线健康（合并树失败即可归因为回归）。"""
        return self.status == BASELINE_HEALTHY

    @property
    def unhealthy(self) -> bool:
        """基线自身就失败（本次分数与"基于健康基线"的运行不可比）。"""
        return self.status == BASELINE_UNHEALTHY


class BaselineProbeCache:
    """进程内 LRU（容量 8）。键 = ``sha256(基线指纹 + checks 指纹)``。

    只在**同一进程**内复用（batch 场景同一基线多次评测受益；单跑无收益）。
    刻意**不落盘**：基线正文可能含用户代码，落盘缓存会引入"离线副本"式的合规问题。
    """

    def __init__(self, capacity: int = DEFAULT_CACHE_SIZE) -> None:
        self._capacity = max(1, int(capacity))
        self._items: OrderedDict[str, BaselineProbe] = OrderedDict()

    def get(self, key: str) -> BaselineProbe | None:
        """取缓存（命中即移到队尾，维持 LRU 语义）。"""
        if not key:
            return None
        probe = self._items.get(key)
        if probe is None:
            return None
        self._items.move_to_end(key)
        # 命中标记 cached=True（不改缓存里的原对象：frozen dataclass 用 replace 派生副本）
        return replace(probe, cached=True)

    def put(self, key: str, probe: BaselineProbe) -> None:
        """写入缓存（超容量淘汰最久未使用项）。"""
        if not key:
            return
        self._items[key] = probe
        self._items.move_to_end(key)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """清空（测试用）。"""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def baseline_cache_key(
    files: Mapping[str, str],
    checks: Sequence[EvalCheck],
    *,
    skeleton: Mapping[str, str] | None = None,
) -> str:
    """基线指纹 + checks 指纹（+ 骨架指纹），指纹变了就不能复用探测结果。

    ★ QA F3：**checks 的全部执行相关字段**都要进指纹 —— ``type`` / ``run`` /
    ``weight`` / ``group`` / ``blocking`` / ``requires`` / ``timeout_s``。
    少拼任何一个都会造成"改了 ``requires: node`` 仍命中旧探测结果"的幽灵缓存
    （``requires`` 直接决定某条检查是 skipped 还是真跑，必须参与指纹）。

    ★ v1.3.1（T1.3）：追加两块，
    ① ``requires_files``（决定该命令在探针侧是否被跳过，同样必须进指纹）；
    ② **骨架指纹** —— ``files`` 传的是**基线原样**，骨架不由它体现：模板改了骨架
       （例如 ruff.toml 收紧规则）而基线没变，键若不变就会命中旧探测结果 ⇒
       "改了骨架，探针还在用旧树"，与 F3 是同一个失效模式。

    纪律：``skeleton`` 为空 / ``None`` 时 **不 update 任何字节**，
    ``requires_files`` 为空时也不追加字节 ⇒ 键与 v1.3 **逐字节相等**
    （既有「同基线同 checks ⇒ 缓存命中」用例零改动）。
    顺序：files 块 → checks 块 → skeleton 块（最后，保持既有前缀字节序列稳定，
    便于排查）。
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[path].encode("utf-8"))
        digest.update(b"\0")
    for check in checks:
        # 用 ``repr`` 拼字段：``None`` 与 ``0``/``""`` 必须可区分（timeout_s 可为 None）
        fingerprint = repr(
            (
                check.type,
                check.run,
                check.weight,
                check.group,
                check.blocking,
                check.requires,
                check.timeout_s,
            )
        )
        digest.update(fingerprint.encode("utf-8"))
        digest.update(b"\0")
        # 只在**声明了** requires_files 时追加字节：空清单与 v1.3 键逐字节相同
        if check.requires_files:
            digest.update(repr(("requires_files", tuple(check.requires_files))).encode("utf-8"))
            digest.update(b"\0")
    if skeleton:
        digest.update(b"# skeleton\0")
        for path in sorted(skeleton):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(skeleton[path].encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def missing_requires_files(
    check: EvalCheck, tree: Mapping[str, str]
) -> tuple[str, ...]:
    """该 check 缺失的 ``requires_files`` 元素（保序、去重）。

    匹配规则（与 ``layout.allowed_prefixes`` 的尾斜杠约定同源）：

    - ``item.endswith("/")`` ⇒ **目录**：``any(path.startswith(item) for path in tree)``
      （沙箱里没有空目录，所以"目录存在"只能表达为"该前缀下 ≥1 个文件"）；
    - 否则 ⇒ **精确文件**：``item in tree``。

    ★ 判定用的是**探针树**（骨架补缺之后），不是基线原样：加载期校验 22 禁止
    ``requires_files`` 与 skeleton 交叉，因此骨架补进的文件不会让这条声明恒真。
    """
    missing: list[str] = []
    seen: set[str] = set()
    for raw in check.requires_files:
        item = str(raw).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        if item.endswith("/"):
            present = any(path.startswith(item) for path in tree)
        else:
            present = item in tree
        if not present:
            missing.append(item)
    return tuple(missing)


def build_probe_tree(
    base_files: Mapping[str, str],
    skeleton: Mapping[str, str] | None = None,
    *,
    reserved_paths: frozenset[str] = frozenset(),
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> tuple[dict[str, str], str, tuple[str, ...]]:
    """构造「探针实际要跑的树」，返回 ``(tree, probe_mode, skeleton_added)``。

    ★ 与合并树**同一个函数、同一套优先级表**（docs/12 §7-L9 修法①，落法 A）：

    ``merge_trees(base_files, {}, mode="patch", skeleton=skeleton,
                  base_paths=frozenset(base_files))``

    ★ **现状修正（C1）**：``mode`` 必须是 ``"patch"`` 而不是 ``"merge"`` ——
    ``merge_trees`` 在 ``mode="merge"`` 下**不注入骨架**，用 merge 就等于没修。
    空补丁（``patch={}``）下：

    - 优先级 1：基线已有该骨架文件 ⇒ ``skipped_existing``（用户文件优先）；
    - 优先级 2（``reserved`` 被模型写）**构造上不可达**（要求 ``path in patch_norm``），
      因此 ``reserved_paths`` 保持缺省空集合（S3：传进去是死配置）；
    - 优先级 3：基线缺的骨架 ⇒ 补齐（``skeleton_added``）。

    规则：

    - ``skeleton`` 为空 / 基线已含全部骨架 ⇒ ``probe_mode="raw"``、
      ``skeleton_added=()``、返回 ``dict(base_files)``
      （**树与 v1.3 逐字节一致**，且此时 ``probe_mode`` 不进报告）；
    - 否则 ``probe_mode="skeleton_filled"``，``skeleton_added`` 来自
      ``merge_result.manifest.skeleton_added``；
    - **不做** protected 判定（探针不应用补丁，``patch={}`` ⇒ ``changed_patch_paths``
      为空），**不**写 ``patch_manifest.json``（探针不落盘任何产物）。

    Args:
        base_files: 基线**原样**（路径 -> 正文）。
        skeleton: 骨架文件集（纯数据；由 ``EvalTask.baseline_skeleton`` 传入）。
        reserved_paths: 模板固定提供的路径（``patch={}`` 下不可达，仅为对齐
            ``merge_trees`` 的签名与将来复用而保留）。
        max_file_bytes: 单文件字节上限（与合并树同参数，超限骨架不补）。

    Returns:
        ``(tree, probe_mode, skeleton_added)``。
    """
    base = dict(base_files)
    if not skeleton:
        return base, PROBE_MODE_RAW, ()
    if all(path in base for path in skeleton):
        # 基线已含全部骨架 ⇒ 合并结果必然等于基线原样（连字节顺序都一致），
        # 直接短路，避免无意义的全量合并（这是「逐字节一致」的最强保证）
        return base, PROBE_MODE_RAW, ()
    merged = merge_trees(
        base,
        {},
        mode="patch",
        skeleton=skeleton,
        reserved_paths=reserved_paths,
        base_paths=frozenset(base),
        max_file_bytes=max_file_bytes,
    )
    added = tuple(sorted(merged.manifest.skeleton_added))
    if not added:
        return base, PROBE_MODE_RAW, ()
    return dict(merged.files), PROBE_MODE_SKELETON_FILLED, added


async def probe_baseline(
    *,
    files: Mapping[str, str],
    checks: Sequence[EvalCheck],
    sandbox_provider: Any | None,
    exec_timeout: float = 120.0,
    cache: BaselineProbeCache | None = None,
    cache_key: str = "",
    # ---- v1.3.1 新增（唯一新增入参，带默认值，既有调用点零改动）----
    skeleton: Mapping[str, str] | None = None,
) -> BaselineProbe:
    """在**独立沙箱会话**内对基线自身跑模板 ``checks``（T1.2 / v1.3.1 T1.1）。

    降级优先（**永不阻断**）：

    - ``files`` 为空 → ``skipped("baseline_unavailable")``
    - ``sandbox_provider is None`` → ``skipped("no_sandbox")``
    - ``checks`` 为空 → ``skipped("no_checks")``
    - 全部检查 requires 缺失 → ``skipped("env_missing")``（**不算 unhealthy**）
    - 全部检查 ``requires_files`` 缺失 → ``skipped("baseline_incomplete")``
      （v1.3.1 新增第 6 个降级原因：基线不完整 ≠ 基线不健康）
    - 会话获取 / 写入异常 → ``skipped("sandbox_error")``

    否则：全部检查通过 → ``healthy``；任一失败 → ``unhealthy``。
    ``skipped`` 与 ``unhealthy`` 的区别是**决定性的**：前者"不知道"，后者"基线真的坏"。

    ★ **M8**：**不**接收 ``probe_mode`` 入参 —— 同一个值只由
    :func:`build_probe_tree` 的返回值决定（双真相源会允许"调用方声明 raw、
    实际却补了骨架"）。

    Args:
        skeleton: 模板骨架（纯数据，由 ``harness._probe`` 从
            ``EvalTask.baseline_skeleton`` 传入）。缺省 ``None`` ⇒ 探针树 = 基线
            原样 ⇒ 与 v1.3 逐字节/逐分一致，且 ``probe_mode`` 不进报告。
    """
    started = time.perf_counter()
    if cache is not None and cache_key:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit

    if not files:
        return _degraded(SKIP_BASELINE_UNAVAILABLE, "基线正文不可用（未解析到基线）", started)
    commands = [check for check in checks if check.type == "command" and check.run.strip()]
    if not commands:
        return _degraded(SKIP_NO_CHECKS, "模板未声明任何 checks", started)
    if sandbox_provider is None:
        return _degraded(SKIP_NO_SANDBOX, "未装配沙箱提供者", started)

    tree, probe_mode, skeleton_added = build_probe_tree(files, skeleton)

    # ---- requires_files 过滤（v1.3.1 T1.2）：基线里没有该检查依赖的文件 ⇒ 不执行 ----
    # 跳过原因与"镜像没这个程序"（``requires_missing:<bin>``）**字符串不同**，
    # 因为问题不同：一个是"文件不在树里"，一个是"环境没这个工具"。
    planned: list[tuple[EvalCheck, str]] = []
    runnable: list[EvalCheck] = []
    for check in commands:
        missing = missing_requires_files(check, tree)
        if missing:
            planned.append((check, f"{SKIP_FILE_MISSING_PREFIX}{missing[0]}"))
        else:
            planned.append((check, ""))
            runnable.append(check)

    if not runnable:
        # 全部 skipped ⇒ 基线不完整（**不写缓存**：与 env_missing 同理，
        # "此刻基线缺文件"会随基线上传/提升而改变）
        details = tuple(
            BaselineCheck(
                command=check.run,
                skipped=True,
                skipped_reason=reason,
                output=f"基线缺少该检查依赖的文件：{reason[len(SKIP_FILE_MISSING_PREFIX):]}",
            )
            for check, reason in planned
        )
        return BaselineProbe(
            status=BASELINE_SKIPPED,
            checks=details,
            reason=SKIP_BASELINE_INCOMPLETE,
            duration_ms=_elapsed_ms(started),
            probe_mode=probe_mode,
            skeleton_added=skeleton_added,
        )

    try:
        async with sandbox_provider.session() as session:
            # ★ 写进沙箱的是**探针树**（骨架补缺后的基线），不是基线原样
            for path, content in tree.items():
                await session.write_file(path, content)
            runs = await run_checks(session, runnable, exec_timeout=exec_timeout)
    except Exception as exc:  # noqa: BLE001 - 会话异常一律降级，绝不阻断评测
        return _degraded(
            SKIP_SANDBOX_ERROR,
            f"基线探测沙箱异常: {exc}",
            started,
            probe_mode=probe_mode,
            skeleton_added=skeleton_added,
        )

    details = _merge_details(planned, runs)
    executed = [run for run in runs if run.status != "skipped"]
    if not executed:
        # ★ QA F5：**不写缓存**。``env_missing`` 表达的是"此刻环境缺失"（如镜像还没装
        # node），一旦缓存，"后来装上了"在**整个进程生命周期**内都拿不到真实结论。
        # 其它降级分支同样不写缓存，此处保持一致。
        return BaselineProbe(
            status=BASELINE_SKIPPED,
            checks=details,
            reason=SKIP_ENV_MISSING,
            duration_ms=_elapsed_ms(started),
            probe_mode=probe_mode,
            skeleton_added=skeleton_added,
        )

    failed = [run for run in executed if run.status == "failed"]
    passed_count = len(executed) - len(failed)
    score = round(passed_count / len(executed) * 100, 2)
    reason = "" if not failed else f"基线检查未通过：{failed[0].command}"
    probe = BaselineProbe(
        status=BASELINE_UNHEALTHY if failed else BASELINE_HEALTHY,
        score=score,
        passed=not failed,
        checks=details,
        reason=reason,
        duration_ms=_elapsed_ms(started),
        probe_mode=probe_mode,
        skeleton_added=skeleton_added,
    )
    _store(cache, cache_key, probe)
    return probe


def _merge_details(
    planned: Sequence[tuple[EvalCheck, str]], runs: Sequence[Any]
) -> tuple[BaselineCheck, ...]:
    """把「声明顺序（含前置 skipped）」与「实际执行结果」拼回**声明顺序**明细。

    ``requires_files`` 缺失的命令**不交给** ``run_checks``（因此不在 ``runs`` 里），
    若直接按 ``runs`` 顺序出明细，报告里会丢掉这些检查 —— 而它们恰恰是
    「基线不完整」的证据，必须保留。
    """
    results = iter(runs)
    details: list[BaselineCheck] = []
    for check, skip_reason in planned:
        if skip_reason:
            details.append(
                BaselineCheck(
                    command=check.run,
                    skipped=True,
                    skipped_reason=skip_reason,
                    output=(
                        "基线缺少该检查依赖的文件："
                        f"{skip_reason[len(SKIP_FILE_MISSING_PREFIX):]}"
                    ),
                )
            )
            continue
        run = next(results, None)
        if run is None:  # 构造上不可达（runnable 与 runs 一一对应）
            details.append(
                BaselineCheck(command=check.run, skipped=True, skipped_reason="")
            )
            continue
        details.append(
            BaselineCheck(
                command=run.command,
                exit_code=run.exit_code,
                timed_out=run.timed_out,
                skipped=run.status == "skipped",
                output=run.output,
                skipped_reason=run.skipped_reason if run.status == "skipped" else "",
            )
        )
    return tuple(details)



def _degraded(
    reason: str,
    message: str,
    started: float,
    *,
    probe_mode: str = "",
    skeleton_added: tuple[str, ...] = (),
) -> BaselineProbe:
    """构造 ``skipped`` 探测结果（降级不阻断）。

    人读文案挂在唯一一条空命令的 ``BaselineCheck.output`` 上，``reason`` 只留短码 ——
    保持 ``BaselineProbe.reason`` 的取值集合封闭（docs/11 §7.5-1），报告层另行拼装文案。

    探针树已构造出来之后才失败的降级路径（``sandbox_error``）会把 ``probe_mode`` /
    ``skeleton_added`` 一起带走 —— 内部字段应当**总是**如实反映"本该跑哪棵树"。
    """
    checks: tuple[BaselineCheck, ...] = (
        (BaselineCheck(command="", output=message),) if message else ()
    )
    return BaselineProbe(
        status=BASELINE_SKIPPED,
        reason=reason,
        checks=checks,
        duration_ms=_elapsed_ms(started),
        probe_mode=probe_mode,
        skeleton_added=skeleton_added,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _store(cache: BaselineProbeCache | None, key: str, probe: BaselineProbe) -> None:
    if cache is not None and key:
        cache.put(key, probe)


__all__ = [
    "BASELINE_HEALTHY",
    "BASELINE_SKIPPED",
    "BASELINE_UNHEALTHY",
    "BaselineCheck",
    "BaselineProbe",
    "BaselineProbeCache",
    "PROBE_MODE_RAW",
    "PROBE_MODE_SKELETON_FILLED",
    "SKIP_BASELINE_INCOMPLETE",
    "SKIP_FILE_MISSING_PREFIX",
    "baseline_cache_key",
    "build_probe_tree",
    "missing_requires_files",
    "probe_baseline",
]
