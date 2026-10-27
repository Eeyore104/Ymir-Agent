"""模板模型与加载器测试（v1.2 · T1.1，docs/09 §4.1.1 / §4.1.5）。

覆盖口径：
- **21 条加载期校验逐条 1 条用例**（``test_checkNN_*``）：1–17 见 v1.2，18–21 见 v1.3
  （受保护路径 / on_hit 枚举 / max_change_ratio 区间 / scoring 配分完整性）；
- 字段名反断言（``type`` 存在 / ``kind`` 不存在）+ ``kind`` 写进 YAML 必 1001；
- ``to_layout_rules()`` 与 YAML 一致；``SkeletonSpec`` 两类路径互补；
- ``list_templates`` 让坏模板以 ``loading_error`` 暴露而不整体失败。

fixture 全部放在 ``tests/fixtures/templates/``，**不依赖** ``config/templates/``
（后者由 T1.4 落两套真模板）。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from ymir.common.contracts import LayoutRules
from ymir.common.errors import BizCode, NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.templates import (
    SANDBOX_IMAGE_PACKAGES,
    CheckSpec,
    TemplateDefinitionError,
    TemplateSpec,
    check_reserved_subset,
    default_role_ids,
    dependency_name,
    list_templates,
    load_template,
    load_template_file,
)
from ymir.templates.models import SANDBOX_IMAGE_NODE_PACKAGES, SkeletonSpec

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "templates"
#: **真实**模板目录（T1.4 落的两套垂直模板）
TEMPLATES_DIR = PROJECT_ROOT / "config" / "templates"
REAL_TEMPLATE_IDS = ("fastapi-vue3", "python-cli", "data-pipeline", "node-express")
#: 已注册角色（与 config/roles/*.yaml 一致；显式传入以免测试依赖工作目录）
ROLES = {"pm", "architect", "engineer", "qa"}


def _demo() -> dict[str, Any]:
    """读取合规模板 fixture（每次返回深拷贝，用例可安全修改）。"""
    raw = yaml.safe_load((FIXTURE_DIR / "demo.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return copy.deepcopy(raw)


def _write(data: dict[str, Any], tmp_path: Path, stem: str = "demo") -> Path:
    path = tmp_path / f"{stem}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _expect_1001(tmp_path: Path, data: dict[str, Any], *, check_no: int | None = None) -> str:
    """断言加载期 1001（可选断言错误信息里带「校验 N」编号）。"""
    with pytest.raises(TemplateDefinitionError) as excinfo:
        load_template_file(_write(data, tmp_path), known_roles=ROLES)
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    message = str(excinfo.value)
    if check_no is not None:
        assert f"校验 {check_no}" in message, message
    return message


# --------------------------------------------------------------------- 基线：合法模板


def test_demo_fixture_loads_and_normalizes() -> None:
    """合规 fixture 加载通过，路径保持 POSIX 归一化形态。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    assert isinstance(spec, TemplateSpec)
    assert spec.id == "demo"
    assert spec.stack.frontend == "vue3"
    assert spec.skeleton.reserved_paths == {".env.example", "pyproject.toml"}
    assert spec.skeleton.seed_paths == {"app/main.py", "requirements.txt"}
    assert set(spec.skeleton_files()) == spec.skeleton.all_paths


def test_load_template_by_id_ok() -> None:
    """按 id 加载（目录扫描口径）。"""
    spec = load_template("demo", templates_dir=FIXTURE_DIR, known_roles=ROLES)
    assert spec.id == "demo"


def test_load_template_missing_is_1002() -> None:
    """不存在的模板 → ``NotFoundError``（1002）。"""
    with pytest.raises(NotFoundError) as excinfo:
        load_template("nope", templates_dir=FIXTURE_DIR, known_roles=ROLES)
    assert excinfo.value.biz_code == BizCode.NOT_FOUND


# ------------------------------------------------------- 加载期校验逐条（1–17 / v1.3 18–21）


def test_check01_id_must_equal_file_stem(tmp_path: Path) -> None:
    """校验 1：``id`` 必须等于文件名主干。"""
    data = _demo()
    data["id"] = "other"
    _expect_1001(tmp_path, data, check_no=1)


def test_check02_version_not_empty(tmp_path: Path) -> None:
    """校验 2：``version`` 非空。"""
    data = _demo()
    data["version"] = "   "
    _expect_1001(tmp_path, data, check_no=2)


def test_check03_language_allowlist(tmp_path: Path) -> None:
    """校验 3（v1.3 放宽）：``python`` / ``typescript`` 合法，其它一律 1001。

    归一化（大小写）只用于比对，``stack.language`` 原样保留。
    """
    for language in ("python", "typescript", "Python", "TYPESCRIPT"):
        data = _demo()
        data["stack"]["language"] = language
        data["stack"]["dependencies"] = []
        spec = load_template_file(_write(data, tmp_path), known_roles=ROLES)
        assert spec.stack.language == language

    for language in ("java", "go", "ruby", ""):
        data = _demo()
        data["stack"]["language"] = language
        _expect_1001(tmp_path, data, check_no=3)


def test_check03_language_allowlist_constant() -> None:
    """校验 3 的取值集合是 ``ALLOWED_LANGUAGES``（新增语言必须同时补镜像与骨架）。"""
    from ymir.templates.models import ALLOWED_LANGUAGES

    assert set(ALLOWED_LANGUAGES) == {"python", "typescript"}


def test_check04_dependencies_must_subset_image(tmp_path: Path) -> None:
    """校验 4：``stack.dependencies ⊆ 镜像清单``（fail-fast）。

    v1.3：``numpy`` / ``pandas`` 已随镜像扩表（B14 同一 PR），因此这里换成
    镜像内确实没有的包（``torch``）来验证拒绝逻辑。
    """
    data = _demo()
    data["stack"]["dependencies"] = ["fastapi", "torch"]
    message = _expect_1001(tmp_path, data, check_no=4)
    assert "torch" in message


def test_check04_node_table_is_language_selected(tmp_path: Path) -> None:
    """校验 4（v1.3 语义细化）：按 ``language`` 选表 —— python / node 各一张，互不串味。"""
    from ymir.templates.loader import image_packages_for
    from ymir.templates.models import SANDBOX_IMAGE_NODE_PACKAGES

    assert image_packages_for("python") == SANDBOX_IMAGE_PACKAGES
    assert image_packages_for("typescript") == SANDBOX_IMAGE_NODE_PACKAGES
    assert image_packages_for("java") == frozenset()
    # 两张表刻意不重叠：node 模板别指望用 pytest，python 模板别指望用 express
    assert not (set(SANDBOX_IMAGE_PACKAGES) & set(SANDBOX_IMAGE_NODE_PACKAGES))
    assert {"express", "typescript", "@types/express", "@types/node"} == set(
        SANDBOX_IMAGE_NODE_PACKAGES
    )

    data = _demo()
    data["stack"]["language"] = "typescript"
    data["stack"]["dependencies"] = ["express", "typescript>=5", "@types/node"]
    spec = load_template_file(_write(data, tmp_path), known_roles=ROLES)
    assert spec.stack.language == "typescript"

    # node 表里没有的包仍被拒（不在镜像内 ⇒ 不谎报）
    data = _demo()
    data["stack"]["language"] = "typescript"
    data["stack"]["dependencies"] = ["lodash"]
    message = _expect_1001(tmp_path, data, check_no=4)
    assert "typescript" in message and "lodash" in message

    # python 模板不能声明 node 依赖（选表是**按语言**，不是并集）
    data = _demo()
    data["stack"]["dependencies"] = ["express"]
    _expect_1001(tmp_path, data, check_no=4)


def test_check04_dependency_names_tolerate_version_specifiers() -> None:
    """校验 4 的正向口径：``fastapi>=0.115`` / ``uvicorn[standard]>=0.30`` 视为合规。"""
    assert dependency_name("fastapi>=0.115") == "fastapi"
    assert dependency_name("uvicorn[standard]>=0.30") == "uvicorn"
    assert {"pytest", "fastapi", "alembic"} <= SANDBOX_IMAGE_PACKAGES


@pytest.mark.parametrize("bad", ["../etc", "/etc/passwd", "C:/windows", "a/../../b"])
def test_check05_layout_paths_normalized(tmp_path: Path, bad: str) -> None:
    """校验 5：``layout.required_*`` / ``allowed_prefixes`` 过 ``normalize_rel_path``。"""
    data = _demo()
    data["layout"]["required_dirs"] = [bad]
    _expect_1001(tmp_path, data, check_no=5)


def test_check06_conventions_path_invalid(tmp_path: Path) -> None:
    """校验 6：``conventions[].path`` 过路径安全校验。"""
    data = _demo()
    data["conventions"] = [{"path": "../escape.md", "required": True}]
    _expect_1001(tmp_path, data, check_no=6)


def test_check06_conventions_path_duplicated(tmp_path: Path) -> None:
    """校验 6：``conventions[].path`` 互不重复（重复即静默覆盖）。"""
    data = _demo()
    data["conventions"] = [{"path": "README.md"}, {"path": "./README.md"}]
    _expect_1001(tmp_path, data, check_no=6)


@pytest.mark.parametrize("bad", ["../x", "/abs/x.py", "D:/x.py"])
def test_check07_skeleton_path_normalized(tmp_path: Path, bad: str) -> None:
    """校验 7：``skeleton.files[].path`` 过 ``normalize_rel_path``（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["files"][0]["path"] = bad
    _expect_1001(tmp_path, data, check_no=7)


def test_check07_skeleton_path_duplicated(tmp_path: Path) -> None:
    """校验 7：``skeleton.files[].path`` 互不重复。"""
    data = _demo()
    data["skeleton"]["files"][1]["path"] = "pyproject.toml"
    _expect_1001(tmp_path, data, check_no=7)


def test_check08_skeleton_content_empty(tmp_path: Path) -> None:
    """校验 8：``content`` 空串 → 1001（pydantic ``min_length=1``）。"""
    data = _demo()
    data["skeleton"]["files"][0]["content"] = ""
    _expect_1001(tmp_path, data)


def test_check08_skeleton_content_whitespace_only(tmp_path: Path) -> None:
    """校验 8：纯空白正文同样视为空 → 校验 8。"""
    data = _demo()
    data["skeleton"]["files"][0]["content"] = "   \n\n\t"
    _expect_1001(tmp_path, data, check_no=8)


def test_check09_reserved_subset_invariant(tmp_path: Path) -> None:
    """校验 9：``reserved_paths ⊄ skeleton.files`` → 1001。

    模型层面该不变量恒真（reserved 由 ``files[].reserved`` 派生），因此定点测
    校验函数本身；YAML 层面的真实形态是"skeleton 下多写一个 reserved_paths 键"，
    由 ``extra="forbid"`` 拒掉（下一条用例）。
    """
    with pytest.raises(TemplateDefinitionError) as excinfo:
        check_reserved_subset("demo", {"a.py", "b.py"}, {"a.py"})
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    assert "校验 9" in str(excinfo.value)
    # 正向：子集时静默通过
    check_reserved_subset("demo", {"a.py"}, {"a.py", "b.py"})


def test_check09_explicit_reserved_paths_key_rejected(tmp_path: Path) -> None:
    """校验 9 的 YAML 形态：``skeleton.reserved_paths`` 是未知字段 → 1001。"""
    data = _demo()
    data["skeleton"]["reserved_paths"] = ["b.py"]
    _expect_1001(tmp_path, data)


def test_check10_too_many_skeleton_files(tmp_path: Path) -> None:
    """校验 10：``len(files) > max_files`` → 校验 10（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["max_files"] = 3
    _expect_1001(tmp_path, data, check_no=10)


def test_check10_total_bytes_exceeded(tmp_path: Path) -> None:
    """校验 10：骨架总字节 > ``max_bytes`` → 校验 10（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["max_bytes"] = 16
    _expect_1001(tmp_path, data, check_no=10)


def test_check11_skeleton_secret_fingerprint(tmp_path: Path) -> None:
    """校验 11：骨架内容命中密钥指纹（``sk-`` 样本）→ 校验 11（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["files"][0]["content"] = "OPENAI_KEY=sk-" + "a1B2c3D4e5F6g7H8i9J0kLmN\n"
    _expect_1001(tmp_path, data, check_no=11)


def test_check12_skeleton_conventions_overlap(tmp_path: Path) -> None:
    """校验 12：``skeleton`` 与 ``conventions`` 路径交叉 → 校验 12（骨架 6 条之一）。"""
    data = _demo()
    data["conventions"] = [{"path": "app/main.py", "required": True}]
    _expect_1001(tmp_path, data, check_no=12)


def test_check13_checks_not_empty(tmp_path: Path) -> None:
    """校验 13：``checks`` 不得为空。"""
    data = _demo()
    data["checks"] = []
    _expect_1001(tmp_path, data, check_no=13)


def test_check13_kind_field_rejected(tmp_path: Path) -> None:
    """校验 13：YAML 写 ``kind: command`` → 1001（``extra="forbid"`` 生效）。

    这条是"防碰巧能跑"的关键：``EvalCheck.type`` 有默认值 ``"command"``，
    若允许额外字段，``kind`` 会被静默忽略（docs/09 §13 共享知识）。
    """
    data = _demo()
    data["checks"] = [{"kind": "command", "run": "python -m pytest -q", "weight": 1}]
    message = _expect_1001(tmp_path, data)
    assert "kind" in message


@pytest.mark.parametrize("bad_run", ["pip install fastapi", "https://x/y", "npm install vue"])
def test_check14_check_run_must_be_offline(tmp_path: Path, bad_run: str) -> None:
    """校验 14：检查命令不得联网 / 安装（离线可复现）。"""
    data = _demo()
    data["checks"] = [{"type": "command", "run": bad_run, "weight": 1}]
    _expect_1001(tmp_path, data, check_no=14)


def test_check15_weight_must_be_positive(tmp_path: Path) -> None:
    """校验 15：``weight <= 0`` → 1001（pydantic ``gt=0`` 前置拦截）。"""
    data = _demo()
    data["checks"] = [{"type": "command", "run": "ruff check .", "weight": 0}]
    _expect_1001(tmp_path, data)


def test_check16_supports_not_empty(tmp_path: Path) -> None:
    """校验 16：``supports`` 非空且取值合法。"""
    data = _demo()
    data["supports"] = []
    _expect_1001(tmp_path, data, check_no=16)


def test_check16_supports_illegal_value(tmp_path: Path) -> None:
    """校验 16：``supports`` 出现未定义模式 → 1001（Literal 拦截）。"""
    data = _demo()
    data["supports"] = ["from_scratch", "rewrite"]
    _expect_1001(tmp_path, data)


def test_check17_prompting_unknown_role(tmp_path: Path) -> None:
    """校验 17：``prompting`` 的键必须是已注册角色 id。"""
    data = _demo()
    data["prompting"]["devops"] = "部署约束"
    message = _expect_1001(tmp_path, data, check_no=17)
    assert "devops" in message


def test_check17_default_role_ids_from_config() -> None:
    """校验 17 的缺省角色来源：``config/roles/*.yaml`` 文件名主干。"""
    ids = default_role_ids()
    assert {"pm", "architect", "engineer", "qa"} <= ids


# ---------------------------------------------------------------- 模型层硬约束（非校验条目）


def test_checkspec_has_type_not_kind() -> None:
    """``CheckSpec.model_fields`` 含 ``type``、**不含** ``kind``（反断言）。"""
    fields = CheckSpec.model_fields
    assert "type" in fields
    assert "kind" not in fields
    assert fields["type"].default == "command"


def test_unknown_field_rejected(tmp_path: Path) -> None:
    """拼错的字段必须 1001，不能静默忽略（``extra="forbid"``）。"""
    data = _demo()
    data["stack"]["runtime_version"] = "3.12"
    message = _expect_1001(tmp_path, data)
    assert "runtime_version" in message


def test_to_layout_rules_matches_yaml() -> None:
    """``to_layout_rules()`` 返回的 ``LayoutRules`` 字段与 YAML 一致。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    rules = spec.to_layout_rules()
    raw = _demo()
    assert isinstance(rules, LayoutRules)
    assert rules.required_dirs == raw["layout"]["required_dirs"]
    assert rules.required_files == raw["layout"]["required_files"]
    assert rules.any_of_files == raw["layout"]["any_of_files"]
    assert rules.forbidden_dirs == raw["layout"]["forbidden_dirs"]
    assert rules.snake_case_py is raw["layout"]["snake_case_py"]
    assert rules.max_file_lines == raw["layout"]["max_file_lines"]
    assert rules.allowed_prefixes == raw["layout"]["allowed_prefixes"]
    assert rules.frontend is not None
    assert rules.frontend.required_glob == raw["layout"]["frontend"]["required_glob"]
    assert rules.frontend.forbidden_imports == raw["layout"]["frontend"]["forbidden_imports"]


def test_layout_rules_roundtrip_json() -> None:
    """``LayoutRules`` 可序列化往返（跨层纯数据的必要条件）。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    rules = spec.to_layout_rules()
    assert LayoutRules.model_validate(rules.model_dump()) == rules


def test_skeleton_paths_partition() -> None:
    """``seed_paths`` 与 ``reserved_paths`` 互补且并集 == ``all_paths``。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    skeleton: SkeletonSpec = spec.skeleton
    assert not (skeleton.reserved_paths & skeleton.seed_paths)
    assert skeleton.reserved_paths | skeleton.seed_paths == skeleton.all_paths
    assert skeleton.all_paths == set(spec.skeleton_files())


def test_skeleton_files_returns_plain_mapping() -> None:
    """``skeleton_files()`` 必须是纯 ``dict[str, str]``（供 rank 3 的 merge_trees）。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    files = spec.skeleton_files()
    assert type(files) is dict  # noqa: E721 - 刻意断言"纯 dict"，不是 Mapping 子类
    assert files["app/main.py"].startswith("from fastapi import FastAPI")


# --------------------------------------------------------------------- 注册表列举


def test_list_templates_reports_loading_error(tmp_path: Path) -> None:
    """坏模板不让整体失败：好模板可用、坏模板带 ``loading_error``。"""
    good = _demo()
    _write(good, tmp_path, stem="demo")
    bad = _demo()
    bad["id"] = "broken"
    bad["checks"] = []
    _write(bad, tmp_path, stem="broken")

    entries = list_templates(templates_dir=tmp_path, known_roles=ROLES)
    by_id = {entry.id: entry for entry in entries}
    assert by_id["demo"].ok and by_id["demo"].loading_error is None
    assert not by_id["broken"].ok
    assert "校验 13" in (by_id["broken"].loading_error or "")


def test_list_templates_summary_has_no_skeleton_content() -> None:
    """对外摘要只给路径、**不回传骨架正文**（``GET /templates`` 口径）。"""
    entries = list_templates(templates_dir=FIXTURE_DIR, known_roles=ROLES)
    assert len(entries) == 1
    summary = entries[0].to_summary()
    assert summary["reserved_paths"] == [".env.example", "pyproject.toml"]
    assert summary["seed_paths"] == ["app/main.py", "requirements.txt"]
    dumped = repr(summary)
    assert "FastAPI()" not in dumped  # 骨架正文不得出现
    assert "requires-python" not in dumped


def test_list_templates_empty_dir(tmp_path: Path) -> None:
    """空目录 / 不存在的目录 → 空列表（不抛异常）。"""
    assert list_templates(templates_dir=tmp_path, known_roles=ROLES) == []
    assert list_templates(templates_dir=tmp_path / "nope", known_roles=ROLES) == []


# ------------------------------------------------- 两套真模板（v1.2 · T1.4，config/templates）


@pytest.fixture(scope="module")
def real_templates() -> dict[str, TemplateSpec]:
    """从**真实** ``config/templates/`` 加载两套模板（21 条校验全过才算可用）。"""
    return {tid: load_template(tid, known_roles=ROLES) for tid in REAL_TEMPLATE_IDS}


def test_real_templates_load_through_all_checks(real_templates: dict[str, TemplateSpec]) -> None:
    """DoD ①：四套模板都通过 21 条加载期校验并列出（v1.3：2 → 4 套）。"""
    assert set(real_templates) == {
        "fastapi-vue3",
        "python-cli",
        "data-pipeline",
        "node-express",
    }
    listed = {entry.id: entry for entry in list_templates(known_roles=ROLES)}
    for template_id in REAL_TEMPLATE_IDS:
        assert listed[template_id].ok, listed[template_id].loading_error


def test_real_templates_checks_are_executable(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ②：每个模板 ``checks`` ≥3 条，且至少 1 条执行型（pytest/compileall/python -m）。"""
    for template_id, spec in real_templates.items():
        assert len(spec.checks) >= 3, template_id
        assert all(check.type == "command" for check in spec.checks), template_id
        runs = [check.run for check in spec.checks]
        # v1.3：node 模板的执行型入口是 node-build / node --test / npx tsc
        executable_tokens = (
            "pytest",
            "compileall",
            "python -m",
            "python -c",
            "node-build",
            "node --test",
            "npx tsc",
        )
        assert any(token in run for run in runs for token in executable_tokens), (
            f"{template_id} 缺少执行型检查：{runs}"
        )


def test_fastapi_skeleton_exactly_at_max_files(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ③：``fastapi-vue3`` 骨架**恰等于**上限（加第 6 个即超限，须先调 max_files）。"""
    spec = real_templates["fastapi-vue3"]
    assert len(spec.skeleton.files) == spec.skeleton.max_files == 5
    assert spec.reserved_paths() == {"pyproject.toml", "ruff.toml", ".env.example"}
    assert spec.seed_paths() == {"requirements.txt", "app/main.py"}


def test_python_cli_skeleton_has_four_files(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ③：``python-cli`` 是 4 个骨架文件（**不**硬套 5 的上限断言）。"""
    spec = real_templates["python-cli"]
    assert len(spec.skeleton.files) == 4
    assert spec.reserved_paths() == {"pyproject.toml", "ruff.toml"}
    assert spec.seed_paths() == {"ymir_cli/__init__.py", "requirements.txt"}


def test_skeletons_contain_no_test_files(real_templates: dict[str, TemplateSpec]) -> None:
    """DoD ④：骨架**不含任何 test 文件**（否则 ``has_tests`` 白送结构分，§12-3）。

    用 ``tests/`` 或文件名里出现 ``test`` 都算违规 —— 后者能挡住 ``test_utils.py``。
    """
    for template_id, spec in real_templates.items():
        paths = set(spec.skeleton_files())
        assert not [path for path in paths if "test" in path.lower()], template_id
        assert not [path for path in paths if path.startswith("tests/")], template_id


def test_env_example_has_no_secret(real_templates: dict[str, TemplateSpec]) -> None:
    """DoD ⑤：``.env.example`` 内容无真实 key（走 ``common.secrets.scan_secret``）。"""
    from ymir.common.secrets import scan_secret

    files = real_templates["fastapi-vue3"].skeleton_files()
    content = files[".env.example"]
    assert scan_secret(content) is None
    assert "APP_ENV=dev" in content


def test_real_templates_structure_fields(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ⑥：技术栈与目录规范的硬结构（前端/后端形态、必需目录）。"""
    fastapi = real_templates["fastapi-vue3"]
    assert fastapi.stack.frontend == "vue3"
    assert fastapi.stack.backend == "fastapi"
    assert {"app", "tests"} <= set(fastapi.layout.required_dirs)

    cli = real_templates["python-cli"]
    assert cli.stack.frontend == "none"
    assert {"ymir_cli", "tests"} <= set(cli.layout.required_dirs)


def test_ruff_toml_consistency_with_checks_and_layout(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ③'：含 ``ruff check .`` 的模板必须有 ``ruff.toml`` 且 ``line-length == 100``。

    行长度（每行字符）与 ``layout.max_file_lines``（每文件行数）是两个轴：
    这里断言前者为 100（文档定案值），后者不小于 100（不矛盾）。
    """
    # v1.3：node 模板没有 ruff（工具链是 tsc / node --test），只对 python 模板断言
    python_templates = {
        tid: spec for tid, spec in real_templates.items() if spec.stack.language == "python"
    }
    assert set(python_templates) == {"fastapi-vue3", "python-cli", "data-pipeline"}
    for template_id, spec in python_templates.items():
        assert any("ruff check" in check.run for check in spec.checks), template_id
        content = spec.skeleton_files()["ruff.toml"]
        line_length = _toml_int(content, "line-length")
        assert line_length == 100, template_id
        assert spec.layout.max_file_lines >= line_length, template_id
        # 单一来源：pyproject.toml 里不得再写 [tool.ruff]
        assert "[tool.ruff]" not in spec.skeleton_files()["pyproject.toml"], template_id


def test_fastapi_ruff_ignores_b008_only(real_templates: dict[str, TemplateSpec]) -> None:
    """B008 与 FastAPI 的 ``Depends`` 写法天然冲突：**只有** fastapi-vue3 忽略它。

    T5.1 真跑实测：v1.2 的 fastapi 基线产物因
    ``def handler(db: Session = Depends(get_session))`` 吃了 4 条 B008，
    被基线探针判成 ``unhealthy`` ⇒ ``regression`` 归因失效。这不是模型的错，
    是模板规则与框架惯例冲突（任何合规写法都恒扣分）。

    ★ 同时锁定「只忽略 B008」：``select`` 必须还在，且其余三套模板的 ruff 配置里
    **不得**出现 B008 —— 防止将来被"统一"顺手带上，那会真的放松检查。
    """
    fastapi_ruff = real_templates["fastapi-vue3"].skeleton_files()["ruff.toml"]
    assert "[lint]" in fastapi_ruff, fastapi_ruff
    assert "select" in fastapi_ruff, fastapi_ruff  # 规则集没被整个关掉
    assert "ignore" in fastapi_ruff and "B008" in fastapi_ruff, fastapi_ruff

    # 骨架只覆盖 from_scratch / 基线缺配置两种情形；基线自带 ruff.toml 时骨架不参与
    # （merge_trees 优先级 1），所以 ruff 检查命令里也要带 `--ignore B008`（基线无关）。
    ruff_checks = [
        check.run
        for check in real_templates["fastapi-vue3"].checks
        if check.run.startswith("ruff check")
    ]
    assert ruff_checks == ["ruff check --ignore B008 ."], ruff_checks

    # 骨架只覆盖 from_scratch / 基线缺配置两种情形；基线自带 ruff.toml 时骨架不参与
    # （merge_trees 优先级 1），所以 ruff 检查命令里也要带 `--ignore B008`（基线无关）。
    ruff_checks = [
        check.run
        for check in real_templates["fastapi-vue3"].checks
        if check.run.startswith("ruff check")
    ]
    assert ruff_checks == ["ruff check --ignore B008 ."], ruff_checks

    for template_id in ("python-cli", "data-pipeline", "node-express"):
        content = real_templates[template_id].skeleton_files().get("ruff.toml")
        if content is None:  # node-express 用 tsc，不跑 ruff
            continue
        assert "B008" not in content, template_id
        assert "ignore" not in content or "B008" not in content, template_id
        # 命令层也不许带 B008（防止被"统一"顺手加上）
        assert all("B008" not in check.run for check in real_templates[template_id].checks), (
            template_id
        )
        # 命令层也不许带 B008（防止被"统一"顺手加上）
        assert all("B008" not in check.run for check in real_templates[template_id].checks), (
            template_id
        )


def test_real_template_unknown_field_rejected(
    tmp_path: Path, real_templates: dict[str, TemplateSpec]
) -> None:
    """DoD ⑦：真模板形态下拼错字段 → 1001（``extra="forbid"`` 在真 YAML 上同样生效）。"""
    raw = yaml.safe_load((TEMPLATES_DIR / "fastapi-vue3.yaml").read_text(encoding="utf-8"))
    raw["stack"]["runtime_verison"] = "3.12"  # 故意拼错
    with pytest.raises(TemplateDefinitionError) as excinfo:
        load_template_file(_write(raw, tmp_path, stem="fastapi-vue3"), known_roles=ROLES)
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    assert "runtime_verison" in str(excinfo.value)


def _toml_int(content: str, key: str) -> int | None:
    """从极简 TOML 片段里读一个整数键（骨架 ruff.toml 只有扁平的 key = value）。"""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key} ") and "=" in stripped:
            raw = stripped.split("=", 1)[1].strip().strip('"')
            try:
                return int(raw)
            except ValueError:
                return None
    return None


# ===================================================== v1.3 校验 18–21（docs/11 §7.2.7）


def test_check18_protected_path_normalized(tmp_path: Path) -> None:
    """校验 18：``protected.paths`` 过 ``normalize_rel_path``，目录前缀保留尾斜杠。"""
    data = _demo()
    data["protected"] = {
        "paths": ["./tests//conftest.py", "tests/fixtures/"],
        "on_hit": "ignore",
    }
    spec = load_template_file(_write(data, tmp_path), known_roles=ROLES)
    assert spec.protected.paths == ["tests/conftest.py", "tests/fixtures/"]
    assert spec.protected_paths() == frozenset({"tests/conftest.py", "tests/fixtures/"})


def test_check18_directory_without_trailing_slash_gets_one(tmp_path: Path) -> None:
    """F4（QA P3）：作者写 ``tests``（本意是目录）⇒ 加载期自动补成 ``tests/``。

    否则会退化成"精确匹配名为 ``tests`` 的文件"，目录保护**静默失效且无报错**。
    """
    data = _demo()
    data["protected"] = {"paths": ["tests"]}
    spec = load_template_file(_write(data, tmp_path), known_roles=ROLES)
    assert spec.protected.paths == ["tests/"]
    # 与显式写法完全等价
    assert spec.protected_paths() == frozenset({"tests/"})

    explicit = _demo()
    explicit["protected"] = {"paths": ["tests/"]}
    assert (
        load_template_file(_write(explicit, tmp_path), known_roles=ROLES).protected.paths
        == ["tests/"]
    )

    # 带扩展名 ⇒ 仍是精确文件匹配（不被误补斜杠）
    file_data = _demo()
    file_data["protected"] = {"paths": ["tests/conftest.py"]}
    assert load_template_file(
        _write(file_data, tmp_path), known_roles=ROLES
    ).protected.paths == ["tests/conftest.py"]


def test_check18_protected_path_invalid(tmp_path: Path) -> None:
    """校验 18：受保护路径非法（``..`` 逃逸）→ 1001。"""
    data = _demo()
    data["protected"] = {"paths": ["../outside.py"]}
    message = _expect_1001(tmp_path, data, check_no=18)
    assert "protected.paths" in message


def test_check18_protected_path_duplicated(tmp_path: Path) -> None:
    """校验 18：归一化后重复 → 1001（重复即静默覆盖）。"""
    data = _demo()
    data["protected"] = {"paths": ["tests/conftest.py", "./tests/conftest.py"]}
    _expect_1001(tmp_path, data, check_no=18)


def test_check18_protected_must_not_cross_skeleton(tmp_path: Path) -> None:
    """校验 18：受保护路径不得与骨架交叉（骨架已由 reserved 机制保护）。"""
    data = _demo()
    data["protected"] = {"paths": ["app/main.py"]}  # seed 骨架文件
    message = _expect_1001(tmp_path, data, check_no=18)
    assert "skeleton" in message


def test_check18_protected_must_not_cross_conventions(tmp_path: Path) -> None:
    """校验 18：受保护路径不得与 conventions 交叉（那是"模型必须产出"的文件）。"""
    data = _demo()
    data["protected"] = {"paths": ["README.md"]}
    message = _expect_1001(tmp_path, data, check_no=18)
    assert "conventions" in message


def test_check18_protected_directory_prefix_cross_conventions(tmp_path: Path) -> None:
    """校验 18：目录前缀语义同样参与交叉判定。"""
    data = _demo()
    data["conventions"].append({"path": "docs/design.md", "required": True})
    data["protected"] = {"paths": ["docs/"]}
    _expect_1001(tmp_path, data, check_no=18)


def test_check19_on_hit_enum_and_default(tmp_path: Path) -> None:
    """校验 19：``on_hit`` 只接受 ``reject``/``ignore``；缺省为 ``reject``（Q1 定案 A）。"""
    default = load_template_file(_write(_demo(), tmp_path), known_roles=ROLES)
    assert default.protected.on_hit == "reject"
    assert default.protected.paths == []

    bad = _demo()
    bad["protected"] = {"paths": [], "on_hit": "block"}
    # Literal 由 pydantic 在加载期拦下（结构非法 → 1001）
    _expect_1001(tmp_path, bad)


def test_check20_max_change_ratio_range(tmp_path: Path) -> None:
    """校验 20：``0 < max_change_ratio ≤ 1``；缺省 0.5（与 merge 常量同源）。"""
    default = load_template_file(_write(_demo(), tmp_path), known_roles=ROLES)
    assert default.protected.max_change_ratio == pytest.approx(0.5)

    for bad_ratio in (0, 0.0, -0.1, 1.5):
        data = _demo()
        data["protected"] = {"paths": [], "max_change_ratio": bad_ratio}
        _expect_1001(tmp_path, data)


def test_check21_scoring_points_must_be_positive(tmp_path: Path) -> None:
    """校验 21：``runnability_points`` 的值必须 > 0。"""
    data = _demo()
    data["scoring"] = {"runnability_points": {"backend": 0}}
    message = _expect_1001(tmp_path, data, check_no=21)
    assert "backend" in message


def test_check21_scoring_group_name_not_empty(tmp_path: Path) -> None:
    """校验 21：组名不能为空。"""
    data = _demo()
    data["scoring"] = {"runnability_points": {"": 10}}
    _expect_1001(tmp_path, data, check_no=21)


def test_check21_frontend_static_requires_layout_frontend(tmp_path: Path) -> None:
    """校验 21：声明 ``frontend_static`` 配分必须有 ``layout.frontend``（否则配分无法兑现）。"""
    data = _demo()
    data["layout"].pop("frontend")
    data["scoring"] = {"runnability_points": {"backend": 10, "frontend_static": 10}}
    message = _expect_1001(tmp_path, data, check_no=21)
    assert "layout.frontend" in message


def test_check21_frontend_flag_requires_layout_frontend(tmp_path: Path) -> None:
    """校验 21：``frontend_scored_in_runnability=true`` 同样需要 ``layout.frontend``。"""
    data = _demo()
    data["layout"].pop("frontend")
    data["scoring"] = {
        "runnability_points": {"backend": 10},
        "frontend_scored_in_runnability": True,
    }
    _expect_1001(tmp_path, data, check_no=21)


def test_check21_valid_scoring_roundtrip(tmp_path: Path) -> None:
    """校验 21 通过形态：配分可导出为跨层纯数据 ``ScoringRules``。"""
    data = _demo()
    data["scoring"] = {
        "runnability_points": {"backend": 10, "frontend_static": 10, "frontend_build": 10},
        "frontend_scored_in_runnability": True,
    }
    spec = load_template_file(_write(data, tmp_path), known_roles=ROLES)
    rules = spec.to_scoring_rules()
    assert rules.enabled is True
    assert rules.runnability_points["frontend_build"] == pytest.approx(10)
    assert rules.frontend_scored_in_runnability is True
    # 未声明 scoring 时 enabled=False（老口径，分数零变化）
    plain = load_template_file(_write(_demo(), tmp_path), known_roles=ROLES)
    assert plain.to_scoring_rules().enabled is False


def test_real_templates_protected_paths_declared(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """真模板（T1.1 / U3）：**每套**都声明受保护夹具，且策略为 ``reject``。

    场景②③的根因都是 ``tests/conftest.py`` 被重写；python 三套必须都保护它，
    node 侧保护对应的 ``tests/setup.ts``（同一「测试基础设施不可重写」口径）。
    """
    expected = {
        "fastapi-vue3": "tests/conftest.py",
        "python-cli": "tests/conftest.py",
        "data-pipeline": "tests/conftest.py",
        "node-express": "tests/setup.ts",
    }
    for template_id, protected_path in expected.items():
        spec = real_templates[template_id]
        assert spec.protected.paths == [protected_path], template_id
        assert spec.protected.on_hit == "reject", template_id
        assert spec.protected_paths() == frozenset({protected_path}), template_id
        assert spec.protected.max_change_ratio == pytest.approx(0.5), template_id


def test_only_fastapi_vue3_uses_grouped_scoring(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """Q3：**只有** ``fastapi-vue3`` 声明分组配分；其余三套保持 v1.2 老口径。

    这是"分数可比性"的分界线：python-cli / data-pipeline / node-express 的
    runnability 仍按 ``checks[].weight`` 归一，历史分数不受影响。
    """
    grouped = real_templates["fastapi-vue3"]
    rules = grouped.to_scoring_rules()
    assert rules.enabled is True
    assert rules.runnability_points == {
        "backend": 10,
        "frontend_static": 10,
        "frontend_build": 10,
    }
    assert rules.frontend_scored_in_runnability is True

    for template_id in ("python-cli", "data-pipeline", "node-express"):
        spec = real_templates[template_id]
        assert spec.to_scoring_rules().enabled is False, template_id
        assert spec.scoring.runnability_points == {}, template_id
        assert spec.scoring.frontend_scored_in_runnability is False, template_id


# ================================================ v1.3 · 新增两套模板（T3.2）

def test_data_pipeline_template_shape(real_templates: dict[str, TemplateSpec]) -> None:
    """``data-pipeline``：python + pandas/定时任务场景，骨架 4 个、protected 生效。"""
    spec = real_templates["data-pipeline"]
    assert spec.stack.language == "python"
    assert spec.stack.backend == "none" and spec.stack.frontend == "none"
    # v1.3：pandas / numpy 已随镜像扩表（B14 同一 PR）⇒ 可以且**应该**声明
    assert {"pandas", "numpy"} <= set(spec.stack.dependencies)
    assert set(spec.stack.dependencies) <= SANDBOX_IMAGE_PACKAGES
    assert len(spec.skeleton.files) == 4
    assert spec.reserved_paths() == {"pyproject.toml", "ruff.toml"}
    assert spec.seed_paths() == {"pipeline/__init__.py", "requirements.txt"}
    assert spec.layout.required_dirs == ["pipeline", "tests"]
    assert spec.layout.allowed_prefixes == ["pipeline/", "tests/"]
    assert spec.protected_paths() == frozenset({"tests/conftest.py"})
    assert spec.to_scoring_rules().enabled is False  # 不声明分组配分（老口径）
    runs = [check.run for check in spec.checks]
    assert any("compileall" in run for run in runs)
    assert any("pytest" in run for run in runs)
    assert any("ruff check" in run for run in runs)
    # pandas 检查刻意**不**写成"假绿"：镜像已预装 ⇒ 现在真能通过
    assert 'python -c "import pandas"' in runs


def test_node_express_template_shape(real_templates: dict[str, TemplateSpec]) -> None:
    """``node-express``：typescript + Express，骨架恰 5 个、checks 全部声明 requires。"""
    spec = real_templates["node-express"]
    assert spec.stack.language == "typescript"
    assert spec.stack.backend == "express" and spec.stack.frontend == "none"
    # v1.3：node 包表与镜像同批落地 ⇒ 依赖可以声明（且必须是镜像内真有的包）
    assert set(spec.stack.dependencies) == {
        "express",
        "typescript",
        "@types/express",
        "@types/node",
    }
    assert set(spec.stack.dependencies) <= SANDBOX_IMAGE_NODE_PACKAGES
    assert len(spec.skeleton.files) == spec.skeleton.max_files == 5
    assert spec.reserved_paths() == {"package.json", "tsconfig.json", ".env.example"}
    assert spec.seed_paths() == {"src/index.ts", "src/app.ts"}
    assert spec.layout.required_dirs == ["src", "tests"]
    assert spec.layout.forbidden_dirs == ["node_modules", "dist"]
    assert spec.protected_paths() == frozenset({"tests/setup.ts"})
    # 每条 check 都必须声明 requires（无 node 工具链时 skipped 不扣分，docs/11 §7.5-3）
    assert all(check.requires for check in spec.checks), [c.run for c in spec.checks]
    assert {check.requires for check in spec.checks} == {"node-build", "node", "npx"}
    # 禁止离线必失败的写法（npm ci / npx -y / npm install）
    for check in spec.checks:
        lowered = check.run.lower()
        assert "npm ci" not in lowered and "npx -y" not in lowered
        assert "npm install" not in lowered


def test_new_templates_skeletons_contain_no_test_files(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """新模板同样遵守「骨架不含测试文件」（否则 ``has_tests`` 白送结构分）。"""
    for template_id in ("data-pipeline", "node-express"):
        paths = set(real_templates[template_id].skeleton_files())
        assert not [path for path in paths if "test" in path.lower()], template_id


def test_node_skeleton_tsconfig_compiles_to_dist(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """``node-express`` 的 ``tsconfig.outDir`` 必须是 ``dist``（checks 用它跑测试）。"""
    import json

    files = real_templates["node-express"].skeleton_files()
    tsconfig = json.loads(files["tsconfig.json"])
    assert tsconfig["compilerOptions"]["outDir"] == "dist"
    assert tsconfig["compilerOptions"]["strict"] is True
    package = json.loads(files["package.json"])
    assert package["scripts"]["build"].startswith("tsc")
    assert package["scripts"]["test"] == "node --test dist"
