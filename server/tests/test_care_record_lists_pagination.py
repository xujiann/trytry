"""P2-8 第六批：五个已有收口的诊疗记录清单切 `deps.paginate`（纯分页整改）。

住院医嘱清单、医嘱执行记录、门急诊处置记录、门急诊护理记录、慢专病转诊超时预警。前四个在
P0-19 / P0-22 补上患者可见性之后才进 A 类（「已有收口」）——住院医嘱两条原本在 P1-49 的
待裁定名单里，理由正是「没有收口，切了就把可枚举面放大」；收口在先、切分页在后，那条前提
没被绕过。转诊超时预警的 `count` 早已是总数（P2-8 第四批），这次补上翻页，排序补 id 尾键。

六项检查全部过关才算纯分页：上限就在返回列表的查询上、查询后没有依赖全部行的计算、没有取行后
的 Python 过滤、排序末位键唯一、要登录、已有收口。原上限都是 200，`paginate` 默认上限 500，
不带参数调用时第一页不缩水。同批判下来的另两条不在这里：体温单（P1-81，截断砍错了端）、
流程待办（P1-82，角色筛在截断之后）是缺陷，各自单独修；卫健工作台的 `.limit(20)` 是
「考核结果排名」前 20 名的展示上限，登记为误报。
"""
from datetime import timedelta

import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import NursingRecord, User, utcnow
from app.spd.models import SpdReferralCase

CAP = 200  # 原硬编码上限


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def world(client, admin):
    """一位患者：一次门诊（处置 1 条、护理 1 条），一次住院（医嘱 1 条、执行 1 次）。"""
    org = client.post("/api/organizations",
                      json={"name": "分页第六批县医院", "org_type": "lead_hospital", "level": "county"},
                      headers=admin).json()
    patient = client.post("/api/patients", json={"name": "分页第六批患者", "id_card": "330000199001016666"},
                          headers=admin).json()
    encounter = client.post("/api/encounters",
                            json={"patient_id": patient["id"], "org_id": org["id"], "doctor_name": "门诊医师",
                                  "diagnosis_name": "急性上呼吸道感染"},
                            headers=admin).json()
    assert client.post(f"/api/outpatient/encounters/{encounter['id']}/treatments",
                       json={"treatment_name": "雾化吸入"}, headers=admin).status_code == 201
    assert client.post(f"/api/outpatient/encounters/{encounter['id']}/nursing-records",
                       json={"content": "输液中，无不适", "nurse_name": "李护士"}, headers=admin).status_code == 201
    ward = client.post("/api/inpatient/wards", json={"org_id": org["id"], "name": "分页第六批病区"},
                       headers=admin).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "P6-01"}, headers=admin).json()
    admission = client.post("/api/inpatient/admissions",
                            json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"]},
                            headers=admin).json()
    order = client.post("/api/inpatient/orders",
                        json={"admission_id": admission["id"], "order_type": "long", "content": "0.9%氯化钠 250ml qd"},
                        headers=admin).json()
    assert client.post(f"/api/inpatient/orders/{order['id']}/executions", json={"note": "首次执行"},
                       headers=admin).status_code == 201
    return {"encounter": encounter["id"], "admission": admission["id"], "order": order["id"],
            "org": org["id"], "patient": patient["id"]}


def _paths(world):
    return [
        (f"/api/outpatient/encounters/{world['encounter']}/treatments", {}),
        (f"/api/outpatient/encounters/{world['encounter']}/nursing-records", {}),
        ("/api/inpatient/orders", {"admission_id": world["admission"]}),
        ("/api/inpatient/orders", {}),
        (f"/api/inpatient/orders/{world['order']}/executions", {}),
        ("/api/spd/referrals-alerts", {"hours": 48}),
    ]


def test_切过的端点都带上了总数头(client, admin, world):
    """一个都不许漏——漏掉的那个就是下一次「列表少了一截没人发现」。"""
    for path, params in _paths(world):
        r = client.get(path, params=params, headers=admin)
        assert r.status_code == 200, (path, r.text)
        assert "X-Total-Count" in r.headers, f"{path} 没带 X-Total-Count"


def test_特征化_不超过一页时响应体不变(client, admin, world):
    """列表形状与内容照旧：单条就是单条，字段不多不少。"""
    enc = world["encounter"]
    treatments = client.get(f"/api/outpatient/encounters/{enc}/treatments", headers=admin).json()
    assert [t["treatment_name"] for t in treatments] == ["雾化吸入"]
    nursing = client.get(f"/api/outpatient/encounters/{enc}/nursing-records", headers=admin).json()
    assert set(nursing[0]) == {"id", "nursing_level", "content", "nurse_name", "recorded_at"}
    orders = client.get("/api/inpatient/orders", params={"admission_id": world["admission"]}, headers=admin).json()
    assert [o["content"] for o in orders] == ["0.9%氯化钠 250ml qd"]
    executions = client.get(f"/api/inpatient/orders/{world['order']}/executions", headers=admin).json()
    assert [e["note"] for e in executions] == ["首次执行"]
    alerts = client.get("/api/spd/referrals-alerts", headers=admin).json()
    assert set(alerts) == {"threshold_hours", "count", "items"}


def test_住院号不存在照旧回空清单(client, admin):
    r = client.get("/api/inpatient/orders", params={"admission_id": 99999999}, headers=admin)
    assert r.status_code == 200 and r.json() == []


@pytest.fixture(scope="module")
def bulk(world):
    """同一次门诊再灌 `CAP + 5` 条护理记录。"""
    with SessionLocal() as db:
        creator = db.query(User).filter(User.username == "admin").one().id
        db.execute(insert(NursingRecord), [
            {"encounter_id": world["encounter"], "content": f"巡视{i}", "nurse_name": "灌量",
             "created_by": creator}
            for i in range(CAP + 5)
        ])
        db.commit()
    return {"total": CAP + 6}


def test_默认页大小不变_往后翻拿得到剩下的(client, admin, world, bulk):
    path = f"/api/outpatient/encounters/{world['encounter']}/nursing-records"
    first = client.get(path, headers=admin)
    assert first.headers["X-Total-Count"] == str(bulk["total"])
    assert len(first.json()) == CAP, "不带参数时第一页照旧是原上限那么多"
    rest = client.get(path, params={"offset": CAP}, headers=admin).json()
    seen = [r["id"] for r in first.json()] + [r["id"] for r in rest]
    assert len(seen) == len(set(seen)) == bulk["total"], "翻页结果有重复或缺漏"
    assert seen == sorted(seen, reverse=True), "新的在前"


@pytest.fixture(scope="module")
def overdue(world):
    """5 张同一时刻落下、超过 48 小时未推进的在途转诊单——同一时刻是常态（批量导入、同一班次）。"""
    old = utcnow() - timedelta(hours=100)
    with SessionLocal() as db:
        creator = db.query(User).filter(User.username == "admin").one().id
        db.execute(insert(SpdReferralCase), [
            {"patient_id": world["patient"], "program_code": "p6_hyp", "direction": "up", "status": "pending",
             "reason": "分页第六批", "initiator_org_id": world["org"], "current_org_id": world["org"],
             "current_level": "county", "created_by": creator, "created_at": old}
            for _ in range(5)
        ])
        db.commit()


def test_转诊超时预警翻页不重不漏且最久的在前(client, admin, overdue):
    """排序按落单时刻升序，补了 id 尾键——并列的单子才翻得稳，第一页留下的是最久未推进的。"""
    first = client.get("/api/spd/referrals-alerts", params={"limit": 3}, headers=admin)
    total = first.json()["count"]
    assert total >= 5 and first.headers["X-Total-Count"] == str(total)
    seen = []
    for offset in range(0, total, 3):
        page = client.get("/api/spd/referrals-alerts", params={"limit": 3, "offset": offset}, headers=admin).json()
        assert page["count"] == total, "每一页的 count 都是总数，不是这一页的条数"
        seen.extend(page["items"])
    assert len({x["id"] for x in seen}) == len(seen) == total, "翻页结果有重复或缺漏"
    keys = [(x["created_at"], x["id"]) for x in seen]
    assert keys == sorted(keys), "最久未推进的在前，同一时刻按 id"
