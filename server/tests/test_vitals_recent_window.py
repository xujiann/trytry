"""体温单截断砍错了端：记满 500 次之后，新记的体征在体温单上看不到（P1-81）。

`GET /api/inpatient/admissions/{id}/vitals` 原先按测量时刻**升序**取前 500 条。一次住院记满 500 次
体征之后（每 4 小时一次约 83 天，重症监护每小时一次约 21 天），截掉的恰好是**最新的那一端**：

- 桌面端「体温单」曲线与表格停在第 500 次，护士刚记的那次怎么也画不上去；
- 医生移动端查房取 `vitals.slice(-8)` 当「最近 8 次」，拿到的其实是第 493～500 次——
  几周前的体温，标题照样写着「最近」。

与临期预警那两处同一个形状（升序 + 截断，砍错了端），与转诊超时预警正相反（那边升序留下的正是
最久未推进的，是对的）。

修法：分页从最近一次往前翻（`offset=0` 是最近 `limit` 条），**每页内仍按测量时刻升序**——曲线要升序，
看体温单要最近的。不超过 500 条时逐字节不变（特征化用例钉住）；超过时第一页换成最近 500 条，
`X-Total-Count` 给出总数，往前翻用 `offset`。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import User, VitalSignRecord

CAP = 500  # 原硬编码上限


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _admission(client, admin, tag):
    org = client.post("/api/organizations",
                      json={"name": f"体温单{tag}院", "org_type": "lead_hospital", "level": "county"},
                      headers=admin).json()
    patient = client.post("/api/patients",
                          json={"name": f"体温单患者{tag}", "id_card": f"3200001980010{tag}"},
                          headers=admin).json()
    ward = client.post("/api/inpatient/wards", json={"name": f"体温单病区{tag}", "org_id": org["id"]},
                       headers=admin).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"V-{tag}"},
                      headers=admin).json()
    adm = client.post("/api/inpatient/admissions",
                      json={"patient_id": patient["id"], "org_id": org["id"], "ward_id": ward["id"],
                            "bed_id": bed["id"], "doctor_name": "体温单医生", "diagnosis_name": "肺炎"},
                      headers=admin)
    assert adm.status_code == 201, adm.text
    return adm.json()["id"]


def _vitals(client, admin, admission_id, **params):
    r = client.get(f"/api/inpatient/admissions/{admission_id}/vitals", params=params, headers=admin)
    assert r.status_code == 200, r.text
    return r


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def small(client, admin):
    """一次住院三次测量：录入顺序与测量时刻不一致，且有两次测量时刻相同。"""
    admission_id = _admission(client, admin, "1234")
    for body in (
        {"measured_at": "2031-05-02 08:00", "temperature": 37.2, "pulse": 80, "recorder": "护士乙"},
        {"measured_at": "2031-05-01 08:00", "temperature": 38.5, "pulse": 96, "sbp": 130, "dbp": 85},
        {"measured_at": "2031-05-02 08:00", "temperature": 36.8, "weight_kg": 61.5},
    ):
        r = client.post(f"/api/inpatient/admissions/{admission_id}/vitals", json=body, headers=admin)
        assert r.status_code == 201, r.text
    return admission_id


def test_特征化_不超过上限时按测量时刻升序全给且逐列不变(client, admin, small):
    rows = _vitals(client, admin, small).json()
    assert [(r["measured_at"], r["temperature"]) for r in rows] == [
        ("2031-05-01 08:00", 38.5), ("2031-05-02 08:00", 37.2), ("2031-05-02 08:00", 36.8),
    ], "升序；同一时刻按录入先后"
    first = rows[0]
    assert set(first) == {"id", "measured_at", "temperature", "pulse", "respiration", "sbp", "dbp",
                          "intake_ml", "output_ml", "weight_kg", "recorder"}
    assert (first["pulse"], first["sbp"], first["dbp"], first["respiration"]) == (96, 130, 85, None)
    assert first["recorder"] == "平台管理员", "不填记录人时记当前用户姓名"
    assert rows[1]["recorder"] == "护士乙" and rows[2]["weight_kg"] == 61.5


def test_总数头与翻页(client, admin, small):
    r = _vitals(client, admin, small, limit=2)
    assert r.headers["X-Total-Count"] == "3"
    newest_two = r.json()
    assert [x["temperature"] for x in newest_two] == [37.2, 36.8], "第一页是最近两次，页内仍升序"
    older = _vitals(client, admin, small, limit=2, offset=2).json()
    assert [x["temperature"] for x in older] == [38.5]


@pytest.fixture(scope="module")
def bulk(client, admin):
    """一次住院 `CAP + 20` 次测量，测量时刻逐小时递增。"""
    admission_id = _admission(client, admin, "5678")
    with SessionLocal() as db:
        creator = db.query(User).filter(User.username == "admin").one().id
        db.execute(insert(VitalSignRecord), [
            {"admission_id": admission_id, "created_by": creator, "recorder": "灌量",
             "measured_at": f"2031-06-{1 + i // 24:02d} {i % 24:02d}:00",
             "temperature": 36.0 + (i % 10) / 10}
            for i in range(CAP + 20)
        ])
        db.commit()
        stamps = [m for (m,) in db.query(VitalSignRecord.measured_at)
                  .filter(VitalSignRecord.admission_id == admission_id)
                  .order_by(VitalSignRecord.measured_at, VitalSignRecord.id).all()]
    return {"admission_id": admission_id, "stamps": stamps}


def test_超过上限时第一页是最近的那一端(client, admin, bulk):
    r = _vitals(client, admin, bulk["admission_id"])
    rows = r.json()
    assert r.headers["X-Total-Count"] == str(CAP + 20)
    assert len(rows) == CAP
    assert rows[-1]["measured_at"] == bulk["stamps"][-1], "最新一次测量不在体温单上——截断砍掉了最新的那一端"
    assert [x["measured_at"] for x in rows] == bulk["stamps"][-CAP:], "页内按测量时刻升序，供曲线直接画"


def test_往前翻拿到更早的那些(client, admin, bulk):
    rows = _vitals(client, admin, bulk["admission_id"], offset=CAP).json()
    assert [x["measured_at"] for x in rows] == bulk["stamps"][:20]
