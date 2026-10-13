"""ORM 声明基类与通用类型。

- ID：UUID v4 字符串（§10.3-4）
- 时间：ISO 8601 UTC 存储（自定义 ``TZDateTime`` TypeDecorator 保证时区感知）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase


def new_uuid() -> str:
    """生成 UUID v4 字符串。"""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """返回当前 UTC 时间（时区感知）。"""
    return datetime.now(UTC)


class TZDateTime(TypeDecorator[datetime]):
    """时区感知日期时间类型。

    以 ISO 8601 UTC 字符串落库，读取时还原为带 UTC 时区的 ``datetime``，
    规避 SQLite 对 ``DateTime(timezone=True)`` 的时区丢失问题。
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"TZDateTime 仅接受 datetime，收到 {type(value)!r}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


class Base(DeclarativeBase):
    """所有 ORM 实体的声明基类。"""
