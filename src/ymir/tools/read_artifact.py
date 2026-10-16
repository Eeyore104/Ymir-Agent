"""read_artifact 工具：按产物名读取项目内最新版本（上游引用解析）。

执行器用它把「阶段输入引用」（如 prd / design）解析为可注入提示词的
上游产物内容；缺少的产物由调用方决策（跳过或报错）。
"""

from __future__ import annotations

from ymir.artifacts.store import ArtifactStore
from ymir.common.contracts import UpstreamArtifact
from ymir.models.entities import DEFAULT_TENANT_ID


async def read_artifacts(
    store: ArtifactStore,
    project_id: str,
    names: list[str],
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[UpstreamArtifact]:
    """按名字读取项目内产物最新版本；不存在或尚未产出的名字跳过。"""
    artifacts: list[UpstreamArtifact] = []
    for name in names:
        loaded = await store.latest_by_name(project_id, name, tenant_id=tenant_id)
        if loaded is None:
            continue
        artifacts.append(
            UpstreamArtifact(name=name, version=loaded.version, files=loaded.files)
        )
    return artifacts
