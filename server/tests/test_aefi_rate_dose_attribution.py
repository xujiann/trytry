"""AEFI 十万剂次发生率：分子按上报机构和发病日期归，分母按接种机构和接种日期归（P2-942，第二十六批「比率与分母」
扫描 H3-5）。

`vaccination_stats` 的分母是同期、同范围的接种剂次（`VaccinationRecord.org_id` / `vaccinated_date`），分子却按 AEFI 报告
的上报机构与发病日期归；而 AEFI 可以关联到具体接种记录（模型注释：「查得出是哪一针引起的」）。东镇 8-31 接种一针，
受种者 9-01 到县医院急诊，县医院关联这一针上报严重 AEFI：东片区 8 月、9 月发生率都是 0，县直 9 月「AEFI 1 / 剂次 0」
（页面「无接种」），全县 8 月 0。

修法：关联了剂次的 AEFI 按那一针的接种机构、接种日期计入分子，与分母同一个口径；没关联剂次的照旧。
"""
import pytest

from app.database import SessionLocal
from app.models import OrgGroup, OrgGroupMember

B = "/api/vaccine-supply"


@pytest.fixture(scope="module")
def world(client, admin):
    east = client.post("/api/organizations", headers=admin, json={
        "name": "P2942 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P2942 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    groups = {}
    with SessionLocal() as db:
        for key, org in (("east", east), ("county", county)):
            group = OrgGroup(name=f"P2942 {key}片区")
            db.add(group)
            db.flush()
            db.add(OrgGroupMember(group_id=group.id, org_id=org))
            groups[key] = group.id
        db.commit()
    doses = {}
    for key, id_card, day in (("aug", "330106202501010094", "2026-08-31"), ("sep", "330106202502020016", "2026-09-05")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2942 受种者{key}", "id_card": id_card}).json()["id"]
        record = client.post("/api/vaccination/records", headers=admin, json={
            "patient_id": patient, "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗", "dose_no": 1,
            "vaccinated_date": day, "org_id": east})
        assert record.status_code == 201, record.text
        doses[key] = (patient, record.json()["id"])
    patient, record = doses["aug"]   # 8-31 那一针的受种者 9-01 到县医院急诊，县医院关联这一针上报
    reported = client.post(f"{B}/aefi", headers=admin, json={
        "patient_id": patient, "record_id": record, "reaction_type": "severe", "symptom": "过敏性休克",
        "onset_date": "2026-09-01", "org_id": county})
    assert reported.status_code == 201, reported.text
    return {"groups": groups}


def _aefi(client, admin, start, end, group_id=None):
    params = {"start_date": start, "end_date": end}
    if group_id is not None:
        params["group_id"] = group_id
    stats = client.get(f"{B}/stats", headers=admin, params=params).json()
    return stats["doses"], stats["aefi"]["total"], stats["aefi"]["rate_per_100k_doses"]


def test_关联剂次的AEFI按那一针的接种机构和日期归(client, admin, world):
    east = world["groups"]["east"]
    assert _aefi(client, admin, "2026-08-01", "2026-08-31", east) == (1, 1, 100000.0)   # 修前 (1, 0, 0.0)
    assert _aefi(client, admin, "2026-09-01", "2026-09-30", east) == (1, 0, 0.0)
    county = world["groups"]["county"]
    assert _aefi(client, admin, "2026-09-01", "2026-09-30", county) == (0, 0, None)     # 修前 (0, 1, None)
