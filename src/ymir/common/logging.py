"""日志初始化（T03：T02 遗留——应用 INFO 日志默认不可见）。

uvicorn 只配置自身的 logger，不动 root；应用模块的 INFO 日志因而默认不可见。
``setup_logging`` 为进程配置一次根 logger（幂等：已有 handler 时不覆盖，
不影响 pytest / 已配置环境）。
"""

from __future__ import annotations

import logging


def setup_logging(*, debug: bool = False) -> None:
    """配置根 logger（幂等）。"""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
