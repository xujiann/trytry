"""住院与床位 `/api/inpatient` 平台侧 14 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_esb_contract.py` / `test_vaccine_supply_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。已治理的 2 个执行记录端点（ExecutionOut）不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **Money 列 `int | float`**：`total_cost` 6000 读回 int、`drug_cost` 1200.5 是
  float——声明成 float 会把病案首页上的「6000 元」印成「6000.0 元」。
- **Float 列恒 float**：`drg_weight` 是 Float 列（0.95），`drg.weight` 同源；
  `occupancy_pct` 是 `*100.0` 真除法或兜底 `0.0`——这些声明 float 才是原样。
- **病案首页两种形状**：POST 回执在 11 键之外恒多一个尾键 `drg`
  （M12 在位时 `assign_drg_group` 的入组结果，固定 6 键）；GET 回读只有 11 键。
  `drg` 按「可选 + exclude_unset」建模镜像代码里的 ImportError 分支
  （M12 摘除时键整个不出现）——该分支在本仓库不可达，故此处只钉"在"的一侧。
- 入院/转科/出院回执与列表行**同形**（`_admission_out` 唯一产地）；
  医嘱停止的两条产地：手工停止带 `stopped_by_name`，出院批量停止**不带**
  （bulk UPDATE 不回填姓名，保持空串）——两条都钉住。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

WARD_KEYS = ["id", "org_id", "name"]
BED_KEYS = ["id", "ward_id", "bed_no", "status"]
ADMISSION_KEYS = [
    "id", "patient_id", "org_id", "ward_id", "bed_id", "doctor_name",
    "diagnosis_name", "status", "admitted_at", "discharged_at",
]
CASE_KEYS = [
    "id", "admission_id", "discharge_diagnosis", "operation", "total_cost",
    "drug_cost", "outcome", "note", "drg_code", "drg_weight", "created_by_name",
]
DRG_KEYS = ["drg_code", "drg_name", "mdc", "mdc_name", "weight", "fallback"]
ORDER_KEYS = [
    "id", "admission_id", "order_type", "content", "status",
    "created_by_name", "stopped_by_name", "created_at", "stopped_at",
]
STAT_KEYS = [
    "org_id", "org_name", "beds_total", "beds_occupied", "occupancy_pct",
    "in_hospital", "discharged_total",
]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def seed(client, admin):
    """一次种完全部场景，测试只做断言。

    时间线：W1/W2 两病区、B1/B2 两床 → P1 入院 B1 → 转科 W2/B2 → 开两条医嘱
    （O1 手工停止、O2 留给出院批量停止）→ 病案首页（肺炎 → ES31 自动入组）→
    出院（释放 B2）→ P2 入院 B1（在院，供统计与过滤）→ O3（在院医嘱）。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约住院医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    client.post(
        "/api/users",
        json={"username": "ipct_doc", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    data["doctor"] = login(client, "ipct_doc", "pass123456")
    data["p1"] = client.post(
        "/api/patients",
        json={"name": "契约住院患者一", "id_card": "330881199001018801"},
        headers=admin,
    ).json()
    data["p2"] = client.post(
        "/api/patients",
        json={"name": "契约住院患者二", "id_card": "330881199001018802"},
        headers=admin,
    ).json()

    resp = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "契约一病区"}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    data["w1"] = resp.json()
    data["w2"] = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "契约二病区"}, headers=admin
    ).json()
    resp = client.post(
        "/api/inpatient/beds", json={"ward_id": data["w1"]["id"], "bed_no": "A-01"}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    data["b1"] = resp.json()
    data["b2"] = client.post(
        "/api/inpatient/beds", json={"ward_id": data["w2"]["id"], "bed_no": "B-01"}, headers=admin
    ).json()

    resp = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": data["p1"]["id"], "ward_id": data["w1"]["id"],
              "bed_id": data["b1"]["id"], "doctor_name": "契约医师", "diagnosis_name": "社区获得性肺炎"},
        headers=data["doctor"],
    )
    assert resp.status_code == 201, resp.text
    data["a1"] = resp.json()
    data["a1_transferred"] = client.post(
        f"/api/inpatient/admissions/{data['a1']['id']}/transfer",
        json={"ward_id": data["w2"]["id"], "bed_id": data["b2"]["id"]},
        headers=data["doctor"],
    ).json()

    resp = client.post(
        "/api/inpatient/orders",
        json={"admission_id": data["a1"]["id"], "order_type": "long", "content": "一级护理"},
        headers=data["doctor"],
    )
    assert resp.status_code == 201, resp.text
    data["o1"] = resp.json()
    data["o1_stopped"] = client.post(
        f"/api/inpatient/orders/{data['o1']['id']}/stop", headers=data["doctor"]
    ).json()
    data["o2"] = client.post(
        "/api/inpatient/orders",
        json={"admission_id": data["a1"]["id"], "order_type": "temp", "content": "青霉素皮试"},
        headers=data["doctor"],
    ).json()

    resp = client.post(
        f"/api/inpatient/admissions/{data['a1']['id']}/case-summary",
        json={"discharge_diagnosis": "社区获得性肺炎", "total_cost": 6000,
              "drug_cost": 1200.5, "outcome": "治愈"},
        headers=data["doctor"],
    )
    assert resp.status_code == 201, resp.text
    data["case"] = resp.json()

    data["a1_discharged"] = client.post(
        f"/api/inpatient/admissions/{data['a1']['id']}/discharge", headers=data["doctor"]
    ).json()

    data["a2"] = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": data["p2"]["id"], "ward_id": data["w1"]["id"],
              "bed_id": data["b1"]["id"], "diagnosis_name": "腰椎间盘突出"},
        headers=data["doctor"],
    ).json()
    data["o3"] = client.post(
        "/api/inpatient/orders",
        json={"admission_id": data["a2"]["id"], "order_type": "long", "content": "二级护理"},
        headers=data["doctor"],
    ).json()
    return data


# ---------------------------------------------------------------- 病区/床位


def test_病区回执精确与列表同形(client, admin, seed):
    body = seed["w1"]
    assert list(body.keys()) == WARD_KEYS
    assert body == {"id": body["id"], "org_id": seed["org"]["id"], "name": "契约一病区"}
    rows = client.get("/api/inpatient/wards", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [WARD_KEYS] * 2
    assert rows == [seed["w1"], seed["w2"]]  # id 升序
    assert client.get(
        f"/api/inpatient/wards?org_id={seed['org']['id']}", headers=admin
    ).json() == rows


def test_床位回执精确_列表反映占用状态(client, admin, seed):
    body = seed["b1"]
    assert list(body.keys()) == BED_KEYS
    assert body == {
        "id": body["id"], "ward_id": seed["w1"]["id"], "bed_no": "A-01", "status": "free",
    }
    # 终态：B1 被 P2 占用；B2 随 P1 出院释放
    rows = client.get("/api/inpatient/beds", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [BED_KEYS] * 2
    assert rows == [
        {**seed["b1"], "status": "occupied"},
        seed["b2"],
    ]
    assert client.get("/api/inpatient/beds?status=free", headers=admin).json() == [seed["b2"]]
    assert client.get(
        f"/api/inpatient/beds?ward_id={seed['w1']['id']}", headers=admin
    ).json() == [{**seed["b1"], "status": "occupied"}]


# ---------------------------------------------------------------- 入出转


def test_入院回执精确形状与键序(seed):
    body = seed["a1"]
    assert list(body.keys()) == ADMISSION_KEYS
    assert body == {
        "id": body["id"],
        "patient_id": seed["p1"]["id"],
        "org_id": seed["org"]["id"],
        "ward_id": seed["w1"]["id"],
        "bed_id": seed["b1"]["id"],
        "doctor_name": "契约医师",
        "diagnosis_name": "社区获得性肺炎",
        "status": "admitted",
        "admitted_at": body["admitted_at"],
        "discharged_at": None,
    }
    assert isinstance(body["admitted_at"], str)


def test_转科回执_仅病区床位变(seed):
    assert seed["a1_transferred"] == {
        **seed["a1"], "ward_id": seed["w2"]["id"], "bed_id": seed["b2"]["id"],
    }


def test_出院回执与住院列表同形_过滤(client, admin, seed):
    body = seed["a1_discharged"]
    assert list(body.keys()) == ADMISSION_KEYS
    assert body == {
        **seed["a1_transferred"], "status": "discharged", "discharged_at": body["discharged_at"],
    }
    assert isinstance(body["discharged_at"], str)
    rows = client.get("/api/inpatient/admissions", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [ADMISSION_KEYS] * 2
    assert rows == [seed["a2"], body]  # id 倒序
    assert client.get("/api/inpatient/admissions?status=admitted", headers=admin).json() == [
        seed["a2"]
    ]
    assert client.get(
        f"/api/inpatient/admissions?patient_id={seed['p1']['id']}", headers=admin
    ).json() == [body]


# ---------------------------------------------------------------- 病案首页


def test_病案首页回执_11键加drg尾键_Money与Float之别(seed):
    body = seed["case"]
    assert list(body.keys()) == CASE_KEYS + ["drg"]
    assert list(body["drg"].keys()) == DRG_KEYS
    assert body == {
        "id": body["id"],
        "admission_id": seed["a1"]["id"],
        "discharge_diagnosis": "社区获得性肺炎",
        "operation": "",
        "total_cost": 6000,
        "drug_cost": 1200.5,
        "outcome": "治愈",
        "note": "",
        "drg_code": "ES31",
        "drg_weight": 0.95,
        "created_by_name": "ipct_doc",
        "drg": {
            "drg_code": "ES31",
            "drg_name": "呼吸系统感染（肺炎）",
            "mdc": "MDCE",
            "mdc_name": "呼吸系统疾病及功能障碍",
            "weight": 0.95,
            "fallback": False,
        },
    }
    # Money 列：整数金额读回 int（声明 float 会印成 6000.0）；Float 列恒 float
    assert type(body["total_cost"]) is int
    assert isinstance(body["drug_cost"], float)
    assert isinstance(body["drg_weight"], float) and isinstance(body["drg"]["weight"], float)


def test_病案首页回读_只有11键无drg(client, admin, seed):
    body = client.get(
        f"/api/inpatient/admissions/{seed['a1']['id']}/case-summary", headers=admin
    ).json()
    assert list(body.keys()) == CASE_KEYS
    assert body == {k: v for k, v in seed["case"].items() if k != "drg"}
    assert type(body["total_cost"]) is int and isinstance(body["drg_weight"], float)


# ---------------------------------------------------------------- 住院医嘱


def test_医嘱回执_开立与两种停止形状(seed):
    body = seed["o1"]
    assert list(body.keys()) == ORDER_KEYS
    assert body == {
        "id": body["id"],
        "admission_id": seed["a1"]["id"],
        "order_type": "long",
        "content": "一级护理",
        "status": "active",
        "created_by_name": "ipct_doc",
        "stopped_by_name": "",
        "created_at": body["created_at"],
        "stopped_at": None,
    }
    # 手工停止：回填停止人与时间
    stopped = seed["o1_stopped"]
    assert stopped == {
        **body, "status": "stopped", "stopped_by_name": "ipct_doc",
        "stopped_at": stopped["stopped_at"],
    }
    assert isinstance(stopped["stopped_at"], str)


def test_医嘱列表_出院批量停止不回填姓名(client, admin, seed):
    rows = client.get(
        f"/api/inpatient/orders?admission_id={seed['a1']['id']}", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [ORDER_KEYS] * 2
    # O2 被出院批量停止：status/stopped_at 变了，stopped_by_name 保持空串
    assert rows[0] == {
        **seed["o2"], "status": "stopped", "stopped_at": rows[0]["stopped_at"],
    }
    assert rows[0]["stopped_by_name"] == "" and isinstance(rows[0]["stopped_at"], str)
    assert rows[1] == seed["o1_stopped"]
    assert client.get("/api/inpatient/orders?status=active", headers=admin).json() == [
        seed["o3"]
    ]
    assert client.get("/api/inpatient/orders", headers=admin).json() == [
        seed["o3"], rows[0], seed["o1_stopped"]
    ]


# ---------------------------------------------------------------- 床位效率统计


def test_床位统计精确_int与float之别(client, admin, seed):
    rows = client.get("/api/inpatient/stats", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [STAT_KEYS]
    expected = {
        "org_id": seed["org"]["id"],
        "org_name": "契约住院医院",
        "beds_total": 2,
        "beds_occupied": 1,
        "occupancy_pct": 50.0,
        "in_hospital": 1,
        "discharged_total": 1,
    }
    assert rows == [expected]
    assert client.get(
        f"/api/inpatient/stats?org_id={seed['org']['id']}", headers=admin
    ).json() == [expected]
    row = rows[0]
    # 计数恒 int；使用率是 *100.0 真除法（或兜底 0.0）：恒 float
    for key in ("beds_total", "beds_occupied", "in_hospital", "discharged_total"):
        assert type(row[key]) is int, key
    assert isinstance(row["occupancy_pct"], float)


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.post("/api/inpatient/wards",
                    json={"org_id": 999999, "name": "无主病区"}, headers=admin),  # 404
        client.post("/api/inpatient/wards",
                    json={"org_id": seed["org"]["id"], "name": "契约一病区"}, headers=admin),  # 409
        client.post("/api/inpatient/beds",
                    json={"ward_id": 999999, "bed_no": "X-01"}, headers=admin),  # 404
        client.post("/api/inpatient/beds",
                    json={"ward_id": seed["w1"]["id"], "bed_no": "A-01"}, headers=admin),  # 409
        client.post("/api/inpatient/admissions",
                    json={"patient_id": 999999, "ward_id": seed["w1"]["id"],
                          "bed_id": seed["b1"]["id"]}, headers=seed["doctor"]),  # 404
        client.post("/api/inpatient/admissions",
                    json={"patient_id": seed["p2"]["id"], "ward_id": seed["w1"]["id"],
                          "bed_id": seed["b1"]["id"]}, headers=seed["doctor"]),  # 已在院 409
        client.post(f"/api/inpatient/admissions/{seed['a1']['id']}/transfer",
                    json={"ward_id": seed["w2"]["id"], "bed_id": seed["b2"]["id"]},
                    headers=seed["doctor"]),  # 已出院 409
        client.post(f"/api/inpatient/admissions/{seed['a2']['id']}/transfer",
                    json={"ward_id": seed["w1"]["id"], "bed_id": seed["b1"]["id"]},
                    headers=seed["doctor"]),  # 同床 422
        client.post(f"/api/inpatient/admissions/{seed['a1']['id']}/case-summary",
                    json={"discharge_diagnosis": "重复"}, headers=seed["doctor"]),  # 409
        client.post(f"/api/inpatient/admissions/{seed['a2']['id']}/case-summary",
                    json={"discharge_diagnosis": "药费超总额", "total_cost": 1, "drug_cost": 2},
                    headers=seed["doctor"]),  # 422
        client.get(f"/api/inpatient/admissions/{seed['a2']['id']}/case-summary",
                   headers=admin),  # 未填写 404
        client.post(f"/api/inpatient/admissions/{seed['a1']['id']}/discharge",
                    headers=seed["doctor"]),  # 已出院 409
        client.post("/api/inpatient/orders",
                    json={"admission_id": seed["a1"]["id"], "order_type": "temp",
                          "content": "出院后补开"}, headers=seed["doctor"]),  # 409
        client.post(f"/api/inpatient/orders/{seed['o1']['id']}/stop",
                    headers=seed["doctor"]),  # 已停止 409
        client.post("/api/inpatient/orders/999999/stop", headers=seed["doctor"]),  # 404
    ]
    assert [r.status_code for r in cases] == [
        404, 409, 404, 409, 404, 409, 409, 422, 409, 422, 404, 409, 409, 409, 404
    ]
    for r in cases:
        assert set(r.json()) == {"detail"}
