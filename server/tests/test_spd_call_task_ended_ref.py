"""已结束的随访 / 复诊不再转呼叫（P2-761，第二十批「停用 / 注销 / 作废的对象仍在被用」扫描 M3-1）。

收尾时撤回待呼叫（P2-498 随访、P2-735 复诊）只撤当时已在队列里的；转呼叫入口原先不看随访的状态、复诊连存在都不查。
看板没刷新时（别人刚登记死亡、居民刚自助答完、门诊当面办结）点一下「转呼叫」，就又建出一条待呼叫——坐席照单打给已经
随访过的人，甚至死者家属。随访与执行同一句「该随访已结束」（失访的照收，还能补录）；复诊同一个口径。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdCallTask, SpdFollowupRecord, SpdRevisit

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2761 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2761 患者{n}", "id_card": f"33010219500101{2760 + n:04d}"}).json()["id"] for n in (1, 2)]
    return {"org": org, "patient": patients[0], "other": patients[1]}


def _followup(world, status):
    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"], status=status,
                                   planned_at="2026-10-01")
        db.add(record)
        db.commit()
        return record.id


def _revisit(world, status, patient=None):
    with SessionLocal() as db:
        revisit = SpdRevisit(patient_id=patient or world["patient"], program_code="hypertension", plan_date="2026-10-15",
                             status=status)
        db.add(revisit)
        db.commit()
        return revisit.id


def _calls(ref_type, ref_id):
    with SessionLocal() as db:
        return db.query(SpdCallTask).filter_by(ref_type=ref_type, ref_id=ref_id).count()


def _call(client, admin, patient, ref_type, ref_id):
    return client.post(f"{B}/call-tasks", headers=admin, json={
        "patient_id": patient, "ref_type": ref_type, "ref_id": ref_id})


@pytest.mark.parametrize("status", ["done", "removed"], ids=["已办结", "已移除"])
def test_已结束的随访转呼叫_409且队列无新增(client, admin, world, status):
    record = _followup(world, status)
    resp = _call(client, admin, world["patient"], "followup", record)
    assert resp.status_code == 409, resp.text   # 修前 201：又建出一条待呼叫
    assert resp.json()["detail"] == "该随访已结束"
    assert _calls("followup", record) == 0


@pytest.mark.parametrize("status", ["planned", "overdue", "unreachable"], ids=["待随访", "已超期", "失访"])
def test_未结束与失访的随访照常转呼叫(client, admin, world, status):
    record = _followup(world, status)
    assert _call(client, admin, world["patient"], "followup", record).status_code == 201
    assert _calls("followup", record) == 1


@pytest.mark.parametrize("status", ["done", "removed"], ids=["已复诊", "已移除"])
def test_已结束的复诊转呼叫_409(client, admin, world, status):
    revisit = _revisit(world, status)
    resp = _call(client, admin, world["patient"], "revisit", revisit)
    assert resp.status_code == 409, resp.text   # 修前 201
    assert resp.json()["detail"] == "该复诊已结束"
    assert _calls("revisit", revisit) == 0


def test_复诊不存在404_不是这位患者的422_未结束的照常(client, admin, world):
    missing = _call(client, admin, world["patient"], "revisit", 999999)
    assert missing.status_code == 404 and missing.json()["detail"] == "复诊计划不存在", missing.text   # 修前 201
    others = _revisit(world, "planned", patient=world["other"])
    wrong = _call(client, admin, world["patient"], "revisit", others)
    assert wrong.status_code == 422 and wrong.json()["detail"] == "复诊计划不属于该患者", wrong.text   # 修前 201
    for status in ("planned", "overdue"):
        revisit = _revisit(world, status)
        assert _call(client, admin, world["patient"], "revisit", revisit).status_code == 201
