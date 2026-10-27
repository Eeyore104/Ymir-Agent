# ymir-agent 沙箱镜像（架构设计 §3.5；v1.3 T2.1 / T2.2 / T3.2）
#
# 构建：docker build -f docker/sandbox.Dockerfile -t ymir-sandbox:latest .
# 使用：设置 YMIR_SANDBOX_IMAGE=ymir-sandbox:latest 后沙箱改用本镜像
#
# 说明：
# - 预装高频测试依赖（pytest / pytest-cov / httpx / pydantic）+ 模板栈
#   （fastapi / uvicorn / sqlalchemy[asyncio] / aiosqlite / alembic /
#   pytest-asyncio），v1.3 再补 **pandas / numpy**（data-pipeline 模板用）；
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
# - **Node 运行时（v1.3 T2.1）**：多阶段从 `node:20-slim` **拷贝** node/npm/npx
#   与全局模块，**不用 NodeSource 的 curl|bash**（外网脚本 + 额外 apt 层）。
#   拷贝而非 `apt-get install nodejs` 的理由：① 版本可钉死（Node 20）；
#   ② 无外网源依赖；③ 不引入 apt 缓存层（体积更可控）。
# - **离线依赖池（v1.3 T2.2 / T3.2）**：
#   `/opt/fe-baseline`（vue3 栈）与 `/opt/node-baseline`（express + tsc）在**构建期**
#   各跑一次 `npm ci`，产出与 lock 一致的 `node_modules`（含 linux 原生二进制）；
#   `/opt/npm-cache` 保留 npm 缓存，使沙箱内 `npm ci --offline` 仍可命中。
#   使用方：`docker/bin/fe-build` / `docker/bin/node-build`（**不进用户产物树**）。
# - 断网可构建是硬约束：本文件所有安装都发生在 **build 期**，运行期（network=none）
#   只读预置产物。

# ---------- stage 1：Node 20 运行时（仅作拷贝源） ----------
FROM node:20-slim AS node-runtime

# ---------- stage 2：沙箱主镜像 ----------
FROM python:3.12-slim

# 可覆盖的 PyPI 索引（构建期参数，不写死在层里）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 网络容错（2026-10-26）：本机经 VPN 到镜像源速率不稳，单包下载偶发超时会让
# pip 放弃 wheel 回退到源码构建 —— greenlet 是 C 扩展，slim 镜像无 gcc，
# 于是报 "Failed building wheel for greenlet"。实测单独装 greenlet 只需 1 秒
# （3.5.6 有 cp312 manylinux wheel），dry-run 解析也正常，所以这不是版本问题，
# 是传输稳定性问题。对策：加大 retries/timeout + 把 greenlet 前置单独安装。
# v1.3：同一 RUN 层追加 pandas / numpy（B14：扩表必须与镜像同一 PR）。
RUN pip config set global.index-url "${PIP_INDEX_URL}" \
    && pip install --no-cache-dir --retries 10 --timeout 120 "pytest>=8.2,<9" pytest-cov httpx pydantic "ruff>=0.6,<0.7" \
    && pip install --no-cache-dir --retries 10 --timeout 120 "greenlet>=3.5,<4" \
    && pip install --no-cache-dir --retries 10 --timeout 120 "fastapi>=0.115" "uvicorn>=0.30" "sqlalchemy[asyncio]>=2.0" "aiosqlite>=0.20" "alembic>=1.13" "pytest-asyncio>=0.23,<1" \
    && pip install --no-cache-dir --retries 10 --timeout 120 "pandas>=2.2,<3" "numpy>=1.26,<3"

# ---------- Node 运行时（拷贝，不装 apt 包） ----------
# node 二进制 + npm/npx 的 CLI 入口在 /usr/local/lib/node_modules/npm，
# 用符号链接挂到 PATH（与官方镜像的布局一致）。
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# ---------- 离线构建脚本（不进用户产物树） ----------
COPY docker/bin/fe-build docker/bin/node-build /usr/local/bin/
RUN chmod 0755 /usr/local/bin/fe-build /usr/local/bin/node-build

# ---------- 离线依赖池（构建期 npm ci，运行期只读） ----------
ENV npm_config_cache=/opt/npm-cache
COPY docker/fe-baseline /opt/fe-baseline
COPY docker/node-baseline /opt/node-baseline
# 两个池子各装一次：lock 与 node_modules 在镜像内自洽，
# 且 linux 平台的 esbuild / rollup 原生包按 lock 里的 optional 依赖落地。
RUN cd /opt/fe-baseline && npm ci --no-audit --no-fund \
    && cd /opt/node-baseline && npm ci --no-audit --no-fund

WORKDIR /workspace
CMD ["sleep", "infinity"]
