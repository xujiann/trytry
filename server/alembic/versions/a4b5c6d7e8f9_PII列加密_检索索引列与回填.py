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
   **真要写索引前先确认密钥不是代码默认值**（`_assert_real_secret`）：拿默认
   secret 算出的索引与应用（带真实密钥）算出的对不上，索引非空但全部失效，
   而且零报错零日志。这条守卫只在**真有行要回填**时才拦，空库/新装不受影响。

Revision ID: a4b5c6d7e8f9
Revises: b1f2a3c4d5e6
Create Date: 2026-08-22
"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH = 500


#: 明确放行默认密钥回填的逃生阀（本地开发/演示库确实就用默认 secret 跑）
_ALLOW_DEFAULT_SECRET_ENV = "MEDPLAT_ALLOW_DEFAULT_SECRET_PII_BACKFILL"


def _assert_real_secret(table: str, plain_col: str, rows: int) -> None:
    """要写索引却拿着默认 secret —— 拒绝继续，并把修法写清楚。

    防的是部署里最容易踩的一脚：发布 shell 没导 MEDPLAT_SECRET 就跑
    `alembic upgrade heads`，迁移拿 config 的默认值算 HMAC，应用随后带真实
    密钥启动——索引列**全部非空但全部算错**，EMPI 去重/实名绑定静默失效，
    全程零报错零日志（查不到只表现为"这人没建过档"，于是重复建档、重复开户）。

    为什么拒跑而不是只警告：迁移日志没人逐行看，而算错的索引和算对的索引
    在库里长得一模一样，事后只能靠抽样解密才发现；此刻停下来重跑一次迁移的
    代价，远小于上线后重复主数据的清理代价。**只在真有行要回填时**才拦
    （空库/新装/CI 一行都不写，无从算错，照常放行），所以这条守卫不会拦住
    任何一次全新部署。本地开发库确实就用默认 secret，导
    `MEDPLAT_ALLOW_DEFAULT_SECRET_PII_BACKFILL=1` 明确放行。
    """
    from app.config import DEFAULT_SECRET, settings

    if settings.secret != DEFAULT_SECRET:
        return
    if os.environ.get(_ALLOW_DEFAULT_SECRET_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return
    raise RuntimeError(
        f"拒绝回填 PII 检索索引：MEDPLAT_SECRET 仍是代码默认值，"
        f"而 {table}.{plain_col} 有 {rows} 行待回填。"
        f"用默认密钥算出的 {table} 索引与应用（带真实密钥）算出的对不上，"
        f"表现为索引非空但全部失效——EMPI 去重与实名绑定会静默漏，且不报任何错。"
        f"请在跑迁移的 shell 里导出与应用**完全相同**的 MEDPLAT_SECRET 后重跑"
        f"（`export MEDPLAT_SECRET=...` 再 `alembic upgrade heads`）；"
        f"确认本库就该用默认密钥（本地开发/演示）时设 {_ALLOW_DEFAULT_SECRET_ENV}=1。"
        f"若索引已被算错，用 `python scripts/pii_encrypt_backfill.py --rebuild-index` 修回。"
    )


def _backfill_idx(table: str, pairs: list[tuple[str, str]]) -> None:
    """逐批回填 idx = pii_index(明文)。带 pii1$ 前缀的行跳过（密文算不出索引，
    这类行只可能来自已跑过的回填脚本，索引由脚本维护——这些行的索引若也丢了，
    修法是 `scripts/pii_encrypt_backfill.py --rebuild-index`，本迁移不负责）。

    真要写索引前先校验密钥不是默认值（见 `_assert_real_secret`）。"""
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
            _assert_real_secret(table, plain_col, len(rows))
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

    # 4. 回填检索索引（幂等，逐批；默认密钥守卫见 `_assert_real_secret`）
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
