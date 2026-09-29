"""慢专病复诊 / 随访任务改期不能改成空日期（P2-715，第十八批「新建 vs 编辑」扫描 V1-2 与「字符串比较当日期」V4-3）。

新建复诊的计划日期是必填日期（`RevisitIn.plan_date: DateStr`），改档却是 `OptionalDateStr`、收空串；随访任务改期
（`RecordPatchIn.planned_at`）同样收空串，而生成计划一律排出日期。改成空串之后：超期扫描按 `plan_date != ""` 跳过它、
永不置逾期；工作台「到期复诊 / 到期随访」按 `<= 今天` 数、`""` 恒小于今天，天天算它到期；按截止日筛清单也总带上它。
高危复诊「已有未结束的就不再开一条」还认它——一条没有日期、永不逾期的计划占着名额。

修法：两处改档与新建同一个必填口径（`DateStr`，仍可不传）。存量里已存成空串的不动：工作台照旧把它算作到期，
正好提示补一个日期。
"""
import pytest

from app import clock
from app.database import SessionLocal
from app.spd.models import SpdFollowupRecord, SpdRevisit

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2715 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2715 患者", "id_card": "330127196905052715"}).json()["id"]
    return {"org": org, "patient": patient}


def _revisit(client, admin, world):
    resp = client.post(f"{B}/revisits", headers=admin, json={
        "patient_id": world["patient"], "program_code": "hypertension", "plan_date": clock.today().isoformat(),
        "items": "复查血压", "source": "manual"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _followup_record(world):
    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"],
                                   planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        return record.id


def test_复诊改期不能改成空日期_改成别的日期与只改提醒照旧(client, admin, world):
    rid = _revisit(client, admin, world)
    blank = client.patch(f"{B}/revisits/{rid}", headers=admin, json={"plan_date": ""})
    assert blank.status_code == 422, blank.text   # 修前 200：之后扫描永不置逾期、工作台天天算它到期
    with SessionLocal() as db:
        assert db.get(SpdRevisit, rid).plan_date == clock.today().isoformat()
    later = "2099-01-01"
    moved = client.patch(f"{B}/revisits/{rid}", headers=admin, json={"plan_date": later, "note": "患者要求改期"})
    assert moved.status_code == 200 and moved.json()["plan_date"] == later, moved.text
    reminded = client.patch(f"{B}/revisits/{rid}", headers=admin, json={"remind_status": "contacted"})
    assert reminded.status_code == 200 and reminded.json()["plan_date"] == later, reminded.text


@pytest.mark.parametrize("value", ["", None])
def test_随访任务改期不能改成空日期(client, admin, world, value):
    rid = _followup_record(world)
    resp = client.patch(f"{B}/followup-records/{rid}", headers=admin, json={"planned_at": value})
    assert resp.status_code == 422, resp.text   # 修前空串 200、planned_at 存成空串
    with SessionLocal() as db:
        assert db.get(SpdFollowupRecord, rid).planned_at == clock.today().isoformat()


def test_随访任务改到别的日期与只改渠道照旧(client, admin, world):
    rid = _followup_record(world)
    moved = client.patch(f"{B}/followup-records/{rid}", headers=admin, json={"planned_at": "2099-01-01"})
    assert moved.status_code == 200 and moved.json()["planned_at"] == "2099-01-01", moved.text
    channel = client.patch(f"{B}/followup-records/{rid}", headers=admin, json={"channel": "wechat"})
    assert channel.status_code == 200 and channel.json()["planned_at"] == "2099-01-01", channel.text
