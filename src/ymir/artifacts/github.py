"""GitHubExporter：Git Data API 单 commit 推送（v1.2，docs/09 §4.3.3 / §4.3.4）。

**执行顺序是契约**（DoD ① 会逐条断言请求序列）：

1. ``token`` 为空 → ``failed(no_token)``，**一个请求都不发**；
2. **分支保护前置检查**（``allow_protected_branch=False`` 且目标是 ``main``/``master``）
   → 抛 1001，**在任何 HTTP 之前**（破坏性默认值必须挡在网络之外）；
3. 文件过滤（路径黑名单 / 白名单例外 / 内容指纹 / 二进制 / 尺寸）—— 仍在网络之前；
4. ``GET /user``（校验 token；读 ``X-OAuth-Scopes`` 判 scope）；
5. ``GET /repos/{repo}/git/ref/heads/{branch}``（取 base commit；404 = 分支不存在）；
6. ``POST /git/blobs`` × N；
7. ``POST /git/trees``（带 ``base_tree``，entries 全为 ``100644`` blob）；
8. ``POST /git/commits``（``parents=[commit_sha]``）—— **1 次推送 = 1 个 commit**；
9. 分支存在 → ``PATCH /git/refs/heads/{branch}``；不存在 → ``POST /git/refs``。

为什么用 Git Data API 而不是 Contents API：后者每文件一个 commit（50 文件 = 50
commit），且更易触发次级速率限制（docs/09 §4.3.3「被否」）。

安全约定：
- token 只从构造参数（由上层从 env 读出）进来，**绝不进请求体/日志/回执**；
- 失败文案里不出现 token、不出现文件正文。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ymir.artifacts.exporter import (
    BlockedFile,
    ErrorClass,
    ExportRequest,
    ExportResult,
    build_result,
    check_protected_branch,
    filter_files,
)
from ymir.common.errors import InvalidParamError

logger = logging.getLogger(__name__)

#: GitHub API 根（测试用 ``httpx.MockTransport`` 拦截，不联网）
GITHUB_API = "https://api.github.com"

#: 超时（connect, read）：docs/09 §4.3.2 定案 15s / connect 5s
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: 404 文案必须写清这条（docs/09 §4.3.4 定案）
NOT_FOUND_HINT = (
    "仓库/分支不存在，或 token 无权访问。"
    "注意：GitHub 对无权访问的私有仓库也返回 404"
)


class GitHubExporter:
    """GitHub 推送通道（实现 :class:`~ymir.artifacts.exporter.Exporter` 协议）。"""

    kind = "github"

    def __init__(
        self,
        *,
        token: str = "",
        client: httpx.AsyncClient | None = None,
        api_base: str = GITHUB_API,
    ) -> None:
        self._token = token
        self._client = client
        self._api_base = api_base.rstrip("/")

    # ------------------------------------------------------------------ 协议入口

    async def export(self, request: ExportRequest) -> ExportResult:
        target = request.target
        branch = target.target_branch(request.artifact_name, request.artifact_version)

        # ① token 缺失：不发任何请求（连 GET /user 都不发）
        if not self._token.strip():
            return _failure(
                request,
                branch,
                error_class="no_token",
                message=(
                    "未配置 GitHub token（请设置 GITHUB_TOKEN 环境变量；"
                    "只需目标 repo 的 repo scope）"
                ),
            )

        # ② 分支保护：必须在任何 HTTP 之前（破坏性默认值挡在网络外）
        check_protected_branch(target)

        # ③ 文件过滤：永远先于网络调用
        outcome = filter_files(request.files, strict_secret_check=request.effective_strict)
        if outcome.failed:
            result = build_result(outcome, request=request)
            return result.model_copy(
                update={
                    "error_code": 1007,
                    "branch": branch,
                    "file_count": len(outcome.allowed),
                }
            )
        if request.dry_run:
            return _dry_run_result(
                request, branch, outcome.allowed, outcome.blocked, outcome.skipped
            )
        if not outcome.allowed:
            return _failure(
                request,
                branch,
                error_class="forbidden",
                message="没有可推送的文件（全部被黑名单/指纹/尺寸过滤掉）",
                blocked=outcome.blocked,
                skipped=outcome.skipped,
            )

        client = self._client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        owns_client = self._client is None
        try:
            return await self._push(
                request, client, branch, outcome.allowed, outcome.blocked, outcome.skipped
            )
        finally:
            if owns_client:
                await client.aclose()

    async def _push(
        self,
        request: ExportRequest,
        client: httpx.AsyncClient,
        branch: str,
        files: dict[str, str],
        blocked: tuple[BlockedFile, ...],
        skipped: tuple[BlockedFile, ...],
    ) -> ExportResult:
        """真正的推送序列（见模块 docstring 的 4~9 步）。"""
        repo = request.target.repo
        headers = self._headers()

        # ④ GET /user：token 有效性 + scope 探测
        resp = await self._get(client, "/user", headers)
        if resp.status_code >= 400:
            return _http_failure(request, branch, resp)
        scopes = resp.headers.get("X-OAuth-Scopes")
        if scopes is not None and "repo" not in {
            item.strip() for item in scopes.split(",") if item.strip()
        }:
            return _failure(
                request,
                branch,
                error_class="insufficient_scope",
                message=f"token 缺少 repo scope（当前 scopes: {scopes or '空'}）",
                blocked=blocked,
                skipped=skipped,
            )

        # ⑤ GET ref：base commit / 分支是否存在
        ref_resp = await self._get(client, f"/repos/{repo}/git/ref/heads/{branch}", headers)
        branch_missing = ref_resp.status_code == 404
        commit_sha: str | None = None
        base_tree_sha: str | None = None
        if branch_missing:
            logger.info("目标分支不存在，将创建：%s", branch)
        elif ref_resp.status_code >= 400:
            return _http_failure(request, branch, ref_resp)
        else:
            commit_sha = str(
                ((ref_resp.json() or {}).get("object") or {}).get("sha") or ""
            )
            # ★ ref 给的是 **commit sha**，而 `POST /git/trees` 的 `base_tree` 要的是
            # **tree sha**（docs/09 §4.3.3）。直接传 commit sha 真机会 422 ——
            # MockTransport 只校验 method/path，抓不到这类值语义错误，故这里必须
            # 显式解析一次 commit 对象取 `tree.sha`。
            if commit_sha:
                resolved = await self._resolve_tree_sha(client, repo, commit_sha, headers)
                if isinstance(resolved, httpx.Response):
                    return _http_failure(
                        request, branch, resolved, blocked=blocked, skipped=skipped
                    )
                if not resolved:
                    return _failure(
                        request,
                        branch,
                        error_class="validation_failed",
                        message=f"无法解析 base commit {commit_sha[:12]} 的 tree sha",
                        blocked=blocked,
                        skipped=skipped,
                    )
                base_tree_sha = resolved

        # ⑥ POST blobs × N
        prefix = request.target.path_prefix
        entries: list[dict[str, Any]] = []
        for path, content in sorted(files.items()):
            blob_resp = await client.post(
                f"{self._api_base}/repos/{repo}/git/blobs",
                headers=headers,
                json={"content": content, "encoding": "utf-8"},
            )
            if blob_resp.status_code >= 400:
                return _http_failure(request, branch, blob_resp, blocked=blocked, skipped=skipped)
            blob_sha = str((blob_resp.json() or {}).get("sha") or "")
            entries.append(
                {
                    "path": f"{prefix}/{path}" if prefix else path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            )

        # ⑦ POST trees（带 base_tree：分支已有内容时是增量覆盖，不是清空重写）
        tree_payload: dict[str, Any] = {"tree": entries}
        if base_tree_sha:
            tree_payload["base_tree"] = base_tree_sha
        tree_resp = await client.post(
            f"{self._api_base}/repos/{repo}/git/trees", headers=headers, json=tree_payload
        )
        if tree_resp.status_code >= 400:
            return _http_failure(request, branch, tree_resp, blocked=blocked, skipped=skipped)
        tree_sha = str((tree_resp.json() or {}).get("sha") or "")

        # ⑧ POST commits（1 次推送 = 1 个 commit）
        message = request.target.commit_message or (
            f"ymir: {request.artifact_name} v{request.artifact_version or 'latest'}"
        )
        commit_payload: dict[str, Any] = {"message": message, "tree": tree_sha}
        commit_payload["parents"] = [commit_sha] if commit_sha else []
        commit_resp = await client.post(
            f"{self._api_base}/repos/{repo}/git/commits", headers=headers, json=commit_payload
        )
        if commit_resp.status_code >= 400:
            return _http_failure(request, branch, commit_resp, blocked=blocked, skipped=skipped)
        commit_sha = str((commit_resp.json() or {}).get("sha") or "")

        # ⑨ 更新 / 创建分支引用
        if branch_missing:
            ref_update = await client.post(
                f"{self._api_base}/repos/{repo}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
        else:
            ref_update = await client.patch(
                f"{self._api_base}/repos/{repo}/git/refs/heads/{branch}",
                headers=headers,
                json={"sha": commit_sha, "force": False},
            )
        if ref_update.status_code >= 400:
            return _http_failure(request, branch, ref_update, blocked=blocked, skipped=skipped)

        logger.info(
            "推送成功：repo=%s branch=%s files=%d commit=%s",
            repo,
            branch,
            len(files),
            commit_sha,
        )
        return ExportResult(
            status="ok",
            files_pushed=sorted(files),
            blocked_files=list(blocked),
            skipped_files=list(skipped),
            bytes_total=sum(len(content.encode("utf-8")) for content in files.values()),
            commit_sha=commit_sha,
            file_count=len(files),
            branch=branch,
        )

    async def remote_head(self, request: ExportRequest) -> str | None:
        """读取目标分支当前 head（幂等判定用；分支不存在返回 ``None``）。

        与 :meth:`export` 分开：只有"已有成功回执"时才需要它，避免每次推送都多一次
        请求。异常一律吞掉（读不到就当"head 未知"，让调用方按需重推）。
        """
        branch = request.target.target_branch(
            request.artifact_name, request.artifact_version
        )
        client = self._client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        owns_client = self._client is None
        try:
            resp = await self._get(
                client,
                f"/repos/{request.target.repo}/git/ref/heads/{branch}",
                self._headers(),
            )
        except Exception:  # noqa: BLE001 - head 读不到不影响主流程
            logger.warning("读取远端 head 失败，按「未知」处理", exc_info=True)
            return None
        finally:
            if owns_client:
                await client.aclose()
        if resp.status_code != 200:
            return None
        return str(((resp.json() or {}).get("object") or {}).get("sha") or "") or None

    # ------------------------------------------------------------------ 内部

    async def _resolve_tree_sha(
        self,
        client: httpx.AsyncClient,
        repo: str,
        commit_sha: str,
        headers: dict[str, str],
    ) -> str | httpx.Response:
        """把 **commit sha** 解析成 **tree sha**（``base_tree`` 只认后者）。

        Returns:
            成功 → tree sha 字符串；HTTP 失败 → 该 ``httpx.Response``（由调用方映射
            错误分支）；payload 缺 ``tree.sha`` → 空串。
        """
        resp = await self._get(client, f"/repos/{repo}/git/commits/{commit_sha}", headers)
        if resp.status_code >= 400:
            return resp
        return str(((resp.json() or {}).get("tree") or {}).get("sha") or "")

    def _headers(self) -> dict[str, str]:
        # token 只在请求头里；不写日志、不进回执
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(
        self, client: httpx.AsyncClient, path: str, headers: dict[str, str]
    ) -> httpx.Response:
        return await client.get(f"{self._api_base}{path}", headers=headers)


class MissingTokenExporter:
    """未配置 token 时的**占位通道**（装配层用，保证 app 仍能起来）。

    没配 GitHub 的人只做本地流程：任何 ``export`` 调用都立刻返回
    ``failed/no_token``（0 网络），而不是让整个应用启动失败。
    """

    kind = "github"

    async def export(self, request: ExportRequest) -> ExportResult:
        branch = request.target.target_branch(
            request.artifact_name, request.artifact_version
        )
        return _failure(
            request,
            branch,
            error_class="no_token",
            message=(
                "未配置 GitHub token，无法推送（设置环境变量 "
                "GITHUB_TOKEN 后重启；本地流程不受影响）"
            ),
        )

    async def remote_head(self, request: ExportRequest) -> str | None:
        """无 token 时读不到远端 head（幂等判定按"未知"处理）。"""
        return None


# --------------------------------------------------------------------- 结果组装


def _dry_run_result(
    request: ExportRequest,
    branch: str,
    allowed: dict[str, str],
    blocked: tuple[BlockedFile, ...],
    skipped: tuple[BlockedFile, ...],
) -> ExportResult:
    """dry-run：算出"将推什么"，但**不推送**（网络 0 次）。"""
    return ExportResult(
        status="skipped",
        files_pushed=[],
        blocked_files=list(blocked),
        skipped_files=list(skipped),
        bytes_total=sum(len(content.encode("utf-8")) for content in allowed.values()),
        file_count=len(allowed),
        branch=branch,
        dry_run=True,
    )


def _failure(
    request: ExportRequest,
    branch: str,
    *,
    error_class: ErrorClass,
    message: str,
    blocked: tuple[BlockedFile, ...] = (),
    skipped: tuple[BlockedFile, ...] = (),
) -> ExportResult:
    """失败回执（``error_code`` 统一 1007：推送域失败码，docs/09 §12-D4）。"""
    return ExportResult(
        status="failed",
        blocked_files=list(blocked),
        skipped_files=list(skipped),
        error_class=error_class,
        error_code=1007,
        error_message=message,
        branch=branch,
        dry_run=request.dry_run,
    )


def _http_failure(
    request: ExportRequest,
    branch: str,
    response: httpx.Response,
    *,
    blocked: tuple[BlockedFile, ...] = (),
    skipped: tuple[BlockedFile, ...] = (),
) -> ExportResult:
    """把 HTTP 响应映射成六分支错误（docs/09 §4.3.4 表）。"""
    error_class, message = map_http_error(response)
    return _failure(
        request,
        branch,
        error_class=error_class,
        message=message,
        blocked=blocked,
        skipped=skipped,
    )


def map_http_error(response: httpx.Response) -> tuple[ErrorClass, str]:
    """HTTP 状态 → ``(error_class, 文案)``；六分支与 docs/09 §4.3.4 一一对应。"""
    status = response.status_code
    if status == 401:
        return "unauthorized", "token 无效或已过期（检查 GITHUB_TOKEN）"
    if status == 403:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            reset = response.headers.get("X-RateLimit-Reset", "")
            return "rate_limited", f"触发速率限制（重置时间 {reset or '未知'}），请稍后重试"
        return "forbidden", "缺少 repo scope 或对该仓库无写权限"
    if status == 404:
        return "not_found", NOT_FOUND_HINT
    if status == 422:
        return "validation_failed", "请求被 GitHub 拒绝（分支保护 / ref 已存在且非快进）"
    return "network", f"GitHub 返回意外状态 {status}"


def map_exception(exc: BaseException) -> tuple[ErrorClass, str]:
    """网络异常 → ``network``（超时 / 连接失败 / 传输错误同一类）。"""
    if isinstance(exc, httpx.TimeoutException):
        return "network", "推送超时（GitHub 不可达或响应过慢）"
    if isinstance(exc, httpx.TransportError):
        return "network", f"网络不可达：{type(exc).__name__}"
    if isinstance(exc, InvalidParamError):
        return "invalid_target", exc.message
    return "network", f"推送失败：{type(exc).__name__}"
