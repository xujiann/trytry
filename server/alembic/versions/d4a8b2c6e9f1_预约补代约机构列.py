"""预约补代约机构列 appointments.booked_org_id（P1-59）

Revision ID: d4a8b2c6e9f1
Revises: c3f7a9e1d5b2
Create Date: 2026-09-28

乡镇替患者约县医院的号是医共体的常态，但预约上只记了号源（`slot_id` → 号源机构），
没记是哪家机构代约的。于是取消预约无从判归属：按号源机构判会让代约的乡镇取消不了
自己约的号，只好谁都不判——任何机构的经办都能取消别家约的号，号源被放回池里又被
别人约走，患者到了医院才知道号没了。

写入点在服务端：管理端代约时取经办人所在机构，**不收请求体**。居民自助预约不落
（那不是哪家机构代约的）。

**不回填。** 预约上没有建单人列，代约机构不可考；按号源机构回填是把"号源方"错当
"代约方"。存量行为空＝归属未定，取消照旧不判，与改动前一致。

列名以 `org_id` 结尾，`visibility._relation_tables()` 会把代约机构算进与患者的服务
关系——替患者约号本就是在为他服务。

发现与修复：为空不影响正确性；若事后要补，只能人工核对后
`UPDATE appointments SET booked_org_id = <机构 id> WHERE id = <行 id> AND booked_org_id IS NULL`。
"""
import sqlalchemy as sa
from alembic import op

revision = "d4a8b2c6e9f1"
down_revision = "c3f7a9e1d5b2"
branch_labels = None
depends_on = None

_FK = "fk_appointments_booked_org_id_organizations"


def upgrade() -> None:
    # SQLite 加外键要重建表，故走 batch（与 `c3f7a9e1d5b2` 同一做法）
    with op.batch_alter_table("appointments") as batch:
        batch.add_column(sa.Column("booked_org_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(_FK, "organizations", ["booked_org_id"], ["id"])
    op.create_index("ix_appointments_booked_org_id", "appointments", ["booked_org_id"])


def downgrade() -> None:
    # 只丢本迁移新加的列；它只在本迁移之后由服务端写入，不承载既有业务数据
    op.drop_index("ix_appointments_booked_org_id", table_name="appointments")
    with op.batch_alter_table("appointments") as batch:
        batch.drop_constraint(_FK, type_="foreignkey")
        batch.drop_column("booked_org_id")
