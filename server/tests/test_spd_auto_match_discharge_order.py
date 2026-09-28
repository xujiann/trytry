"""「按患者特征自动匹配」的出院场景按出院时间取最近的 N 条，不按住院编号（P2-692，第十七批「最近 / 最新」扫描 U4-7）。

docstring 写着「出院场景按出院时间取近 N 天」，窗口按出院时间圈了，截取却是 `order_by(Admission.id.desc()).limit(N)`：
住院编号是入院时给的，住了 40 天、昨天才出院的脑卒中患者编号最小，窗口里出院的多于 N 人时他被截掉——页面不送
`limit`（缺省 200）、回执只说「扫描 N 条」，每次扫描都轮不到他，排进来的反倒是 6 天前出院的短住患者。
"""
from datetime import timedelta

import pytest

from app.clock import now_naive
from app.database import SessionLocal
from app.models import Admission, User
from app.spd.models import SpdFollowupRecord

ID_CARDS = ["33010619620808001X", "330106196208080028", "330106196208080036"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2692 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2692 神经内科"}).json()["id"]
    rule = client.post("/api/spd/followup-rules", headers=admin, json={
        "code": "P2692_stroke", "name": "P2692 脑卒中出院随访", "scene": "inpatient",
        "diagnosis_keywords": ["P2692脑卒中"], "points": [7]})
    assert rule.status_code == 201, rule.text
    patients = []
    for i, id_card in enumerate(ID_CARDS):
        resp = client.post("/api/patients", headers=admin, json={"name": f"P2692 患者{i}", "id_card": id_card})
        assert resp.status_code in (200, 201), resp.text
        patients.append(resp.json()["id"])
    now = now_naive()
    # 入院先后 = 编号先后：长住的最先入院、昨天才出院；两位短住的后入院、5～6 天前出院
    stays = [(patients[0], 40, 1), (patients[1], 3, 6), (patients[2], 3, 5)]
    beds = [client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P2692-{i}"}).json()["id"]
            for i in range(len(stays))]   # 先建床位：开着写会话再调接口，SQLite 上互相锁住
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        for bed, (pid, stay_days, discharged_days_ago) in zip(beds, stays):
            discharged_at = now - timedelta(days=discharged_days_ago)
            db.add(Admission(patient_id=pid, org_id=org, ward_id=ward, bed_id=bed, diagnosis_name="P2692脑卒中",
                             status="discharged", created_by=admin_id,
                             admitted_at=discharged_at - timedelta(days=stay_days), discharged_at=discharged_at))
            db.flush()
        db.commit()
    return {"org": org, "long_stay": patients[0]}


def test_窗口里出院的多于上限_昨天出院的长住患者不被截掉(client, admin, world):
    resp = client.post("/api/spd/followup-plans/auto-match", headers=admin, json={
        "scene": "inpatient", "org_id": world["org"], "days": 7, "limit": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["scanned"] == 2
    with SessionLocal() as db:
        planned = db.query(SpdFollowupRecord.id).filter(SpdFollowupRecord.patient_id == world["long_stay"]).count()
    assert planned == 1   # 修前 0：按住院编号截，编号最小的长住患者在这 2 条之外
