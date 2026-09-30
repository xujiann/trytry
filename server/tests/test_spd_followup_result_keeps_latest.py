"""随访结果「追加」后按列宽截掉的是最新写的：先回写的通话摘要占满，医生执行随访写的结论被截掉，接口 200 且无提示
（P2-1045，第三十批「服务端拼出来的文本写进 String(N)」扫描 D1-3）。

执行随访与呼叫回写都往 `spd_followup_records.result`（512）里追加（P2-291）：`(旧 + " " + 新).strip()[:512]` 截的是尾巴——
通话摘要回写满 512 字之后，医生写的「医生结论：血糖控制不佳，二甲双胍加量至0.5g tid，一周后门诊复查」落库只剩开头几个字，
加量与复查安排丢了；第二通接通电话的结果在第一通占满后整段丢掉。呼叫回写那一处还截到 500（列宽其实是 512）。兄弟函数
`service.feedback_appended`（P2-961）写明「与执行随访追加结果同一口径……超出列宽时留最新的」。通话原文另在呼叫任务上，截旧的
不丢东西。

修法：两处都改成超出列宽时留最新的、从头上截掉旧的；装得下的与修前一字不差。
"""
import pytest

from app import clock
from app.spd.routers.followup import FOLLOWUP_RESULT_MAX

B = "/api/spd"
PROGRAM = "hypertension"
CONCLUSION = "医生结论：血糖控制不佳，二甲双胍加量至0.5g tid，一周后门诊复查"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21045 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21045 患者", "id_card": "330127197107071045", "phone": "13928701045"}).json()["id"]
    return {"org": org, "patient": patient}


def _record(world, result=""):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], program_code=PROGRAM, org_id=world["org"],
                                   planned_at=clock.today().isoformat(), status="planned", result=result)
        db.add(record)
        db.commit()
        return record.id


def _result(record_id):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        return db.get(SpdFollowupRecord, record_id).result


def test_通话摘要占满之后_执行随访写的结论留下(client, admin, world):
    record = _record(world, result="通" * FOLLOWUP_RESULT_MAX)   # 存量：先回写的通话摘要已占满
    done = client.post(f"{B}/followup-records/{record}/execute", headers=admin,
                       json={"answers": {}, "channel": "phone", "result": CONCLUSION})
    assert done.status_code == 200, done.text
    result = _result(record)
    assert len(result) == FOLLOWUP_RESULT_MAX and result.endswith(CONCLUSION)   # 修前截成「…通通 医生结论：血糖控制不佳」前后


def test_第一通占满之后_第二通接通的结果留下(client, admin, world):
    record = _record(world)

    def call():   # 同一条随访同时只挂一条待呼叫：上一通回写了再派下一通
        got = client.post(f"{B}/call-tasks", headers=admin, json={
            "patient_id": world["patient"], "ref_type": "followup", "ref_id": record})
        assert got.status_code == 201, got.text
        return got.json()["id"]

    first = client.post(f"{B}/call-tasks/{call()}/result", headers=admin,
                        json={"status": "connected", "duration_s": 300, "result": "甲" * 512})
    assert first.status_code == 200, first.text
    second = client.post(f"{B}/call-tasks/{call()}/result", headers=admin,
                         json={"status": "connected", "duration_s": 60, "result": "约了下周二门诊复查"})
    assert second.status_code == 200, second.text
    result = _result(record)
    assert len(result) == FOLLOWUP_RESULT_MAX and result.endswith("约了下周二门诊复查")   # 修前整段丢掉


def test_装得下的与修前一字不差(client, admin, world):
    record = _record(world, result="已电话联系")
    assert client.post(f"{B}/followup-records/{record}/execute", headers=admin,
                       json={"answers": {}, "channel": "phone", "result": "血压平稳"}).status_code == 200
    assert _result(record) == "已电话联系 血压平稳"
