"""居民账户绑定档案的部分唯一索引：一份档案只绑一个账户，约束下沉数据库

应用层查重（bind_realname / _autobind_by_phone 的"先查 taken 再绑"）是
check-then-act，并发下两个账户可同时绑上同一份档案——与全域基金池 D-2
同一个形状。修法同样是把兜底落在数据库：patient_id 上建部分唯一索引，
NULL（未绑定）放行任意多个，非 NULL 唯一。

存量冲突不阻塞升级、也不替人改数据
----------------------------------

**本迁移的第一版会静默解绑，那是错的。** 它执行

    UPDATE resident_accounts SET patient_id = NULL
     WHERE patient_id IS NOT NULL AND id NOT IN (SELECT MIN(id) ... GROUP BY patient_id)

把"一档多户"里除 id 最小以外的账户全部解绑。后果：受影响居民下次登录发现自己
看不到档案，而库里**没有任何记录**说明是谁在什么时候被解绑的——`patient_id` 被
置空的那一刻，"这个账户曾经绑过谁"这条信息就永久消失了（解绑走的是迁移里的裸
SQL，不经过应用层，`audit_logs` 里一条都没有）。而且"留 id 最小的"本身就是错的
判据：先注册的未必是本人，一档多户最常见的成因恰恰是**家人替老人注册**，本人的
账户往往是后建的那个。

现在改成与 `e5b7c9d1f3a4`（住院结算单唯一索引）一致的**不阻塞路径**：探到冲突就
跳过建索引、打一条指名 patient_id 与账户 id 的 ERROR 日志，由人来判断哪个账户
是本人，处置完再由 DBA 手工补建索引。选"跳过"而不是"直接失败"，是因为账户绑定
关系不像结算单那样有下游凭证，拦住整条升级链的代价（后面几十个迁移全上不去）
远大于晚几天建索引；应用层的 409 查重此刻仍然生效，重复只会存量不会新增。

人工处置 SQL（DBA 执行）::

    -- ① 列出冲突：一份档案上挂了几个账户，分别是谁
    SELECT ra.patient_id, ra.id AS account_id, ra.phone, ra.nickname,
           ra.created_at, ra.last_login_at, ra.status
      FROM resident_accounts ra
     WHERE ra.patient_id IN (
             SELECT patient_id FROM resident_accounts
              WHERE patient_id IS NOT NULL
              GROUP BY patient_id HAVING COUNT(*) > 1)
     ORDER BY ra.patient_id, ra.id;

    -- ② 逐条人工确认哪个账户是本人（比对手机号与 patients.phone、看最近登录时间），
    --    再解绑其余账户。一次一条，不要写成批量 UPDATE。
    UPDATE resident_accounts SET patient_id = NULL WHERE id = <确认要解绑的账户 id>;
    -- 家人代管的，改走代管关系而不是本人绑定：
    INSERT INTO resident_family_members (account_id, patient_id, relation, created_at)
         VALUES (<账户 id>, <档案 id>, '其他', CURRENT_TIMESTAMP);

    -- ③ 冲突清空后补建索引
    CREATE UNIQUE INDEX uq_resident_account_patient
        ON resident_accounts (patient_id) WHERE patient_id IS NOT NULL;

第 ③ 步还有一条自动路径：补偿迁移 `a1c3e5b7d9f2`（平台链末尾）在**它自己那一次**
执行时会重探一遍，无冲突就补建。所以——本库当时若停在 `a1c3e5b7d9f2` 之前（本迁移
与它之间还隔着十几条 revision，中途停一停很常见），处置完直接 `alembic upgrade heads`
即可；若本库已经升到了链尾，alembic 不会重跑已记录的 revision，请按上面第 ③ 步
**手工 CREATE INDEX**。

已经被静默解绑的存量数据无法自动恢复
------------------------------------

**在本次整改之前跑过第一版的库，解绑已经发生，而且恢复不了。** `patient_id` 是
这条绑定关系的唯一载体，置空即信息丢失；平台没有为此留任何影子表、审计或日志。
能做的只有人工排查嫌疑记录并请居民重新实名绑定——排查方法（含 SQL）写在
`docs/运维手册.md`"静默解绑的人工排查"一节，补偿迁移 `a1c3e5b7d9f2` 也会在升级时
把嫌疑条数打进日志提醒运维。

Revision ID: d3e4f5a6b7c8
Revises: e0f1a2b3c4d5
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_INDEX_NAME = "uq_resident_account_patient"
_TABLE = "resident_accounts"
_WHERE = "patient_id IS NOT NULL"

#: 一份档案挂了多个账户的明细（patient_id, account_id），按档案聚拢
CONFLICT_SQL = (
    "SELECT patient_id, id AS account_id FROM resident_accounts "
    "WHERE patient_id IN ("
    "  SELECT patient_id FROM resident_accounts WHERE patient_id IS NOT NULL"
    "  GROUP BY patient_id HAVING COUNT(*) > 1) "
    "ORDER BY patient_id, id"
)


def _conflicts(bind: sa.engine.Connection) -> dict[int, list[int]]:
    """{档案 id: [账户 id, …]}——只读，不改任何一行。"""
    grouped: dict[int, list[int]] = {}
    for patient_id, account_id in bind.execute(sa.text(CONFLICT_SQL)).fetchall():
        grouped.setdefault(patient_id, []).append(account_id)
    return grouped


def _has_index(bind: sa.engine.Connection) -> bool:
    return _INDEX_NAME in {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind):
        return  # 已建过（重放安全）
    conflicts = _conflicts(bind)
    if conflicts:
        logger.error(
            "resident_accounts 存量存在“一份档案绑多个账户”（档案 id: 账户 id 列表 = %s），"
            "本次跳过建 %s。**不自动解绑**：patient_id 一旦置空，“这个账户曾经绑过谁”"
            "就永久丢失，且“留 id 最小的”未必是本人（家人替老人注册很常见）。"
            "请人工确认本人账户后逐条处置，SQL 见本迁移 docstring；"
            "处置完按 docstring 第 ③ 步手工 CREATE INDEX（本库若还没升到链尾 a1c3e5b7d9f2，"
            "下一次 `alembic upgrade heads` 会自动补建）。"
            "应用层的 409 查重仍然生效，重复只会存量不会新增。",
            dict(sorted(conflicts.items())),
            _INDEX_NAME,
        )
        return
    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["patient_id"],
        unique=True,
        sqlite_where=sa.text(_WHERE),
        postgresql_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    # upgrade 可能因存量冲突而跳过建索引，降级要能对付"索引不存在"
    bind = op.get_bind()
    if _has_index(bind):
        op.drop_index(_INDEX_NAME, table_name=_TABLE)
