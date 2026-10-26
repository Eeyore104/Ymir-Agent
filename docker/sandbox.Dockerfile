# ymir-agent 沙箱镜像（架构设计 §3.5）
#
# 构建：docker build -f docker/sandbox.Dockerfile -t ymir-sandbox:latest .
# 使用：设置 YMIR_SANDBOX_IMAGE=ymir-sandbox:latest 后沙箱改用本镜像
#
# 说明：
# - 预装高频测试依赖（pytest / pytest-cov / httpx / pydantic）+ 模板栈
#   （fastapi / uvicorn / sqlalchemy[asyncio] / aiosqlite / alembic /
#   pytest-asyncio，v1.2 扩容），减少评测阶段联网需求（配合默认 network=none 断网执行）；
#   **两处易漏项（2026-10-23 T5.1 真跑实测）**：
#   ① `sqlalchemy` 必须带 `[asyncio]` extra —— 否则缺 greenlet，FastAPI 异步
#      session 一 import 就报 "The SQLAlchemy asyncio module requires greenlet"；
#   ② 必须有 `pytest-asyncio` —— 模型为 FastAPI 生成的 conftest 常规会
#      `import pytest_asyncio`，缺了直接 exit 4（collection error）。
#   这两项都不会在单元测试里暴露（沙箱内才执行）。
# - **pytest 与 ruff 是基线跑分（scripts/run_benchmark.py）启动预检的必需项**
#   （docs/07 §4.2.1）：镜像内 `python -m pytest --version` 与 `ruff --version`
#   必须可执行，任一缺失则预检 exit 2、不发起任何 run（避免静默跑出全红基线）；
#   版本基准：pytest 8.x / ruff 0.6.x（设计基准 0.6.9）；
# - PyPI 默认用阿里云镜像。**原先用清华镜像，但 2026-10-23 实测其对大 wheel
#   直下返回 403 Forbidden**（索引页 200、wheel 403，疑与出口 IP 限流有关），
#   导致 `docker build` 失败；阿里云同文件 200。可用
#   `--build-arg PIP_INDEX_URL=<你的源>` 覆盖（如内网源/官方源）；
# - Node 预装与前端构建校验留待 v1.3 镜像变体（v1.2 前端只做静态检查）。

FROM python:3.12-slim

# 可覆盖的 PyPI 索引（构建期参数，不写死在层里）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 网络容错（2026-10-26）：本机经 VPN 到镜像源速率不稳，单包下载偶发超时会让
# pip 放弃 wheel 回退到源码构建 —— greenlet 是 C 扩展，slim 镜像无 gcc，
# 于是报 "Failed building wheel for greenlet"。实测单独装 greenlet 只需 1 秒
# （3.5.6 有 cp312 manylinux wheel），dry-run 解析也正常，所以这不是版本问题，
# 是传输稳定性问题。对策：加大 retries/timeout + 把 greenlet 前置单独安装。
RUN pip config set global.index-url "${PIP_INDEX_URL}" \
    && pip install --no-cache-dir --retries 10 --timeout 120 "pytest>=8.2,<9" pytest-cov httpx pydantic "ruff>=0.6,<0.7" \
    && pip install --no-cache-dir --retries 10 --timeout 120 "greenlet>=3.5,<4" \
    && pip install --no-cache-dir --retries 10 --timeout 120 "fastapi>=0.115" "uvicorn>=0.30" "sqlalchemy[asyncio]>=2.0" "aiosqlite>=0.20" "alembic>=1.13" "pytest-asyncio>=0.23,<1"

WORKDIR /workspace
CMD ["sleep", "infinity"]
