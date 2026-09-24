"""慢专病宣教推送按患者判可见性（P0-34）。

`POST /api/spd/edu-pushes` 的 docstring 写着「向纳管患者推送」，实际只看角色：任一机构的医生 /
公卫 / 经办把全县任意患者号填进 `patient_ids`（一次最多 1000 个），平台就替他给这些人**真发短信**、
投居民收件箱。2026-09-24 逐条判「按请求体里的患者号新建挂在患者上的行」时实测：与患者毫无关系的
乙院医生 201，短信照发。同一个文件、同一种"逗号分隔的患者号"表单的批量干预
（`create_interventions`）早就逐个判可见性。

照同文件口径逐个判并留痕（`resource="spd_edu"`，与居民端读宣教同一个词）。与批量干预不同的是**先判完再发**：
立即推送在循环里就把短信发出去了，判到第二个才 403 时，第一条短信已经送达、库里却回滚得一条不剩。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog
from app.sms import set_sms_provider
from app.spd.models import SpdEduPush


class _RecordingSms:
    name = "recording"

    def __init__(self):
        self.sent: list[str] = []

    def send(self, phone: str, content: str) -> bool:
        self.sent.append(phone)
        return True


@pytest.fixture
def sms():
    stub = _RecordingSms()
    set_sms_provider(stub)
    yield stub
    set_sms_provider(None)


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def edu_world(client):
    """甲院接诊过患者甲、乙院接诊过患者乙；两家各一名医生。"""
    admin = _login(client, "admin", "admin123")
    orgs, patients = {}, {}
    for key, name, id_card, phone in (("a", "宣教推送甲院", "320000198808086671", "13900000671"),
                                      ("b", "宣教推送乙院", "320000198808086672", "13900000672")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p034_doc_{key}", "password": "pw123456", "full_name": f"{name}医生",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
        patients[key] = client.post("/api/patients",
                                    json={"name": f"宣教推送患者{key}", "id_card": id_card, "phone": phone},
                                    headers=admin).json()["id"]
    heads = {key: _login(client, f"p034_doc_{key}") for key in orgs}
    for key in orgs:
        enc = client.post("/api/encounters",
                          json={"patient_id": patients[key], "org_id": orgs[key], "encounter_type": "outpatient"},
                          headers=heads[key])
        assert enc.status_code == 201, enc.text
    material = client.post("/api/spd/edu-materials",
                           json={"code": "p034_edu", "title": "限盐宣教", "content": "每日盐不超过5克"},
                           headers=admin).json()
    return {"h": heads, "patients": patients, "material_id": material["id"]}


def _pushes(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(SpdEduPush).filter(SpdEduPush.patient_id == patient_id).count()
    finally:
        db.close()


def _logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "spd_edu"
        ).count()
    finally:
        db.close()


def _push(client, edu_world, who, patient_ids):
    return client.post("/api/spd/edu-pushes",
                       json={"material_id": edu_world["material_id"], "patient_ids": patient_ids, "channel": "sms"},
                       headers=edu_world["h"][who])


def test_无关机构推给别家患者被拒且一条短信都不发(client, edu_world, sms):
    pid = edu_world["patients"]["a"]
    before = _pushes(pid)
    r = _push(client, edu_world, "b", [pid])
    assert r.status_code == 403, r.text
    assert sms.sent == [], "被拒的推送照样把短信发出去了"
    assert _pushes(pid) == before


def test_一批里混进一个无关患者_整批不发(client, edu_world, sms):
    """判到第二个才 403 时第一条短信不能已经发出去——短信发出去就收不回来了。"""
    mine, theirs = edu_world["patients"]["a"], edu_world["patients"]["b"]
    before = _pushes(mine)
    r = _push(client, edu_world, "a", [mine, theirs])
    assert r.status_code == 403, r.text
    assert sms.sent == [], "先发后判：本院患者的短信在 403 之前已经发出"
    assert _pushes(mine) == before


def test_有关系照常推且逐人留痕(client, edu_world, sms):
    pid = edu_world["patients"]["a"]
    before = _logs(pid)
    r = _push(client, edu_world, "a", [pid])
    assert r.status_code == 201, r.text
    assert r.json() == {"pushed": 1, "sent": 1, "failed": 0, "material": "限盐宣教"}
    assert sms.sent == ["13900000671"]
    assert _logs(pid) == before + 1


def test_素材不存在照旧404(client, edu_world, sms):
    r = client.post("/api/spd/edu-pushes",
                    json={"material_id": 987654, "patient_ids": [edu_world["patients"]["a"]]},
                    headers=edu_world["h"]["b"])
    assert r.status_code == 404, r.text
