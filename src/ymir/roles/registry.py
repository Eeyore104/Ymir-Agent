"""角色注册表（架构设计 §3.1、E4 扩展点）：扫描 ``config/roles/*.yaml``。

热增角色：新增 YAML + 提示词模板即注册，SOP 引用 ``role_id`` 生效，无需改代码。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.roles.definition import RoleDefinition


class RoleRegistry:
    """角色注册表（构造即加载；``reload`` 支持热更新）。"""

    def __init__(self, roles_dir: Path | str) -> None:
        self._dir = Path(roles_dir)
        self._roles: dict[str, RoleDefinition] = {}
        self.reload()

    @property
    def roles_dir(self) -> Path:
        return self._dir

    def reload(self) -> None:
        """重新扫描角色定义目录（YAML 非法 / ID 重复时报错）。"""
        roles: dict[str, RoleDefinition] = {}
        if self._dir.is_dir():
            for path in sorted(self._dir.glob("*.yaml")):
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                role = RoleDefinition.model_validate(data)
                if role.id in roles:
                    raise InvalidParamError(f"角色 ID 重复: {role.id}（{path.name}）")
                roles[role.id] = role
        self._roles = roles

    def get(self, role_id: str) -> RoleDefinition:
        role = self._roles.get(role_id)
        if role is None:
            raise NotFoundError(
                f"角色未注册: {role_id}（已注册：{sorted(self._roles)}）"
            )
        return role

    def all(self) -> list[RoleDefinition]:
        return [self._roles[key] for key in sorted(self._roles)]

    def ids(self) -> list[str]:
        return sorted(self._roles)

    def __contains__(self, role_id: str) -> bool:
        return role_id in self._roles
