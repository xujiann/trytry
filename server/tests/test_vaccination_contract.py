"""疫苗接种 `/api/vaccination` 四个待治理端点（禁忌簇 + 接种前评估）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。records 两端点已治理（RecordOut），仅作剂次种子。

本簇的建模判断（都以此处的精确断言为依据）：

- `_contra_out` 是禁忌出参的**唯一产地**（登记回执 / 清单行 / 解除回执 /
  pre-check 的 inactive 行同形），十一键一个模型。
- `valid_until` 是 `String(10)` 非空列：空串可达（长期禁忌），恒 str 非 null。
- `lifted_at` 是 `DateTime` 可空列且**不经手工 isoformat 直接透出**——未解除为
  null，解除后为 ISO 串（微秒在内），本网将响应值与 DB `isoformat()` 逐字符
  回绑，钉住序列化字节格式（medwaste `stored_at` 同款先例）。
- `expired`/`blocking` 由日期现算（过期不改状态）：today 覆盖参数下两键翻转，
  两分支都要钉。
- pre-check 五键恒在，无条件键；`contraindications` 是**字符串列表**（只有
  reason），与 inactive 的整行不同——别把两者建成同一个形状。
- 本簇无 Money/Float 出参，数值全 int。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import VaccineContraindication

CONTRA_KEYS = [
    "id", "patient_id", "vaccine_code", "reason", "contra_type", "status",
    "valid_until", "expired", "blocking", "lift_reason", "lifted_at",
]
PRECHECK_KEYS = [
    "allowed", "contraindications", "inactive_contraindications", "previous_doses", "next_dose_no",
]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "疫苗契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约受种者", "id_card": "330281199203038897", "gender": "女",
              "birth_date": "1992-03-03"},
        headers=admin,
    ).json()
    return {"org": org, "patient": patient}


@pytest.fixture(scope="module")
def contras(client, admin, base):
    pid = base["patient"]["id"]
    temp = client.post(
        "/api/vaccination/contraindications",
        json={"patient_id": pid, "vaccine_code": "HPV9", "reason": "急性发热",
              "contra_type": "temporary", "valid_until": "2026-09-15"},
        headers=admin,
    )
    assert temp.status_code == 201, temp.text
    perm = client.post(
        "/api/vaccination/contraindications",
        json={"patient_id": pid, "vaccine_code": "HPV9", "reason": "既往严重过敏",
              "contra_type": "permanent"},
        headers=admin,
    )
    assert perm.status_code == 201, perm.text
    return {"temp": temp.json(), "perm": perm.json()}


def test_登记回执精确形状与键序(base, contras):
    pid = base["patient"]["id"]
    body = contras["temp"]
    assert list(body.keys()) == CONTRA_KEYS
    assert body == {
        "id": body["id"],
        "patient_id": pid,
        "vaccine_code": "HPV9",
        "reason": "急性发热",
        "contra_type": "temporary",
        "status": "active",
        "valid_until": "2026-09-15",
        "expired": False,
        "blocking": True,
        "lift_reason": "",
        "lifted_at": None,  # DateTime 可空列：未解除为 null，不是空串
    }
    # 长期禁忌：valid_until 空串可达（String 非空列），不是 null
    assert contras["perm"] == {
        "id": contras["perm"]["id"],
        "patient_id": pid,
        "vaccine_code": "HPV9",
        "reason": "既往严重过敏",
        "contra_type": "permanent",
        "status": "active",
        "valid_until": "",
        "expired": False,
        "blocking": True,
        "lift_reason": "",
        "lifted_at": None,
    }
    assert type(body["id"]) is int and type(body["expired"]) is bool


def test_清单精确_过期按日期现算不改状态(client, admin, base, contras):
    pid = base["patient"]["id"]
    rows = client.get(
        f"/api/vaccination/contraindications?patient_id={pid}&today=2026-12-01", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [CONTRA_KEYS] * 2  # id 倒序
    # 暂时禁忌已过有效期：status 仍 active（不改行），expired/blocking 由日期现算翻转
    assert rows == [
        contras["perm"],
        {**contras["temp"], "expired": True, "blocking": False},
    ]
    # 未过期视角：与登记回执逐字节一致
    fresh = client.get(
        f"/api/vaccination/contraindications?patient_id={pid}&today=2026-09-01", headers=admin
    ).json()
    assert fresh == [contras["perm"], contras["temp"]]
    assert client.get(
        f"/api/vaccination/contraindications?patient_id={pid}&vaccine_code=FLU", headers=admin
    ).json() == []


@pytest.fixture(scope="module")
def lifted(client, admin, contras):
    body = client.post(
        f"/api/vaccination/contraindications/{contras['perm']['id']}/lift",
        json={"lift_reason": "脱敏治疗后复评通过"},
        headers=admin,
    )
    assert body.status_code == 200, body.text
    return body.json()


def test_解除回执精确_lifted_at字节格式回绑DB(base, contras, lifted):
    assert list(lifted.keys()) == CONTRA_KEYS
    with SessionLocal() as db:
        row = db.get(VaccineContraindication, contras["perm"]["id"])
        db_iso = row.lifted_at.isoformat()
    # 序列化字节格式 = datetime.isoformat()（微秒在内），逐字符回绑
    assert lifted["lifted_at"] == db_iso and type(lifted["lifted_at"]) is str
    assert lifted == {
        **contras["perm"],
        "status": "lifted",
        "blocking": False,
        "lift_reason": "脱敏治疗后复评通过",
        "lifted_at": db_iso,
    }


def test_清单_include_lifted开关(client, admin, base, contras, lifted):
    pid = base["patient"]["id"]
    only_active = client.get(
        f"/api/vaccination/contraindications?patient_id={pid}&include_lifted=false&today=2026-09-01",
        headers=admin,
    ).json()
    assert only_active == [contras["temp"]]
    assert client.get(
        f"/api/vaccination/contraindications?patient_id={pid}&today=2026-09-01", headers=admin
    ).json() == [lifted, contras["temp"]]


def test_接种前评估_三分支精确(client, admin, base, contras, lifted):
    pid = base["patient"]["id"]
    # 干净分支：无禁忌无剂次（另一疫苗）
    clean = client.get(
        f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=FLU&today=2026-09-01",
        headers=admin,
    ).json()
    assert list(clean.keys()) == PRECHECK_KEYS
    assert clean == {
        "allowed": True,
        "contraindications": [],
        "inactive_contraindications": [],
        "previous_doses": 0,
        "next_dose_no": 1,
    }
    # 拦截分支：暂时禁忌生效中；已解除的长期禁忌进 inactive（整行 _contra_out 形）
    blocked = client.get(
        f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=HPV9&today=2026-09-01",
        headers=admin,
    ).json()
    assert blocked == {
        "allowed": False,
        "contraindications": ["急性发热"],  # 只有 reason 的字符串列表
        "inactive_contraindications": [lifted],
        "previous_doses": 0,
        "next_dose_no": 1,
    }
    # 过期分支：有效期已过按日期现算放行，过期行进 inactive 且 expired 翻转
    expired = client.get(
        f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=HPV9&today=2026-12-01",
        headers=admin,
    ).json()
    assert expired == {
        "allowed": True,
        "contraindications": [],
        "inactive_contraindications": [lifted, {**contras["temp"], "expired": True, "blocking": False}],
        "previous_doses": 0,
        "next_dose_no": 1,
    }


def test_接种前评估_剂次联动(client, admin, base):
    pid = base["patient"]["id"]
    dose = client.post(
        "/api/vaccination/records",
        json={"patient_id": pid, "vaccine_code": "FLU", "vaccine_name": "流感疫苗",
              "vaccinated_date": "2026-08-01", "org_id": base["org"]["id"]},
        headers=admin,
    )
    assert dose.status_code == 201, dose.text
    body = client.get(
        f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=FLU&today=2026-09-01",
        headers=admin,
    ).json()
    assert body["previous_doses"] == 1 and body["next_dose_no"] == 2
    assert type(body["previous_doses"]) is int and type(body["next_dose_no"]) is int
    assert client.get(
        "/api/vaccination/pre-check?patient_id=999999&vaccine_code=FLU", headers=admin
    ).status_code == 404
