# ymir-agent

> 多 Agent 协作的软件开发平台：输入一句自然语言需求，四个 AI 角色（产品经理 / 架构师 / 工程师 / QA）按可配置的 SOP 流水线协作，依次产出 **PRD → 架构设计 → 可运行代码 → 四维评测报告**；全程人工可在关键节点审批与驳回，所有产物统一版本管理。

一句话：**把一句需求，变成一套经过评审、评测、可导出的完整软件交付物。**

## 为什么值得一看

- **自研轻量编排内核**（状态机 + 四角色 + HITL 审批），不 fork 任何底座，流程状态全部落库、可恢复；
- **OpenAI 兼容模型网关**：分级路由（贵模型规划 / 便宜模型执行）+ token 预算 + 用量计量，成本可控；
- **Docker 沙箱**承接不可信代码执行，接口化可替换；
- **产物库 + 版本管理**：代码 / 文档 / 评测报告统一落库，支持 diff 与 zip 导出；
- **协议化扩展点（E1-E10）**：新增能力 = 新增一个实现类 + 一行配置，不改内核。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · Alembic · SQLite(WAL) |
| 工程化 | uv · pytest · ruff · GitHub Actions |
| 前端 | Vue 3 · Element Plus · Pinia · vue-router · Vite · TypeScript |
| 实时通信 | SSE（server→client）+ REST（client→server） |

## 快速开始

前置：Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js ≥ 18。

```bash
# 1) 安装后端依赖（生成/校验 uv.lock）
uv sync

# 2) 初始化数据库（SQLite，位于 data/ymir.db）
uv run alembic upgrade head

# 3) 启动后端 API（http://127.0.0.1:8000，文档 /docs）
uv run uvicorn ymir.api.app:app --port 8000

# 4) 另开终端启动前端控制台（http://127.0.0.1:5173）
cd web && npm install && npm run dev
```

冒烟：

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/api/v1/projects \
  -H "Content-Type: application/json" \
  -d '{"name":"demo","description":"示例项目"}'
```

质量门禁：

```bash
uv run ruff check .
uv run pytest
```

## 目录结构（monorepo）

```
ymir-agent/
├── src/ymir/            # 后端源码（config / common / models / storage / api ...）
│   ├── config.py        # pydantic-settings 配置
│   ├── common/          # 业务错误码与异常基类
│   ├── models/          # ORM 实体（§3.10 数据表）
│   ├── storage/         # DB 引擎/会话 + Repository
│   └── api/             # FastAPI app / schemas / routers
├── migrations/          # Alembic 迁移
├── web/                 # Vue3 控制台
├── tests/               # pytest 单测 + 集成测试
└── data/                # 运行时生成（SQLite + 产物，gitignore）
```

## 架构图入口

- 分层架构与九大抽象：`docs/02-架构设计.md`
- 类图：`docs/class-diagram.mermaid`
- 时序图：`docs/sequence-diagram.mermaid`
- 产品需求：`docs/03-产品需求文档（PRD）.md`

## API 响应约定

- 成功：`{"code": 0, "data": ...}`
- 列表：`{"code": 0, "data": {"items": [...], "total": n}}`
- 错误：`{"code": <biz_code>, "message": "..."}`
- 业务码：`1001` 参数错误 / `1002` 资源不存在 / `1003` 状态冲突 / `1004` 预算超限 / `1005` 沙箱错误 / `1006` 模型调用失败
- 时间一律 ISO 8601 UTC 存储，前端本地化渲染；ID 使用 UUID v4 字符串。

## License

MIT License（详见 [LICENSE](LICENSE)；合规边界见 `docs/02-架构设计.md` §8.3）。
