"""基线快照解析（v1.2，docs/09 §4.2.1 步骤 ①②）。

把用户上传的 zip **安全地**解成 ``{相对路径: 文本}``，供
:meth:`ymir.artifacts.store.ArtifactStore.save` 落成 ``type=baseline`` 的不可变产物。

安全与预算（全在**纯函数**里做，便于单测且保证"校验先于任何磁盘写入"）：

1. **zip slip**：每个条目名都过 :func:`ymir.common.paths.normalize_rel_path`
   （拒绝 ``../x`` / ``/abs`` / ``C:/win``）；不做 ``extractall``，路径只当字典键；
2. **重复路径**：归一化后撞名直接 1001（否则静默覆盖）；
3. **数量/体积上限**：先按 zip 头里声明的 ``file_size`` 预估（防 zip 炸弹解压爆内存），
   再按实际内容复核（声明值不可信）；
4. **文本口径**：v1.2 的基线是**源码快照**，只接受 UTF-8 文本；二进制文件报 1001
   并指明路径（避免"悄悄存了乱码"的不可审计状态）。
"""

from __future__ import annotations

import io
import zipfile

from ymir.common.errors import InvalidParamError
from ymir.common.paths import normalize_rel_path

#: 上传时忽略的条目（mac / windows 的目录元数据噪音）
_IGNORED_PREFIXES: tuple[str, ...] = ("__MACOSX/",)
_IGNORED_NAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})


def read_zip_files(
    data: bytes, *, max_files: int, max_bytes: int
) -> tuple[dict[str, str], int]:
    """解析 zip 字节流；返回 ``(files, size_bytes)``。

    Args:
        data: 原始 zip 字节。
        max_files: 文件数上限（``Settings.baseline_max_files``）。
        max_bytes: 解压后总字节上限（``Settings.baseline_max_bytes``）。

    Returns:
        ``files``：``归一化相对路径 -> 文本内容``；``size_bytes``：内容总字节数。

    Raises:
        InvalidParamError: 非 zip / 路径非法 / 重复路径 / 超上限 / 非 UTF-8 文本（1001）。
    """
    if not data:
        raise InvalidParamError("基线内容为空（未收到 zip 字节）")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidParamError(f"基线不是合法的 zip 文件: {exc}") from exc

    names: list[str] = []
    declared_bytes = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename in _IGNORED_NAMES:
                continue
            if info.filename.startswith(_IGNORED_PREFIXES):
                continue
            names.append(info.filename)
            declared_bytes += max(0, info.file_size)
        if len(names) > max_files:
            raise InvalidParamError(f"基线文件数 {len(names)} 超过上限 {max_files}")
        if declared_bytes > max_bytes:
            raise InvalidParamError(
                f"基线解压后约 {declared_bytes} 字节，超过上限 {max_bytes}"
            )

        files: dict[str, str] = {}
        size_bytes = 0
        for name in names:
            path = normalize_rel_path(name)
            if path in files:
                raise InvalidParamError(f"基线包含重复路径: {path}")
            raw = archive.read(name)
            size_bytes += len(raw)
            if size_bytes > max_bytes:
                raise InvalidParamError(
                    f"基线解压后超过上限 {max_bytes} 字节（实际已累计 {size_bytes}）"
                )
            try:
                files[path] = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidParamError(
                    f"基线包含非 UTF-8 文本文件: {path}（v1.2 基线只接受源码文本）"
                ) from exc
    if not files:
        raise InvalidParamError("基线 zip 中没有可用的文件")
    return files, size_bytes
