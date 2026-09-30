"""随访方案的诊断关键词、病历质控的要点关键词认得出大小写、全角、夹空格的写法（P2-917，第二十五批「搜索与模糊匹配」扫描 J4-2）。

自动匹配（门诊场景）与出院即派生都按原样 `k in text` 找关键词：方案关键词 `I10` / `COPD`，诊断编码写成 `i10`、`Ｉ１０`，
诊断名写成 `copd急性加重`、`ＣＯＰＤ急性加重`、`C OPD急性加重` 就不派随访，也没有任何提示（自动匹配 `scanned 7 matched 2`）。
DRG 与审方禁忌早就两侧都过 `text_key`（P2-792）。修后三处（含病历质控「要点关键词」）共用 `texttypes.has_keyword`：两侧
归一再比，关键词归一后为空的不算命中。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.routers.quality import _check_record_rule
from app.spd.models import SpdFollowupRecord
from app.spd.subscribers import on_admission_discharged
from app.texttypes import has_keyword

_CARDS = itertools.count(1)


def _patient(client, admin, name):
    made = client.post("/api/patients", headers=admin, json={
        "name": name, "id_card": f"33010219700101{next(_CARDS):03d}X"})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


def _records(patient):
    with SessionLocal() as db:
        return db.query(SpdFollowupRecord).filter(SpdFollowupRecord.patient_id == patient).count()


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2917 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def test_门诊自动匹配_各种写法都派随访(client, admin, org):
    rule = client.post("/api/spd/followup-rules", headers=admin, json={
        "code": "P2917_OUT", "name": "P2917 门诊随访", "scene": "outpatient",
        "diagnosis_keywords": ["I10", "COPD"], "points": [7]})
    assert rule.status_code == 201, rule.text
    cases = [("I10", "原发性高血压"), ("i10", "原发性高血压"), ("Ｉ１０", "原发性高血压"),
             ("", "COPD急性加重"), ("", "copd急性加重"), ("", "ＣＯＰＤ急性加重"), ("", "C OPD急性加重")]
    patients = []
    for n, (code, name) in enumerate(cases):
        patient = _patient(client, admin, f"P2917 门诊{n}")
        made = client.post("/api/encounters", headers=admin, json={
            "patient_id": patient, "org_id": org, "diagnosis_code": code, "diagnosis_name": name})
        assert made.status_code == 201, made.text
        patients.append(patient)
    got = client.post("/api/spd/followup-plans/auto-match", headers=admin,
                      json={"scene": "outpatient", "org_id": org, "days": 7})
    assert got.status_code == 200, got.text
    assert [_records(p) for p in patients] == [1] * len(cases)   # 修前只有原样写法的两位排出随访


def test_出院即派生_小写与全角都派(client, admin, org):
    rule = client.post("/api/spd/followup-rules", headers=admin, json={
        "code": "P2917_IN", "name": "P2917 慢阻肺出院随访", "scene": "inpatient",
        "diagnosis_keywords": ["COPD"], "points": [7, 30]})
    assert rule.status_code == 201, rule.text
    for n, name in enumerate(("copd急性加重", "ＣＯＰＤ急性加重")):
        patient = _patient(client, admin, f"P2917 出院{n}")
        with SessionLocal() as db:
            on_admission_discharged(db, {"patient_id": patient, "org_id": org, "diagnosis_name": name,
                                         "discharged_on": "2026-09-29"})
            db.commit()
        assert _records(patient) == 2, name   # 修前 0


def test_病历质控要点关键词同一句_空关键词不算():
    class Rule:
        rule = "keyword_present"
        config = {"keywords": ["ＣＯＰＤ", ""]}

    assert _check_record_rule(Rule, "诊断：copd 急性加重") == ""        # 修前「未体现要点」
    assert _check_record_rule(Rule, "诊断：肺炎") != ""                   # 修前空关键词让任何文字都过
    assert has_keyword("原发性高血压 I10", ["i10"]) and not has_keyword("肺炎", ["", "   "])
