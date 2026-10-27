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

分层（docs/11 §7.5-6）：``evaluation``(rank 1) 只依赖 ``sandbox``(3) / ``common``(5)
与本包；**禁止** import ``templates``(2) —— 检查项以纯数据 ``EvalCheck`` 传入。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

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
    files: Mapping[str, str], checks: Sequence[EvalCheck]
) -> str:
    """基线指纹 + checks 指纹（指纹变了就不能复用探测结果）。

    ★ QA F3：**checks 的全部执行相关字段**都要进指纹 —— ``type`` / ``run`` /
    ``weight`` / ``group`` / ``blocking`` / ``requires`` / ``timeout_s``。
    少拼任何一个都会造成"改了 ``requires: node`` 仍命中旧探测结果"的幽灵缓存
    （``requires`` 直接决定某条检查是 skipped 还是真跑，必须参与指纹）。
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
    return digest.hexdigest()


async def probe_baseline(
    *,
    files: Mapping[str, str],
    checks: Sequence[EvalCheck],
    sandbox_provider: Any | None,
    exec_timeout: float = 120.0,
    cache: BaselineProbeCache | None = None,
    cache_key: str = "",
) -> BaselineProbe:
    """在**独立沙箱会话**内对基线自身跑模板 ``checks``（T1.2）。

    降级优先（**永不阻断**）：

    - ``files`` 为空 → ``skipped("baseline_unavailable")``
    - ``sandbox_provider is None`` → ``skipped("no_sandbox")``
    - ``checks`` 为空 → ``skipped("no_checks")``
    - 全部检查 requires 缺失 → ``skipped("env_missing")``（**不算 unhealthy**）
    - 会话获取 / 写入异常 → ``skipped("sandbox_error")``

    否则：全部检查通过 → ``healthy``；任一失败 → ``unhealthy``。
    ``skipped`` 与 ``unhealthy`` 的区别是**决定性的**：前者"不知道"，后者"基线真的坏"。
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

    try:
        async with sandbox_provider.session() as session:
            for path, content in files.items():
                await session.write_file(path, content)
            runs = await run_checks(session, commands, exec_timeout=exec_timeout)
    except Exception as exc:  # noqa: BLE001 - 会话异常一律降级，绝不阻断评测
        return _degraded(SKIP_SANDBOX_ERROR, f"基线探测沙箱异常: {exc}", started)

    details = tuple(
        BaselineCheck(
            command=run.command,
            exit_code=run.exit_code,
            timed_out=run.timed_out,
            skipped=run.status == "skipped",
            output=run.output,
        )
        for run in runs
    )
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
    )
    _store(cache, cache_key, probe)
    return probe


def _degraded(reason: str, message: str, started: float) -> BaselineProbe:
    """构造 ``skipped`` 探测结果（降级不阻断）。

    人读文案挂在唯一一条空命令的 ``BaselineCheck.output`` 上，``reason`` 只留短码 ——
    保持 ``BaselineProbe.reason`` 的取值集合封闭（docs/11 §7.5-1），报告层另行拼装文案。
    """
    checks: tuple[BaselineCheck, ...] = (
        (BaselineCheck(command="", output=message),) if message else ()
    )
    return BaselineProbe(
        status=BASELINE_SKIPPED,
        reason=reason,
        checks=checks,
        duration_ms=_elapsed_ms(started),
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
    "baseline_cache_key",
    "probe_baseline",
]
