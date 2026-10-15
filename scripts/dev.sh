#!/usr/bin/env bash
# ymir-agent 一键启动（Windows Git Bash / macOS / Linux 兼容）
#
#   bash scripts/dev.sh
#
# 同时起：
#   后端  uvicorn ymir.api.app:app  -> http://127.0.0.1:8000（/docs 接口文档）
#   前端  vite dev                  -> http://127.0.0.1:5173（控制台）
#
# Ctrl+C 会一并退出两个子进程。首次运行前请先 `uv sync` 与 `cd web && npm install`。
set -euo pipefail

# uv 安装位置（本机可能装在 ~/.local/bin）
export PATH="$HOME/.local/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "[dev] 未找到 uv，请先安装：https://docs.astral.sh/uv/" >&2
  exit 1
fi

echo "[dev] 仓库根目录：$ROOT_DIR"
echo "[dev] 启动后端 http://127.0.0.1:8000 …"
uv run uvicorn ymir.api.app:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

if [ -d "$ROOT_DIR/web/node_modules" ]; then
  echo "[dev] 启动前端 http://127.0.0.1:5173 …"
  ( cd "$ROOT_DIR/web" && npm run dev ) &
  FRONTEND_PID=$!
else
  echo "[dev] 未安装前端依赖（web/node_modules 缺失），跳过前端。"
  echo "[dev] 需要时执行：cd web && npm install && npm run dev"
  FRONTEND_PID=""
fi

cleanup() {
  echo ""
  echo "[dev] 正在退出…"
  kill "$BACKEND_PID" >/dev/null 2>&1 || true
  if [ -n "$FRONTEND_PID" ]; then
    kill "$FRONTEND_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup INT TERM EXIT

# 任一子进程退出即整体退出（后台任务前台化）
wait
