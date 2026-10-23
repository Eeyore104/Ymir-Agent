"""导出通道协议与文件过滤（v1.2，docs/09 §4.3.1 / §4.3.2）。

本模块是**纯函数 + 数据模型 + 协议**，**零网络、零 IO**：

- :class:`ExportTarget` / :class:`ExportRequest` / :class:`ExportResult`：推送域的
  入参与回执模型（``extra="forbid"``，请求体里出现 ``token`` 等未知字段直接 1001）；
- :class:`Exporter`：通道协议（``async def export(request)`）—— 具体通道
  （``GitHubExporter``）在 T3.2 实现，本模块不 import 任何 HTTP 客户端；
- :func:`filter_files`：**永远先于网络调用**的文件过滤（路径黑名单 → 白名单例外
  → 内容指纹 → 二进制 → 尺寸），这是"密钥类文件永不外流"的唯一实现点。

两个容易踩的坑（都已按定案处理）：

1. ``ExportTarget.kind`` **就是** ``kind``（推送通道类型），与 ``EvalCheck.type``
   （检查类型）是**完全不同**的字段 —— 不要因为 T1.1 的 ``kind/type`` 教训过度泛化；
2. ``.env.example`` / ``.env.sample`` / ``.env.template`` **必须放行**：它们是文档
   不是密钥，且 ``fastapi-vue3`` 的骨架自带 ``.env.example``（否则模板骨架永远
   推不出去，与 Q1 自相矛盾）。例外只豁免**路径黑名单**，**不豁免**内容指纹。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ymir.common.errors import InvalidParamError
from ymir.common.paths import normalize_rel_path
from ymir.common.secrets import scan_secret

#: 单文件尺寸上限（超限**不推**，记 ``skipped_files``）
MAX_FILE_BYTES: int = 256 * 1024

#: 单次推送总字节上限（超限**整体失败**，避免推出半拉子工程）
MAX_TOTAL_BYTES: int = 8 * 1024 * 1024

#: 路径黑名单（fail-closed：命中即不推）。12 类，与 docs/09 §4.3.2 一一对应。
BLACKLIST_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".env.*.local",
    "*.key",
    "*.pem",
    "id_rsa*",
    "id_ed25519*",
    ".aws/**",
    ".ssh/**",
    "*.p12",
    "*.pfx",
    "**/credentials*.json",
)

#: 白名单例外（**3 个精确文件名**）：文档而非密钥，允许推送。
WHITELIST_NAMES: frozenset[str] = frozenset(
    {".env.example", ".env.sample", ".env.template"}
)

#: 受保护分支（动它们需要显式 ``allow_protected_branch``，v1.2 K5）
PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master"})

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9._-]{1,100}$")

#: 分支名非法片段（git check-ref-format 的核心约束）
_BRANCH_FORBIDDEN = ("..", "//", "@{", " ", "~", "^", ":", "?", "*", "[", "\\")

ExportStatus = Literal["ok", "skipped", "failed", "skipped_identical"]
ErrorClass = Literal[
    # 过滤阶段（T3.1）
    "too_large",
    "secret",
    # 目标 / 鉴权（T3.2，与 docs/09 §4.3.4 的六分支一一对应）
    "no_token",
    "insufficient_scope",
    "unauthorized",
    "rate_limited",
    "forbidden",
    "not_found",
    "validation_failed",
    "network",
    # 协议级
    "invalid_target",
]


class BlockedFile(BaseModel):
    """被拦下的文件（``path`` + ``reason``）。**绝不含文件正文**（防二次泄露）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class ExportTarget(BaseModel):
    """推送目标（最小权限：只描述"推到哪里"，**不接受 token**）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["github"] = Field(default="github", description="通道类型（github）")
    repo: str = Field(description="owner/name")
    branch: str | None = Field(default=None, description="None → ymir/<artifact>-v<n>")
    path_prefix: str = Field(default="", description="monorepo 子目录（为空 = 根）")
    create_repo: bool = Field(default=False, description="仓库不存在时是否新建")
    private: bool = Field(default=True, description="新建仓库是否私有")
    commit_message: str | None = Field(default=None)
    allow_protected_branch: bool = Field(
        default=False, description="显式允许推 main/master（缺省拒绝，K5）"
    )
    strict_secret_check: bool = Field(
        default=False, description="True：任一文件命中密钥指纹即整体失败"
    )

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        raw = value.strip()
        if not _REPO_RE.match(raw):
            raise InvalidParamError(f"repo 必须形如 owner/name（当前 {value!r}）")
        return raw

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip():
            # 不静默 trim：首尾空白通常是拼贴事故，静默修正会掩盖问题
            raise InvalidParamError(f"branch 首尾不得有空白: {value!r}")
        raw = value
        if not raw:
            raise InvalidParamError("branch 不能为空字符串（缺省请传 null）")
        if len(raw) > 200:
            raise InvalidParamError("branch 过长（>200）")
        if raw.startswith(("-", "/", ".")) or raw.endswith(("/", ".")):
            raise InvalidParamError(f"branch 不得以 - / . 开头或以 / . 结尾: {value!r}")
        for token in _BRANCH_FORBIDDEN:
            if token in raw:
                raise InvalidParamError(f"branch 含非法片段 {token!r}: {value!r}")
        if raw.endswith(".lock"):
            raise InvalidParamError(f"branch 不得以 .lock 结尾: {value!r}")
        return raw

    @field_validator("path_prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        return normalize_rel_path(raw)

    def target_branch(self, artifact_name: str, version: int | None) -> str:
        """实际推送分支：显式 ``branch`` > ``ymir/<artifact>-v<n>``（破坏性默认值规避）。"""
        if self.branch:
            return self.branch
        suffix = f"v{version}" if version is not None else "latest"
        safe_name = normalize_rel_path(artifact_name).replace("/", "-")
        return f"ymir/{safe_name}-{suffix}"


class ExportRequest(BaseModel):
    """一次导出请求（``files`` 由编排层选树后填入，传输层不感知树语义）。"""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(default="")
    #: 产物 id（API 侧首选；服务据此精确定位版本，避免同名产物歧义）
    artifact_id: str = Field(default="")
    #: 产物名（脚本/测试的便捷入口；``artifact_id`` 为空时按名字取最新版本）
    artifact_name: str = Field(default="code")
    artifact_version: int | None = Field(default=None)
    target: ExportTarget
    #: 树语义由调用方决定（传输层无感）：``merged`` = 基线 ⊕ 补丁，``files`` = 原产物
    tree: Literal["files", "merged"] = Field(default="merged")
    files: dict[str, str] = Field(default_factory=dict)
    dry_run: bool = Field(default=False, description="只算不推")
    strict_secret_check: bool = Field(default=False)

    @property
    def effective_strict(self) -> bool:
        """严格模式（请求级或目标级任一为真，取"更严"的那个）。"""
        return self.strict_secret_check or self.target.strict_secret_check


class ExportResult(BaseModel):
    """一次导出的回执（**必须披露**被拦/被跳过的文件，防止"以为推上去了"）。"""

    model_config = ConfigDict(extra="forbid")

    status: ExportStatus = "ok"
    files_pushed: list[str] = Field(default_factory=list)
    blocked_files: list[BlockedFile] = Field(default_factory=list)
    skipped_files: list[BlockedFile] = Field(default_factory=list)
    error_class: ErrorClass | None = None
    error_code: int | None = Field(default=None, description="失败时的业务码（通常是 1007）")
    error_message: str | None = Field(default=None, description="失败原因（**不含 token**）")
    commit_sha: str | None = Field(default=None, description="推送成功后的 commit sha")
    file_count: int = Field(default=0, description="本次实际（或 dry-run 时将）推送的文件数")
    bytes_total: int = 0
    branch: str = ""
    dry_run: bool = False


def parse_export_target(data: Mapping[str, Any]) -> ExportTarget:
    """从请求 dict 构造 :class:`ExportTarget`；校验失败统一转 **1001**。

    pydantic 的 ``ValidationError`` 在 HTTP 层会被 ``api/errors.py`` 映射成
    400/1001，但脚本（CLI / 测试）直接构造模型时拿到的还是 ``ValidationError``。
    这里统一成 :class:`InvalidParamError`，让两条入口的语义一致
    —— 尤其保证"请求体里塞 ``token``"在任何入口都是 1001，而不是 500/422。
    """
    try:
        return ExportTarget.model_validate(dict(data))
    except ValidationError as exc:
        raise InvalidParamError(f"推送目标非法: {exc}") from exc


class Exporter(Protocol):

    kind: str

    async def export(self, request: ExportRequest) -> ExportResult: ...


@dataclass(frozen=True)
class FilterOutcome:
    """文件过滤结果（纯数据，便于单测与回执组装）。"""

    allowed: dict[str, str] = field(default_factory=dict)
    blocked: tuple[BlockedFile, ...] = ()
    skipped: tuple[BlockedFile, ...] = ()
    bytes_total: int = 0
    failed: bool = False
    error_class: ErrorClass | None = None


def _suffixes(path: str) -> tuple[str, ...]:
    """路径的各级后缀（``a/b/c`` → ``a/b/c``、``b/c``、``c``）。

    用后缀匹配目录类模式（``.aws/**``），这样 ``svc/.aws/config`` 也会被拦下 ——
    只匹配整串会漏掉"嵌在子目录里的凭据目录"，那是最常见的泄露形态。
    """
    parts = path.split("/")
    return tuple("/".join(parts[index:]) for index in range(len(parts)))


def is_path_blocked(path: str) -> bool:
    """路径是否命中黑名单（目录类模式按后缀匹配，``*.key`` 之类兼顾 basename）。"""
    base = path.rsplit("/", 1)[-1]
    candidates = (path, base, *_suffixes(path))
    for pattern in BLACKLIST_PATTERNS:
        short = pattern.removeprefix("**/")
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(candidate, short):
                return True
    return False


def is_whitelisted(path: str) -> bool:
    """是否为白名单例外（**3 个精确文件名**，只放行路径黑名单）。"""
    return path.rsplit("/", 1)[-1] in WHITELIST_NAMES


def filter_files(
    files: Mapping[str, str],
    *,
    strict_secret_check: bool = False,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> FilterOutcome:
    """推送前的文件过滤（纯函数、零网络）。

    逐文件判定顺序（顺序即语义）：

    1. 路径过 :func:`ymir.common.paths.normalize_rel_path` —— 非法 → ``blocked``；
    2. 含 ``\\x00`` → ``skipped``（二进制，Git Data API 的 blob 语义不适合文本树）；
    3. 超 ``max_file_bytes`` → ``skipped``（too_large）；
    4. 命中路径黑名单（白名单 3 个例外除外）→ ``blocked``；
    5. 命中内容指纹（``common.secrets.scan_secret``）→ ``blocked``（reason 只含规则名）。

    「命中密钥」与「路径黑名单」都是 ``blocked``（永不推送）；``skipped`` 是"这次
    不推但不算违规"（二进制 / 超大）。``strict_secret_check=True`` 时任一密钥命中
    → 整体 ``failed``（不剔除后继续）。

    Returns:
        :class:`FilterOutcome`：``allowed`` / ``blocked`` / ``skipped`` / 统计。
    """
    allowed: dict[str, str] = {}
    blocked: list[BlockedFile] = []
    skipped: list[BlockedFile] = []
    seen: set[str] = set()

    for raw_path in sorted(files):
        content = files[raw_path]
        try:
            path = normalize_rel_path(raw_path)
        except InvalidParamError:
            blocked.append(BlockedFile(path=str(raw_path), reason="invalid_path"))
            continue
        if path in seen:
            blocked.append(BlockedFile(path=path, reason="duplicate_path"))
            continue
        seen.add(path)

        if "\x00" in content:
            skipped.append(BlockedFile(path=path, reason="binary"))
            continue
        if len(content.encode("utf-8")) > max_file_bytes:
            skipped.append(BlockedFile(path=path, reason="too_large"))
            continue
        if is_path_blocked(path) and not is_whitelisted(path):
            blocked.append(BlockedFile(path=path, reason="path_blacklist"))
            continue
        rule = scan_secret(content)
        if rule is not None:
            # 只回规则名，**绝不回匹配到的密钥原文**
            blocked.append(BlockedFile(path=path, reason=f"secret:{rule}"))
            continue
        allowed[path] = content

    bytes_total = sum(len(content.encode("utf-8")) for content in allowed.values())
    has_secret = any(item.reason.startswith("secret:") for item in blocked)
    if strict_secret_check and has_secret:
        return FilterOutcome(
            allowed={},
            blocked=tuple(blocked),
            skipped=tuple(skipped),
            bytes_total=0,
            failed=True,
            error_class="secret",
        )
    if bytes_total > max_total_bytes:
        return FilterOutcome(
            allowed={},
            blocked=tuple(blocked),
            skipped=tuple(skipped),
            bytes_total=bytes_total,
            failed=True,
            error_class="too_large",
        )
    return FilterOutcome(
        allowed=allowed,
        blocked=tuple(blocked),
        skipped=tuple(skipped),
        bytes_total=bytes_total,
    )


def build_result(
    outcome: FilterOutcome, *, request: ExportRequest
) -> ExportResult:
    """把过滤结果组装成回执（``dry_run`` 时``files_pushed`` 为空、状态 ``skipped``）。"""
    branch = request.target.target_branch(request.artifact_name, request.artifact_version)
    if outcome.failed:
        status: ExportStatus = "failed"
    elif request.dry_run or not outcome.allowed:
        status = "skipped"
    else:
        status = "ok"
    return ExportResult(
        status=status,
        files_pushed=[] if request.dry_run or outcome.failed else sorted(outcome.allowed),
        # dry-run 也要能回答"将推送几个文件"（前端预演弹窗用）
        file_count=len(outcome.allowed),
        blocked_files=list(outcome.blocked),
        skipped_files=list(outcome.skipped),
        error_class=outcome.error_class,
        bytes_total=outcome.bytes_total,
        branch=branch,
        dry_run=request.dry_run,
    )


def check_protected_branch(target: ExportTarget) -> None:
    """保护分支守卫（缺省拒推 ``main``/``master``，K5）。

    Raises:
        InvalidParamError: 目标是受保护分支且未显式 ``allow_protected_branch``（1001）。
    """
    branch = (target.branch or "").strip()
    if branch in PROTECTED_BRANCHES and not target.allow_protected_branch:
        raise InvalidParamError(
            f"拒绝推送到受保护分支 {branch!r}：请改用新分支，或显式 allow_protected_branch=true"
        )
