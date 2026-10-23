"""模板库接口（v1.2 · T4.1，docs/09 §4.1.2 / §4.4.3）。

- ``GET /templates``：模板列表。**坏模板不拖垮整体**：该条目 ``loading_error`` 非空，
  其余照常返回，HTTP 仍是 200（``templates/loader.list_templates`` 的既有语义）。
- ``GET /templates/{id}``：模板详情。**绝不回传骨架正文** —— 只给
  ``reserved_paths`` / ``seed_paths`` 路径清单 + stack / layout / checks，
  避免"骨架实现细节"经 API 泄露给前端（也让"只给路径不给内容"的提示词纪律在
  API 侧保持一致）。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.schemas import ApiResponse, PageData, TemplateSummaryOut
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.templates import list_templates, load_template
from ymir.templates.loader import TemplateDefinitionError

router = APIRouter(tags=["templates"])


@router.get(
    "/templates",
    response_model=ApiResponse[PageData[TemplateSummaryOut]],
    summary="模板列表（坏模板以 loading_error 暴露，不整体失败）",
)
async def list_all_templates() -> dict[str, object]:
    """列举模板目录下全部模板（平台自带，只读）。"""
    entries = list_templates()
    items = [TemplateSummaryOut(**entry.to_summary()) for entry in entries]
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.get(
    "/templates/{template_id}",
    response_model=ApiResponse[TemplateSummaryOut],
    summary="模板详情（只给路径，不回传骨架正文）",
)
async def get_template(template_id: str) -> dict[str, object]:
    """单个模板详情。

    Raises:
        NotFoundError: 模板不存在（1002）。
        InvalidParamError: 模板存在但配置非法（1001，``TemplateDefinitionError``）。
    """
    try:
        spec = load_template(template_id)
    except TemplateDefinitionError as exc:
        raise InvalidParamError(f"模板定义非法: {exc}") from exc
    except NotFoundError:
        raise
    summary = TemplateSummaryOut(
        id=spec.id,
        version=spec.version,
        name=spec.name,
        description=spec.description,
        supports=list(spec.supports),
        stack=spec.stack.model_dump(mode="json"),
        layout=spec.layout.model_dump(mode="json"),
        conventions=[item.path for item in spec.conventions],
        reserved_paths=sorted(spec.reserved_paths()),
        seed_paths=sorted(spec.seed_paths()),
        checks=[check.model_dump(mode="json") for check in spec.checks],
        prompting=dict(spec.prompting),
        prompting_roles=sorted(spec.prompting),
        loading_error=None,
    )
    return {"code": 0, "data": summary}
