"""角色变更记录的两个角色列 16 → 32，与 users.role / 自定义角色键同宽（P1-91 查出）

`users.role` 是 `String(32)`，自定义角色的键最长 32（`rbac.RoleIn.key`），改角色的入参也收到 32
（`users.RoleUpdate.role`）——唯独留痕表 `role_change_logs.old_role / new_role` 只有 16。于是给用户
改成一个键长 17～32 的自定义角色：SQLite 不管长度照存；**PostgreSQL 写留痕那一行时超长，整个改角色
请求 500**（角色也没改成，因为同一个事务）。反方向同理：一个已经是长键角色的用户再被改走，old_role 超长。

本迁移只放宽列宽，不碰任何存量数据（C 档结构变更）。

## 回退

`downgrade()` 把两列收回 16。**先探存量**：只要有一行任一列超过 16，就拒绝回退并指名这些行的 id——
PostgreSQL 上收窄遇到超长值本来就会报错，这里是提前报、报清楚；SQLite 的 batch 重建不管长度，
不探就会把超长值原样抄过去，回退「成功」却与模型对不上。回退前的人工处置：

    -- 查：哪些留痕行的角色键超过 16
    SELECT id, user_id, old_role, new_role, created_at FROM role_change_logs
     WHERE length(old_role) > 16 OR length(new_role) > 16;
    -- 这些是审计留痕，不要改写内容；要回退就先把对应的自定义角色停用、
    -- 把相关用户改回短键角色（走接口，留新的留痕），确认上面查不出行之后再回退。
    -- 若必须保留这些留痕又必须回退，只能放弃回退（保留 32 宽度不影响旧版本代码读写）。

Revision ID: c3e4f5a6b7d9
Revises: b9c8d7e6f5a4
Create Date: 2026-09-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e4f5a6b7d9"
down_revision: Union[str, Sequence[str], None] = "b9c8d7e6f5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("role_change_logs") as batch:
        batch.alter_column("old_role", existing_type=sa.String(length=16), type_=sa.String(length=32))
        batch.alter_column("new_role", existing_type=sa.String(length=16), type_=sa.String(length=32))


def downgrade() -> None:
    too_long = op.get_bind().execute(sa.text(
        "SELECT id FROM role_change_logs WHERE length(old_role) > 16 OR length(new_role) > 16 ORDER BY id"
    )).scalars().all()
    if too_long:
        raise RuntimeError(
            "拒绝回退：role_change_logs 里有角色键超过 16 的留痕行，收回 16 会报错（PG）或与模型对不上（SQLite）。"
            f"行 id：{too_long}。处置办法见本迁移 docstring 的「回退」一节。"
        )
    with op.batch_alter_table("role_change_logs") as batch:
        batch.alter_column("new_role", existing_type=sa.String(length=32), type_=sa.String(length=16))
        batch.alter_column("old_role", existing_type=sa.String(length=32), type_=sa.String(length=16))
