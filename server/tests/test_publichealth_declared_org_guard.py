"""传染病报告、慢病建档不得以别家机构名义写入（P0-35 第三批）。

两个端点都由请求体声明「这是哪家机构的」（报告机构 `org_id`、管理机构 `managed_by_org_id`），
只查这家机构存不存在。2026-09-24 实测：乙院医生以甲院名义报告传染病、建慢病档，两条都 201——

- 传染病报告挂在甲院名下进多点预警：预警按「几家机构报了同一病种」计，**一个人以几家机构的
  名义各报一例，就能凭空触发多点预警**。同一件事在症候群上报上早就守住了（`test_不得以别家机构名义写入`）；
- 慢病档案进甲院的管理人数与随访任务。

照 `assert_org_writable` 的既定口径：非全域角色只能以本机构名义写（用户手册：「只能写本机构的；
确需代录请用管理层 / 管理员账号」）。

同一形状的**家医签约**（`contracts.sign`，签约机构同样由请求声明）实测同样 201，但**不在本批**：
它早已登记为 P1-45「已实测越权但故意未修，等业务裁定」——县医院经办代乡镇院登记签约可能是正在用的
合法流程，一刀切会掐掉它。在棘轮里单列为待裁定。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import ChronicPatient, InfectiousCase


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


_seq = itertools.count(1)


@pytest.fixture(scope="module")
def ph_world(client):
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "公卫声明甲院"), ("b", "公卫声明乙院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p035c_doc_{key}", "password": "pw123456", "full_name": f"{name}医生",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text

    def new_patient() -> int:
        n = next(_seq)
        return client.post("/api/patients", json={"name": f"公卫声明患者{n}", "id_card": f"3200001990020266{70 + n}"},
                           headers=admin).json()["id"]

    return {"orgs": orgs, "new_patient": new_patient, "h": {k: _login(client, f"p035c_doc_{k}") for k in orgs}}


def _count(model, **filters) -> int:
    db = SessionLocal()
    try:
        q = db.query(model)
        for col, value in filters.items():
            q = q.filter(getattr(model, col) == value)
        return q.count()
    finally:
        db.close()


def _case(kind, org_id, patient_id):
    """(接口, 请求体, 落库的表, 用于计数的过滤条件)"""
    if kind == "传染病报告":
        return ("/api/infectious/cases",
                {"org_id": org_id, "disease_code": "J11", "disease_name": "流行性感冒", "onset_date": "2026-09-20"},
                InfectiousCase, {"org_id": org_id})
    return ("/api/chronic",
            {"patient_id": patient_id, "disease": "hypertension", "managed_by_org_id": org_id},
            ChronicPatient, {"managed_by_org_id": org_id, "patient_id": patient_id})


KINDS = ["传染病报告", "慢病建档"]


@pytest.mark.parametrize("kind", KINDS)
def test_不得以别家机构名义写入(client, ph_world, kind):
    url, body, model, filters = _case(kind, ph_world["orgs"]["a"], ph_world["new_patient"]())
    before = _count(model, **filters)
    r = client.post(url, json=body, headers=ph_world["h"]["b"])
    assert r.status_code == 403, (kind, r.text)
    assert "机构名义" in r.json()["detail"], r.text
    assert _count(model, **filters) == before, f"{kind} 被拒却落了库"


@pytest.mark.parametrize("kind", KINDS)
def test_以本机构名义照常写入(client, ph_world, kind):
    url, body, model, filters = _case(kind, ph_world["orgs"]["a"], ph_world["new_patient"]())
    before = _count(model, **filters)
    r = client.post(url, json=body, headers=ph_world["h"]["a"])
    assert r.status_code == 201, (kind, r.text)
    assert _count(model, **filters) == before + 1
