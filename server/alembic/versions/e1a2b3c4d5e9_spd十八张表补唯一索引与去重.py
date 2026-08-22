"""spd 十八张表：把"唯一"从模型注解落到数据库

模型上这 18 张表的键一直写着 `unique=True`，迁移却把它们建成了**普通索引**——
于是"唯一"只存在于模型注解里，数据库层面根本不存在。实测（PG 16，跑完
`upgrade heads` 的空库）：同一个村医可以插出两个积分账户，余额分别是 10 和 99。
`award_points()` 入账取 `.first()`、兑换核销扣减也取 `.first()`，于是**积分记进
一个账户、兑换扣的是另一个**，账面对不上且全程不报错。

接口层其实一直是防着的：这些表的写入点都走 `insert_if_absent` / 捕获 IntegrityError
（`test_stage14_concurrency` 的"唯一表"判据读的是**模型** metadata，模型上写着
`unique=True`，所以这些表一直在它的管辖范围内）。问题出在**数据库从不产生冲突**——
那条 `409 该编码已存在` 的分支写好了却永远走不到，防御形同虚设。

## 存量重复不阻塞升级，也不替人删数据

照平台 `e5b7c9d1f3a4`（住院结算单部分唯一索引）的范式：升级时先探一遍，

- **干净的表**：普通索引就地改成唯一索引；
- **有重复的表**：**跳过**这张表的唯一索引，把冲突组逐行落进 `spd_dedup_reports`
  （`strategy='pending'`，整行 JSON 存在 `removed_row` 里）并打一条点名到
  表 / 键值 / 行 id 的 ERROR 日志。

为什么不在迁移里直接归并：重复行可能是实施期现场调过的配置，"两份配置差在哪"
要人看；积分账户虽然能按余额相加自动并，但那是一笔改账面的动作，同样该由人
按下按钮。迁移替人删数据，是这类修复最容易变成事故的地方（平台已把这条写成
通则，见 CLAUDE.md §4 与 `tests/test_migration_data_safety.py`）。

## 人工处置

台账查得到冲突（`SELECT * FROM spd_dedup_reports WHERE strategy = 'pending'`），
归并与补建索引用仓库自带的修复脚本，**先看再做**：

    python scripts/spd_dedup.py            # 只报告：哪些表有冲突、打算怎么并
    python scripts/spd_dedup.py --apply    # 归并（改指引用 → 积分相加 → 存档 → 删行）
                                           # 并补建本迁移跳过的唯一索引

不想用脚本、要手工处置的，按台账逐条改完之后 DBA 手工补建即可：

    CREATE UNIQUE INDEX ix_spd_point_accounts_user_id ON spd_point_accounts (user_id);

## downgrade

只把唯一索引改回普通索引（跳过没建成的那些）。台账表整表删除——它是本迁移自己
建的，不含存量业务数据。

**本迁移属 spd 链**（down_revision 指向 spd head），不得挂到平台链。

Revision ID: e1a2b3c4d5e9
Revises: d8e9f1a2b3c8
Create Date: 2026-08-22 12:10:00.000000

"""
import json
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1a2b3c4d5e9'
down_revision: Union[str, Sequence[str], None] = 'd8e9f1a2b3c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


#: (表, 索引名, 唯一键列)
#: 与 `scripts/spd_dedup.py` 的 TARGETS 同源——脚本从本模块 import，不各写一份。
TARGETS: list[tuple[str, str, str]] = [
    ("spd_assess_plans", "ix_spd_assess_plans_code", "code"),
    ("spd_case_report_tasks", "ix_spd_case_report_tasks_code", "code"),
    ("spd_centers", "ix_spd_centers_code", "code"),
    ("spd_data_sources", "ix_spd_data_sources_code", "code"),
    ("spd_devices", "ix_spd_devices_sn", "sn"),
    ("spd_edu_materials", "ix_spd_edu_materials_code", "code"),
    ("spd_followup_rules", "ix_spd_followup_rules_code", "code"),
    ("spd_goods", "ix_spd_goods_code", "code"),
    ("spd_intervention_templates", "ix_spd_intervention_templates_code", "code"),
    ("spd_point_accounts", "ix_spd_point_accounts_user_id", "user_id"),
    ("spd_point_rules", "ix_spd_point_rules_code", "code"),
    ("spd_programs", "ix_spd_programs_code", "code"),
    ("spd_questionnaires", "ix_spd_questionnaires_code", "code"),
    ("spd_referral_rules", "ix_spd_referral_rules_code", "code"),
    ("spd_report_templates", "ix_spd_report_templates_code", "code"),
    ("spd_service_packages", "ix_spd_service_packages_code", "code"),
    ("spd_tags", "ix_spd_tags_code", "code"),
    ("spd_village_doctors", "ix_spd_village_doctors_user_id", "user_id"),
]

#: 引用清单（去重时要改指的外键）来自模型的外键图，供修复脚本使用；
#: 放在这里是为了和 TARGETS 挨着，改表时不容易漏。
REFERENCES: dict[str, list[tuple[str, str]]] = {
    "spd_assess_plans": [("spd_scores", "plan_id")],
    "spd_case_report_tasks": [("spd_case_reports", "task_id")],
    "spd_data_sources": [("spd_sync_logs", "source_id")],
    "spd_edu_materials": [("spd_edu_pushes", "material_id")],
    "spd_followup_rules": [("spd_followup_records", "rule_id")],
    "spd_goods": [("spd_redeems", "goods_id")],
    "spd_intervention_templates": [("spd_interventions", "template_id")],
    "spd_point_accounts": [("spd_point_records", "account_id"),
                           ("spd_redeems", "account_id"),
                           ("spd_signins", "account_id")],
    "spd_programs": [("spd_program_versions", "program_id"),
                     ("spd_targets", "program_id"),
                     ("spd_path_templates", "program_id")],
    "spd_report_templates": [("spd_report_tasks", "template_id")],
    "spd_service_packages": [("spd_package_bindings", "package_id")],
}

#: 可自动归并的表：列 → 归并方式（当前只有"相加"一种）。修复脚本用。
MERGE_SUM = {"spd_point_accounts": ("balance", "earned", "used")}


def duplicate_groups(conn, table: str, key: str) -> list[tuple]:
    """返回 [(键值, [id 从小到大])]，只含真的有重复的组。纯读。"""
    dup_keys = conn.execute(
        sa.text(
            f"SELECT {key} FROM {table} WHERE {key} IS NOT NULL "
            f"GROUP BY {key} HAVING COUNT(*) > 1"
        )
    ).scalars().all()
    groups = []
    for value in dup_keys:
        ids = conn.execute(
            sa.text(f"SELECT id FROM {table} WHERE {key} = :v ORDER BY id"),
            {"v": value},
        ).scalars().all()
        groups.append((value, list(ids)))
    return groups


def row_json(conn, table: str, row_id: int) -> str:
    row = conn.execute(
        sa.text(f"SELECT * FROM {table} WHERE id = :i"), {"i": row_id}
    ).mappings().first()
    return json.dumps(dict(row or {}), ensure_ascii=False, default=str)


def _record_conflict(conn, table: str, key: str, value, keep: int, other: int) -> None:
    """把一条冲突记进台账。只写本迁移新建的台账表，不碰业务表。"""
    conn.execute(
        sa.text(
            "INSERT INTO spd_dedup_reports "
            "(table_name, key_column, key_value, kept_id, removed_id, "
            " strategy, removed_row, note, created_at) "
            "VALUES (:t, :kc, :kv, :keep, :other, 'pending', :row, :note, CURRENT_TIMESTAMP)"
        ),
        {
            "t": table, "kc": key, "kv": str(value), "keep": keep, "other": other,
            "row": row_json(conn, table, other),
            "note": ("积分账户可按余额相加归并，仍须人工确认"
                     if table in MERGE_SUM
                     else "两份配置差异待人工裁定，默认保留最早一条"),
        },
    )


def upgrade() -> None:
    conn = op.get_bind()

    op.create_table(
        "spd_dedup_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("table_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("key_column", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("key_value", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("kept_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("strategy", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("removed_row", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_spd_dedup_reports_table_name"), "spd_dedup_reports",
                    ["table_name"], unique=False)
    op.create_index(op.f("ix_spd_dedup_reports_key_value"), "spd_dedup_reports",
                    ["key_value"], unique=False)
    op.create_index(op.f("ix_spd_dedup_reports_created_at"), "spd_dedup_reports",
                    ["created_at"], unique=False)

    skipped = []
    for table, index_name, key in TARGETS:
        groups = duplicate_groups(conn, table, key)
        if groups:
            for value, ids in groups:
                for other in ids[1:]:
                    _record_conflict(conn, table, key, value, ids[0], other)
            logger.error(
                "%s 存量已有重复的 %s（%s），本次跳过建唯一索引 %s。"
                "冲突已记入 spd_dedup_reports，用 `python scripts/spd_dedup.py` 查看、"
                "`--apply` 归并并补建索引（人工处置 SQL 见本迁移 docstring）。",
                table, key,
                "; ".join(f"{v}→id {ids}" for v, ids in groups),
                index_name,
            )
            skipped.append(table)
            continue
        op.drop_index(index_name, table_name=table)
        op.create_index(index_name, table, [key], unique=True)

    if skipped:
        logger.error(
            "本次有 %d/%d 张 spd 表因存量重复未建成唯一索引：%s。"
            "在处置完之前，这些表的唯一性仍只存在于模型注解里。",
            len(skipped), len(TARGETS), "、".join(skipped),
        )


def downgrade() -> None:
    """把唯一性拿掉。upgrade 可能因存量重复跳过某些表，这里要能对付"没建成"。"""
    inspector = sa.inspect(op.get_bind())
    for table, index_name, key in TARGETS:
        info = {i["name"]: i for i in inspector.get_indexes(table)}.get(index_name)
        if info is None or not info.get("unique"):
            continue
        op.drop_index(index_name, table_name=table)
        op.create_index(index_name, table, [key], unique=False)
    op.drop_index(op.f("ix_spd_dedup_reports_created_at"), table_name="spd_dedup_reports")
    op.drop_index(op.f("ix_spd_dedup_reports_key_value"), table_name="spd_dedup_reports")
    op.drop_index(op.f("ix_spd_dedup_reports_table_name"), table_name="spd_dedup_reports")
    op.drop_table("spd_dedup_reports")
