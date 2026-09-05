"""慢专病居民端 + 智能随访端五条不变式下沉到库之后的回归（P1-30）。

洞的形状与 `test_logical_unique_races.py` 同族：接口层都是"先查有没有、没有就建"
（或"先判状态、再改状态"），两步之间没有闸门。并发下两路都查不到就都建——
**不报错，静默写出两条**：

| 表 | 静默双写的后果 |
|---|---|
| `spd_service_applies` | 同病种两条待受理申请，团队收件箱里多一条要人手工拒绝 |
| `spd_consults` | 同病种两条开放会话，消息分叉在两条线程里，医生列表各显一条 |
| `spd_call_tasks` | 同一条随访两条待呼叫任务，网关被推两次（患者被拨两遍） |
| `spd_qc_samples` | 同一条随访在同一批次里被抽两次，质控合格率的分母翻倍 |
| `spd_tasks` | 一次随访执行派出两条"异常处置"待办，多的那条被超期扫描翻成超期、挂进督办 |

前四条下沉为唯一索引（前三条是**部分**索引：拒绝后可再申请、关闭后可再开会话、
回写结果后可再发起呼叫都是合法多行），写入点分别改走
`insert_or_conflict`（该 409 的）与 `insert_if_absent`（该复用/该跳过的）。
第五条没有索引可建——`spd_tasks` 上没有指向随访记录的列，同患者同病种同日两条
不同随访各派一条任务是合法的——它的不变式长在**父行**上：随访记录的终态跃迁
只能成功一次，故守在 `service.close_followup_record` 的条件 UPDATE 上。

本档钉三件事：

1. **行为面**：抢输的一路拿到与顺序请求一字不差的 409（`spd_consults` 例外：
   顺序第二次请求本来就是"复用同一条会话"，抢输者也必须拿到 201 与同一
   `consult_id`，否则居民那条消息就丢了）；合法的多条不受影响。
2. **防拆卸**：索引必须留在模型上、也真的建在库上；写入点必须仍走助手/条件
   UPDATE，不许回潮成 check-then-act。
3. **绕开接口层直插**：SQLite 的库级写锁让线程探针对"索引被拆掉"不敏感
   （拆了照样大概率不重复），直插才是确定性的网——那正是并发抢输者实际到达的
   位置。真并发的证明在 `test_spd_portal_followup_unique_races.py`（真 PG）。
"""
import inspect as pyinspect
import re

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from conftest import login

import app.spd.models as spd_models  # noqa: F401 —— 让 spd 表注册进 metadata（静态钉要用）
import app.spd.routers.followup as followup_mod
import app.spd.routers.portal as portal_mod
import app.spd.service as spd_service
from app.database import SessionLocal, engine
from app.models import Base

B = "/api/spd"
P = "/api/portal/spd"


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def world(client, h):
    """一个机构 + 两名患者（甲已实名绑定居民账户）+ 一份带异常规则的问卷与随访方案。

    随访记录在各用例里现造（`_new_record`）：办结是终态，共用一条会互相串味。
    """
    org = client.post(
        "/api/organizations",
        json={"name": "唯一性回归卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    me = client.post(
        "/api/patients",
        json={"name": "唯一甲", "id_card": "330281199203030031", "gender": "男",
              "birth_date": "1992-03-03", "phone": "13900003031"},
        headers=h,
    ).json()
    other = client.post(
        "/api/patients",
        json={"name": "唯一乙", "id_card": "330281199203030042", "gender": "女",
              "birth_date": "1992-03-04", "phone": "13900003042"},
        headers=h,
    ).json()
    questionnaire = client.post(
        f"{B}/questionnaires",
        json={"code": "uq_fu_q", "name": "唯一性回归问卷", "scene": "inpatient",
              "items": [{"key": "pain", "title": "疼痛评分", "type": "number"}],
              "abnormal_rules": [{"when": {"field": "pain", "op": ">=", "value": 7},
                                  "level": "high", "action": "立即上转评估"}],
              "track_dept": "内科", "handle_role": "doctor"},
        headers=h,
    )
    assert questionnaire.status_code == 201, questionnaire.text
    rule = client.post(
        f"{B}/followup-rules",
        json={"code": "uq_fu_rule", "name": "唯一性回归随访", "scene": "inpatient",
              "dept": "内科", "points": [0], "questionnaire_code": "uq_fu_q"},
        headers=h,
    )
    assert rule.status_code == 201, rule.text

    # 居民令牌：短信验证码登录 + 实名绑定（与既有居民端用例同一取法）
    phone = me["phone"] if me.get("phone") else "13900003031"
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    token = client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).json()["access_token"]
    ph = {"Authorization": f"Bearer {token}"}
    # 号码在患者主索引里唯一命中时登录即自动绑定，此时补做实名会 409——两种都算绑上了
    bound = client.post(
        "/api/portal/auth/realname",
        json={"name": "唯一甲", "id_card": "330281199203030031"}, headers=ph,
    )
    assert bound.status_code in (200, 409), bound.text
    home = client.get(f"{P}/home", headers=ph)
    assert home.status_code == 200, home.text
    assert home.json()["patient"]["id"] == me["id"], "居民令牌必须解析到本人的档案"
    return {"org": org, "me": me, "other": other, "rule": rule.json(), "ph": ph}


def _new_record(client, h, world, patient_id=None):
    """现造一条 planned 随访记录，返回其 id。"""
    resp = client.post(
        f"{B}/followup-plans",
        json={"patient_id": patient_id or world["me"]["id"], "rule_id": world["rule"]["id"],
              "org_id": world["org"]["id"]},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["items"][0]["id"]


def _report_tasks(client, h, patient_id):
    rows = client.get(
        f"{B}/tasks", params={"patient_id": patient_id, "task_type": "report"}, headers=h
    )
    assert rows.status_code == 200, rows.text
    return rows.json()


def _count(model, **filters):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**filters).count()
    finally:
        db.close()


# ================================================================ 行为面：服务申请


def test_同病种第二条待受理申请是409且拒绝后可再申请(client, h, world):
    """`(patient_id, program_code) WHERE status='pending'` 只锁待受理这一态。"""
    first = client.post(
        f"{P}/service-applies",
        json={"program_code": "uq_apply", "note": "想加入管理"}, headers=world["ph"],
    )
    assert first.status_code == 201, first.text
    again = client.post(
        f"{P}/service-applies", json={"program_code": "uq_apply"}, headers=world["ph"]
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "该病种已有待受理的服务申请"
    assert _count(spd_models.SpdServiceApply,
                  patient_id=world["me"]["id"], program_code="uq_apply") == 1

    handled = client.post(
        f"{B}/service-applies/{first.json()['id']}/handle",
        json={"status": "rejected", "handle_note": "资料不全"}, headers=h,
    )
    assert handled.status_code == 200, handled.text
    retry = client.post(
        f"{P}/service-applies", json={"program_code": "uq_apply"}, headers=world["ph"]
    )
    assert retry.status_code == 201, "被拒后再申请是合法多行——写成全量唯一这条会红"
    assert _count(spd_models.SpdServiceApply,
                  patient_id=world["me"]["id"], program_code="uq_apply") == 2


def test_绕开接口层直插第二条待受理申请库里拦得住(client, world):
    """接口层预检在顺序请求下就会 409，行为用例分辨不出兜底是否真生效——
    这里直插，那正是并发抢输者实际到达的位置。"""
    db = SessionLocal()
    try:
        db.add(spd_models.SpdServiceApply(
            patient_id=world["me"]["id"], program_code="uq_apply", status="pending",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 已拒绝的历史行不在部分索引范围内，可以再多一条
        db.add(spd_models.SpdServiceApply(
            patient_id=world["me"]["id"], program_code="uq_apply", status="rejected",
        ))
        db.commit()
    finally:
        db.rollback()
        db.close()


# ================================================================ 行为面：在线咨询


def test_同病种开放会话复用不新开且关闭后可再开(client, h, world):
    """这条的抢输语义与别的表相反：**不 409**，复用那条会话，否则消息会丢。"""
    first = client.post(
        f"{P}/consults", json={"program_code": "uq_consult", "content": "最近头晕"},
        headers=world["ph"],
    )
    assert first.status_code == 201, first.text
    consult_id = first.json()["consult_id"]
    again = client.post(
        f"{P}/consults", json={"program_code": "uq_consult", "content": "补充：昨天量了180"},
        headers=world["ph"],
    )
    assert again.status_code == 201
    assert again.json()["consult_id"] == consult_id, "同病种开放会话应复用"
    messages = client.get(f"{P}/consults/{consult_id}/messages", headers=world["ph"]).json()
    assert len(messages) == 2, "两条消息都要落进同一条会话"
    assert _count(spd_models.SpdConsult, patient_id=world["me"]["id"],
                  program_code="uq_consult", status="open") == 1

    closed = client.post(f"{B}/consults/{consult_id}/close", headers=h)
    assert closed.status_code == 200, closed.text
    reopened = client.post(
        f"{P}/consults", json={"program_code": "uq_consult", "content": "又不舒服了"},
        headers=world["ph"],
    )
    assert reopened.status_code == 201
    assert reopened.json()["consult_id"] != consult_id, "关闭后再发起要开新会话"


def test_不同病种与一般咨询互不冲突(client, world):
    """键是 (patient_id, program_code)，`program_code=''` 的一般咨询自成一条线程。"""
    a = client.post(
        f"{P}/consults", json={"program_code": "uq_consult_b", "content": "问题一"},
        headers=world["ph"],
    )
    b = client.post(f"{P}/consults", json={"content": "一般咨询"}, headers=world["ph"])
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["consult_id"] != b.json()["consult_id"]


def test_绕开接口层直插第二条开放会话库里拦得住(client, world):
    db = SessionLocal()
    try:
        db.add(spd_models.SpdConsult(
            patient_id=world["me"]["id"], program_code="uq_consult_b", status="open",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 已关闭的历史会话不在部分索引范围内
        db.add(spd_models.SpdConsult(
            patient_id=world["me"]["id"], program_code="uq_consult_b", status="closed",
        ))
        db.commit()
    finally:
        db.rollback()
        db.close()


# ================================================================ 行为面：呼叫任务


def test_同一随访第二条待呼叫任务是409且回写结果后可再发起(client, h, world):
    record_id = _new_record(client, h, world)
    first = client.post(
        f"{B}/call-tasks",
        json={"patient_id": world["me"]["id"], "ref_type": "followup", "ref_id": record_id},
        headers=h,
    )
    assert first.status_code == 201, first.text
    again = client.post(
        f"{B}/call-tasks",
        json={"patient_id": world["me"]["id"], "ref_type": "followup", "ref_id": record_id},
        headers=h,
    )
    assert again.status_code == 409
    assert again.json()["detail"] == (
        "该患者对同一对象已有待呼叫任务，请先回写其结果（未接通/取消）后再发起"
    )
    assert _count(spd_models.SpdCallTask, patient_id=world["me"]["id"],
                  ref_type="followup", ref_id=record_id) == 1, "抢输的一路不许留下半行"

    done = client.post(
        f"{B}/call-tasks/{first.json()['id']}/result",
        json={"status": "failed", "result": "三次未接听"}, headers=h,
    )
    assert done.status_code == 200, done.text
    retry = client.post(
        f"{B}/call-tasks",
        json={"patient_id": world["me"]["id"], "ref_type": "followup", "ref_id": record_id},
        headers=h,
    )
    assert retry.status_code == 201, "未接通后重新发起是合法多行——写成全量唯一这条会红"


def test_无被引用对象与不同患者的呼叫任务不在键内(client, h, world):
    """`ref_id IS NULL` 的患者级外呼在部分索引范围外；同一份宣教素材可排给多名患者。"""
    a = client.post(
        f"{B}/call-tasks", json={"patient_id": world["me"]["id"], "ref_type": "revisit"},
        headers=h,
    )
    b = client.post(
        f"{B}/call-tasks", json={"patient_id": world["me"]["id"], "ref_type": "revisit"},
        headers=h,
    )
    assert a.status_code == 201 and b.status_code == 201, "无 ref_id 的呼叫不在键内"

    edu_a = client.post(
        f"{B}/call-tasks",
        json={"patient_id": world["me"]["id"], "ref_type": "edu", "ref_id": 9001}, headers=h,
    )
    edu_b = client.post(
        f"{B}/call-tasks",
        json={"patient_id": world["other"]["id"], "ref_type": "edu", "ref_id": 9001},
        headers=h,
    )
    assert edu_a.status_code == 201 and edu_b.status_code == 201, (
        "同一份素材排给两名患者是合法的——键里含 patient_id 正是为此"
    )


def test_绕开接口层直插第二条待呼叫任务库里拦得住(client, h, world):
    record_id = _new_record(client, h, world)
    db = SessionLocal()
    try:
        kwargs = dict(patient_id=world["me"]["id"], ref_type="followup", ref_id=record_id,
                      phone="13900003031", status="pending")
        db.add(spd_models.SpdCallTask(**kwargs))
        db.commit()
        db.add(spd_models.SpdCallTask(**kwargs))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(spd_models.SpdCallTask(**{**kwargs, "status": "cancelled"}))
        db.commit()
    finally:
        db.rollback()
        db.close()


# ================================================================ 行为面：抽查计划


def test_同批次重复生成抽查计划不重复抽人且换批次可再抽(client, h, world):
    """重复调用返回 `created=0` 是本来就有的幂等语义，唯一索引只是把它兜住。"""
    record_id = _new_record(client, h, world)
    executed = client.post(
        f"{B}/followup-records/{record_id}/execute",
        json={"answers": {"pain": 3}, "result": "无异常"}, headers=h,
    )
    assert executed.status_code == 200, executed.text

    first = client.post(f"{B}/qc-samples/plan", json={"ratio": 1, "batch": "UQC1"}, headers=h)
    assert first.status_code == 200, first.text
    plan = first.json()
    assert plan["created"] == plan["planned"] >= 1
    second = client.post(f"{B}/qc-samples/plan", json={"ratio": 1, "batch": "UQC1"}, headers=h)
    assert second.status_code == 200, second.text
    assert second.json() == {**plan, "created": 0}, "重跑同一批次不再抽人，响应形状不变"

    rows = client.get(f"{B}/qc-samples", params={"batch": "UQC1"}, headers=h).json()
    assert len(rows) == plan["planned"]
    assert len({r["record_id"] for r in rows}) == len(rows), "同一批次里一条随访只抽一次"

    other_batch = client.post(
        f"{B}/qc-samples/plan", json={"ratio": 1, "batch": "UQC2"}, headers=h
    ).json()
    assert other_batch["created"] == plan["planned"], "换批次重抽是合法的（跨批次不在键内）"


def test_绕开接口层直插同批次重复抽样库里拦得住(client, h, world):
    rows = client.get(f"{B}/qc-samples", params={"batch": "UQC1"}, headers=h).json()
    assert rows, "前一条用例应已抽出样本"
    db = SessionLocal()
    try:
        db.add(spd_models.SpdQcSample(record_id=rows[0]["record_id"], batch="UQC1", dept="内科"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(spd_models.SpdQcSample(record_id=rows[0]["record_id"], batch="UQC3", dept="内科"))
        db.commit()
    finally:
        db.rollback()
        db.close()


# ================================================================ 行为面：随访办结与处置任务


def test_随访第二次执行是409且异常处置任务只派一条(client, h, world):
    record_id = _new_record(client, h, world)
    before = len(_report_tasks(client, h, world["me"]["id"]))
    first = client.post(
        f"{B}/followup-records/{record_id}/execute",
        json={"answers": {"pain": 8}, "result": "疼痛明显"}, headers=h,
    )
    assert first.status_code == 200, first.text
    assert first.json()["action"] == "立即上转评估"
    assert len(_report_tasks(client, h, world["me"]["id"])) == before + 1

    again = client.post(
        f"{B}/followup-records/{record_id}/execute",
        json={"answers": {"pain": 8}, "result": "重复执行"}, headers=h,
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "该随访已结束"
    assert len(_report_tasks(client, h, world["me"]["id"])) == before + 1, (
        "抢输/重复的一路一条任务都不许派"
    )


def test_失访后补录答案仍可办结(client, h, world):
    """`allowed_from` 必须含 `unreachable`：失访后拿到答案再补录是现有行为，
    照抄居民端的 (planned, overdue) 会把它变成 409。"""
    record_id = _new_record(client, h, world)
    unreachable = client.post(
        f"{B}/followup-records/{record_id}/execute",
        json={"unreachable": True, "result": "三次未接听"}, headers=h,
    )
    assert unreachable.status_code == 200, unreachable.text
    assert unreachable.json()["status"] == "unreachable"
    assert "action" not in unreachable.json(), "失访分支不带 action 键（契约照旧）"

    before = len(_report_tasks(client, h, world["me"]["id"]))
    done = client.post(
        f"{B}/followup-records/{record_id}/execute",
        json={"answers": {"pain": 9}, "result": "补录"}, headers=h,
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    assert len(_report_tasks(client, h, world["me"]["id"])) == before + 1


def test_居民自助随访第二次是409且医护再执行同样被挡(client, h, world):
    record_id = _new_record(client, h, world)
    before = len(_report_tasks(client, h, world["me"]["id"]))
    first = client.post(
        f"{P}/followups/{record_id}/self-answer", json={"answers": {"pain": 8}},
        headers=world["ph"],
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"id": record_id, "abnormal_level": "high",
                            "action": "立即上转评估"}
    again = client.post(
        f"{P}/followups/{record_id}/self-answer", json={"answers": {"pain": 8}},
        headers=world["ph"],
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "该随访已结束"
    # 跨通道也是同一道闸：居民已办结，医护再执行同样 409
    doctor = client.post(
        f"{B}/followup-records/{record_id}/execute",
        json={"answers": {"pain": 8}}, headers=h,
    )
    assert doctor.status_code == 409
    assert doctor.json()["detail"] == "该随访已结束"
    assert len(_report_tasks(client, h, world["me"]["id"])) == before + 1


# ================================================================ 防拆卸静态钉


@pytest.mark.parametrize(
    "table,index_name,columns,where_fragments",
    [
        ("spd_consults", "uq_spd_consult_open_patient_program",
         ["patient_id", "program_code"], ["open"]),
        ("spd_service_applies", "uq_spd_apply_pending_patient_program",
         ["patient_id", "program_code"], ["pending"]),
        ("spd_call_tasks", "uq_spd_call_task_pending_ref",
         ["patient_id", "ref_type", "ref_id"], ["pending", "ref_id IS NOT NULL"]),
    ],
)
def test_三条部分唯一索引不许消失(table, index_name, columns, where_fragments):
    """模型侧的声明就是不变式的落点，删掉就等于把静默双写的洞放回去。

    同时钉住"是部分索引"——写成全量唯一会拒掉合法的多条（拒绝后再申请、
    关闭后再开会话、未接通后再发起呼叫），那是另一种坏。
    """
    index = next(
        (i for i in Base.metadata.tables[table].indexes if i.name == index_name), None
    )
    assert index is not None, f"{table} 的 {index_name} 没了——静默双写的洞回来了"
    assert index.unique, f"{index_name} 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == columns, f"{index_name} 的键变了"
    where = str(index.dialect_options["sqlite"].get("where", ""))
    for fragment in where_fragments:
        assert fragment in where, (
            f"{index_name} 的部分条件不再包含 {fragment!r}：全量唯一会拒掉合法的多条"
        )


def test_抽查唯一索引是全量唯一而不是部分():
    """`(record_id, batch)` 两列都是 NOT NULL，跨批次重抽本就落在键外——
    这条**不该**带部分条件，带了就等于放宽。"""
    index = next(
        (i for i in Base.metadata.tables["spd_qc_samples"].indexes
         if i.name == "uq_spd_qc_sample_record_batch"),
        None,
    )
    assert index is not None, "spd_qc_samples 的 uq_spd_qc_sample_record_batch 没了"
    assert index.unique
    assert [c.name for c in index.columns] == ["record_id", "batch"]
    assert index.dialect_options["sqlite"].get("where") is None


def test_四条索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    inspector = sa_inspect(engine)
    for table, index_name in (
        ("spd_consults", "uq_spd_consult_open_patient_program"),
        ("spd_service_applies", "uq_spd_apply_pending_patient_program"),
        ("spd_call_tasks", "uq_spd_call_task_pending_ref"),
        ("spd_qc_samples", "uq_spd_qc_sample_record_batch"),
    ):
        names = {i["name"] for i in inspector.get_indexes(table)}
        assert index_name in names, f"{table} 上没有 {index_name}（库与模型对不上）"


def test_四个写入点必须仍走并发助手():
    """回潮成 `db.add(...) + commit` 就等于把兜底拆了——线程探针在 SQLite 上看不出来。"""
    apply_src = pyinspect.getsource(portal_mod.apply_service)
    assert "insert_or_conflict" in apply_src and "db.add(apply)" not in apply_src

    consult_src = pyinspect.getsource(portal_mod.start_consult)
    assert "insert_if_absent" in consult_src and "db.add(consult)" not in consult_src
    assert "insert_or_conflict" not in consult_src, (
        "咨询会话的抢输者必须复用（201 + 同一 consult_id），改成 409 会丢掉居民那条消息"
    )

    call_src = pyinspect.getsource(followup_mod.create_call_task)
    assert "insert_or_conflict" in call_src and "db.add(task)" not in call_src

    qc_src = pyinspect.getsource(followup_mod.plan_qc)
    assert "insert_if_absent" in qc_src
    assert not re.search(r"db\.add\(\s*\n?\s*SpdQcSample\(", qc_src)


def test_随访办结必须是条件更新且处置任务只在命中后派():
    """判定与写在同一条 UPDATE 里，`db.add(SpdTask(...))` 必须排在闸门之后。"""
    helper_src = pyinspect.getsource(spd_service.close_followup_record)
    assert "update(SpdFollowupRecord)" in helper_src and ".rowcount" in helper_src

    for func, allowed in (
        (followup_mod.execute_followup, 'allowed_from=("planned", "overdue", "unreachable")'),
        (portal_mod.self_answer_followup, 'allowed_from=("planned", "overdue")'),
    ):
        src = pyinspect.getsource(func)
        assert "close_followup_record(" in src, f"{func.__name__} 不再走条件更新"
        assert allowed in src, f"{func.__name__} 的可办结态变了"
        assert not re.search(r'record\.status\s*=\s*"(done|unreachable)"', src), (
            f"{func.__name__} 回潮成 Python 侧改状态——判定与写又被拆成两步"
        )
        assert src.index("close_followup_record(") < src.index("SpdTask("), (
            f"{func.__name__} 必须先过闸门再派处置任务"
        )
