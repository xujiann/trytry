"""多重用药预警按同时在用的品种数判，不数历次处方里的全部品种（P2-144）。

阈值常量的注释写「同时在用药品达到该数即提示多重用药风险」，画像的说明写「在用药品清单」；实现数的却是这位居民
历次通过审方的处方里的**全部**品种——十年里断续吃过五种短程药的居民，今天一种没吃也挂着「多重用药风险」，
真正同时吃着五种药的居民与之无从区分，这条预警就没有了区分度。

修法：一味药只要有一张处方按用药天数算、今天还在服药期内，就算在用；预警按在用品种数判。
画像照旧列历次处方里的全部药品，每行标出在不在用，另给在用品种数。
"""
from datetime import timedelta

import pytest

from app.clock import now_naive


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2144 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2144 居民", "id_card": "330106195811111440", "gender": "男"}).json()["id"]
    return {"org": org, "patient": patient}


def _prescribe(client, admin, world, code, *, days, days_ago):
    resp = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "diagnosis_name": "上呼吸道感染",
        "items": [{"drug_code": code, "drug_name": f"药品{code}", "daily_dose": 1, "days": days}]})
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["status"] in ("auto_passed", "approved"), resp.json()
    from app.database import SessionLocal
    from app.models import Prescription

    with SessionLocal() as db:
        db.get(Prescription, resp.json()["id"]).created_at = now_naive() - timedelta(days=days_ago)
        db.commit()


def _profile(client, admin, world):
    resp = client.get(f"/api/medication/profile/{world['patient']}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_吃过五种短程药_今天一种没吃_不报多重用药(client, admin, world):
    for i in range(5):
        _prescribe(client, admin, world, f"P2144-OLD{i}", days=7, days_ago=60 + i)
    body = _profile(client, admin, world)
    assert body["polypharmacy_warning"] is False   # 修前 True：数的是历次处方里的全部品种
    assert (body["distinct_drugs"], body["in_use_drugs"]) == (5, 0)
    assert {d["in_use"] for d in body["drugs"]} == {False}


def test_同时在用五种_报多重用药(client, admin, world):
    # 一种长期药：两个月前开了 90 天，今天仍在服药期内
    _prescribe(client, admin, world, "P2144-OLD0", days=90, days_ago=60)
    for i in range(4):
        _prescribe(client, admin, world, f"P2144-NEW{i}", days=14, days_ago=1)
    body = _profile(client, admin, world)
    assert (body["distinct_drugs"], body["in_use_drugs"]) == (9, 5)
    assert body["polypharmacy_warning"] is True
    in_use = {d["drug_code"] for d in body["drugs"] if d["in_use"]}
    assert in_use == {"P2144-OLD0"} | {f"P2144-NEW{i}" for i in range(4)}


def test_用药天数取到上限也不崩(client, admin, world):
    """用药天数的入参上限是 INT4_MAX，按 timedelta 加会溢出成 500。"""
    _prescribe(client, admin, world, "P2144-HUGE", days=2147483647, days_ago=3)
    body = _profile(client, admin, world)
    assert next(d for d in body["drugs"] if d["drug_code"] == "P2144-HUGE")["in_use"] is True
