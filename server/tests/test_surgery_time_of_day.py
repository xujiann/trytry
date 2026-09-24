"""手术排班与手术间撮合的时刻：只认半角 `HH:MM`、而且得是一天里真有的时刻（P2-46）。

两处原先都只卡形状 `^\\d{2}:\\d{2}$`：

- `\\d` 放行全角数字。「０８:００」按字符串比较排在一切半角时刻之后，冲突判定
  （已排 start < 新 end 且 已排 end > 新 start）就永远判不出它与半角时段重叠——
  同一手术间同一时段可以排进两台手术；
- 形状对、时刻不存在的 `25:61` 照样落库。

修法：`datetypes.TimeStr`（与 `DateStr` 同一个做法：先卡半角形状，再卡取值 00:00–23:59），
排班请求体与撮合查询参数都换上它。合法时刻的行为一字不变（冲突判定、紧邻放行照旧）。
"""
import pytest

from app.datetypes import check_time
from conftest import login

GOOD = ["00:00", "08:30", "23:59"]
BAD = ["24:00", "25:61", "08:60", "０８:００", "٠٨:٠٠", "8:00", "08:00\n", "08：00"]


@pytest.mark.parametrize("value", GOOD)
def test_合法时刻原样返回(value):
    assert check_time(value) == value


@pytest.mark.parametrize("value", BAD)
def test_全角_越界_缺位的时刻都拒(value):
    with pytest.raises(ValueError):
        check_time(value)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations",
                      json={"name": "时刻校验县医院", "org_type": "lead_hospital", "level": "county"},
                      headers=admin).json()
    ward = client.post("/api/inpatient/wards", json={"org_id": org["id"], "name": "时刻校验病区"},
                       headers=admin).json()
    room = client.post("/api/surgery/rooms", json={"org_id": org["id"], "name": "时刻校验手术间"},
                       headers=admin).json()
    for username, role in (("p246_doc", "doctor"), ("p246_dir", "director"), ("p246_op", "operator")):
        client.post("/api/users", json={"username": username, "password": "passw0rd1", "full_name": username,
                                        "role": role, "org_id": org["id"]}, headers=admin)
    h = {k: login(client, f"p246_{k}", "passw0rd1") for k in ("doc", "dir", "op")}

    def approved_request(i):
        bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"T{i:02d}"},
                          headers=admin).json()
        patient = client.post("/api/patients", json={"name": f"时刻校验患者{i}", "id_card": f"33118219901010{i:04d}"},
                              headers=admin).json()
        adm = client.post("/api/inpatient/admissions",
                          json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"]},
                          headers=admin).json()
        req = client.post("/api/surgery/requests", json={"admission_id": adm["id"], "surgery_name": "时刻校验术"},
                          headers=h["doc"]).json()
        client.post(f"/api/surgery/requests/{req['id']}/approve", json={"approved": True}, headers=h["dir"])
        return req["id"]

    first = approved_request(1)
    r = client.post(f"/api/surgery/requests/{first}/schedule",
                    json={"room_id": room["id"], "scheduled_date": "2031-03-03",
                          "start_time": "09:00", "end_time": "11:00"}, headers=h["op"])
    assert r.status_code == 201, r.text
    return {"org": org["id"], "room": room["id"], "h": h, "next": approved_request}


def _schedule(client, world, request_id, start, end):
    return client.post(f"/api/surgery/requests/{request_id}/schedule",
                       json={"room_id": world["room"], "scheduled_date": "2031-03-03",
                             "start_time": start, "end_time": end}, headers=world["h"]["op"])


def test_特征化_半角重叠照旧409_紧邻照旧放行(client, world):
    rid = world["next"](2)
    assert _schedule(client, world, rid, "10:00", "12:00").status_code == 409
    assert _schedule(client, world, rid, "11:00", "12:30").status_code == 201


@pytest.mark.parametrize("start,end", [("０９:３０", "１０:３０"), ("25:00", "26:00"), ("09:30", "10:61")])
def test_全角或不存在的时刻排不进去(client, world, start, end):
    """全角那一组正落在已排的 09:00–11:00 里——改前 201，同一手术间同一时段排进了两台。"""
    rid = world["next"](3 if start.startswith("０") else 4 if start == "25:00" else 5)
    r = _schedule(client, world, rid, start, end)
    assert r.status_code == 422, (start, end, r.status_code, r.text[:200])


def test_撮合的时间窗也按同一口径校验(client, admin, world):
    ok = client.get("/api/resources/match/or-rooms",
                    params={"org_id": world["org"], "scheduled_date": "2031-03-03",
                            "start_time": "08:00", "end_time": "18:00"}, headers=admin)
    assert ok.status_code == 200, ok.text
    for start, end in (("０８:００", "18:00"), ("08:00", "24:30")):
        bad = client.get("/api/resources/match/or-rooms",
                         params={"org_id": world["org"], "scheduled_date": "2031-03-03",
                                 "start_time": start, "end_time": end}, headers=admin)
        assert bad.status_code == 422, (start, end, bad.status_code, bad.text[:200])
