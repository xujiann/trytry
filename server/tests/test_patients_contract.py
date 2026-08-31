"""患者主索引 `/api/patients` 四个待治理端点（档案调阅授权簇）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。已治理的三个端点（PatientOut）不在治理范围，但**核心档案的
脱敏口径必须一并钉住**：本模块出参含 privacy 脱敏字段（admin 明文 / 非 admin
掩码），契约只声明形状、不碰 `desensitize` 一行——admin 与非 admin 两种视角
在这里各钉一遍，防止治理动到脱敏字节。

本簇的建模判断（都以此处的精确断言为依据）：

- 授权回执四键（id/patient_id/scope/status）与撤销回执两键（id/status）
  **不同形，两个模型**，不互相注入。
- 授权清单行五键（id/grantee_org_id/scope/expire_date/status），`expire_date`
  是 `String(10)` 非空列——空串可达（长期授权），恒为 str 不是 null。
- 校验端点四键恒在（patient_id/org_id/scope/allowed），无条件键。
- 本簇无 Money/Float 出参，数值全 int；无条件键，无需 exclude_unset。
- `list_authorizations` / `check_authorization` 的 **AccessLog 留痕语义**
  （resource=authorization, basis=consent_admin，"可问责而非可阻断"，404 之前
  也留痕）在此逐条钉住——治理不许动 visibility/AccessLog 一行。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import AccessLog

PATIENT_KEYS = ["name", "id_card", "gender", "birth_date", "phone", "id", "ehc_no"]
GRANT_RECEIPT_KEYS = ["id", "patient_id", "scope", "status"]
AUTH_ROW_KEYS = ["id", "grantee_org_id", "scope", "expire_date", "status"]
CHECK_KEYS = ["patient_id", "org_id", "scope", "allowed"]


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
def base(client, admin):
    lead = client.post(
        "/api/organizations",
        json={"name": "患者契约总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "患者契约卫生院", "org_type": "township", "level": "township",
              "parent_id": lead["id"]},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约患者", "id_card": "330281199001015566", "gender": "男",
              "birth_date": "1990-01-01", "phone": "13800001111"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/users",
        json={"username": "pat_doc", "password": "passw0rd1", "role": "doctor",
              "org_id": lead["id"]},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    doctor = login(client, "pat_doc", "passw0rd1")
    return {"lead": lead, "township": township, "patient": patient, "doctor": doctor}


def _auth_access_rows(patient_id):
    with SessionLocal() as db:
        return [
            (r.username, r.patient_id, r.resource, r.basis)
            for r in db.query(AccessLog)
            .filter(AccessLog.resource == "authorization", AccessLog.patient_id == patient_id)
            .order_by(AccessLog.id)
            .all()
        ]


# -------------------------------------------------- 脱敏口径（admin/非 admin 两视角）


def test_档案脱敏_admin明文非admin掩码逐字节(client, admin, base):
    """已治理端点的特征化网：治理授权簇不许动 privacy 一个字节。"""
    p = base["patient"]
    mine = client.get(f"/api/patients/{p['ehc_no']}", headers=admin).json()
    assert list(mine.keys()) == PATIENT_KEYS
    assert mine == {
        "name": "契约患者",
        "id_card": "330281199001015566",  # admin 明文
        "gender": "男",
        "birth_date": "1990-01-01",
        "phone": "13800001111",
        "id": p["id"],
        "ehc_no": p["ehc_no"],
    }
    masked = client.get(f"/api/patients/{p['ehc_no']}", headers=base["doctor"]).json()
    assert list(masked.keys()) == PATIENT_KEYS
    assert masked == {
        "name": "契约患者",
        "id_card": "3302**********5566",  # 保留前4后4
        "gender": "男",
        "birth_date": "1990-01-01",
        "phone": "138******11",  # 保留前3后2
        "id": p["id"],
        "ehc_no": p["ehc_no"],
    }
    search = client.get("/api/patients?keyword=契约患者", headers=base["doctor"])
    assert search.headers["x-total-count"] == "1"
    assert search.json() == [masked]
    assert client.get("/api/patients?keyword=契约患者", headers=admin).json() == [mine]


# -------------------------------------------------- 授权（发放/清单/校验/撤销）


@pytest.fixture(scope="module")
def grants(client, admin, base):
    """admin 与 doctor 各发一笔授权（角色矩阵：doctor/operator 可代录，admin 直通）。"""
    pid = base["patient"]["id"]
    g1 = client.post(
        f"/api/patients/{pid}/authorizations",
        json={"grantee_org_id": base["township"]["id"], "scope": "encounter",
              "expire_date": "2026-12-31"},
        headers=admin,
    )
    assert g1.status_code == 201, g1.text
    g2 = client.post(
        f"/api/patients/{pid}/authorizations",
        json={"grantee_org_id": base["township"]["id"], "scope": "all",
              "expire_date": "2027-06-30"},
        headers=base["doctor"],
    )
    assert g2.status_code == 201, g2.text
    return {"g1": g1.json(), "g2": g2.json()}


def test_授权回执精确形状与键序(base, grants):
    pid = base["patient"]["id"]
    assert list(grants["g1"].keys()) == GRANT_RECEIPT_KEYS
    assert grants["g1"] == {
        "id": grants["g1"]["id"], "patient_id": pid, "scope": "encounter", "status": "active"
    }
    assert type(grants["g1"]["id"]) is int
    assert grants["g2"] == {
        "id": grants["g2"]["id"], "patient_id": pid, "scope": "all", "status": "active"
    }


def test_授权清单精确_id倒序且留痕(client, admin, base, grants):
    pid = base["patient"]["id"]
    before = len(_auth_access_rows(pid))
    rows = client.get(f"/api/patients/{pid}/authorizations", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [AUTH_ROW_KEYS] * 2
    assert rows == [
        {"id": grants["g2"]["id"], "grantee_org_id": base["township"]["id"], "scope": "all",
         "expire_date": "2027-06-30", "status": "active"},
        {"id": grants["g1"]["id"], "grantee_org_id": base["township"]["id"], "scope": "encounter",
         "expire_date": "2026-12-31", "status": "active"},
    ]
    # 可问责而非可阻断：这一眼本身就要留痕（admin 也不豁免）
    after = _auth_access_rows(pid)
    assert len(after) == before + 1
    assert after[-1] == ("admin", pid, "authorization", "consent_admin")


def test_授权校验精确_四键与有效期口径(client, admin, base, grants):
    pid, org_id = base["patient"]["id"], base["township"]["id"]
    before = len(_auth_access_rows(pid))
    body = client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={org_id}&scope=encounter&today=2026-09-01",
        headers=admin,
    ).json()
    assert list(body.keys()) == CHECK_KEYS
    assert body == {"patient_id": pid, "org_id": org_id, "scope": "encounter", "allowed": True}
    # scope=exam 无专项授权，但 g2 是 scope=all → 仍放行（any(all or 同scope)）
    assert client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={org_id}&scope=exam&today=2026-09-01",
        headers=admin,
    ).json() == {"patient_id": pid, "org_id": org_id, "scope": "exam", "allowed": True}
    # 全部过期日之后：不放行
    assert client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={org_id}&scope=encounter&today=2027-07-01",
        headers=admin,
    ).json()["allowed"] is False
    assert len(_auth_access_rows(pid)) == before + 3  # 每问一次留一条
    # today 格式非法：422（resolve_business_date 口径）
    assert client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={org_id}&today=bad", headers=admin
    ).status_code == 422


def test_撤销回执精确_两键并即时失效(client, admin, base, grants):
    pid, org_id = base["patient"]["id"], base["township"]["id"]
    body = client.post(
        f"/api/patients/{pid}/authorizations/{grants['g2']['id']}/revoke", headers=admin
    ).json()
    assert list(body.keys()) == ["id", "status"]
    assert body == {"id": grants["g2"]["id"], "status": "revoked"}
    # scope=all 已撤销：exam 不再被 all 兜住
    assert client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={org_id}&scope=exam&today=2026-09-01",
        headers=admin,
    ).json() == {"patient_id": pid, "org_id": org_id, "scope": "exam", "allowed": False}


def test_授权四端点404分支(client, admin, base, grants):
    assert client.post(
        "/api/patients/999999/authorizations",
        json={"grantee_org_id": base["township"]["id"], "scope": "all", "expire_date": "2026-12-31"},
        headers=admin,
    ).status_code == 404
    assert client.post(
        f"/api/patients/{base['patient']['id']}/authorizations",
        json={"grantee_org_id": 999999, "scope": "all", "expire_date": "2026-12-31"},
        headers=admin,
    ).json() == {"detail": "被授权机构不存在"}
    # 撤销：auth 存在但 patient_id 不匹配 → 404
    assert client.post(
        f"/api/patients/999999/authorizations/{grants['g1']['id']}/revoke", headers=admin
    ).json() == {"detail": "授权记录不存在"}
    assert client.get("/api/patients/999999/authorizations", headers=admin).status_code == 404
