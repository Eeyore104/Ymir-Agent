"""HITL 审批引擎（架构设计 §3.8）：三模式策略 + 审批生命周期 + 服务层。

模块划分：
- :mod:`~ymir.hitl.policy`：三模式策略与关卡语义（纯函数，无 IO）；
- :mod:`~ymir.hitl.engine`：审批生命周期（挂点阻断 / 决策应用 / 驳回回退重跑）；
- :mod:`~ymir.hitl.service`：聚合查询与决策入口（供 API 层调用）。

挂点经 :func:`~ymir.orchestrator.gates.build_approval_hook` 注入
``OrchestrationEngine``，API 层经 ``POST /approvals/{id}/decision`` 决策。
"""

from __future__ import annotations

from ymir.hitl.engine import ApprovalEngine
from ymir.hitl.policy import ApprovalPolicy
from ymir.hitl.service import ApprovalService

__all__ = ["ApprovalEngine", "ApprovalPolicy", "ApprovalService"]
