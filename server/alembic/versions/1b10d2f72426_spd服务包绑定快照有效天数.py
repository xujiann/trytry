"""spd 服务包绑定快照有效天数（P2-576）

服务包的「有效期至」（`spd_package_bindings.period_end`）只在绑包那一刻按纳管档案的服务起始日算一次：绑包时
没填起始日的，有效期一直是空的（管理端建档与调整档案原先都不收起始日，界面建的档案全是这样）；事后补填或更正
起始日也不重算。要在改起始日时重算，得知道这条绑定当初是按几天的包绑的——包本身的天数之后可以改，不能拿现值算。

**本迁移属 spd 链**（down_revision 指向 spd head），不得挂到平台链。

## 升级

纯加列：`period_days`（整数，非空）。存量行以库默认 0 补齐后去掉默认（`c3bfca22dbdd` 同一写法）——只填本迁移
新加的列。0 表示「本列上线前绑的，天数未知」：这些绑定改起始日时有效期保持原值（即修前行为），不按服务包现在的
天数去猜。要让存量绑定也跟着起始日走，由人按下面补录（PG；补的是本迁移新加的列，算错了照同一条重跑即可）：

    -- 先看有哪些、各自的起始日 / 有效期 / 服务包现在的天数
    SELECT b.id, b.enrollment_id, e.service_start, b.period_end, p.period_days AS package_days_now
      FROM spd_package_bindings b
      JOIN spd_enrollments e ON e.id = b.enrollment_id
      JOIN spd_service_packages p ON p.id = b.package_id
     WHERE b.status = 'bound' AND b.period_days = 0;
    -- 起始日与有效期都有的，按两者之差回推（P2-547 修前绑的有效期多算一天，先按 P2-547 的订正 SQL 订正过再推）
    UPDATE spd_package_bindings b SET period_days = (b.period_end::date - e.service_start::date) + 1
      FROM spd_enrollments e
     WHERE e.id = b.enrollment_id AND b.status = 'bound' AND b.period_days = 0
       AND b.period_end <> '' AND e.service_start <> '';
    -- 绑包时没填起始日的（有效期为空），只能按服务包现在的天数补：包在绑定之后改过天数的（P2-573），逐条核对后再补
    UPDATE spd_package_bindings b SET period_days = p.period_days
      FROM spd_service_packages p
     WHERE p.id = b.package_id AND b.id = :binding_id AND b.period_days = 0;

补录之后，下次改这份档案的服务起始日，有效期就按快照天数重算。想不等改档就补出空着的有效期（只填空值）：

    UPDATE spd_package_bindings b
       SET period_end = to_char(e.service_start::date + (b.period_days - 1), 'YYYY-MM-DD')
      FROM spd_enrollments e
     WHERE e.id = b.enrollment_id AND b.status = 'bound' AND b.period_days > 0
       AND b.period_end = '' AND e.service_start <> '';

## 回退

删列。回退后改起始日不再重算有效期（修前行为），已算出的有效期原样保留。回退前要保留快照的话先导出：

    SELECT id, period_days FROM spd_package_bindings WHERE period_days > 0;

Revision ID: 1b10d2f72426
Revises: c3bfca22dbdd
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1b10d2f72426"
down_revision: Union[str, Sequence[str], None] = "c3bfca22dbdd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "spd_package_bindings",
        sa.Column("period_days", sa.Integer(), nullable=False, server_default="0"),
    )
    with op.batch_alter_table("spd_package_bindings") as batch:
        batch.alter_column("period_days", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("spd_package_bindings") as batch:
        batch.drop_column("period_days")
