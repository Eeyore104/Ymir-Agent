"""垂直场景模板库（v1.2，docs/09 §4.1，rank 2）。

一个模板 = ``config/templates/<id>.yaml``，语义是「**约束包 + 关键骨架**」：

- 约束包：``stack`` / ``layout`` / ``conventions`` / ``checks`` / ``prompting``；
- 关键骨架：``skeleton.files``（3–5 个配置类文件，≤32KB，含 ``reserved`` 标记）。

模板是 **run 的运行参数**（``run.context["template"]``），**不是**新流水线 ——
``sop/validator.py`` 的 ``_check_inputs`` 会否决任何"非上游产物"的 ``stage.inputs``。

跨层纪律（docs/09 §3.1）：``templates`` 只经纯数据对外交流 ——
``to_layout_rules()`` 产出 ``common.contracts.LayoutRules``（evaluation 消费）、
``skeleton_files()`` 产出 ``dict[str, str]``（``artifacts.merge_trees`` 消费），
禁止把 ``TemplateSpec`` 传给 rank 1/3 的包（那会造成逆层或实现耦合）。
"""

from __future__ import annotations

from ymir.templates.loader import (
    DEFAULT_ROLES_DIR,
    DEFAULT_TEMPLATES_DIR,
    TemplateDefinitionError,
    check_reserved_subset,
    default_role_ids,
    dependency_name,
    image_packages_for,
    list_templates,
    load_template,
    load_template_file,
)
from ymir.templates.models import (
    ALLOWED_LANGUAGES,
    SANDBOX_IMAGE_NODE_PACKAGES,
    SANDBOX_IMAGE_PACKAGES,
    SECRET_FINGERPRINTS,
    CheckSpec,
    ConventionFile,
    LayoutSpec,
    SkeletonFile,
    SkeletonSpec,
    StackSpec,
    TemplateEntry,
    TemplateSpec,
)

__all__ = [
    "ALLOWED_LANGUAGES",
    "DEFAULT_ROLES_DIR",
    "DEFAULT_TEMPLATES_DIR",
    "SANDBOX_IMAGE_NODE_PACKAGES",
    "SANDBOX_IMAGE_PACKAGES",
    "SECRET_FINGERPRINTS",
    "CheckSpec",
    "ConventionFile",
    "LayoutSpec",
    "SkeletonFile",
    "SkeletonSpec",
    "StackSpec",
    "TemplateDefinitionError",
    "TemplateEntry",
    "TemplateSpec",
    "check_reserved_subset",
    "default_role_ids",
    "dependency_name",
    "image_packages_for",
    "list_templates",
    "load_template",
    "load_template_file",
]
