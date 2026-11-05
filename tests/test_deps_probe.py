"""T03.4 / T1-R2：依赖声明探针的**纯函数**用例（v1.5，docs/17 §1.2.3）。

**要防的裂缝**：``"因为镜像没装依赖而没跑"`` 与 ``"代码真的坏了"`` 在报告里长得一样。
判据只能是**运行前、与代码无关、可重复**的那三条（读交付树的依赖声明 ∩ 镜像预装清单），
**禁止**解析报错文本（同一棵树两次跑可能给出不同结论，那不是判据）。

本文件钉住四件事：

1. 三种声明形态都能读（``requirements*.txt`` / ``pyproject.toml[project].dependencies`` /
   任意层级的 ``package.json``，含 ``web/package.json``）；
2. URL / ``-e`` / ``-r`` / ``--index-url`` 行**跳过不判**，并计入
   ``deps_probe_ignored:<n>``（不静默）；
3. 差集按**生态**分组（``python:*`` 不能因为 ``web/package.json`` 缺包而跳过）；
4. 纯函数纪律：畸形输入**绝不抛**（坏文件 = 忽略，不是异常）。
"""

from __future__ import annotations

import pytest

from ymir.evaluation.deps import (
    ECOSYSTEM_NODE,
    ECOSYSTEM_PYTHON,
    IGNORED_PREFIX,
    MISSING_DEPS_PREFIX,
    check_requires_deps,
    declared_deps,
    describe_ignored,
    image_package_set,
    missing_deps_by_language,
    missing_deps_for,
    missing_image_deps,
    normalize_dep_name,
)

#: 测试用镜像包表（**故意比真实表小**：只放断言需要的，避免被镜像内容牵着走）
IMAGE: dict[str, list[str]] = {
    "python": ["fastapi", "pytest", "pandas"],
    "node": ["express", "typescript"],
}


# --------------------------------------------------------------------- 名字归一


class TestNormalizeDepName:
    """只解析"名字形态"：截到名字为止，非法 ⇒ ``""``。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("fastapi", "fastapi"),
            ("fastapi>=0.115", "fastapi"),
            ("uvicorn[standard]>=0.30", "uvicorn"),
            ("pytest;python_version<'3.13'", "pytest"),
            ("  httpx  ", "httpx"),
            ("@types/node", "@types/node"),
            ("@types/node@22", "@types/node"),
            ("vue-router@3.0.0", "vue-router"),
        ],
    )
    def test_normalizes_to_bare_name(self, raw: str, expected: str) -> None:
        assert normalize_dep_name(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "-e .", "--index-url https://x", "https://example.com/x.whl", "git+https://a/b"],
    )
    def test_non_name_forms_are_empty(self, raw: str) -> None:
        assert normalize_dep_name(raw) == ""


# --------------------------------------------------------------------- 三种声明


class TestDeclaredDeps:
    """三种声明形态 + 「被忽略的行数」。"""

    def test_requirements_txt(self) -> None:
        files = {
            "requirements.txt": (
                "# 依赖\n"
                "fastapi>=0.115\n"
                "uvicorn[standard]\n"
                "\n"
                "-e .\n"
                "-r other.txt\n"
                "--index-url https://pypi.org/simple\n"
                "https://example.com/pkg.whl\n"
                "pkg-not-in-image\n"
            )
        }
        declared, ignored = declared_deps(files)
        assert declared[ECOSYSTEM_PYTHON] == {
            "fastapi",
            "uvicorn",
            "pkg-not-in-image",
        }
        # 4 行非名字形态（-e / -r / --index-url / URL）
        assert ignored == 4
        assert describe_ignored(ignored) == f"{IGNORED_PREFIX}4"

    def test_requirements_in_subdirectory(self) -> None:
        """``backend/requirements-dev.txt`` 也算依赖声明（按文件名判定）。"""
        declared, _ = declared_deps({"backend/requirements-dev.txt": "pandas\n"})
        assert declared[ECOSYSTEM_PYTHON] == {"pandas"}

    def test_pyproject_pep621(self) -> None:
        files = {
            "pyproject.toml": (
                "[project]\n"
                'name = "demo"\n'
                'dependencies = ["fastapi>=0.115", "uvicorn[standard]", "pkg-x>=1"]\n'
                "\n[project.optional-dependencies]\n"
                'dev = ["not-read"]\n'
            )
        }
        declared, ignored = declared_deps(files)
        # 只读 [project].dependencies（optional-dependencies 不在"交付依赖"口径内）
        assert declared[ECOSYSTEM_PYTHON] == {"fastapi", "uvicorn", "pkg-x"}
        assert ignored == 0

    def test_package_json_including_web(self) -> None:
        files = {
            "web/package.json": (
                '{"dependencies": {"vue": "^3.4.0"},'
                ' "devDependencies": {"vite": "^5.0.0", "vue-tsc": "^2"}}'
            )
        }
        declared, _ = declared_deps(files)
        assert declared[ECOSYSTEM_NODE] == {"vue", "vite", "vue-tsc"}
        assert declared[ECOSYSTEM_PYTHON] == frozenset()

    def test_empty_tree_declares_nothing(self) -> None:
        declared, ignored = declared_deps({})
        assert declared == {ECOSYSTEM_PYTHON: frozenset(), ECOSYSTEM_NODE: frozenset()}
        assert ignored == 0

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "[]",
            '{"dependencies": "not-a-dict"}',
            '{"dependencies": {"": "x"}}',
        ],
    )
    def test_corrupt_package_json_never_raises(self, content: str) -> None:
        declared, _ = declared_deps({"package.json": content})
        assert declared[ECOSYSTEM_NODE] == frozenset()

    def test_corrupt_toml_never_raises(self) -> None:
        declared, _ = declared_deps({"pyproject.toml": "[project\nname="})
        assert declared[ECOSYSTEM_PYTHON] == frozenset()


# --------------------------------------------------------------------- 差集


class TestMissingDeps:
    """``declared − image``：按生态分组 + 扁平形态。"""

    def test_missing_is_grouped_by_ecosystem(self) -> None:
        files = {
            "requirements.txt": "fastapi\npkg-not-in-image\n",
            "web/package.json": '{"dependencies": {"vue": "^3"}}',
        }
        missing, _ = missing_deps_for(files, IMAGE)
        assert missing[ECOSYSTEM_PYTHON] == ("pkg-not-in-image",)
        assert missing[ECOSYSTEM_NODE] == ("vue",)

    def test_flat_form_is_ordered_and_deduped(self) -> None:
        declared = {
            ECOSYSTEM_PYTHON: frozenset({"b-missing", "a-missing"}),
            ECOSYSTEM_NODE: frozenset({"c-missing", "a-missing"}),
        }
        flat = missing_image_deps(declared, IMAGE)
        assert flat == ("a-missing", "b-missing", "c-missing")
        assert len(set(flat)) == len(flat)

    def test_unknown_ecosystem_table_is_not_judged(self) -> None:
        """镜像表缺该生态 ⇒ **不判**（"表未知"≠"全缺"，否则一切永久未验证）。"""
        declared = {ECOSYSTEM_NODE: frozenset({"vue"})}
        assert missing_deps_by_language(declared, {"python": ["fastapi"]}) == {}
        assert missing_deps_by_language(declared, None) == {}

    def test_declared_present_is_not_missing(self) -> None:
        """反例（docs/17 A2 的 ``test_declared_dep_present_still_runs``）：真在表里 ⇒ 不缺失。"""
        missing, _ = missing_deps_for({"requirements.txt": "fastapi\npytest\n"}, IMAGE)
        # 两张镜像表都非空 ⇒ 两个生态都会出现在结果里（空元组 = 该生态不缺）
        assert missing == {ECOSYSTEM_PYTHON: (), ECOSYSTEM_NODE: ()}

    def test_case_insensitive_comparison(self) -> None:
        declared = {ECOSYSTEM_PYTHON: frozenset({"FastAPI"})}
        assert missing_deps_by_language(declared, IMAGE) == {ECOSYSTEM_PYTHON: ()}


# --------------------------------------------------------------------- 判定


class TestCheckRequiresDeps:
    """``run_checks`` 用的判定入口：返回**跳过原因**或空串。"""

    MISSING = {ECOSYSTEM_PYTHON: ("pkg-a",), ECOSYSTEM_NODE: ()}

    def test_wildcard_token_uses_its_own_ecosystem(self) -> None:
        """★ 关键：``python:*`` 只受 python 侧缺失影响（node 缺包不能连坐后端）。"""
        missing = {ECOSYSTEM_PYTHON: (), ECOSYSTEM_NODE: ("vue",)}
        assert (
            check_requires_deps(["python:*"], missing_deps=missing, image_packages=IMAGE) == ""
        )
        assert check_requires_deps(
            ["node:*"], missing_deps=missing, image_packages=IMAGE
        ) == f"{MISSING_DEPS_PREFIX}vue"

    def test_wildcard_reports_first_missing_package(self) -> None:
        assert (
            check_requires_deps(["python:*"], missing_deps=self.MISSING, image_packages=IMAGE)
            == f"{MISSING_DEPS_PREFIX}pkg-a"
        )

    def test_concrete_package_must_be_in_any_table(self) -> None:
        assert check_requires_deps(["pandas"], missing_deps={}, image_packages=IMAGE) == ""
        assert (
            check_requires_deps(["pkg-not-in-image"], missing_deps={}, image_packages=IMAGE)
            == f"{MISSING_DEPS_PREFIX}pkg-not-in-image"
        )

    def test_unknown_image_table_disables_the_probe(self) -> None:
        """镜像表未知 ⇒ **不判定**（老调用方 / 未注入的 EvalTask 零影响）。"""
        for table in (None, {}):
            assert (
                check_requires_deps(
                    ["python:*"], missing_deps=self.MISSING, image_packages=table
                )
                == ""
            )

    def test_no_declaration_is_always_runnable(self) -> None:
        assert check_requires_deps([], missing_deps=self.MISSING, image_packages=IMAGE) == ""
        assert check_requires_deps(None, missing_deps=self.MISSING, image_packages=IMAGE) == ""

    def test_blank_tokens_are_ignored(self) -> None:
        assert (
            check_requires_deps(["", "  "], missing_deps=self.MISSING, image_packages=IMAGE) == ""
        )

    def test_image_package_set_flattens_and_folds_case(self) -> None:
        assert image_package_set(IMAGE) == {"fastapi", "pytest", "pandas", "express", "typescript"}
        assert image_package_set(IMAGE, ECOSYSTEM_PYTHON) == {"fastapi", "pytest", "pandas"}
