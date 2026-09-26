"""押金不足预警先筛、先排，再分页（P2-137）。

预警说明：在院患者中「押金余额 − 未结费用 < 阈值」的清单，按 gap 从小到大排——最缺钱的排最前。实现却先按住院号
分页、再在这一页里筛、在这一页里排：`X-Total-Count` 数的是全部在院患者（筛之前的），排序只在一页之内；在院超过
一页（500 人）时，最缺钱的那位只要不在第一页，预警页上就没有他——页面只取第一页。

修法：余额与未结费用下推成关联子查询，先筛、按 gap 排好序再分页。
"""
import itertools

import pytest

from conftest import login

_SEQ = itertools.count(1)
ITEM = "P2137-FEE"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2137 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2137 病区"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "治疗费(P2137)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "治疗费(P2137)", "category": "treatment", "price": 50})
    assert item.status_code in (201, 409), item.text
    created = client.post("/api/users", headers=admin, json={
        "username": "p2137_op", "password": "pw123456", "role": "operator", "org_id": org})
    assert created.status_code in (200, 201), created.text
    op = login(client, "p2137_op", "pw123456")   # 只看得到本机构：共用库里别家的在院患者不串进来

    admissions = {}
    for name, deposit, qty in (("A", 300, 4), ("B", 50, 2), ("C", 0, 4)):   # gap：+100 / −50 / −200
        n = next(_SEQ)
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P2137-{n}"}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2137 患者{name}", "id_card": f"33010619880808{n:04d}", "gender": "男"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"}).json()["id"]
        if deposit:
            assert client.post("/api/billing/deposits", headers=admin, json={
                "admission_id": adm, "amount": deposit, "method": "cash"}).status_code == 201
        assert client.post("/api/billing/details", headers=admin, json={
            "patient_id": patient, "admission_id": adm, "item_code": ITEM, "quantity": qty}).status_code == 201
        admissions[name] = adm
    return {"op": op, "adm": admissions}


def _alerts(client, world, **params):
    resp = client.get("/api/billing/deposits/alerts", headers=world["op"], params=params)
    assert resp.status_code == 200, resp.text
    return [r["admission_id"] for r in resp.json()], int(resp.headers["X-Total-Count"])


def test_最缺钱的排最前_总数是筛过之后的(client, world):
    a = world["adm"]
    assert _alerts(client, world) == ([a["C"], a["B"]], 2)   # 修前总数 3（全部在院的）


def test_分页在筛与排之后(client, world):
    a = world["adm"]
    assert _alerts(client, world, limit=1) == ([a["C"]], 2)   # 修前 ([], 3)：第一页是住院号最小的 A，不缺钱
    assert _alerts(client, world, offset=1, limit=1) == ([a["B"]], 2)


def test_阈值调大提前预警(client, world):
    a = world["adm"]
    assert _alerts(client, world, threshold=150) == ([a["C"], a["B"], a["A"]], 3)
