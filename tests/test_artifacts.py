"""产物库测试（T03）：版本不可变 / 血缘 / diff / zip / 路径安全。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.diff import diff_file_sets
from ymir.artifacts.store import ArtifactStore
from ymir.artifacts.versioning import (
    compute_content_hash,
    normalize_files,
    normalize_rel_path,
)
from ymir.common.errors import InvalidParamError, NotFoundError, StateConflictError
from ymir.storage.repositories import ProjectRepository


async def _make_project(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="产物库测试项目")
        await session.commit()
        return project.id


def _store(session_factory: async_sessionmaker[AsyncSession], tmp_path) -> ArtifactStore:
    return ArtifactStore(session_factory, tmp_path / "artifacts")


# --------------------------------------------------------------------- versioning


class TestVersioning:
    def test_normalize_ok(self) -> None:
        assert normalize_rel_path("prd.md") == "prd.md"
        assert normalize_rel_path("./a//b/./c.py") == "a/b/c.py"
        assert normalize_rel_path("a\\b\\c.py") == "a/b/c.py"

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "/etc/passwd", "C:\\evil.txt", "c:/x", "../up.md", "a/../../b.md"],
    )
    def test_normalize_rejects(self, bad: str) -> None:
        with pytest.raises(InvalidParamError):
            normalize_rel_path(bad)

    def test_normalize_files_conflict(self) -> None:
        with pytest.raises(InvalidParamError):
            normalize_files({"a/b.py": "x", "./a//b.py": "y"})

    @pytest.mark.parametrize(
        "files",
        [
            {"a": "x", "a/b": "y"},  # a 同时是文件与目录
            {"pkg": "x", "pkg/sub/mod.py": "y"},
        ],
    )
    def test_normalize_files_prefix_conflict(self, files: dict[str, str]) -> None:
        # T03 QA P3 修复：文件与目录同名 → 业务错误（而非写盘期原生 OSError）
        with pytest.raises(InvalidParamError):
            normalize_files(files)

    def test_hash_stable_and_order_free(self) -> None:
        h1 = compute_content_hash({"a.py": "print(1)", "b.md": "# 标题"})
        h2 = compute_content_hash({"b.md": "# 标题", "a.py": "print(1)"})
        assert h1 == h2
        h3 = compute_content_hash({"a.py": "print(2)", "b.md": "# 标题"})
        assert h1 != h3


# --------------------------------------------------------------------- diff


class TestDiff:
    def test_added_removed_modified_unchanged(self) -> None:
        base = {"keep.md": "same", "mod.md": "old\nline2", "gone.md": "bye"}
        target = {"keep.md": "same", "mod.md": "new\nline2", "fresh.py": "print(1)"}
        report = diff_file_sets(base, target)
        by_path = {f.path: f for f in report.files}
        assert set(by_path) == {"mod.md", "gone.md", "fresh.py"}
        assert by_path["mod.md"].status == "modified"
        assert by_path["gone.md"].status == "removed"
        assert by_path["fresh.py"].status == "added"
        assert report.files_changed == 3
        # 统计：mod 1+1、gone 1-、fresh 1+
        assert report.additions == 2
        assert report.deletions == 2

    def test_unified_text_grouped_by_file(self) -> None:
        report = diff_file_sets({"f.py": "a\n"}, {"f.py": "b\n"})
        assert len(report.files) == 1
        text = report.files[0].diff
        assert text.startswith("--- a/f.py")
        assert "+++ b/f.py" in text
        assert "@@" in text


# --------------------------------------------------------------------- store


class TestArtifactStore:
    async def test_save_first_version(self, session_factory, tmp_path) -> None:
        project_id = await _make_project(session_factory)
        store = _store(session_factory, tmp_path)
        saved = await store.save(
            project_id=project_id,
            name="prd",
            artifact_type="prd",
            files={"prd.md": "# PRD v1", "prd.json": "{}"},
            created_by="pm",
            message="首次生成",
        )
        assert saved.version == 1
        # 文件落盘
        version_dir = store.root / project_id / saved.artifact_id / "v1"
        assert (version_dir / "prd.md").read_text(encoding="utf-8") == "# PRD v1"
        # 元数据
        loaded = await store.load(saved.artifact_id)
        assert loaded.version == 1
        assert loaded.files["prd.md"] == "# PRD v1"
        assert loaded.version_row.parent_version_id is None
        assert loaded.version_row.created_by == "pm"

    async def test_second_version_lineage_and_immutability(
        self, session_factory, tmp_path
    ) -> None:
        project_id = await _make_project(session_factory)
        store = _store(session_factory, tmp_path)
        v1 = await store.save(
            project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "# v1"}
        )
        v2 = await store.save(
            project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "# v2"}
        )
        assert v2.artifact_id == v1.artifact_id  # 同名复用
        assert v2.version == 2

        loaded1 = await store.load(v2.artifact_id, 1)
        loaded2 = await store.load(v2.artifact_id, 2)
        # 版本不可变：v1 内容保持原样
        assert loaded1.files["prd.md"] == "# v1"
        assert loaded2.files["prd.md"] == "# v2"
        # 血缘：v2.parent = v1
        assert loaded2.version_row.parent_version_id == loaded1.version_row.id
        # current_version 指向最新
        _, versions = await store.get_artifact_detail(v2.artifact_id)
        assert [row.version for row in versions] == [1, 2]

    async def test_version_dir_conflict_rejected(self, session_factory, tmp_path) -> None:
        project_id = await _make_project(session_factory)
        store = _store(session_factory, tmp_path)
        v1 = await store.save(
            project_id=project_id, name="prd", artifact_type="prd", files={"a.md": "1"}
        )
        # 模拟 v2 目录被外部占用（版本不可变：禁止覆盖）
        (store.root / project_id / v1.artifact_id / "v2").mkdir(parents=True)
        with pytest.raises(StateConflictError):
            await store.save(
                project_id=project_id, name="prd", artifact_type="prd", files={"a.md": "2"}
            )

    async def test_path_traversal_rejected(self, session_factory, tmp_path) -> None:
        project_id = await _make_project(session_factory)
        store = _store(session_factory, tmp_path)
        with pytest.raises(InvalidParamError):
            await store.save(
                project_id=project_id,
                name="code",
                artifact_type="code",
                files={"../escape.py": "boom"},
            )

    async def test_load_missing(self, session_factory, tmp_path) -> None:
        store = _store(session_factory, tmp_path)
        with pytest.raises(NotFoundError):
            await store.load("no-such-artifact")
        project_id = await _make_project(session_factory)
        saved = await store.save(
            project_id=project_id, name="prd", artifact_type="prd", files={"p.md": "x"}
        )
        with pytest.raises(NotFoundError):
            await store.load(saved.artifact_id, 99)

    async def test_latest_by_name(self, session_factory, tmp_path) -> None:
        project_id = await _make_project(session_factory)
        store = _store(session_factory, tmp_path)
        assert await store.latest_by_name(project_id, "prd") is None
        await store.save(
            project_id=project_id, name="prd", artifact_type="prd", files={"p.md": "v1"}
        )
        await store.save(
            project_id=project_id, name="prd", artifact_type="prd", files={"p.md": "v2"}
        )
        latest = await store.latest_by_name(project_id, "prd")
        assert latest is not None
        assert latest.version == 2
        assert latest.files["p.md"] == "v2"

    async def test_diff_end_to_end(self, session_factory, tmp_path) -> None:
        project_id = await _make_project(session_factory)
        store = _store(session_factory, tmp_path)
        saved = await store.save(
            project_id=project_id,
            name="prd",
            artifact_type="prd",
            files={"prd.md": "需求 A\n需求 B\n", "extra.json": "{}"},
        )
        await store.save(
            project_id=project_id,
            name="prd",
            artifact_type="prd",
            files={"prd.md": "需求 A\n需求 C\n", "extra.json": "{}"},
        )
        report = await store.diff(saved.artifact_id, 1, 2)
        assert report.files_changed == 1
        assert report.files[0].path == "prd.md"
        assert report.files[0].status == "modified"
        assert "-需求 B" in report.files[0].diff
        assert "+需求 C" in report.files[0].diff

    async def test_export_zip(self, session_factory, tmp_path) -> None:
        project_id = await _make_project(session_factory)
        store = _store(session_factory, tmp_path)
        await store.save(
            project_id=project_id,
            name="prd",
            artifact_type="prd",
            files={"prd.md": "# v1"},
            message="初版",
        )
        await store.save(
            project_id=project_id,
            name="prd",
            artifact_type="prd",
            files={"prd.md": "# v2"},
            message="修订",
        )
        await store.save(
            project_id=project_id,
            name="design",
            artifact_type="design",
            files={"design.md": "# 设计"},
        )
        payload = await store.export_zip(project_id)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "prd/v2/prd.md" in names  # 只导最新版本内容
            assert "design/v1/design.md" in names
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            prd_meta = next(a for a in manifest["artifacts"] if a["name"] == "prd")
            assert [v["version"] for v in prd_meta["versions"]] == [1, 2]
            assert prd_meta["versions"][1]["message"] == "修订"
            assert zf.read("prd/v2/prd.md").decode("utf-8") == "# v2"
