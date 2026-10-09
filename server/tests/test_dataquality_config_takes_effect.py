"""数据质控规则配置收下却不起作用或标错表：skip_empty 只有引用校验认；两个逻辑校验不看被检表却按被检表标表名；区间遇到
文字列上的空串判法不对（P2-1569，第四十六批扫描 AJ2-6）。

修前（scan46 aj2 `r4_dataquality.py` T4 / T5 / T7 实测）：
- 配置校验对所有类型都放行 `skip_empty`，实际只有引用校验读它：枚举加 `skip_empty: true`，性别为空串的档案照报违规；
- `critical_closed_loop` / `chronic_followup_indicator` 只扫自己那张表（危急值报告 / 慢病随访），明细却按 `rule.target_table`
  标表名：前者配到 patients 上 201，明细显示「patients · 记录 1」，其实 1 是危急值报告号，库里恰好也有 1 号患者；
- 区间只把 None 当缺失：`birth_date` 只设下限时空串被报成「birth_date= 低于下限 1900-01-01」，只设上限时空串一条都不报——
  `qc_rules_seed.py` 写的是「数值越界或缺失即违规」，P2-234 定过「空串也是没填」。

修后：枚举、区间也认 skip_empty（None 与空串都跳过，与引用同一语义；内置规则里带它的只有两条引用校验，行为不变）；那两个
逻辑校验的被检表必须是它实际扫的表，否则建 / 改 422，存量的扫描时跳过并点名（P2-81 / P2-1123 的既有处理）；区间把空串与
None 一样按「缺失，无法判定区间」报。
"""
import pytest

from app.database import SessionLocal
from app.models import Patient, QcRule

Q = "/api/dataquality"
MISSING = "birth_date 缺失，无法判定区间"


@pytest.fixture(scope="module")
def blank(client):
    """一份性别、出生日期都是空串的档案（入口拦得住，存量与入站进得来）。"""
    with SessionLocal() as db:
        row = Patient(ehc_no="P21569-BLANK", name="P21569 空项", id_card="110101197001011238", gender="", birth_date="")
        db.add(row)
        db.commit()
        return row.id


def _rule(client, admin, code, table, rule_type, config):
    made = client.post(f"{Q}/rules", headers=admin, json={
        "code": code, "name": f"{code} 规则", "target_table": table, "rule_type": rule_type, "config": config,
        "severity": "warn"})
    return made


def _messages(client, admin, code, record_id) -> list[str]:
    got = client.get(f"{Q}/run", headers=admin, params={"rule_code": code, "limit": 1000})
    assert got.status_code == 200 and got.json()["skipped_rules"] == [], got.text[:300]
    return [v["message"] for v in got.json()["items"] if v["record_id"] == record_id]


def test_枚举与区间带skip_empty_空串不再报_不带的照报(client, admin, blank):
    for code, rule_type, config in (
            ("P21569_ENUM", "enum", {"field": "gender", "values": ["男", "女", "未知"]}),
            ("P21569_RANGE", "range", {"field": "birth_date", "min": "1900-01-01"})):
        for suffix, extra in (("_ALL", {}), ("_SKIP", {"skip_empty": True})):
            made = _rule(client, admin, code + suffix, "patients", rule_type, {**config, **extra})
            assert made.status_code == 201, made.text
        assert len(_messages(client, admin, code + "_ALL", blank)) == 1   # 对照组：空串照报
        assert _messages(client, admin, code + "_SKIP", blank) == []      # 修前枚举照报「gender= 不在允许取值 …」


@pytest.mark.parametrize(("check", "own"), [
    ("critical_closed_loop", "exam_reports"),
    ("chronic_followup_indicator", "followups"),
], ids=["危急值闭环", "慢病随访指标"])
def test_不看被检表的逻辑校验_被检表写错_建与改都422(client, admin, check, own):
    problem = f"逻辑校验 {check} 扫的是 {own}，被检表要写 {own}"
    made = _rule(client, admin, f"P21569_{own}", "patients", "logic", {"check": check})
    assert made.status_code == 422, made.text   # 修前 201，明细把别的表的记录号标成 patients
    assert made.json() == {"detail": f"规则配置非法：{problem}"}
    other = _rule(client, admin, f"P21569_EDIT_{own}", "patients", "logic", {"check": "date_not_future", "field": "birth_date"})
    assert other.status_code == 201, other.text
    got = client.patch(f"{Q}/rules/{other.json()['id']}", headers=admin, json={"config": {"check": check}})
    assert got.status_code == 422 and got.json() == {"detail": f"规则配置非法：{problem}"}, got.text   # 修前 200
    assert client.delete(f"{Q}/rules/{other.json()['id']}", headers=admin).status_code == 200
    assert _rule(client, admin, f"P21569_OK_{own}", own, "logic", {"check": check}).status_code == 201   # 写对照收


def test_存量里配错表的_扫描时跳过并点名_其余照扫(client, admin):
    with SessionLocal() as db:
        stock = QcRule(code="P21569_OLD", name="P21569 存量", target_table="patients", rule_type="logic",
                       config={"check": "critical_closed_loop"}, severity="warn", active=True)
        db.add(stock)
        db.commit()
        stock_id = stock.id
    try:
        expected = [{"rule_code": "P21569_OLD", "rule_name": "P21569 存量",
                     "problem": "逻辑校验 critical_closed_loop 扫的是 exam_reports，被检表要写 exam_reports"}]
        summary = client.get(f"{Q}/summary", headers=admin)
        assert summary.status_code == 200, summary.text[:300]
        assert summary.json()["skipped_rules"] == expected and summary.json()["rules_checked"] >= 15
        run = client.get(f"{Q}/run", headers=admin)
        assert run.status_code == 200 and run.json()["skipped_rules"] == expected, run.text[:300]
    finally:
        with SessionLocal() as db:
            db.query(QcRule).filter(QcRule.id == stock_id).delete()
            db.commit()


@pytest.mark.parametrize("bounds", [{"min": "1900-01-01"}, {"max": "2026-12-31"}], ids=["只设下限", "只设上限"])
def test_区间遇到文字列上的空串_两种设法都报缺失(client, admin, blank, bounds):
    code = f"P21569_BIRTH_{'MIN' if 'min' in bounds else 'MAX'}"
    made = _rule(client, admin, code, "patients", "range", {"field": "birth_date", **bounds})
    assert made.status_code == 201, made.text
    # 修前：只设下限报「birth_date= 低于下限 1900-01-01」，只设上限一条不报
    assert _messages(client, admin, code, blank) == [MISSING]
