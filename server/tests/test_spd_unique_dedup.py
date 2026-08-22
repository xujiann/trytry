"""P0-1 的回归网：18 张 spd 表的唯一性必须落在**数据库**上，不是只写在模型注解里。

这套用例守两件事：

1. **唯一索引真的建出来了**——模型声明 `unique=True` 的 spd 索引，迁移必须建成唯一。
   这是把"模型说了算"换成"数据库说了算"：应用层的 check-then-act 在并发下必然漏，
   DB 约束是最后一道，而这 18 处此前**一处都没有**（实测可插出同一村医的两个积分账户，
   余额 10 / 99，`award_points` 与兑换核销各取 `.first()`，账面对不上还不报错）。
2. **去重迁移不静默删数据**——被移除的行整行 JSON 进 `spd_dedup_reports`，
   积分账户按余额相加归并。删掉的可能是实施期现场调过的配置，必须可还原。

第 1 条在 SQLite 上就能验（反射得出唯一性），第 2 条要真跑迁移，
放在 `test_postgres_real.py` 的 PG 档里跑真数据（本文件只验模型侧的一致性与留痕表形状）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

from conftest import reset_database

from app.database import Base, engine
import app.spd.models as spd_models  # noqa: F401 - 导入即注册


#: 迁移 e1a2b3c4d5e9 修掉的 18 处。写死在用例里而不是从模型现算——
#: 从模型现算就变成"模型说啥就是啥"，那正是出问题的那条路径。
EXPECTED_UNIQUE = {
    ("spd_assess_plans", "ix_spd_assess_plans_code"),
    ("spd_case_report_tasks", "ix_spd_case_report_tasks_code"),
    ("spd_centers", "ix_spd_centers_code"),
    ("spd_data_sources", "ix_spd_data_sources_code"),
    ("spd_devices", "ix_spd_devices_sn"),
    ("spd_edu_materials", "ix_spd_edu_materials_code"),
    ("spd_followup_rules", "ix_spd_followup_rules_code"),
    ("spd_goods", "ix_spd_goods_code"),
    ("spd_intervention_templates", "ix_spd_intervention_templates_code"),
    ("spd_point_accounts", "ix_spd_point_accounts_user_id"),
    ("spd_point_rules", "ix_spd_point_rules_code"),
    ("spd_programs", "ix_spd_programs_code"),
    ("spd_questionnaires", "ix_spd_questionnaires_code"),
    ("spd_referral_rules", "ix_spd_referral_rules_code"),
    ("spd_report_templates", "ix_spd_report_templates_code"),
    ("spd_service_packages", "ix_spd_service_packages_code"),
    ("spd_tags", "ix_spd_tags_code"),
    ("spd_village_doctors", "ix_spd_village_doctors_user_id"),
}


@pytest.fixture(scope="module")
def client():
    reset_database()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_headers(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_十八处唯一索引在模型上都还是唯一的(client):
    """任何一处被改回非唯一，这条就红——那正是当初出问题的形态。"""
    downgraded = []
    for table_name, index_name in sorted(EXPECTED_UNIQUE):
        table = Base.metadata.tables[table_name]
        index = next((i for i in table.indexes if i.name == index_name), None)
        if index is None:
            downgraded.append(f"{table_name}.{index_name} 索引不见了")
        elif not index.unique:
            downgraded.append(f"{table_name}.{index_name} 不再是唯一索引")
    assert not downgraded, "唯一性被削弱：\n  " + "\n  ".join(downgraded)


def test_建出来的库上唯一性真的成立(client):
    """看**数据库**怎么说，不看模型怎么写——这两者对不上正是 P0-1 的成因。"""
    inspector = inspect(engine)
    missing = []
    for table_name, index_name in sorted(EXPECTED_UNIQUE):
        indexes = {i["name"]: i for i in inspector.get_indexes(table_name)}
        info = indexes.get(index_name)
        if info is None or not info.get("unique"):
            missing.append(f"{table_name}.{index_name}")
    assert not missing, (
        "这些索引在库里不是唯一的（模型写着 unique=True）：\n  " + "\n  ".join(missing)
        + "\n接口层的 insert_if_absent / IntegrityError 分支会因此永远走不到——"
        "防御写了却不生效，并发下照样插出重复行。"
    )


def test_去重留痕表存得下整行(client):
    """`removed_row` 必须是 JSON 列：留痕的价值在于能还原被删那一行的全部字段。"""
    table = Base.metadata.tables["spd_dedup_reports"]
    assert "removed_row" in table.columns
    assert table.columns["removed_row"].type.__class__.__name__.upper().startswith("JSON")
    for column in ("table_name", "key_value", "kept_id", "removed_id", "strategy", "created_at"):
        assert column in table.columns, f"留痕表缺 {column}，事后没法定位是哪条被并掉的"


def test_唯一冲突在接口上表现为409而不是500(client, admin_headers):
    """DB 约束成立之后，第二次建同编码必须是 409（业务冲突），不是 500（内部错误）。

    这条正是 P0-1 修好之前**永远不会触发**的分支：没有 DB 约束，
    `insert_if_absent` 的冲突路径根本走不到，接口会当成"新建成功"。
    """
    body = {"code": "uniq_probe", "name": "唯一性探针病种", "category": "chronic"}
    first = client.post("/api/spd/programs", json=body, headers=admin_headers)
    assert first.status_code == 201, first.text
    second = client.post("/api/spd/programs", json=body, headers=admin_headers)
    assert second.status_code == 409, f"重复编码应 409，实际 {second.status_code}：{second.text}"
