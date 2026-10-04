"""接种日期能填将来：暂时禁忌（暂缓）按将来那天判成已过期，今天照打、照扣批次库存（P2-1304，第三十八批扫描 AB4-6）。

`vaccinate` 的接种日期（`OptionalDateStr`）没有上界，而查禁忌、查批次效期、落库用的都是这个日期（P2-196）：登记了
「发热 38.6℃，暂缓」、有效期到 10-10 的暂时禁忌，今天日期留空登记 409；把日期敲成 2026-11-04 就 201、批次已用数加一。
当天出的反应（AEFI 要求发病日期不早于关联剂次的接种日期，P2-884）关联不上这一剂，10 月接种剂次统计也不计这一针。

修法：查禁忌、扣库存之前，接种日期晚于今天的 422，与出生日期 P2-713 / 发病日期 P2-454 同一句。只在这一处按字段修，
「事件日期普遍不拦将来」的通用做法属 P2-443（待裁定），不在本条。
"""
from datetime import timedelta

import pytest

from conftest import business_today


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21304 接种门诊", "org_type": "township", "level": "township"}).json()["id"]
    kid = client.post("/api/patients", headers=admin, json={
        "name": "P21304 婴儿", "id_card": "330106202604041304", "gender": "男", "birth_date": "2026-04-04"})
    assert kid.status_code == 201, kid.text
    batch = client.post("/api/vaccine-supply/batches", headers=admin, json={
        "vaccine_code": "DTaP", "vaccine_name": "百白破", "batch_no": "P21304-001", "expire_date": "2099-06-30",
        "org_id": org, "quantity": 10})
    assert batch.status_code == 201, batch.text
    today = business_today()
    # 暂缓到今天：今天还拦，明天起按日期现算就不拦了——把接种日期敲成明天，修前就绕过去了
    contra = client.post("/api/vaccination/contraindications", headers=admin, json={
        "patient_id": kid.json()["id"], "vaccine_code": "DTaP", "reason": "发热38.6℃（急性上感），暂缓",
        "contra_type": "temporary", "valid_until": today.isoformat()})
    assert contra.status_code == 201 and contra.json()["blocking"] is True, contra.text
    return {"org": org, "kid": kid.json()["id"], "batch": batch.json()["id"], "today": today}


def _vaccinate(client, admin, world, **extra):
    return client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": world["kid"], "vaccine_code": "DTaP", "vaccine_name": "百白破", "org_id": world["org"],
        "batch_id": world["batch"], **extra})


def _used(client, admin, world):
    rows = client.get("/api/vaccine-supply/batches", headers=admin).json()
    return next(b for b in rows if b["id"] == world["batch"])["used_quantity"]


def _doses(client, admin, world):
    return client.get("/api/vaccination/records", headers=admin, params={"patient_id": world["kid"]}).json()


def test_接种日期晚于今天_422_不绕过暂时禁忌_不扣库存(client, admin, world):
    for later in (world["today"] + timedelta(days=1), world["today"] + timedelta(days=31)):
        resp = _vaccinate(client, admin, world, vaccinated_date=later.isoformat())
        assert resp.status_code == 422, resp.text   # 修前 201：暂缓禁忌按将来那天判成已过期，照打、照扣库存
        assert resp.json() == {"detail": f"接种日期（{later.isoformat()}）不得晚于今天"}
    assert _used(client, admin, world) == 0
    assert _doses(client, admin, world) == []


def test_日期留空与今天照旧被暂时禁忌拦下(client, admin, world):
    used = _used(client, admin, world)
    blank = _vaccinate(client, admin, world)
    assert blank.status_code == 409 and blank.json()["detail"] == "存在接种禁忌：发热38.6℃（急性上感），暂缓", blank.text
    today = _vaccinate(client, admin, world, vaccinated_date=world["today"].isoformat())
    assert today.status_code == 409, today.text
    assert _used(client, admin, world) == used


def test_过去的日期照旧按既有规矩判(client, admin, world):
    used = _used(client, admin, world)
    # 禁忌按这一针的日期判（P2-196）：暂时禁忌只有有效期末日，末日之前的过去日期照样拦
    yesterday = (world["today"] - timedelta(days=1)).isoformat()
    assert _vaccinate(client, admin, world, vaccinated_date=yesterday).status_code == 409
    # 早于出生日期的照旧 422（P2-940）
    early = _vaccinate(client, admin, world, vaccinated_date="2026-01-01")
    assert early.status_code == 422 and "早于出生日期（2026-04-04）" in early.json()["detail"], early.text
    assert _used(client, admin, world) == used
    # 禁忌解除之后，过去的日期照收、照扣一支，落的就是填的那天
    contra = client.get("/api/vaccination/contraindications", headers=admin, params={"patient_id": world["kid"]}).json()
    lifted = client.post(f"/api/vaccination/contraindications/{contra[0]['id']}/lift", headers=admin,
                         json={"lift_reason": "体温正常三天"})
    assert lifted.status_code == 200, lifted.text
    done = _vaccinate(client, admin, world, vaccinated_date=yesterday)
    assert done.status_code == 201 and done.json()["vaccinated_date"] == yesterday, done.text
    assert _used(client, admin, world) == used + 1
