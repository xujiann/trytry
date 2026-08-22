"""工程包 E3：PII 列加密——检索索引列、长度扩容、双轨唯一索引与索引回填

为什么先加索引列再谈加密：加密开关（MEDPLAT_PII_ENCRYPTION_ENABLED）默认关，
开启的前提是等值检索（EMPI 去重、实名绑定、HL7 对档）在密文态仍然可用——
这靠 HMAC 检索索引列（*_idx），必须在开关打开**之前**铺满存量数据。
本迁移做三件事：

1. 三列 idx（patients.id_card_idx / patients.phone_idx / resident_accounts.phone_idx，
   String(64) 可空）+ 普通索引 + **部分唯一索引**（NOT NULL 时唯一）。原明文列的
   唯一约束**保留**：关态仍靠它挡重复；开态密文带随机 nonce 各不相同不会误撞，
   唯一性由 idx 部分唯一索引接管（双轨，两态都有约束兜底）。
2. 明文列长度 18/20 → 256，容纳 pii1$ 密文（迁移即扩，先扩后加密才不用二次停机）。
3. 回填 idx = HMAC(现明文)。SQL 做不了 HMAC——用 op.get_bind() 在 Python 里逐批
   UPDATE（WHERE idx IS NULL，天然幂等：中断重跑只处理剩余行）。批内 executemany；
   Alembic 的事务边界下各批随迁移一起提交，存量明文改写为密文属运维动作，
   由 scripts/pii_encrypt_backfill.py 负责（真·分批 commit + 回读校验）。

Revision ID: a4b5c6d7e8f9
Revises: b1f2a3c4d5e6
Create Date: 2026-08-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH = 500


def _backfill_idx(table: str, pairs: list[tuple[str, str]]) -> None:
    """逐批回填 idx = pii_index(明文)。带 pii1$ 前缀的行跳过（密文算不出索引，
    这类行只可能来自已跑过的回填脚本，索引由脚本维护）。"""
    from app.pii import pii_index

    bind = op.get_bind()
    for plain_col, idx_col in pairs:
        while True:
            # NOT LIKE 'pii1$%'：密文行直接跳过（$ 在 LIKE 里无通配含义，两方言一致）
            rows = bind.execute(
                sa.text(
                    f"SELECT id, {plain_col} AS plain FROM {table} "  # noqa: S608 - 表列名为本文件常量
                    f"WHERE {idx_col} IS NULL AND {plain_col} IS NOT NULL "
                    f"AND {plain_col} != '' AND {plain_col} NOT LIKE 'pii1$%' "
                    f"LIMIT {_BATCH}"
                )
            ).fetchall()
            if not rows:
                break
            bind.execute(
                sa.text(f"UPDATE {table} SET {idx_col} = :idx WHERE id = :id"),  # noqa: S608
                [{"id": r.id, "idx": pii_index(str(r.plain))} for r in rows],
            )


def upgrade() -> None:
    # 1. 加列（可空——加列即刻生效，回填分批随后）
    op.add_column("patients", sa.Column("id_card_idx", sa.String(length=64), nullable=True))
    op.add_column("patients", sa.Column("phone_idx", sa.String(length=64), nullable=True))
    op.add_column(
        "resident_accounts", sa.Column("phone_idx", sa.String(length=64), nullable=True)
    )

    # 2. 明文列扩容（SQLite 改类型需 batch 重建表；PG 渲染为普通 ALTER）
    with op.batch_alter_table("patients") as batch:
        batch.alter_column(
            "id_card", existing_type=sa.String(length=18), type_=sa.String(length=256)
        )
        batch.alter_column(
            "phone", existing_type=sa.String(length=20), type_=sa.String(length=256)
        )
    with op.batch_alter_table("resident_accounts") as batch:
        batch.alter_column(
            "phone", existing_type=sa.String(length=20), type_=sa.String(length=256)
        )

    # 3. 索引：普通检索索引 + 部分唯一索引（NOT NULL 时唯一；双轨理由见文件头）
    op.create_index(op.f("ix_patients_id_card_idx"), "patients", ["id_card_idx"], unique=False)
    op.create_index(op.f("ix_patients_phone_idx"), "patients", ["phone_idx"], unique=False)
    op.create_index(
        op.f("ix_resident_accounts_phone_idx"), "resident_accounts", ["phone_idx"], unique=False
    )
    op.create_index(
        "uq_patient_id_card_idx",
        "patients",
        ["id_card_idx"],
        unique=True,
        sqlite_where=sa.text("id_card_idx IS NOT NULL"),
        postgresql_where=sa.text("id_card_idx IS NOT NULL"),
    )
    op.create_index(
        "uq_resident_account_phone_idx",
        "resident_accounts",
        ["phone_idx"],
        unique=True,
        sqlite_where=sa.text("phone_idx IS NOT NULL"),
        postgresql_where=sa.text("phone_idx IS NOT NULL"),
    )

    # 4. 回填检索索引（幂等，逐批）
    _backfill_idx("patients", [("id_card", "id_card_idx"), ("phone", "phone_idx")])
    _backfill_idx("resident_accounts", [("phone", "phone_idx")])


def downgrade() -> None:
    op.drop_index("uq_resident_account_phone_idx", table_name="resident_accounts")
    op.drop_index("uq_patient_id_card_idx", table_name="patients")
    op.drop_index(op.f("ix_resident_accounts_phone_idx"), table_name="resident_accounts")
    op.drop_index(op.f("ix_patients_phone_idx"), table_name="patients")
    op.drop_index(op.f("ix_patients_id_card_idx"), table_name="patients")
    with op.batch_alter_table("resident_accounts") as batch:
        batch.alter_column(
            "phone", existing_type=sa.String(length=256), type_=sa.String(length=20)
        )
        batch.drop_column("phone_idx")
    with op.batch_alter_table("patients") as batch:
        batch.alter_column(
            "phone", existing_type=sa.String(length=256), type_=sa.String(length=20)
        )
        batch.alter_column(
            "id_card", existing_type=sa.String(length=256), type_=sa.String(length=18)
        )
        batch.drop_column("phone_idx")
        batch.drop_column("id_card_idx")
