"""慢专病患者移动端（`app/spd/routers/portal.py`）的**响应契约**（26 个端点）。

这一批把 spd 侧居民端的契约欠账清零（674 → 648）。三处判断值得单独钉住：

1. **`/screenings` 有三种形状**（草稿+量表 / 草稿无量表 / 落库），键集合两两不同。
   逐字段建模会把三种形状的字段互相注入 `null`（草稿响应里冒出 `"id": null`），
   故 `response_model_exclude_unset=True`，本文件三条分支各钉一遍。
2. **`score` 是 `int | float`**：有量表时是 `round(total, 2)`（float），无量表时是
   字面量 `0`（int）。声明成 float 会把兜底分从 `0` 变成 `0.0`。
3. **字段顺序即字节**：序列化按模型声明顺序走。`SpdScreeningOut` 一开始把
   `advice` 排在 `result` 前面，逐字节比对当场抓到；本文件用有序键列表钉住。

与平台侧 portal 的 Money 陷阱相反，spd 这边是 **Float 列**（`SpdMeasurement.value`、
`SpdAssessment.score`）：整数值读回来就是 `140.0`，声明 float 才是原样。
"""
import io

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Encounter, Organization, Patient, ResidentAccount
from app.spd import models as S


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        org = Organization(name="慢专病契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        me = Patient(ehc_no="EHC-SC-001", name="慢契约", id_card="330277199001011234",
                     gender="male", birth_date="1990-01-01", phone="13913304001")
        db.add(me)
        db.flush()
        acc = ResidentAccount(phone="13913304001", patient_id=me.id, nickname="小契",
                              wechat_openid="", status="active")
        db.add(acc)
        db.flush()
        db.add(Encounter(patient_id=me.id, org_id=org.id, doctor_name="李医生",
                         encounter_type="outpatient", diagnosis_code="I10",
                         diagnosis_name="高血压", summary="血压偏高"))

        db.add(S.SpdProgram(code="SCT", name="契约高血压", active=True))
        team = S.SpdTeam(name="契约团队", org_id=org.id)
        db.add(team)
        db.flush()
        enr = S.SpdEnrollment(patient_id=me.id, program_code="SCT", status="active",
                              stage="stable", risk_level="mid", team_id=team.id,
                              doctor_user_id=7, next_followup_at="2026-09-01",
                              habits={"smoke": "no"}, risk_factors=["family"],
                              complications=[], tags=["vip"], org_id=org.id)
        db.add(enr)
        db.flush()
        pkg = S.SpdServicePackage(code="SCTPKG", name="契约基础包", program_code="SCT")
        db.add(pkg)
        db.flush()
        db.add(S.SpdPackageBinding(enrollment_id=enr.id, package_id=pkg.id, status="bound",
                                   items=[{"code": "fu", "total": 4, "used": 1}],
                                   period_end="2026-12-31"))
        # Float 列：整数值与小数值各一
        db.add(S.SpdMeasurement(patient_id=me.id, program_code="SCT", metric="bp_sys",
                                value=140, unit="mmHg", level="high", source="manual"))
        db.add(S.SpdMeasurement(patient_id=me.id, program_code="SCT",
                                metric="glucose_fasting", value=6.5, unit="mmol/L",
                                level="mid", source="device"))
        db.add(S.SpdScale(
            code="SCT-RISK", name="契约风险自查", program_code="SCT", category="screen",
            status="published", qr_token="SCT-TOKEN",
            items=[{"key": "q1", "type": "single",
                    "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]}],
            scoring={"ranges": [{"min": 0, "max": 1, "risk": "low", "advice": "保持"},
                                {"min": 2, "risk": "high", "advice": "尽快就诊"}]}))
        db.add(S.SpdServiceApply(patient_id=me.id, program_code="SCT", status="pending",
                                 note="想加入", handle_note=""))
        db.add(S.SpdPathInstance(enrollment_id=enr.id, template_id=1, template_code="TPL",
                                 current_node_key="n2", progress=50, status="running"))
        task = S.SpdTask(program_code="SCT", patient_id=me.id, task_type="report",
                         title="上报血压", org_id=org.id, status="pending",
                         due_date="2026-09-05", form_code="F1", require_evidence=False,
                         result={}, review_note="")
        task2 = S.SpdTask(program_code="SCT", patient_id=me.id, task_type="report",
                          title="需凭证", org_id=org.id, status="pending",
                          due_date="2026-09-06", require_evidence=True)
        db.add_all([task, task2])
        case = S.SpdReferralCase(patient_id=me.id, program_code="SCT", direction="up",
                                 status="applied", current_level="town", reason="血压失控",
                                 trigger_evidence={"bp": 180}, materials=["报告1"],
                                 initiator_org_id=org.id)
        db.add(case)
        db.flush()
        db.add(S.SpdReferralStep(case_id=case.id, step="apply", action="submit",
                                 opinion="同意"))
        fu = S.SpdFollowupRecord(patient_id=me.id, program_code="SCT", scene="routine",
                                 planned_at="2026-08-20", executed_at="", channel="",
                                 status="planned", result="", abnormal_level="",
                                 questionnaire_code="SCTQ", org_id=org.id)
        db.add_all([fu, S.SpdFollowupRecord(
            patient_id=me.id, program_code="SCT", scene="routine", planned_at="2026-07-20",
            executed_at="2026-07-21", channel="phone", status="done", result="平稳",
            abnormal_level="low", org_id=org.id)])
        db.add(S.SpdQuestionnaire(code="SCTQ", name="契约问卷", scene="routine",
                                  abnormal_rules=[]))
        interv = S.SpdIntervention(patient_id=me.id, program_code="SCT", goal="降压",
                                   content="低盐饮食", measures="每日测压",
                                   frequency="daily", next_at="2026-09-01",
                                   status="planned", feedback="")
        db.add(interv)
        mat = S.SpdEduMaterial(code="SCTEDU", title="怎么吃", media_type="article",
                               content="少盐", media_url="", program_code="SCT")
        db.add(mat)
        db.flush()
        push = S.SpdEduPush(patient_id=me.id, material_id=mat.id, status="sent")
        db.add(push)
        db.add(S.SpdRevisit(patient_id=me.id, program_code="SCT", plan_date="2026-09-10",
                            dept="心内科", items="血压、血脂", status="planned",
                            actual_date=""))
        db.add(S.SpdAssessment(patient_id=me.id, program_code="SCT", scale_id=1,
                               scale_code="SCT-RISK", score=2, risk_level="high",
                               advice="尽快就诊"))
        consult = S.SpdConsult(patient_id=me.id, program_code="SCT", doctor_id=7,
                               status="open")
        db.add(consult)
        db.flush()
        db.add(S.SpdConsultMessage(consult_id=consult.id, sender="patient",
                                   sender_id=acc.id, content="医生您好"))
        db.commit()
        return {"task": task.id, "task2": task2.id, "case": case.id, "fu": fu.id,
                "consult": consult.id, "interv": interv.id, "push": push.id,
                "phone": "13913304001"}


@pytest.fixture(scope="module")
def auth(client, seeded):
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code",
                           json={"phone": seeded["phone"],
                                 "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login",
                            json={"phone": seeded["phone"],
                                  "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"Authorization": f"Bearer {token}"}


B = "/api/portal/spd"


# ------------------------------------------------- 三种形状的自查
def test_自查草稿带量表时回答题数与总题数在响应里(client, auth):
    body = client.post(f"{B}/screenings", headers=auth,
                       json={"program_code": "SCT", "scale_code": "SCT-RISK",
                             "answers": {"q1": "是"}, "draft": True}).json()
    assert list(body) == ["draft", "score", "risk_level", "advice", "answered",
                          "total_items"]
    assert body["draft"] is True and body["answered"] == 1 and body["total_items"] == 1
    assert body["score"] == 2.0 and body["risk_level"] == "high"
    # 落库分支的三个键**整个不出现**，不是 null
    assert "id" not in body and "result" not in body and "can_apply" not in body


def test_自查草稿无量表时兜底分是int零而不是零点零(client, auth):
    """兜底值 `{"score": 0, ...}` 是字面量 int。声明成 float 会变成 `0.0`——
    同一个字段在两条分支上一个 int 一个 float，这正是 `int | float` 的用处。"""
    body = client.post(f"{B}/screenings", headers=auth,
                       json={"program_code": "SCT", "answers": {}, "draft": True}).json()
    assert list(body) == ["draft", "score", "risk_level", "advice"]
    assert body["score"] == 0 and isinstance(body["score"], int)
    # 无量表时没有 answered/total_items 可言，这两个键也不该冒出来
    assert "answered" not in body and "total_items" not in body


def test_自查落库分支的键与顺序(client, auth):
    """顺序也钉——序列化按模型声明顺序走，`result` 必须在 `advice` 之前。
    写这个模型时排错过一次，逐字节比对抓到的。"""
    body = client.post(f"{B}/screenings", headers=auth,
                       json={"program_code": "SCT", "scale_code": "SCT-RISK",
                             "answers": {"q1": "是"}}).json()
    assert list(body) == ["id", "score", "risk_level", "result", "advice", "can_apply"]
    assert body["result"] == "suspect" and body["can_apply"] is True
    assert "draft" not in body and "answered" not in body


# ------------------------------------------------- Float 列：整数值读回来是 x.0
def test_指标与评估的整数值仍是float(client, auth):
    rows = {r["metric"]: r for r in client.get(f"{B}/measurements", headers=auth).json()}
    assert rows["bp_sys"]["value"] == 140.0
    assert isinstance(rows["bp_sys"]["value"], float)
    assert rows["glucose_fasting"]["value"] == 6.5
    assessments = client.get(f"{B}/assessments", headers=auth).json()
    assert assessments[0]["score"] == 2.0 and isinstance(assessments[0]["score"], float)


# ------------------------------------------------- 首页与档案
def test_首页的键集合与动态指标字典(client, auth):
    body = client.get(f"{B}/home", headers=auth).json()
    assert set(body) == {"patient", "programs", "latest_metrics", "todo", "packages",
                         "enrolled"}
    assert set(body["patient"]) == {"id", "name", "gender", "birth_date"}
    assert set(body["todo"]) == {"followups", "tasks", "revisits", "interventions",
                                 "unread_edu"}
    # latest_metrics 只含**测过的**指标；没测过的不该被注入 null
    assert set(body["latest_metrics"]) == {"bp_sys", "glucose_fasting"}
    assert set(body["latest_metrics"]["bp_sys"]) == {"value", "unit", "level",
                                                     "measured_at"}
    pkg = body["packages"][0]
    assert pkg["total"] == 4 and pkg["used"] == 1
    # progress 恒为 float（真除法 + 无服务项时字面量 0.0）
    assert pkg["progress"] == 25.0 and isinstance(pkg["progress"], float)
    prog = body["programs"][0]
    assert set(prog) == {"program_code", "program_name", "stage", "risk_level",
                         "team_id", "team_name", "doctor_user_id", "next_followup_at"}


def test_档案的四个JSON列原样透出且时间轴两种来源同形(client, auth):
    body = client.get(f"{B}/archive", headers=auth).json()
    assert set(body) == {"patient", "profiles", "timeline"}
    profile = body["profiles"][0]
    assert profile["habits"] == {"smoke": "no"}
    assert profile["risk_factors"] == ["family"]
    assert profile["complications"] == [] and profile["tags"] == ["vip"]
    kinds = {t["kind"] for t in body["timeline"]}
    assert kinds == {"encounter", "followup"}
    # 两种来源必须同形，否则合并后按 at 排序就对不齐
    assert all(set(t) == {"kind", "at", "title", "detail"} for t in body["timeline"])


# ------------------------------------------------- 其余列表与详情
def test_量表与扫码进入的键集合(client, auth):
    scales = {s["code"]: s for s in client.get(f"{B}/scales", headers=auth).json()}
    assert set(scales["SCT-RISK"]) == {"id", "code", "name", "program_code", "items"}
    assert scales["SCT-RISK"]["items"][0]["key"] == "q1"
    by_token = client.get(f"{B}/scales/by-token/SCT-TOKEN", headers=auth).json()
    assert set(by_token) == {"id", "code", "name", "category", "items"}
    # 令牌无效一律 404，且错误体不带契约字段
    miss = client.get(f"{B}/scales/by-token/NOPE", headers=auth)
    assert miss.status_code == 404 and set(miss.json()) == {"detail"}


def test_全流程视图的三层嵌套(client, auth):
    body = client.get(f"{B}/journey", headers=auth).json()
    assert set(body) == {"programs"}
    prog = body["programs"][0]
    assert set(prog) == {"program_code", "program_name", "stage", "risk_level", "status",
                         "paths", "tasks", "referrals"}
    assert set(prog["paths"][0]) == {"id", "template_code", "current_node_key",
                                     "progress", "status"}
    assert isinstance(prog["paths"][0]["progress"], int)   # Integer 列，不是百分比 float
    assert set(prog["referrals"][0]) == {"id", "direction", "status", "created_at"}


def test_任务清单与随访与干预与宣教与复诊的键集合(client, auth):
    tasks = client.get(f"{B}/tasks", headers=auth).json()
    assert tasks and set(tasks[0]) == {"id", "title", "task_type", "status", "due_date",
                                       "form_code", "require_evidence", "result",
                                       "review_note"}
    followups = client.get(f"{B}/followups", headers=auth).json()
    assert set(followups[0]) == {"id", "scene", "planned_at", "executed_at", "channel",
                                 "status", "result", "abnormal_level"}
    planned = next(f for f in followups if f["status"] == "planned")
    assert planned["executed_at"] == ""      # 未执行：空串，不是 null
    interventions = client.get(f"{B}/interventions", headers=auth).json()
    assert set(interventions[0]) == {"id", "goal", "content", "measures", "frequency",
                                     "next_at", "status", "feedback", "read", "created_at"}
    edu = client.get(f"{B}/edu", headers=auth).json()
    assert set(edu[0]) == {"id", "material_id", "title", "media_type", "content",
                           "media_url", "status", "created_at"}
    revisits = client.get(f"{B}/revisits", headers=auth).json()
    assert set(revisits[0]) == {"id", "plan_date", "dept", "items", "status",
                                "actual_date"}
    # items 是 String 列（顿号分隔的文本），不是 JSON 数组
    assert revisits[0]["items"] == "血压、血脂"


def test_转诊列表与详情的键集合不同(client, auth, seeded):
    """详情**没有** `created_at`、多出 `materials` 与 `steps`。
    详情模型一度继承列表模型，凭空要求了一个详情不返回的字段，被响应校验拦下。
    """
    rows = client.get(f"{B}/referrals", headers=auth).json()
    assert set(rows[0]) == {"id", "direction", "status", "current_level", "reason",
                            "trigger_evidence", "created_at"}
    assert rows[0]["trigger_evidence"] == {"bp": 180}
    detail = client.get(f"{B}/referrals/{seeded['case']}", headers=auth).json()
    assert set(detail) == {"id", "direction", "status", "current_level", "reason",
                           "trigger_evidence", "materials", "steps"}
    assert "created_at" not in detail
    assert detail["materials"] == ["报告1"]
    assert set(detail["steps"][0]) == {"step", "action", "opinion", "created_at"}


def test_咨询会话与消息的键集合(client, auth, seeded):
    started = client.post(f"{B}/consults", headers=auth,
                          json={"program_code": "SCT", "content": "想问下用药"})
    assert started.status_code == 201
    assert set(started.json()) == {"consult_id", "status"}
    rows = client.get(f"{B}/consults", headers=auth).json()
    assert set(rows[0]) == {"id", "program_code", "doctor_id", "status", "created_at"}
    msgs = client.get(f"{B}/consults/{seeded['consult']}/messages", headers=auth).json()
    assert set(msgs[0]) == {"id", "sender", "content", "created_at"}


# ------------------------------------------------- 写侧
def test_写侧端点的响应形状(client, auth, seeded):
    created = client.post(f"{B}/measurements", headers=auth,
                          json={"metric": "bp_sys", "value": 138, "unit": "mmHg",
                                "program_code": "SCT"})
    assert created.status_code == 201
    assert set(created.json()) == {"id", "level", "measured_at"}

    applied = client.post(f"{B}/service-applies", headers=auth,
                          json={"program_code": "SCT-NEW", "note": "另一个病种"})
    assert applied.status_code == 201 and set(applied.json()) == {"id", "status"}
    applies = client.get(f"{B}/service-applies", headers=auth).json()
    assert set(applies[0]) == {"id", "program_code", "status", "note", "handle_note",
                               "created_at"}

    submitted = client.post(f"{B}/tasks/{seeded['task']}/submit", headers=auth,
                            json={"result": {"bp": 130}})
    assert submitted.status_code == 200
    assert submitted.json() == {"id": seeded["task"], "status": "submitted"}

    answered = client.post(f"{B}/followups/{seeded['fu']}/self-answer", headers=auth,
                           json={"answers": {"a": 1}})
    assert answered.status_code == 200
    assert set(answered.json()) == {"id", "abnormal_level", "action"}

    feedback = client.post(f"{B}/interventions/{seeded['interv']}/feedback", headers=auth,
                           json={"feedback": "已做到", "done": False})
    assert feedback.status_code == 200
    assert set(feedback.json()) == {"id", "status", "read"}

    read = client.post(f"{B}/edu/{seeded['push']}/read", headers=auth)
    assert read.status_code == 200 and set(read.json()) == {"id", "status"}


def test_任务附件上传的响应形状(client, auth, seeded):
    """居民端自己的上传通道（平台附件路由只认业务令牌）。"""
    resp = client.post(f"{B}/tasks/{seeded['task2']}/attachments", headers=auth,
                       files={"file": ("bp.png",
                                       io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64),
                                       "image/png")})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"attachment_id", "filename", "size"}
    assert body["filename"] == "bp.png" and body["size"] == 72


def test_四类错误体都只有detail(client, auth):
    for resp in (
        client.post(f"{B}/tasks/999999/submit", headers=auth, json={"result": {}}),
        client.get(f"{B}/referrals/999999", headers=auth),
        client.get(f"{B}/consults/999999/messages", headers=auth),
        client.post(f"{B}/screenings", headers=auth,
                    json={"program_code": "SCT", "scale_code": "NOPE", "answers": {}}),
    ):
        assert resp.status_code == 404
        assert set(resp.json()) == {"detail"}
