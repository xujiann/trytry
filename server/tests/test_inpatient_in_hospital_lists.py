"""在院患者清单按「在院」取数，不在「最新 200 条住院」里挑（P2-154）。

住院临床文书的患者选择框、医生移动端查房、住院管理页的「住院记录」三处原先都不带条件取住院列表——拿到的是
**最新 200 条**（出院的在院的都算），再在页面上挑在院的。住得久的患者被新入院的挤出前 200 条，就从三处同时消失：
病程、护理、体温单写不了，查房选不到，住院管理页上连「出院」按钮都没有；最新 200 条碰巧都出院了，页面直说
「暂无在院患者」。接口本来就收 `status`，页面没用。
"""
import os
import re

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import Admission, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2155 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2155 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2155-1"}).json()["id"]
    long_stay, others = (client.post("/api/patients", headers=admin, json={
        "name": f"P2155 患者{i}", "id_card": f"33010619550505{i:04d}", "gender": "女"}).json()["id"] for i in (1, 2))
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": long_stay, "ward_id": ward, "bed_id": bed, "diagnosis_name": "脑梗死恢复期"})
    assert admitted.status_code == 201, admitted.text
    with SessionLocal() as db:   # 之后又办了 200 次住院、都已出院
        operator = db.query(User).filter(User.username == "admin").one().id
        db.add_all([Admission(patient_id=others, org_id=org, ward_id=ward, bed_id=bed, status="discharged",
                              diagnosis_name="阑尾炎", created_by=operator) for _ in range(200)])
        db.commit()
    return {"long_stay": admitted.json()["id"]}


def test_不带条件只见最新200条_按在院取才见得到住得久的(client, admin, world):
    latest = client.get("/api/inpatient/admissions", headers=admin).json()
    assert world["long_stay"] not in [a["id"] for a in latest]   # 页面原先的取法：他不在
    in_hospital = client.get("/api/inpatient/admissions?status=admitted&limit=500", headers=admin).json()
    assert [a["id"] for a in in_hospital] == [world["long_stay"]]


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_三处在院清单都按在院取数():
    for parts in (("pages-mgmt.js",), ("pages-clinical.js",), ("m", "doctor.js")):
        source = _read(*parts)
        assert "/api/inpatient/admissions?status=admitted&limit=500" in source, "/".join(parts)
        # 「取最新 N 条再在页面上挑在院」这个形状不许回来
        assert not re.search(r'api\("/api/inpatient/admissions"\)\)?\s*\.filter\(\(a\) => a\.status === "admitted"\)',
                             source), "/".join(parts)
        assert not re.search(r'api\("/api/inpatient/admissions"\);\s*\n\s*const \w+ = \w+\.filter\(\(a\) => a\.status === "admitted"\)',
                             source), "/".join(parts)
