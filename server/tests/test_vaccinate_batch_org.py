"""接种登记只能用接种机构自己的疫苗批次（P0-42）。

接种登记按请求里的接种机构判归属（`assert_org_writable(body.org_id)`，只能以本机构名义接种），
可请求里的批次号（`batch_id`）从来不看是哪家的：2026-09-24 实测，乙卫生院的医生给自己的受种者
登记接种、`batch_id` 填甲卫生院的批次——201，**甲院那一批的已用数加一**，接种记录上落的是甲院的批号。
疫苗按批号强监管：甲院的台账平白少了一支（实物还在冷柜里），乙院这一针在追溯链上挂到了别人的批次，
哪天按批号召回，找的是甲院。批次由持有机构自己入库（`create_batch` 按 `body.org_id` 判归属），
平台没有跨机构调拨——「用别家的批次」没有正当路径。

修法：批次三查之前先查归属——批次的机构必须是接种机构，否则 422（与医废「点位不属于该机构」同一口径：
次级对象对不上主对象，是请求本身不成立，不是权限问题）。库存不动。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def vac_world(client):
    """甲、乙两家卫生院各有一名接种医生；甲院入库了一批 10 支的疫苗。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "接种批次甲卫生院"), ("b", "接种批次乙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p042_doc_{key}", "password": "pw123456", "full_name": f"p042_doc_{key}",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
        r = client.post("/api/users",
                        json={"username": f"p042_ph_{key}", "password": "pw123456", "full_name": f"p042_ph_{key}",
                              "role": "public_health", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    h = {f"{role}_{k}": _login(client, f"p042_{role}_{k}") for k in orgs for role in ("doc", "ph")}
    batch = client.post("/api/vaccine-supply/batches",
                        json={"vaccine_code": "P042HEPB", "vaccine_name": "乙肝疫苗（批次归属测试）",
                              "batch_no": "P042-A-001", "expire_date": "2030-12-31", "org_id": orgs["a"],
                              "quantity": 10},
                        headers=h["ph_a"])
    assert batch.status_code == 201, batch.text
    patients = [client.post("/api/patients", json={"name": f"接种批次受种者{i}", "id_card": f"32000020200101{i:04d}"},
                            headers=admin).json()["id"] for i in (1, 2)]
    return {"admin": admin, "h": h, "orgs": orgs, "batch_id": batch.json()["id"], "patients": patients}


def _used(client, world) -> int:
    rows = client.get(f"/api/vaccine-supply/batches?org_id={world['orgs']['a']}", headers=world["admin"]).json()
    return next(b for b in rows if b["id"] == world["batch_id"])["used_quantity"]


def _vaccinate(client, world, who, org_key, patient):
    return client.post("/api/vaccination/records",
                       json={"patient_id": patient, "vaccine_code": "P042HEPB", "vaccine_name": "乙肝疫苗",
                             "org_id": world["orgs"][org_key], "batch_id": world["batch_id"]},
                       headers=world["h"][who])


def test_用别家机构的批次接种被拒_库存不动(client, vac_world):
    before = _used(client, vac_world)
    r = _vaccinate(client, vac_world, "doc_b", "b", vac_world["patients"][1])
    assert r.status_code == 422, r.text
    assert "批次" in r.json()["detail"]
    assert _used(client, vac_world) == before, "被拒的接种不该扣甲院的库存"


def test_本机构批次照常接种并扣库存(client, vac_world):
    before = _used(client, vac_world)
    r = _vaccinate(client, vac_world, "doc_a", "a", vac_world["patients"][0])
    assert r.status_code == 201, r.text
    assert r.json()["batch_id"] == vac_world["batch_id"]
    assert _used(client, vac_world) == before + 1
