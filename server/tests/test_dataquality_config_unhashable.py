"""数据质控规则的逻辑校验名、引用表名写成列表 / 对象：建规则 500，存量里有一条这样的就让汇总与运行检查整体 500（P2-1582，
第四十六批 AJ2 修复时顺带查出，P2-1123 同一类）。

修前：`rule_config_problem` 拿 `config["check"]` 直接做 `in _LOGIC_CHECKS`、拿 `config["ref_table"]` 直接查表名字典，值是列表 /
对象时抛 TypeError——建 / 改规则回 500；扫描前 `_usable_rules` 逐条过这道校验，存量里一条这样的规则就让 `/summary` 与
`/run` 整体 500，质控页打不开，页上的停用按钮也就点不到。同函数里字典编码（`ref_code_system`）、字段名都早已只收文字。

修后：两处都只收文字，写成别的建 / 改规则 422；存量照 P2-81 / P2-1123 扫描时跳过并在 `skipped_rules` 点名，其余照扫。
"""
import pytest

from app.database import SessionLocal
from app.models import QcRule

Q = "/api/dataquality"

BAD = [
    pytest.param("patients", "logic", {"check": ["id_card_checksum"]}, id="逻辑校验名写成列表"),
    pytest.param("patients", "logic", {"check": {"name": "id_card_checksum"}}, id="逻辑校验名写成对象"),
    pytest.param("patients", "cross_ref", {"field": "gender", "ref_table": ["patients"]}, id="引用表名写成列表"),
]


@pytest.mark.parametrize("table,kind,config", BAD)
def test_建规则_校验名或引用表名不是文字_422(client, admin, table, kind, config):
    got = client.post(f"{Q}/rules", headers=admin, json={
        "code": "P21582_NEW", "name": "P21582 新建", "target_table": table, "rule_type": kind,
        "config": config, "severity": "warn"})
    assert got.status_code == 422, got.text   # 修前 500


def test_改规则_改成不是文字的校验名_422_原值不变(client, admin):
    created = client.post(f"{Q}/rules", headers=admin, json={
        "code": "P21582_EDIT", "name": "P21582 改", "target_table": "patients", "rule_type": "logic",
        "config": {"check": "id_card_checksum"}, "severity": "warn"})
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]
    try:
        got = client.patch(f"{Q}/rules/{rule_id}", headers=admin, json={"config": {"check": ["id_card_checksum"]}})
        assert got.status_code == 422, got.text   # 修前 500
        with SessionLocal() as db:
            assert db.get(QcRule, rule_id).config == {"check": "id_card_checksum"}
    finally:
        client.delete(f"{Q}/rules/{rule_id}", headers=admin)


@pytest.mark.parametrize("table,kind,config", BAD)
def test_存量里这样存着的_汇总与运行检查跳过并点名_其余照扫(client, admin, table, kind, config):
    with SessionLocal() as db:
        stock = QcRule(code="P21582_OLD", name="P21582 存量", target_table=table, rule_type=kind, config=config,
                       severity="warn", active=True)
        db.add(stock)
        db.commit()
        stock_id = stock.id
    try:
        summary = client.get(f"{Q}/summary", headers=admin)
        assert summary.status_code == 200, summary.text[:300]   # 修前整体 500
        assert [s["rule_code"] for s in summary.json()["skipped_rules"]] == ["P21582_OLD"]
        assert summary.json()["rules_checked"] >= 15   # 内置规则照扫
        run = client.get(f"{Q}/run", headers=admin)
        assert run.status_code == 200, run.text[:300]
        assert [s["rule_code"] for s in run.json()["skipped_rules"]] == ["P21582_OLD"]
    finally:
        with SessionLocal() as db:
            db.query(QcRule).filter(QcRule.id == stock_id).delete()
            db.commit()
