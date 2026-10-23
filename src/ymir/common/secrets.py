"""密钥内容指纹（rank 5，**唯一实现**，docs/09 §4.3.2）。

同一套正则供两处共用，**禁止第二份拷贝**：

- ``templates``(rank 2)：骨架加载期校验 11（``.env.example`` 只许写空值）；
- ``artifacts``(rank 3)：推送前的内容过滤（T3.1，``artifacts/exporter.py``）。

放在 ``common`` 而不是 ``templates`` 的原因（v1.2 r4 架构收口）：``artifacts ->
templates`` 是逆层（CI 拦）。下沉到 rank 5 后 ``2 -> 5`` / ``3 -> 5`` 都合法。

安全约定：:func:`scan_secret` 只返回**命中的规则**，绝不返回匹配到的密钥正文 ——
否则指纹会随日志/回执/前端弹窗二次泄露（docs/09 §4.3.2「结果披露」只披露 path）。
"""

from __future__ import annotations

import re

#: 密钥内容指纹（docs/09 §4.3.2 的 5 条规则，一字不改）。
SECRET_FINGERPRINTS: tuple[str, ...] = (
    r"AKIA[0-9A-Z]{16}",  # AWS Access Key
    r"sk-[A-Za-z0-9]{20,}",  # OpenAI 风格
    r"gh[pousr]_[A-Za-z0-9]{20,}",  # GitHub token
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",  # 私钥头
    r"xox[baprs]-[A-Za-z0-9-]{10,}",  # Slack token
)

#: 编译缓存（模块级，避免每次校验/过滤都重复 ``re.compile``）。
_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern) for pattern in SECRET_FINGERPRINTS
)

__all__ = ["SECRET_FINGERPRINTS", "scan_secret"]


def scan_secret(text: str) -> str | None:
    """扫描文本是否命中密钥指纹。

    Args:
        text: 待检查的文本（文件正文 / 骨架正文 / 环境变量示例）。

    Returns:
        命中时返回**规则字符串**（如 ``"sk-[A-Za-z0-9]{20,}"``），未命中返回 ``None``。
        ★ 刻意不返回匹配文本：密钥不得进日志、回执、错误信息。
    """
    for pattern in _COMPILED:
        if pattern.search(text):
            return pattern.pattern
    return None
