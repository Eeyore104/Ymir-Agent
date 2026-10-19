"""评测套件（§12.2）：``evals/suites/*.yaml`` 的结构化表示与加载。

套件声明：``id`` / ``version`` / ``weights``（四维权重）/ ``thresholds``（双门槛）/
``tasks``（回归基线任务：原始需求 + 目标产物引用 + 确定性检查）/
``binding_mode``（任务 ↔ 产物的绑定方式，docs/07 §4.1.2）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.evaluation.evaluators.base import DEFAULT_WEIGHTS, EvalTask

DEFAULT_SUITES_DIR = PROJECT_ROOT / "evals" / "suites"

#: 绑定方式（docs/07 §4.1.2 唯一权威表）。
BINDING_MODE_SINGLE: str = "single"
BINDING_MODE_PER_TASK: str = "per_task"

#: 绑定方式字面量类型（pydantic 校验用）。
BindingMode = Literal["single", "per_task"]


class Thresholds(BaseModel):
    """双门槛（§12.3）。"""

    total: float = Field(default=75.0, ge=0, le=100)
    runnability: float = Field(default=60.0, ge=0, le=100)


class EvalSuite(BaseModel):
    """评测套件定义。"""

    id: str
    version: str = "1.0"
    description: str = ""
    #: 任务 ↔ 产物绑定方式：``single``（缺省，v1.0 行为）/ ``per_task``（基准套件）
    binding_mode: BindingMode = BINDING_MODE_SINGLE
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: Thresholds = Field(default_factory=Thresholds)
    tasks: list[EvalTask] = Field(default_factory=list)

    @property
    def is_per_task(self) -> bool:
        """是否按任务各自绑定产物（``per_task`` 模式）。"""
        return self.binding_mode == BINDING_MODE_PER_TASK

    @classmethod
    def load(cls, suite_id: str, *, suites_dir: Path | None = None) -> EvalSuite:
        """加载并校验套件（找不到 -> 1002；结构非法 -> 1001）。"""
        directory = suites_dir or DEFAULT_SUITES_DIR
        path = directory / f"{suite_id}.yaml"
        if not path.exists():
            raise NotFoundError(f"评测套件不存在: {suite_id}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        try:
            suite = cls.model_validate(raw)
        except ValidationError as exc:
            raise InvalidParamError(f"评测套件结构非法（{suite_id}）: {exc}") from exc
        return suite

    def task(self, task_id: str) -> EvalTask | None:
        for item in self.tasks:
            if item.id == task_id:
                return item
        return None
