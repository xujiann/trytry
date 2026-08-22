"""spd 十八张表：先去重、后把索引改成真的唯一

模型上这 18 张表的键一直写着 `unique=True`，迁移却把它们建成了**普通索引**——
于是"唯一"只存在于模型注解里，数据库层面根本不存在。实测（PG 16，跑完
`upgrade heads` 的空库）：同一个村医可以插出两个积分账户，余额分别是 10 和 99。
`award_points()` 入账取 `.first()`、兑换核销扣减也取 `.first()`，于是**积分记进
一个账户、兑换扣的是另一个**，账面对不上且全程不报错。

接口层其实一直是防着的：这些表的写入点都走 `insert_if_absent` / 捕获 IntegrityError
（`test_stage14_concurrency` 的"唯一表"判据读的是**模型** metadata，模型上写着
`unique=True`，所以这些表一直在它的管辖范围内）。问题出在**数据库从不产生冲突**——
那条 `409 该编码已存在` 的分支写好了却永远走不到，防御形同虚设。

## 去重不能盲删

存量库里的重复行可能是实施期现场调过的配置，删掉就找不回来了。本迁移的处置：

- **积分账户（`spd_point_accounts.user_id`）**：可自动归并——余额、累计获得、
  累计兑换相加并成一条，积分流水/兑换/签到改指到保留行。`strategy=merge`。
- **其余 17 张（编码类 + 村医档案）**：无法自动归并（两份配置差在哪要人看），
  保留 **id 最小的那条**（最早建的），其余行**整行 JSON 存进 `spd_dedup_reports`**
  后移除，并把指向它们的外键改指到保留行。`strategy=keep_earliest`。

留痕表让这一步可回溯：事后要核对"被删的那份配置差在哪"，`removed_row` 里是完整
的原始行。**不做静默删除**——这是数据修复迁移最容易变成事故的地方。

## downgrade

只把唯一索引改回普通索引；**不恢复被去重的行**（那需要判断保留行此后是否被改过，
迁移无法自证安全）。要恢复请查 `spd_dedup_reports` 手工处理——这一点写在这里，
免得有人以为 downgrade 能把数据变回去。

**本迁移属 spd 链**（down_revision 指向 spd head），不得挂到平台链。

Revision ID: e1a2b3c4d5e9
Revises: d8e9f1a2b3c8
Create Date: 2026-08-22 12:10:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1a2b3c4d5e9'
down_revision: Union[str, Sequence[str], None] = 'd8e9f1a2b3c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (表, 索引名, 唯一键列, [(引用表, 引用列), ...])
#: 引用清单来自模型的外键图（`Base.metadata` 反查），不是手数出来的——
#: 少列一个引用表，去重时就会留下指向已删行的孤儿。
TARGETS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    ("spd_assess_plans", "ix_spd_assess_plans_code", "code",
     [("spd_scores", "plan_id")]),
    ("spd_case_report_tasks", "ix_spd_case_report_tasks_code", "code",
     [("spd_case_reports", "task_id")]),
    ("spd_centers", "ix_spd_centers_code", "code", []),
    ("spd_data_sources", "ix_spd_data_sources_code", "code",
     [("spd_sync_logs", "source_id")]),
    ("spd_devices", "ix_spd_devices_sn", "sn", []),
    ("spd_edu_materials", "ix_spd_edu_materials_code", "code",
     [("spd_edu_pushes", "material_id")]),
    ("spd_followup_rules", "ix_spd_followup_rules_code", "code",
     [("spd_followup_records", "rule_id")]),
    ("spd_goods", "ix_spd_goods_code", "code",
     [("spd_redeems", "goods_id")]),
    ("spd_intervention_templates", "ix_spd_intervention_templates_code", "code",
     [("spd_interventions", "template_id")]),
    ("spd_point_accounts", "ix_spd_point_accounts_user_id", "user_id",
     [("spd_point_records", "account_id"), ("spd_redeems", "account_id"),
      ("spd_signins", "account_id")]),
    ("spd_point_rules", "ix_spd_point_rules_code", "code", []),
    ("spd_programs", "ix_spd_programs_code", "code",
     [("spd_program_versions", "program_id"), ("spd_targets", "program_id"),
      ("spd_path_templates", "program_id")]),
    ("spd_questionnaires", "ix_spd_questionnaires_code", "code", []),
    ("spd_referral_rules", "ix_spd_referral_rules_code", "code", []),
    ("spd_report_templates", "ix_spd_report_templates_code", "code",
     [("spd_report_tasks", "template_id")]),
    ("spd_service_packages", "ix_spd_service_packages_code", "code",
     [("spd_package_bindings", "package_id")]),
    ("spd_tags", "ix_spd_tags_code", "code", []),
    ("spd_village_doctors", "ix_spd_village_doctors_user_id", "user_id", []),
]

#: 可自动归并的表：列 → 归并方式（当前只有"相加"一种）
MERGE_SUM = {"spd_point_accounts": ("balance", "earned", "used")}


def _duplicate_groups(conn, table: str, key: str) -> list[tuple]:
    """返回 [(键值, [id 从小到大])]，只含真的有重复的组。"""
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


def _row_json(conn, table: str, row_id: int) -> str:
    row = conn.execute(
        sa.text(f"SELECT * FROM {table} WHERE id = :i"), {"i": row_id}
    ).mappings().first()
    return json.dumps(dict(row or {}), ensure_ascii=False, default=str)


def _dedup(conn, table: str, key: str, refs: list[tuple[str, str]]) -> int:
    """把一张表的重复键归并掉，返回移除的行数。无重复时是纯读、零写。"""
    removed = 0
    for value, ids in _duplicate_groups(conn, table, key):
        keep, losers = ids[0], ids[1:]
        strategy = "merge" if table in MERGE_SUM else "keep_earliest"
        for loser in losers:
            snapshot = _row_json(conn, table, loser)
            # ① 先把引用改指到保留行，避免删出孤儿（外键会直接拦下）
            for ref_table, ref_col in refs:
                conn.execute(
                    sa.text(
                        f"UPDATE {ref_table} SET {ref_col} = :keep WHERE {ref_col} = :loser"
                    ),
                    {"keep": keep, "loser": loser},
                )
            # ② 可归并的把数字并进保留行（积分账户：余额与累计相加）
            if table in MERGE_SUM:
                sums = ", ".join(
                    f"{c} = {c} + (SELECT {c} FROM {table} WHERE id = :loser)"
                    for c in MERGE_SUM[table]
                )
                conn.execute(
                    sa.text(f"UPDATE {table} SET {sums} WHERE id = :keep"),
                    {"keep": keep, "loser": loser},
                )
            # ③ 存档后移除
            conn.execute(
                sa.text(
                    "INSERT INTO spd_dedup_reports "
                    "(table_name, key_column, key_value, kept_id, removed_id, "
                    " strategy, removed_row, note, created_at) "
                    "VALUES (:t, :kc, :kv, :keep, :loser, :s, :row, :note, CURRENT_TIMESTAMP)"
                ),
                {
                    "t": table, "kc": key, "kv": str(value), "keep": keep,
                    "loser": loser, "s": strategy, "row": snapshot,
                    "note": ("余额与累计已并入保留行" if strategy == "merge"
                             else "无法自动归并，保留最早一条，差异待人工裁定"),
                },
            )
            conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :i"), {"i": loser})
            removed += 1
    return removed


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
        sa.Column("strategy", sa.String(length=16), nullable=False, server_default="keep_earliest"),
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

    for table, index_name, key, refs in TARGETS:
        _dedup(conn, table, key, refs)
        op.drop_index(index_name, table_name=table)
        op.create_index(index_name, table, [key], unique=True)


def downgrade() -> None:
    """只把唯一性拿掉；**不还原被去重的数据**（理由见模块文档）。"""
    for table, index_name, key, _refs in TARGETS:
        op.drop_index(index_name, table_name=table)
        op.create_index(index_name, table, [key], unique=False)
    op.drop_index(op.f("ix_spd_dedup_reports_created_at"), table_name="spd_dedup_reports")
    op.drop_index(op.f("ix_spd_dedup_reports_key_value"), table_name="spd_dedup_reports")
    op.drop_index(op.f("ix_spd_dedup_reports_table_name"), table_name="spd_dedup_reports")
    op.drop_table("spd_dedup_reports")
