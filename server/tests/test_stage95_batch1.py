"""阶段九·五 第一批：疫苗批次/冷链/AEFI、医废点位与追溯、多点触发监测。

补的是第七轮子功能级重审里被降到 ⚠️ 的三条主项（㉕㉖㊱）。
用例围着口径转，不围着 CRUD 转——CRUD 对不对看接口能不能调通就知道了，
口径对不对只有用例说了算。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "九五县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "九五受种者", "id_card": "320000201001011234", "birth_date": "2010-01-01"},
        headers=admin,
    ).json()


# ================================================================ ㉕ 疫苗批次


def test_批次三查各自给出明确原因(client, admin, org, patient):
    """过期、封存、库存不足不能合并成一句"批次不可用"——接种台前的人
    要知道是该换批次还是该补货。"""
    expired = client.post(
        "/api/vaccine-supply/batches",
        json={"vaccine_code": "V1", "vaccine_name": "疫苗一", "batch_no": "B-OLD",
              "expire_date": "2020-01-01", "org_id": org["id"], "quantity": 10},
        headers=admin,
    ).json()
    empty = client.post(
        "/api/vaccine-supply/batches",
        json={"vaccine_code": "V1", "vaccine_name": "疫苗一", "batch_no": "B-EMPTY",
              "expire_date": "2099-01-01", "org_id": org["id"], "quantity": 0},
        headers=admin,
    ).json()
    frozen = client.post(
        "/api/vaccine-supply/batches",
        json={"vaccine_code": "V1", "vaccine_name": "疫苗一", "batch_no": "B-FROZEN",
              "expire_date": "2099-01-01", "org_id": org["id"], "quantity": 10},
        headers=admin,
    ).json()
    client.post(
        f"/api/vaccine-supply/batches/{frozen['id']}/freeze",
        json={"frozen_reason": "冷链超温待核查"}, headers=admin,
    )

    def vaccinate(batch_id):
        return client.post(
            "/api/vaccination/records",
            json={"patient_id": patient["id"], "vaccine_code": "V1", "vaccine_name": "疫苗一",
                  "dose_no": 1, "vaccinated_date": "2026-08-12", "org_id": org["id"],
                  "batch_id": batch_id},
            headers=admin,
        )

    assert "过期" in vaccinate(expired["id"]).json()["detail"]
    assert "封存" in vaccinate(frozen["id"]).json()["detail"]
    assert "库存" in vaccinate(empty["id"]).json()["detail"]


def test_接种扣减批次库存并记下批号(client, admin, org, patient):
    batch = client.post(
        "/api/vaccine-supply/batches",
        json={"vaccine_code": "V2", "vaccine_name": "疫苗二", "batch_no": "B-OK",
              "manufacturer": "某生物", "expire_date": "2099-01-01",
              "org_id": org["id"], "quantity": 5},
        headers=admin,
    ).json()
    record = client.post(
        "/api/vaccination/records",
        json={"patient_id": patient["id"], "vaccine_code": "V2", "vaccine_name": "疫苗二",
              "dose_no": 1, "vaccinated_date": "2026-08-12", "org_id": org["id"],
              "batch_id": batch["id"], "site": "左上臂", "vaccinator": "王护士"},
        headers=admin,
    )
    assert record.status_code == 201, record.text
    assert record.json()["batch_no"] == "B-OK"
    after = client.get(
        "/api/vaccine-supply/batches", params={"vaccine_code": "V2"}, headers=admin
    ).json()[0]
    assert after["used_quantity"] == 1 and after["remaining"] == 4


def test_按批号反查受种者(client, admin, org, patient):
    """召回时唯一有用的那个查询。"""
    batch = client.get(
        "/api/vaccine-supply/batches", params={"vaccine_code": "V2"}, headers=admin
    ).json()[0]
    resp = client.get(f"/api/vaccine-supply/batches/{batch['id']}/recipients", headers=admin)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["recipients"][0]["patient_id"] == patient["id"]


def test_批次封存后可以解除(client, admin, org):
    """凡是拦得住的，都要放得开——D-1/D-2/D-4 的教训。"""
    batch = client.post(
        "/api/vaccine-supply/batches",
        json={"vaccine_code": "V3", "vaccine_name": "疫苗三", "batch_no": "B-3",
              "expire_date": "2099-01-01", "org_id": org["id"], "quantity": 3},
        headers=admin,
    ).json()
    client.post(f"/api/vaccine-supply/batches/{batch['id']}/freeze",
                json={"frozen_reason": "待核查"}, headers=admin)
    resp = client.post(f"/api/vaccine-supply/batches/{batch['id']}/unfreeze", headers=admin)
    assert resp.status_code == 200
    assert resp.json()["usable"] is True


def test_批次号在同一疫苗下唯一(client, admin, org):
    body = {"vaccine_code": "V4", "vaccine_name": "疫苗四", "batch_no": "B-DUP",
            "expire_date": "2099-01-01", "org_id": org["id"], "quantity": 1}
    assert client.post("/api/vaccine-supply/batches", json=body, headers=admin).status_code == 201
    assert client.post("/api/vaccine-supply/batches", json=body, headers=admin).status_code == 409


# ================================================================ ㉕ 冷链


def test_超温标记但不自动封存批次(client, admin, org):
    """封存整批是有成本的决定，平台把异常标出来由人决定——
    与"超支不自动扣减"同一条原则。"""
    batch = client.post(
        "/api/vaccine-supply/batches",
        json={"vaccine_code": "V5", "vaccine_name": "疫苗五", "batch_no": "B-5",
              "expire_date": "2099-01-01", "org_id": org["id"], "quantity": 10},
        headers=admin,
    ).json()
    resp = client.post(
        "/api/vaccine-supply/cold-chain",
        json={"org_id": org["id"], "device_name": "冰箱1", "temperature": 15.0,
              "recorded_at": "2026-08-12 09:00:00"},
        headers=admin,
    )
    assert resp.status_code == 201
    assert resp.json()["exceeded"] is True
    assert "不自动封存" in resp.json()["hint"]
    still = client.get("/api/vaccine-supply/batches", params={"vaccine_code": "V5"},
                       headers=admin).json()[0]
    assert still["status"] == "normal"


def test_区间内的温度不算超温(client, admin, org):
    resp = client.post(
        "/api/vaccine-supply/cold-chain",
        json={"org_id": org["id"], "device_name": "冰箱1", "temperature": 5.0,
              "recorded_at": "2026-08-12 10:00:00"},
        headers=admin,
    )
    assert resp.json()["exceeded"] is False


def test_未超温的记录不需要处置(client, admin, org):
    normal = client.post(
        "/api/vaccine-supply/cold-chain",
        json={"org_id": org["id"], "device_name": "冰箱2", "temperature": 4.0,
              "recorded_at": "2026-08-12 11:00:00"},
        headers=admin,
    ).json()
    resp = client.post(f"/api/vaccine-supply/cold-chain/{normal['id']}/handle",
                       json={"handle_note": "无"}, headers=admin)
    assert resp.status_code == 422


# ================================================================ ㉕ AEFI


def test_AEFI关联接种记录时自动带出批号(client, admin, org, patient):
    """上报现场往往不知道打的是哪一批，让人凭记忆填只会填错。"""
    record = client.get(
        "/api/vaccination/records", params={"patient_id": patient["id"]}, headers=admin
    ).json()[0]
    resp = client.post(
        "/api/vaccine-supply/aefi",
        json={"patient_id": patient["id"], "record_id": record["id"],
              "reaction_type": "general", "symptom": "接种部位红肿",
              "onset_date": "2026-08-13", "org_id": org["id"]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["batch_no"] == record["batch_no"]
    assert resp.json()["vaccine_code"] == record["vaccine_code"]


def test_AEFI不关联记录时必须填疫苗编码(client, admin, org, patient):
    resp = client.post(
        "/api/vaccine-supply/aefi",
        json={"patient_id": patient["id"], "symptom": "发热",
              "onset_date": "2026-08-13", "org_id": org["id"]},
        headers=admin,
    )
    assert resp.status_code == 422


def test_AEFI不能挂到别人的接种记录上(client, admin, org, patient):
    other = client.post(
        "/api/patients", json={"name": "另一人", "id_card": "320000201001015678"}, headers=admin
    ).json()
    record = client.get(
        "/api/vaccination/records", params={"patient_id": patient["id"]}, headers=admin
    ).json()[0]
    resp = client.post(
        "/api/vaccine-supply/aefi",
        json={"patient_id": other["id"], "record_id": record["id"], "symptom": "发热",
              "onset_date": "2026-08-13", "org_id": org["id"]},
        headers=admin,
    )
    assert resp.status_code == 422


def test_AEFI发生率分母是剂次且无接种时不报0(client, admin, org):
    """分母为 0 时返回 null 而不是 0——0 会被读成"零发生率"。"""
    empty = client.get(
        "/api/vaccine-supply/stats",
        params={"start_date": "1990-01-01", "end_date": "1990-12-31"},
        headers=admin,
    ).json()
    assert empty["doses"] == 0
    assert empty["aefi"]["rate_per_100k_doses"] is None

    real = client.get("/api/vaccine-supply/stats", headers=admin).json()
    assert real["doses"] > 0
    assert real["aefi"]["rate_per_100k_doses"] is not None


def test_统计里过期与封存批次分别计数(client, admin):
    stats = client.get("/api/vaccine-supply/stats", headers=admin).json()
    assert stats["batches"]["expired"] >= 1
    assert stats["batches"]["frozen"] >= 1


# ================================================================ ㊱ 医废


def test_追溯码由平台生成且唯一(client, admin, org):
    """让人填码，迟早会有两包医废共用一个码。"""
    source = client.post(
        "/api/medwaste/locations",
        json={"org_id": org["id"], "name": "外科病区", "location_type": "source"},
        headers=admin,
    ).json()
    codes = set()
    for _ in range(3):
        resp = client.post(
            "/api/medwaste",
            json={"org_id": org["id"], "waste_type": "infectious", "weight_kg": 2.5,
                  "collected_date": "2026-08-12", "source_location_id": source["id"]},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        codes.add(resp.json()["trace_code"])
    assert len(codes) == 3
    assert all(c.startswith("MW-20260812-") for c in codes)


def test_扫码追溯给出完整时间轴(client, admin, org):
    source = client.get(
        "/api/medwaste/locations", params={"org_id": org["id"], "location_type": "source"},
        headers=admin,
    ).json()[0]
    storage = client.post(
        "/api/medwaste/locations",
        json={"org_id": org["id"], "name": "医废暂存间", "location_type": "storage"},
        headers=admin,
    ).json()
    waste = client.post(
        "/api/medwaste",
        json={"org_id": org["id"], "waste_type": "sharp", "weight_kg": 1.0,
              "collected_date": "2026-08-12", "source_location_id": source["id"]},
        headers=admin,
    ).json()
    employee = client.post(
        "/api/mgmt/employees",
        json={"org_id": org["id"], "name": "转运员老张", "title": "工勤"},
        headers=admin,
    ).json()

    client.post(f"/api/medwaste/{waste['id']}/store",
                json={"storage_location_id": storage["id"]}, headers=admin)
    client.post(f"/api/medwaste/{waste['id']}/handover",
                json={"handler_name": "忽略", "handler_employee_id": employee["id"]},
                headers=admin)

    traced = client.get(f"/api/medwaste/trace/{waste['trace_code']}", headers=admin)
    assert traced.status_code == 200, traced.text
    steps = [t["step"] for t in traced.json()["timeline"]]
    assert steps == ["收集", "暂存", "交接"]
    # 转运人员挂了员工档案，名字从档案取，不采信请求体里的
    assert traced.json()["handler_name"] == "转运员老张"


def test_暂存点位不能当产生点用(client, admin, org):
    storage = client.get(
        "/api/medwaste/locations", params={"org_id": org["id"], "location_type": "storage"},
        headers=admin,
    ).json()[0]
    resp = client.post(
        "/api/medwaste",
        json={"org_id": org["id"], "waste_type": "infectious", "weight_kg": 1.0,
              "collected_date": "2026-08-12", "source_location_id": storage["id"]},
        headers=admin,
    )
    assert resp.status_code == 422


def test_转运人员工作量统计把未挂档案的单列(client, admin, org):
    """名字重合就会张冠李戴，所以不硬凑进某个人头上。"""
    waste = client.post(
        "/api/medwaste",
        json={"org_id": org["id"], "waste_type": "chemical", "weight_kg": 3.0,
              "collected_date": "2026-08-12"},
        headers=admin,
    ).json()
    client.post(f"/api/medwaste/{waste['id']}/handover",
                json={"handler_name": "临时工小李"}, headers=admin)
    stats = client.get("/api/medwaste/handler-stats", headers=admin).json()
    assert stats["unlinked_records"]["count"] >= 1
    assert any(h["name"] == "转运员老张" for h in stats["handlers"])


def test_点位停用后历史记录仍可追溯(client, admin, org):
    source = client.get(
        "/api/medwaste/locations", params={"org_id": org["id"], "location_type": "source"},
        headers=admin,
    ).json()[0]
    waste = client.post(
        "/api/medwaste",
        json={"org_id": org["id"], "waste_type": "infectious", "weight_kg": 1.0,
              "collected_date": "2026-08-12", "source_location_id": source["id"]},
        headers=admin,
    ).json()
    assert client.delete(f"/api/medwaste/locations/{source['id']}",
                         headers=admin).status_code == 200
    traced = client.get(f"/api/medwaste/trace/{waste['trace_code']}", headers=admin)
    assert traced.status_code == 200
    assert traced.json()["timeline"][0]["location"] == "外科病区"
    client.post(f"/api/medwaste/locations/{source['id']}/reactivate", headers=admin)


# ================================================================ ㉖ 多点触发


def test_症候群阈值按机构自定义(client, admin, org):
    """写死一个数只会让大机构天天报警、小机构从不报警。"""
    village = client.post(
        "/api/organizations",
        json={"name": "九五村卫生室", "org_type": "village", "level": "village"},
        headers=admin,
    ).json()
    big = client.post(
        "/api/surveillance/syndromes",
        json={"org_id": org["id"], "syndrome": "fever", "case_count": 40,
              "threshold": 60, "record_date": "2026-08-12"},
        headers=admin,
    ).json()
    small = client.post(
        "/api/surveillance/syndromes",
        json={"org_id": village["id"], "syndrome": "fever", "case_count": 6,
              "threshold": 5, "record_date": "2026-08-12"},
        headers=admin,
    ).json()
    assert big["alert"] is False  # 40 例在县医院不算异常
    assert small["alert"] is True  # 6 例在村卫生室就该看一眼


def test_同机构同症候群同日重复上报按覆盖(client, admin, org):
    """日报要反复核对，累加会算出虚高的病例数（与基金预结同一条教训）。"""
    first = client.post(
        "/api/surveillance/syndromes",
        json={"org_id": org["id"], "syndrome": "diarrhea", "case_count": 10,
              "threshold": 20, "record_date": "2026-08-11"},
        headers=admin,
    ).json()
    second = client.post(
        "/api/surveillance/syndromes",
        json={"org_id": org["id"], "syndrome": "diarrhea", "case_count": 14,
              "threshold": 20, "record_date": "2026-08-11"},
        headers=admin,
    ).json()
    assert second["overwritten"] is True
    assert second["id"] == first["id"]
    assert second["case_count"] == 14  # 不是 24


def test_阈值为0不参与预警(client, admin, org):
    resp = client.post(
        "/api/surveillance/syndromes",
        json={"org_id": org["id"], "syndrome": "rash", "case_count": 99,
              "threshold": 0, "record_date": "2026-08-12"},
        headers=admin,
    ).json()
    assert resp["alert"] is False


def test_病原送检为0时阳性率报null不报0(client, admin, org):
    """0 会被读成"检了没检出"，实际是"没检"。"""
    resp = client.post(
        "/api/surveillance/pathogens",
        json={"org_id": org["id"], "pathogen_name": "甲型流感病毒", "specimen_type": "咽拭子",
              "tested_count": 0, "positive_count": 0, "record_date": "2026-08-12"},
        headers=admin,
    ).json()
    assert resp["positive_rate_pct"] is None


def test_阳性数不能大于送检数(client, admin, org):
    resp = client.post(
        "/api/surveillance/pathogens",
        json={"org_id": org["id"], "pathogen_name": "诺如病毒", "tested_count": 5,
              "positive_count": 8, "record_date": "2026-08-12"},
        headers=admin,
    )
    assert resp.status_code == 422


def test_小样本高阳性率不进预警(client, admin, org):
    """送检 2 例检出 1 例是 50%，但这个数字没有意义。"""
    client.post(
        "/api/surveillance/pathogens",
        json={"org_id": org["id"], "pathogen_name": "小样本病原", "tested_count": 2,
              "positive_count": 1, "record_date": "2026-08-12"},
        headers=admin,
    )
    client.post(
        "/api/surveillance/pathogens",
        json={"org_id": org["id"], "pathogen_name": "大样本病原", "tested_count": 50,
              "positive_count": 12, "record_date": "2026-08-12"},
        headers=admin,
    )
    alerts = client.get(
        "/api/surveillance/alerts", params={"today": "2026-08-12", "days": 7}, headers=admin
    ).json()
    names = [p["pathogen_name"] for p in alerts["pathogen_alerts"]]
    assert "大样本病原" in names
    assert "小样本病原" not in names


def test_两路信号分列不做综合评分(client, admin):
    """把两路压成一个分数，看的人就说不清是哪一路在响——
    而处置动作恰恰取决于此。"""
    alerts = client.get(
        "/api/surveillance/alerts", params={"today": "2026-08-12"}, headers=admin
    ).json()
    assert "syndrome_alerts" in alerts and "pathogen_alerts" in alerts
    assert "score" not in alerts
    assert alerts["syndrome_alerts"]  # 上面村卫生室那条应当在


# ================================================================ ㉖ 应急资源


def test_应急资源缺口与过期分开报(client, admin, org):
    """缺口是补货问题，过期是报废问题，处置的人不同。"""
    client.post(
        "/api/surveillance/resources",
        json={"org_id": org["id"], "resource_type": "material", "name": "N95口罩",
              "quantity": 50, "unit": "只", "min_quantity": 500},
        headers=admin,
    )
    client.post(
        "/api/surveillance/resources",
        json={"org_id": org["id"], "resource_type": "material", "name": "消毒液",
              "quantity": 100, "unit": "瓶", "min_quantity": 10,
              "expire_date": "2020-01-01"},
        headers=admin,
    )
    client.post(
        "/api/surveillance/resources",
        json={"org_id": org["id"], "resource_type": "team", "name": "流调队",
              "quantity": 8, "unit": "人", "contact": "13800000000"},
        headers=admin,
    )
    ready = client.get("/api/surveillance/resources/readiness", headers=admin).json()
    entry = [o for o in ready["orgs"] if o["org_id"] == org["id"]][0]
    assert [x["name"] for x in entry["below_min"]] == ["N95口罩"]
    assert [x["name"] for x in entry["expired"]] == ["消毒液"]
    assert ready["by_type"]["team"]["count"] == 1


def test_应急队伍不该有效期(client, admin, org):
    resp = client.post(
        "/api/surveillance/resources",
        json={"org_id": org["id"], "resource_type": "team", "name": "消杀队",
              "quantity": 5, "expire_date": "2030-01-01"},
        headers=admin,
    )
    assert resp.status_code == 422
