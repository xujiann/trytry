"""逻辑类数据质控规则收下 filter 却不用：QC001 加「只查未注销档案」返回 200，已注销的档案照报 error（P2-1567，第四十六批
扫描 AJ2-4；P2-81 / P2-1123 的余项）。

修前（scan46 aj2 `r4_dataquality.py` T3 实测）：`rule_config_problem` 对所有规则类型都校验并放行 filter，`_filtered` 却只被
必填 / 区间 / 枚举 / 引用四类调用——PATCH QC001 加 `{"filter": {"deactivated_at": null}}` 返回 200，扫描仍报已注销的那份
档案；QC003（必填）加同一个 filter 就生效了。P2-81 已把「filter 写错被忽略、扫了全表」当缺陷修过。

修后：查询就落在被检表上的三个逻辑校验（`id_card_checksum`、`date_not_future`、`datetime_order`）改走 `_filtered`，filter
生效；自己定了扫哪张表的两个（`critical_closed_loop`、`chronic_followup_indicator`）带 filter 建 / 改规则都 422「不支持
filter」；修前已经这样存着的，扫描时跳过并在 `skipped_rules` 点名（P2-81 / P2-1123 的既有处理），其余规则照常扫。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Patient, QcRule

Q = "/api/dataquality"
ALIVE = {"deactivated_at": None}
NO_FILTER = {
    "critical_closed_loop": "逻辑校验 critical_closed_loop 不支持 filter（它按自己的条件扫 exam_reports，filter 写了也不起作用）",
    "chronic_followup_indicator": ("逻辑校验 chronic_followup_indicator 不支持 filter（它按自己的条件扫 followups，"
                                   "filter 写了也不起作用）"),
}


@pytest.fixture(scope="module")
def patients(client):
    """两份校验位都错、出生日期都写在将来的档案，其中一份已注销：三个逻辑校验不带 filter 都报两份。"""
    with SessionLocal() as db:
        live = Patient(ehc_no="P21567-LIVE", name="P21567 在册", id_card="110101197001011230", birth_date="2099-01-01")
        gone = Patient(ehc_no="P21567-GONE", name="P21567 注销", id_card="110101197001011231", birth_date="2099-01-01",
                       deactivated_at=datetime(2026, 9, 1))
        db.add_all([live, gone])
        db.commit()
        return {"live": live.id, "gone": gone.id}


def _hit_ids(client, admin, code, patients) -> set[int]:
    got = client.get(f"{Q}/run", headers=admin, params={"rule_code": code, "limit": 1000})
    assert got.status_code == 200, got.text
    assert got.json()["skipped_rules"] == [], got.json()["skipped_rules"]
    return {v["record_id"] for v in got.json()["items"]} & set(patients.values())


def _seed_rule(client, admin, code):
    return next(r for r in client.get(f"{Q}/rules", headers=admin).json() if r["code"] == code)


def test_QC001带未注销filter_已注销档案不再报(client, admin, patients):
    qc001 = _seed_rule(client, admin, "QC001")
    original = qc001["config"]
    assert _hit_ids(client, admin, "QC001", patients) == {patients["live"], patients["gone"]}
    try:
        got = client.patch(f"{Q}/rules/{qc001['id']}", headers=admin, json={"config": {**original, "filter": ALIVE}})
        assert got.status_code == 200, got.text
        assert _hit_ids(client, admin, "QC001", patients) == {patients["live"]}   # 修前已注销的那份照报
    finally:
        client.patch(f"{Q}/rules/{qc001['id']}", headers=admin, json={"config": original})


@pytest.mark.parametrize(("code", "config"), [
    ("P21567_FUTURE", {"check": "date_not_future", "field": "birth_date"}),
    ("P21567_ORDER", {"check": "datetime_order", "start_field": "birth_date", "end_field": "created_at"}),
], ids=["日期不晚于今天", "起止先后"])
def test_另两个扫被检表的逻辑校验_filter同样生效(client, admin, patients, code, config):
    """不带 filter 的对照组两份都报；带「未注销」filter 的只报在册那份（修前两份都报）。"""
    for suffix, extra in (("_ALL", {}), ("_ALIVE", {"filter": ALIVE})):
        made = client.post(f"{Q}/rules", headers=admin, json={
            "code": code + suffix, "name": f"{code} 规则", "target_table": "patients", "rule_type": "logic",
            "config": {**config, **extra}, "severity": "warn"})
        assert made.status_code == 201, made.text
    assert _hit_ids(client, admin, code + "_ALL", patients) == {patients["live"], patients["gone"]}
    assert _hit_ids(client, admin, code + "_ALIVE", patients) == {patients["live"]}


@pytest.mark.parametrize(("check", "table", "seed"), [
    ("critical_closed_loop", "exam_reports", "QC007"),
    ("chronic_followup_indicator", "followups", "QC013"),
], ids=["危急值闭环", "慢病随访指标"])
def test_自己定了扫哪张表的逻辑校验带filter_建与改都422(client, admin, check, table, seed):
    made = client.post(f"{Q}/rules", headers=admin, json={
        "code": f"P21567_{seed}", "name": "P21567 带 filter", "target_table": table, "rule_type": "logic",
        "config": {"check": check, "filter": {"id": 1}}})
    assert made.status_code == 422, made.text   # 修前 201
    assert made.json() == {"detail": f"规则配置非法：{NO_FILTER[check]}"}
    rule = _seed_rule(client, admin, seed)
    got = client.patch(f"{Q}/rules/{rule['id']}", headers=admin, json={"config": {**rule["config"], "filter": {"id": 1}}})
    assert got.status_code == 422, got.text   # 修前 200
    assert got.json() == {"detail": f"规则配置非法：{NO_FILTER[check]}"}
    # 空的 {} 等于没写，照收；内置规则的配置一个字没动
    got = client.patch(f"{Q}/rules/{rule['id']}", headers=admin, json={"config": {**rule["config"], "filter": {}}})
    assert got.status_code == 200, got.text
    assert client.patch(f"{Q}/rules/{rule['id']}", headers=admin, json={"config": rule["config"]}).status_code == 200


def test_存量里带filter的这两种_扫描时跳过并点名_其余照扫(client, admin):
    with SessionLocal() as db:
        stock = QcRule(code="P21567_OLD", name="P21567 存量", target_table="exam_reports", rule_type="logic",
                       config={"check": "critical_closed_loop", "filter": {"critical": True}}, severity="warn",
                       active=True)
        db.add(stock)
        db.commit()
        stock_id = stock.id
    try:
        expected = [{"rule_code": "P21567_OLD", "rule_name": "P21567 存量", "problem": NO_FILTER["critical_closed_loop"]}]
        summary = client.get(f"{Q}/summary", headers=admin)
        assert summary.status_code == 200, summary.text[:300]
        assert summary.json()["skipped_rules"] == expected   # 修前照扫，filter 悄悄不起作用
        assert summary.json()["rules_checked"] >= 15
        run = client.get(f"{Q}/run", headers=admin)
        assert run.status_code == 200 and run.json()["skipped_rules"] == expected, run.text[:300]
    finally:
        with SessionLocal() as db:
            db.query(QcRule).filter(QcRule.id == stock_id).delete()
            db.commit()
