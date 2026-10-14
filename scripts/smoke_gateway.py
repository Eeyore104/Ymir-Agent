"""模型网关真实调用冒烟（手动触发，产生真实费用）。

验证链路（T2-5 验收点③）：
1. ``config/models.yaml`` 路由解析（按 tier 找可用候选）；
2. ``ModelGateway.chat`` 真实调用 DeepSeek 并返回内容；
3. token / 成本 / 延迟落 ``model_calls`` 表（预算与计量数据源）。

用法::

    cp .env.example .env          # 填入 DEEPSEEK_API_KEY
    uv run python scripts/smoke_gateway.py
    uv run python scripts/smoke_gateway.py --tier planner --prompt "一句话解释状态机"

退出码：0 成功；1 配置缺失或调用失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from ymir.config import get_settings, load_env_file
from ymir.gateway.gateway import build_gateway
from ymir.models.entities import ModelCall
from ymir.storage.db import build_engine, build_session_factory


async def run_smoke(tier: str, prompt: str) -> int:
    load_env_file()
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        gateway = build_gateway(session_factory)

        candidates = [
            f"{route.provider}/{route.model}" for route in gateway.router.candidates(tier)
        ]
        print(f"[1/4] tier={tier} 可用候选: {candidates or '（无）'}")
        if not candidates:
            print(
                "!! 无可用候选：请在仓库根 .env 中设置 DEEPSEEK_API_KEY 后重试",
                file=sys.stderr,
            )
            return 1

        print(f"[2/4] 发起真实调用: {prompt!r}")
        result = await gateway.chat(
            [{"role": "user", "content": prompt}],
            tier=tier,
            temperature=0.0,
            max_tokens=128,
        )
        content = result.content.strip()
        print(f"[3/4] 返回（{result.provider}/{result.model}，{result.latency_ms}ms）：")
        for line in content[:600].splitlines() or ["<空>"]:
            print(f"      {line}")
        print(
            f"      usage: prompt={result.usage.prompt_tokens} "
            f"completion={result.usage.completion_tokens} "
            f"cost=¥{result.cost_cny:.6f}"
        )

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ModelCall).order_by(ModelCall.created_at.desc()).limit(1)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            print("!! model_calls 未落库", file=sys.stderr)
            return 1
        last = rows[0]
        print(f"[4/4] model_calls 落库确认: id={last.id}")
        print(
            f"      provider={last.provider} model={last.model} tier={last.tier} "
            f"tokens={last.prompt_tokens}+{last.completion_tokens} "
            f"cost=¥{last.cost_cny:.6f} latency={last.latency_ms}ms"
        )
        print("\nSMOKE OK")
        return 0
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本：如实报告失败原因
        print(f"!! 冒烟失败: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="模型网关真实调用冒烟（产生真实费用）")
    parser.add_argument(
        "--tier",
        choices=["planner", "executor"],
        default="executor",
        help="模型层级（planner=推理模型 / executor=快模型）",
    )
    parser.add_argument(
        "--prompt",
        default="用一句话说明软件工程中的「需求评审」是什么。",
        help="发给模型的用户消息",
    )
    args = parser.parse_args()
    return asyncio.run(run_smoke(args.tier, args.prompt))


if __name__ == "__main__":
    raise SystemExit(main())
