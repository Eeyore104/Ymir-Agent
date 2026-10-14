"""产物库（架构设计 §3.6）：版本不可变 + 血缘 + diff + zip 导出。"""

from ymir.artifacts.diff import DiffReport, FileDiff, diff_file_sets
from ymir.artifacts.store import ArtifactStore, LoadedVersion, SavedVersion
from ymir.artifacts.versioning import (
    compute_content_hash,
    normalize_files,
    normalize_rel_path,
)

__all__ = [
    "ArtifactStore",
    "DiffReport",
    "FileDiff",
    "LoadedVersion",
    "SavedVersion",
    "compute_content_hash",
    "diff_file_sets",
    "normalize_files",
    "normalize_rel_path",
]
