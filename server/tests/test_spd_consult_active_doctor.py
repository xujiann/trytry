"""居民发起专病咨询派给的是早先那份已结案档案的医生：结案后重新纳管的患者，咨询找不到现在的主管医生（P2-299）。

`start_consult` 新开会话时按「患者 + 病种」取纳管档案、不排序 `.first()`——同病种只有**在管**那份唯一（部分唯一索引），
结案（迁出 / 排除）后重新纳管的患者有两份：早先已结案的一份排在前面，咨询派给了当年的主管医生。
其余挂档案的业务（随访处置、干预、转诊积分……）都走 `service.enrollment_for`：写了病种的，在管的那份优先。

修法：同一个取法。没写病种的仍不挂医生（与原先一致）。
"""
import pytest

from app.config import settings
from app.database import SessionLocal

PHONE = "13912202299"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import Patient, ResidentAccount, User
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2299 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username in ("p2299_old_doc", "p2299_new_doc"):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": "doctor", "org_id": org})
        assert created.status_code == 201, created.text
    with SessionLocal() as db:
        old_doc, new_doc = (db.query(User.id).filter(User.username == u).scalar()
                            for u in ("p2299_old_doc", "p2299_new_doc"))
        me = Patient(ehc_no="EHC-P2299", name="P2299 居民", id_card="330106198001012299", gender="女",
                     birth_date="1980-01-01", phone=PHONE)
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone=PHONE, patient_id=me.id, nickname="P2299", wechat_openid="", status="active"))
        # 早先那份已结案（编号小、排在前面），现在在管的是新的一份
        db.add(SpdEnrollment(patient_id=me.id, program_code="hypertension", org_id=org, doctor_user_id=old_doc,
                             status="closed"))
        db.flush()
        db.add(SpdEnrollment(patient_id=me.id, program_code="hypertension", org_id=org, doctor_user_id=new_doc,
                             status="active"))
        db.commit()
        patient_id = me.id
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"patient": patient_id, "new_doc": new_doc, "resident": {"Authorization": f"Bearer {token}"}}


def test_咨询派给在管档案的主管医生_不是早先已结案那份的(client, world):
    from app.spd.models import SpdConsult

    got = client.post("/api/portal/spd/consults", headers=world["resident"],
                      json={"program_code": "hypertension", "content": "最近血压偏高，药要不要加量？"})
    assert got.status_code == 201, got.text
    with SessionLocal() as db:
        consult = db.get(SpdConsult, got.json()["consult_id"])
        assert consult.doctor_id == world["new_doc"]   # 修前是早先那份已结案档案的医生
