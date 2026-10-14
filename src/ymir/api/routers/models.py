"""模型网关配置视图（§6.1）：``GET /models``（脱敏）。

只暴露 provider 名称 / 端点 / 密钥就绪状态与价目，绝不回传密钥内容。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import GatewayDep
from ymir.api.schemas import ApiResponse, ModelInfo, ModelsOut, ProviderInfo

router = APIRouter(tags=["models"])


@router.get(
    "/models",
    response_model=ApiResponse[ModelsOut],
    summary="模型提供方与路由配置（脱敏）",
)
async def list_models(gateway: GatewayDep) -> dict[str, object]:
    config = gateway.registry.config
    providers = [
        ProviderInfo(
            name=name,
            base_url=spec.base_url,
            enabled=spec.enabled,
            key_ready=gateway.registry.api_key(name) is not None,
            models=[
                ModelInfo(
                    name=model_name,
                    tier=model_spec.tier,
                    price_in=model_spec.price_in,
                    price_out=model_spec.price_out,
                )
                for model_name, model_spec in spec.models.items()
            ],
        )
        for name, spec in config.providers.items()
    ]
    data = ModelsOut(
        version=config.version,
        providers=providers,
        routing={tier: list(models) for tier, models in config.routing.items()},
        budget={
            "per_run_tokens": float(config.budget.per_run_tokens),
            "per_day_cost_cny": float(config.budget.per_day_cost_cny),
        },
    )
    return {"code": 0, "data": data}
