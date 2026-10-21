# ymir-agent 沙箱镜像（架构设计 §3.5）
#
# 构建：docker build -f docker/sandbox.Dockerfile -t ymir-sandbox:latest .
# 使用：设置 YMIR_SANDBOX_IMAGE=ymir-sandbox:latest 后沙箱改用本镜像
#
# 说明：
# - 预装高频测试依赖（pytest / pytest-cov / httpx / pydantic），
#   减少评测阶段联网需求（配合默认 network=none 断网执行）；
# - **pytest 与 ruff 是基线跑分（scripts/run_benchmark.py）启动预检的必需项**
#   （docs/07 §4.2.1）：镜像内 `python -m pytest --version` 与 `ruff --version`
#   必须可执行，任一缺失则预检 exit 2、不发起任何 run（避免静默跑出全红基线）；
#   版本基准：pytest 8.x / ruff 0.6.x（设计基准 0.6.9）；
# - PyPI 使用清华镜像（中国网络环境）；
# - Node 预装留 v1.x 镜像变体（前端类产物评测）。

FROM python:3.12-slim

RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir "pytest>=8.2,<9" pytest-cov httpx pydantic "ruff>=0.6,<0.7"

WORKDIR /workspace
CMD ["sleep", "infinity"]
