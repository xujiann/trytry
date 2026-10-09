"""数据质控规则配置校验放过、扫描时整表误报的两种写法：引用的字典编码写错；枚举取值类型与列类型不符（P2-1568，第四十六批
扫描 AJ2-5）。

修前（scan46 aj2 `r4_dataquality.py` T2 / T6 实测）：
- `ref_code_system` 只查是不是字符串——写成 `icd10`（统一编码字典里叫 diagnosis）建规则 201，扫描时字典查不到、合法集合为空，
  I10、E11 这些字典里明明有的诊断全判「不存在于 icd10 字典」error；引用表写错早就 422。
- 枚举不按列类型查——整数列 `chronic_patients.level` 写成 `["1", "2", "3"]` 建规则 201，扫描时 1 不在 {"1", "2", "3"} 里，
  3 份档案全判违规；区间同样的写法早就 422。

修后：`ref_code_system` 必须是 `dictionaries.SYSTEM_CODES` 之一；枚举取值照区间那条按列类型查（文字列收文字、数值列收数、
是否列收 true / false，时间与对象列不能按枚举判定；null 表示空值也算合规，不比类型）。建、改规则同一判据 422；修前已经存着的，
扫描时跳过并在 `skipped_rules` 点名（P2-81 / P2-1123 的既有处理）。
"""
import pytest

from app.database import SessionLocal
from app.models import QcRule

Q = "/api/dataquality"
DICT_PROBLEM = "字典 icd10 未登记（可选：charge、consumable、diagnosis、drug）"
LEVEL_PROBLEM = "level 是数值列，枚举的取值必须是数"


def _create(client, admin, code, table, rule_type, config):
    """建成停用的：修法撤掉时建得进去的坏规则不参与扫描，不连累别的用例。"""
    return client.post(f"{Q}/rules", headers=admin, json={
        "code": code, "name": f"{code} 规则", "target_table": table, "rule_type": rule_type, "config": config,
        "severity": "warn", "active": False})


def _seed_rule(client, admin, code):
    return next(r for r in client.get(f"{Q}/rules", headers=admin).json() if r["code"] == code)


def test_引用字典编码写错_建与改都422_写对照收(client, admin):
    bad = {"field": "diagnosis_code", "ref_code_system": "icd10", "skip_empty": True}
    got = _create(client, admin, "P21568_ICD", "encounters", "cross_ref", bad)
    assert got.status_code == 422, got.text   # 修前 201，扫描把字典里有的诊断全判 error
    assert got.json() == {"detail": f"规则配置非法：{DICT_PROBLEM}"}
    qc005 = _seed_rule(client, admin, "QC005")
    got = client.patch(f"{Q}/rules/{qc005['id']}", headers=admin, json={"config": bad})
    assert got.status_code == 422 and got.json() == {"detail": f"规则配置非法：{DICT_PROBLEM}"}, got.text   # 修前 200
    good = _create(client, admin, "P21568_DIAG", "encounters", "cross_ref", {**bad, "ref_code_system": "diagnosis"})
    assert good.status_code == 201, good.text
    assert client.patch(f"{Q}/rules/{qc005['id']}", headers=admin, json={"config": qc005["config"]}).status_code == 200


@pytest.mark.parametrize(("table", "config", "problem"), [
    ("chronic_patients", {"field": "level", "values": ["1", "2", "3"]}, LEVEL_PROBLEM),
    ("chronic_patients", {"field": "level", "values": [1, True]}, LEVEL_PROBLEM),
    ("patients", {"field": "gender", "values": ["男", 1]}, "gender 是文字列，枚举的取值也要写成文字"),
    ("exam_reports", {"field": "critical", "values": ["true"]}, "critical 只有是 / 否两种取值，枚举的取值只能写 true / false"),
    ("patients", {"field": "created_at", "values": ["2026-01-01"]}, "created_at 不是文字、数值或是否列，不能按枚举判定"),
], ids=["整数列写成文字", "整数列混进布尔", "文字列混进数", "是否列写成文字", "时间列"])
def test_枚举取值与列类型不符_建规则422(client, admin, table, config, problem):
    got = _create(client, admin, "P21568_ENUM_BAD", table, "enum", config)
    assert got.status_code == 422, got.text   # 修前 201，扫描整表判违规
    assert got.json() == {"detail": f"规则配置非法：{problem}"}


def test_枚举取值按列类型写对照收_改成不符的422(client, admin):
    from app.models import ChronicPatient, Patient

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21568 卫生院", "org_type": "township", "level": "township"})
    assert org.status_code in (200, 201), org.text
    with SessionLocal() as db:
        patient = Patient(ehc_no="P21568-P", name="P21568 慢病", id_card="110101197001011238")
        db.add(patient)
        db.flush()
        rows = [ChronicPatient(patient_id=patient.id, disease=d, level=lvl, managed_by_org_id=org.json()["id"])
                for d, lvl in (("hypertension", 1), ("diabetes", 3))]
        db.add_all(rows)
        db.commit()
        ids = {r.id for r in rows}
    made = client.post(f"{Q}/rules", headers=admin, json={
        "code": "P21568_LEVEL", "name": "慢病分级须为1/2/3", "target_table": "chronic_patients", "rule_type": "enum",
        "config": {"field": "level", "values": [1, 2, 3]}, "severity": "warn"})
    assert made.status_code == 201, made.text
    run = client.get(f"{Q}/run", headers=admin, params={"rule_code": "P21568_LEVEL"}).json()
    assert {v["record_id"] for v in run["items"]} & ids == set()   # 写对了，合规的档案不误报
    got = client.patch(f"{Q}/rules/{made.json()['id']}", headers=admin,
                       json={"config": {"field": "level", "values": ["1", "2", "3"]}})
    assert got.status_code == 422 and got.json() == {"detail": f"规则配置非法：{LEVEL_PROBLEM}"}, got.text   # 修前 200
    for table, config in (("patients", {"field": "gender", "values": ["男", "女", None]}),   # null：空值也算合规
                          ("exam_reports", {"field": "critical", "values": [True, False]}),
                          ("followups", {"field": "sbp", "values": [120, 130.5]})):
        assert _create(client, admin, f"P21568_OK_{table}", table, "enum", config).status_code == 201


def test_存量里的这两种_扫描时跳过并点名_其余照扫(client, admin):
    with SessionLocal() as db:
        rows = [QcRule(code=code, name=f"{code} 规则", target_table=table, rule_type=rule_type, config=config,
                       severity="warn", active=True)
                for code, table, rule_type, config in (
                    ("P21568_OLD_ICD", "encounters", "cross_ref", {"field": "diagnosis_code", "ref_code_system": "icd10"}),
                    ("P21568_OLD_LVL", "chronic_patients", "enum", {"field": "level", "values": ["1", "2", "3"]}))]
        db.add_all(rows)
        db.commit()
        ids = [r.id for r in rows]
    try:
        expected = [{"rule_code": "P21568_OLD_ICD", "rule_name": "P21568_OLD_ICD 规则", "problem": DICT_PROBLEM},
                    {"rule_code": "P21568_OLD_LVL", "rule_name": "P21568_OLD_LVL 规则", "problem": LEVEL_PROBLEM}]
        summary = client.get(f"{Q}/summary", headers=admin)
        assert summary.status_code == 200, summary.text[:300]
        assert summary.json()["skipped_rules"] == expected   # 修前照扫，整表误报
        assert summary.json()["rules_checked"] >= 15
        run = client.get(f"{Q}/run", headers=admin)
        assert run.status_code == 200 and run.json()["skipped_rules"] == expected, run.text[:300]
    finally:
        with SessionLocal() as db:
            db.query(QcRule).filter(QcRule.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
