"""慢专病照护域（`app/spd/routers/care.py`）31 个端点的**响应契约**。

主取证是套件级字节比对：加契约前后各跑一遍全套件（两轮用例集相同，见
`docs/接口标准与治理.md` 第 4 条注意），**落在 spd/care 内的差异 0 处**。

本文件补的是比对**证明不了**的部分——按两步自查，这 31 个里有 **7 个端点
一次都没被任何用例跑到**：

    GET /assessments            GET /assessments/stats
    GET /intervention-templates GET /case-report-tasks
    GET /health-prescriptions   GET /consults
    GET /consults/{id}/messages

前后都没记录，比对显示"一致"不是证据，是没证据。这里逐个调起来，**都造了
数据**（空集在任何契约下都合法，什么字段都钉不住）。

另把四处需要判断的地方钉死：

1. `by_item` 是**两层动态字典**（题目 key → 选项 → 计数）；
2. `trend.latest` 无数据时为 null，不是空对象；
3. `RevisitOut.items` 是 String 列，不是 JSON 数组；
4. 三处"新建回执与列表不同形"，各是两个模型。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Encounter, Organization, Patient, User
from app.security import hash_password
from app.spd import models as S

B = "/api/spd"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        org = Organization(name="照护契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        user = User(username="carect", password_hash=hash_password("Care-ct-2026!"),
                    full_name="照护医生", role="doctor", org_id=org.id)
        db.add(user)
        db.flush()
        patient = Patient(ehc_no="EHC-CA-001", name="照护患者", id_card="330155199001011234",
                          gender="male", birth_date="1990-01-01")
        db.add(patient)
        db.flush()
        # 没有这两条，本模块所有涉患者的端点都会被 visibility 挡成 403
        # （"本机构与该患者无就诊、签约或转诊关系"）——这不是接口缺陷，
        # 是 §8 的可见性红线在起作用，用例必须造出真实的服务关系。
        db.add(Encounter(patient_id=patient.id, org_id=org.id, doctor_name="照护医生",
                         encounter_type="outpatient", diagnosis_code="I10",
                         diagnosis_name="高血压", summary="首诊"))
        db.add(S.SpdEnrollment(patient_id=patient.id, program_code="CA", org_id=org.id,
                               status="active", stage="stable", risk_level="mid"))
        scale = S.SpdScale(code="CA-SCALE", name="照护量表", category="risk",
                           program_code="CA", version="v1", status="published",
                           items=[{"key": "q1", "type": "single",
                                   "options": [{"label": "是", "score": 2},
                                               {"label": "否", "score": 0}]}],
                           scoring={"ranges": [{"min": 0, "risk": "high",
                                                "advice": "尽快就诊"}]})
        db.add(scale)
        db.add(S.SpdInterventionTemplate(code="CA-TPL", name="低盐饮食", program_code="CA",
                                         category="diet", content="每日食盐<5g",
                                         measures="记录饮食", frequency="daily",
                                         cycle_days=30, auto_risk_level="high"))
        task = S.SpdCaseReportTask(code="CA-TASK", name="危急值上报", program_code="CA",
                                   dept="内科", manager_user_id=user.id,
                                   assignee_ids=[user.id], org_ids=[org.id], active=True)
        db.add(task)
        db.add(S.SpdHealthPrescription(patient_id=patient.id, program_code="CA",
                                       doctor_id=user.id, drug_advice="按时服药",
                                       rehab_advice="每日步行", life_advice="戒烟",
                                       target_note="血压<140/90"))
        consult = S.SpdConsult(patient_id=patient.id, program_code="CA",
                               doctor_id=user.id, status="open")
        db.add(consult)
        db.flush()
        db.add(S.SpdConsultMessage(consult_id=consult.id, sender="patient",
                                   sender_id=patient.id, content="医生您好"))
        db.commit()
        return {"org": org.id, "user": user.id, "patient": patient.id,
                "task": task.id, "consult": consult.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "carect",
                              "password": "Care-ct-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------- 七个零覆盖端点
def test_评估列表与统计的两层动态字典(client, auth, seeded):
    """`by_item` 是 题目key → 选项 → 计数 的两层字典，两层的键都由量表决定。
    逐字段建模等于把某一张量表写死进契约。"""
    created = client.post(f"{B}/assessments", headers=auth,
                          json={"patient_id": seeded["patient"], "scale_code": "CA-SCALE",
                                "answers": {"q1": "是"}, "program_code": "CA"})
    assert created.status_code == 201, created.text[:300]
    assert set(created.json()) == {"id", "patient_id", "patient_name", "scale_id",
                                   "scale_code", "scale_version", "program_code",
                                   "answers", "score", "risk_level", "advice",
                                   "channel", "created_at"}
    assert created.json()["patient_name"] == "照护患者"

    rows = client.get(f"{B}/assessments", headers=auth).json()
    assert rows and set(rows[0]) == set(created.json())

    stats = client.get(f"{B}/assessments/stats", headers=auth).json()
    assert set(stats) == {"persons", "times", "by_risk", "by_item"}
    assert stats["persons"] == 1 and stats["times"] >= 1
    # 两层：题目 key "q1" → 选项 "是" → 次数
    assert stats["by_item"]["q1"]["是"] >= 1
    assert all(isinstance(v, dict) for v in stats["by_item"].values())


def test_干预模板的新建回执与列表不同形(client, auth):
    rows = client.get(f"{B}/intervention-templates", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "code", "name", "program_code", "category",
                                     "content", "measures", "frequency", "cycle_days",
                                     "auto_risk_level"}
    created = client.post(f"{B}/intervention-templates", headers=auth,
                          json={"code": "CA-TPL2", "name": "规律运动",
                                "category": "exercise"})
    assert created.status_code == 201
    # 新建只回三个键——与列表的十个键不是同一组，故两个模型
    assert set(created.json()) == {"id", "code", "name"}


def test_个案上报任务与健康处方的键集合(client, auth, seeded):
    tasks = client.get(f"{B}/case-report-tasks", headers=auth).json()
    assert tasks and set(tasks[0]) == {"id", "code", "name", "program_code", "dept",
                                       "manager_user_id", "assignee_ids", "org_ids",
                                       "active"}
    assert tasks[0]["assignee_ids"] == [seeded["user"]]
    assert tasks[0]["org_ids"] == [seeded["org"]]

    scripts = client.get(f"{B}/health-prescriptions", headers=auth,
                         params={"patient_id": seeded["patient"]}).json()
    assert scripts and set(scripts[0]) == {"id", "program_code", "drug_advice",
                                           "rehab_advice", "life_advice", "target_note",
                                           "doctor_id", "created_at"}


def test_咨询会话与消息的键集合(client, auth, seeded):
    rows = client.get(f"{B}/consults", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "patient_id", "patient_name", "program_code",
                                     "doctor_id", "status", "messages", "created_at"}
    # messages 是另算的计数，不是列
    assert rows[0]["messages"] == 1
    msgs = client.get(f"{B}/consults/{seeded['consult']}/messages", headers=auth).json()
    assert msgs and set(msgs[0]) == {"id", "sender", "sender_id", "content", "created_at"}


# ------------------------------------------------- 三处会改字节的判断
def test_趋势无数据时latest是null(client, auth, seeded):
    """"最近一次不存在"与"最近一次是空的"是两回事——无数据时 handler 给 None。"""
    empty = client.get(f"{B}/measurements/trend", headers=auth,
                       params={"patient_id": seeded["patient"], "metric": "never_used"}).json()
    assert set(empty) == {"metric", "granularity", "points", "level_distribution",
                          "total", "latest"}
    assert empty["latest"] is None and empty["points"] == [] and empty["total"] == 0

    client.post(f"{B}/measurements", headers=auth,
                json={"patient_id": seeded["patient"], "metric": "bp_sys", "value": 140,
                      "unit": "mmHg", "program_code": "CA"})
    got = client.get(f"{B}/measurements/trend", headers=auth,
                     params={"patient_id": seeded["patient"], "metric": "bp_sys"}).json()
    assert got["latest"] is not None
    # Float 列：整数值读回来是 140.0。**必须 isinstance 判 float**——
    # 只写 `== 140.0` 抓不到把它声明成 int 的变异，因为 Python 里
    # `140 == 140.0` 为真（变异验证实测到这一点，本条因此加了类型断言）。
    assert got["latest"]["value"] == 140.0
    assert isinstance(got["latest"]["value"], float)
    assert isinstance(got["points"][0]["avg"], float)
    assert set(got["points"][0]) == {"label", "avg", "min", "max", "count"}


def test_复诊项目是字符串不是数组(client, auth, seeded):
    """`SpdRevisit.items` 是 String 列（顿号分隔的文本）。名字像数组，
    列类型说了算——与 spd/portal 那批同一个坑。"""
    created = client.post(f"{B}/revisits", headers=auth,
                          json={"patient_id": seeded["patient"], "program_code": "CA",
                                "plan_date": "2026-09-10", "dept": "心内科",
                                "items": "血压、血脂"})
    assert created.status_code == 201
    assert created.json()["items"] == "血压、血脂"
    assert isinstance(created.json()["items"], str)
    # log 才是 JSON 数组，新建时为空
    assert created.json()["log"] == []
    assert created.json()["patient_name"] == ""   # 新建不查姓名
    rows = client.get(f"{B}/revisits", headers=auth).json()
    assert rows[0]["patient_name"] == "照护患者"   # 列表查了姓名


def test_个案上报的新建与处置同形(client, auth, seeded):
    created = client.post(f"{B}/case-reports", headers=auth,
                          json={"task_id": seeded["task"], "patient_id": seeded["patient"],
                                "program_code": "CA", "report_type": "review",
                                "content": "血压 200/120"})
    assert created.status_code == 201 and set(created.json()) == {"id", "status"}
    handled = client.post(f"{B}/case-reports/{created.json()['id']}/handle", headers=auth,
                          json={"status": "done", "handle_note": "已处置"})
    assert handled.status_code == 200
    # 新建与处置同形，共用一个模型
    assert set(handled.json()) == set(created.json())

    rows = client.get(f"{B}/case-reports", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "task_id", "patient_id", "patient_name",
                                     "program_code", "report_type", "content",
                                     "trigger_rule", "status", "handle_note", "created_at"}
