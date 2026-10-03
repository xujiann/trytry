"""FHIR Encounter 入站不往就诊摘要里写系统来源标记（P2-1249，第三十六批扫描 U1-7）。

`_do_fhir_encounter` 原先建就诊时写死 `summary="FHIR Encounter 入站同步"`：这是给系统看的来源标记（入站来源本来就记在
交换日志 ExchangeLog），却落进了就诊摘要——居民端「我的档案」的就诊卡片把它印成「摘要」，慢专病居民端档案时间线的
detail 也是这句（修前实测 `{'diagnosis_name': '急性胃肠炎', ..., 'summary': 'FHIR Encounter 入站同步'}`）。

修法：入站不写来源标记。FHIR R4 Encounter 没有摘要 / 备注元素，平台的映射也没定义摘要取自哪里，摘要留空。全仓没有
代码靠这句文字认入站来源。存量不动（迁移不改业务数据，CLAUDE.md §4）。
"""
import pytest

from app.database import SessionLocal
from app.models import Encounter, ExchangeLog, SmsCode

MARK = "入站同步"
PHONE = "13700131249"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21249 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21249 居民", "id_card": "330102199006061249", "phone": PHONE}).json()
    resp = client.post("/api/integration/fhir/Encounter", headers={**admin, "X-Source-System": "HIS-P21249"}, json={
        "resourceType": "Encounter", "status": "finished", "class": {"code": "AMB"},
        "subject": {"reference": f"Patient/{patient['ehc_no']}"},
        "serviceProvider": {"reference": f"Organization/{org}"},
        "reasonCode": [{"text": "急性胃肠炎"}]})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == PHONE).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    return {"encounter_id": resp.json()["encounter_id"], "me": {"Authorization": f"Bearer {token}"}}


def test_入站就诊的摘要不含来源标记(world):
    with SessionLocal() as db:
        encounter = db.get(Encounter, world["encounter_id"])
        assert (encounter.diagnosis_name, encounter.summary) == ("急性胃肠炎", "")   # 修前 summary 是「FHIR Encounter 入站同步」
        # 来源照旧记在交换日志
        assert db.query(ExchangeLog).filter(ExchangeLog.message_type == "fhir_encounter",
                                            ExchangeLog.source_system == "HIS-P21249",
                                            ExchangeLog.success.is_(True)).count() == 1


def test_居民端档案与时间线不出现来源标记(client, world):
    archive = client.get("/api/portal/me/archive", headers=world["me"])
    assert archive.status_code == 200, archive.text
    assert archive.json()["encounters"] == [
        {"diagnosis_name": "急性胃肠炎", "encounter_type": "outpatient", "summary": ""}]
    spd = client.get("/api/portal/spd/archive", headers=world["me"])
    assert spd.status_code == 200, spd.text
    assert [(t["title"], t["detail"]) for t in spd.json()["timeline"] if t["kind"] == "encounter"] == [("急性胃肠炎", "")]
    assert MARK not in archive.text and MARK not in spd.text
