"""绩效指标 remote_exam 改名：远程诊断服务量 → 共享诊断协同量

原名名不副实：该维度按 `exam_requests.from_org_id`（**申请方**）计，衡量的是
基层通过平台解决检查需求的次数，不是诊断工作量；而且真正领取并出报告的
共享诊断中心一分不得。同一轮改动让两侧都计入，名字也随之涵盖两侧。

**只改没被现场改过的那些**：`performance_indicators.name` 是管理端可编辑的
（`PATCH /api/performance/indicators/{key}`），种子又是"只增不改"。无条件覆盖
会把某个县自己改过的名字冲掉——所以 WHERE 里带上旧的默认值做判据。

Revision ID: b5d9f3a71c2e
Revises: a4c8e2f60b19
Create Date: 2026-08-22
"""
from alembic import op

revision = "b5d9f3a71c2e"
down_revision = "a4c8e2f60b19"
branch_labels = None
depends_on = None

OLD_NAME = "远程诊断服务量"
NEW_NAME = "共享诊断协同量"


def upgrade() -> None:
    op.execute(
        "UPDATE performance_indicators "
        f"SET name = '{NEW_NAME}' "
        f"WHERE key = 'remote_exam' AND name = '{OLD_NAME}'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE performance_indicators "
        f"SET name = '{OLD_NAME}' "
        f"WHERE key = 'remote_exam' AND name = '{NEW_NAME}'"
    )
