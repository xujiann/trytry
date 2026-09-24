"""转诊与会诊申请不得以别家机构名义发起（P0-35 第二批）。

两张单子都由请求体声明两个机构：发起方（`from_org_id`）与接收方 / 受邀方（`to_org_id`）。
端点只查两家存不存在，于是任一机构的医生能以**别家的名义**把患者转出去、替别家申请会诊。
2026-09-24 实测：乙院医生以甲院名义给甲院转诊、申请会诊，两条都 201。后果不止是一张假单子——
转诊与会诊的两方机构列都算「服务关系」，一张以别家名义建的单子让两家都凭空获得调阅这位患者的依据。

修法：发起方只能是本机构（`assert_org_writable`）；接收方 / 受邀方按设计就是别家，不在此列。
（发起之后哪一步由哪一方做，是 P0-31 / P1-71 待裁定的另一件事。）
"""
import pytest

from app.database import SessionLocal
from app.models import Consultation, Referral


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def collab_world(client):
    """甲卫生院、乙卫生院、丙县医院；甲乙各一名医生。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "协同发起甲院"), ("b", "协同发起乙院"), ("c", "协同接收丙县医院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    for key in ("a", "b"):
        r = client.post("/api/users",
                        json={"username": f"p035b_doc_{key}", "password": "pw123456", "full_name": f"p035b_doc_{key}",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    patient = client.post("/api/patients", json={"name": "协同发起患者", "id_card": "320000199001016671"},
                          headers=admin).json()
    return {"orgs": orgs, "patient_id": patient["id"],
            "h": {k: _login(client, f"p035b_doc_{k}") for k in ("a", "b")}}


CASES = [
    ("/api/referrals", Referral, lambda: {"direction": "up", "reason": "上转进一步诊治"}),
    ("/api/consultations", Consultation, lambda: {"question": "请协助判读心电图"}),
]


def _count(model, from_org_id, patient_id) -> int:
    db = SessionLocal()
    try:
        return db.query(model).filter(model.from_org_id == from_org_id, model.patient_id == patient_id).count()
    finally:
        db.close()


@pytest.mark.parametrize("url, model, extra", CASES, ids=["转诊", "会诊申请"])
def test_不得以别家机构名义发起(client, collab_world, url, model, extra):
    o, pid = collab_world["orgs"], collab_world["patient_id"]
    before = _count(model, o["a"], pid)
    r = client.post(url, json={"patient_id": pid, "from_org_id": o["a"], "to_org_id": o["c"], **extra()},
                    headers=collab_world["h"]["b"])
    assert r.status_code == 403, (url, r.text)
    assert "机构名义" in r.json()["detail"], r.text
    assert _count(model, o["a"], pid) == before, f"{url} 被拒却落了库"


@pytest.mark.parametrize("url, model, extra", CASES, ids=["转诊", "会诊申请"])
def test_以本机构名义发起给别家照常(client, collab_world, url, model, extra):
    """接收方 / 受邀方按设计就是别家：甲院发给丙院照常 201。"""
    o, pid = collab_world["orgs"], collab_world["patient_id"]
    before = _count(model, o["a"], pid)
    r = client.post(url, json={"patient_id": pid, "from_org_id": o["a"], "to_org_id": o["c"], **extra()},
                    headers=collab_world["h"]["a"])
    assert r.status_code == 201, (url, r.text)
    assert _count(model, o["a"], pid) == before + 1
