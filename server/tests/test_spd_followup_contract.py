"""慢专病随访域（`spd/followup`）的**响应契约**：30 个端点。

取证靠套件级字节捕获（前后各跑一遍全套件，逐 (方法,路径,状态) 比对）。本文件补
捕获**证明不了**的那部分——比对基线里有 **10 个端点一次 2xx 都没被跑过**，
"前后一致"在它们身上不是证据，是没证据：

    POST   /followup-rules              POST   /questionnaires
    PATCH  /questionnaires/{id}         PATCH  /followup-records/{id}
    POST   /followup-plans/auto-match   GET    /report-tasks
    DELETE /report-tasks/{id}           GET    /report-instances/{id}
    GET    /health-calendar

（它们在基线里只有 403 记录——权限用例跑过，正常路径没跑过。）

## 本文件第一条用例来自一个**真出过的错**

`FollowupRuleOut.points` 我先写成了 `list[dict[str, Any]]`，理由是"JSON 列多半
存对象"。实际存的是**天偏移整数**（1/7/30/90），加上契约当场 500，套件里 8 条
用例连带红。判据从来不是"JSON 列看着像什么"，而是**写入方实际存了什么**——
同一份文件里 `FollowupRuleIn.points` 早就写着 `list[int]`，校验也是 `p < 0 or
p > 3650`，我没看。`test_方案点位是天偏移整数不是对象` 把这条钉死。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Encounter, Organization, Patient, User
from app.security import hash_password
from app.spd import models as S


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        org = Organization(name="随访契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        user = User(username="fuct", password_hash=hash_password("Fu-ct-2026!"),
                    full_name="随访主任", role="director", org_id=org.id)
        db.add(user)
        patient = Patient(ehc_no="EHC-FU-001", name="随访患者", gender="male",
                          birth_date="1970-03-04", id_card="330102197003040011",
                          phone="13700000011")
        db.add_all([user, patient])
        db.flush()
        # 随访计划与健康日历都过 `assert_patient_visible`（§8 可见性）——
        # 没有就诊关系时本机构看不到该患者，会 403 而不是 200。
        db.add(Encounter(patient_id=patient.id, org_id=org.id, doctor_name="随访主任",
                         encounter_type="outpatient", diagnosis_code="I10",
                         diagnosis_name="高血压", summary="首诊"))
        db.commit()
        return {"org": org.id, "user": user.id, "patient": patient.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "fuct",
                              "password": "Fu-ct-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


B = "/api/spd"


# ------------------------------------------------- 真出过的错
def test_方案点位是天偏移整数不是对象(client, auth):
    """`points` 是 JSON 列，里面存的是**随访天偏移**（术后第 1/7/30/90 天），
    不是题目对象。声明成 `list[dict]` 会让这个端点直接 500——不是字节漂移，
    是接口不可用。

    这条同时钉住整数不被改成浮点：`[1, 7, 30, 90]` 与 `[1.0, 7.0, ...]` 是
    不同的字节，而 Python 里 `1 == 1.0` 为真，只断言相等抓不到（Money 陷阱
    的同一形状，本仓库已在 assess 那批栽过一次）。
    """
    created = client.post(f"{B}/followup-rules", headers=auth,
                          json={"code": "fc_post", "name": "术后随访", "scene": "surgery",
                                "dept": "普外科", "surgery_keywords": ["阑尾"],
                                "points": [1, 7, 30, 90],
                                "questionnaire_code": "fc_q", "executor_role": "nurse"})
    assert created.status_code == 201, created.text[:400]
    body = created.json()
    assert body["points"] == [1, 7, 30, 90]
    assert all(isinstance(p, int) and not isinstance(p, bool) for p in body["points"]), (
        f"随访点位被改成了非整数：{body['points']!r}"
    )
    # 键顺序照 `_rule_out` 的出键顺序
    assert list(body) == [
        "id", "code", "name", "scene", "dept", "program_code",
        "diagnosis_keywords", "surgery_keywords", "order_keywords", "points",
        "questionnaire_code", "executor_role", "allow_depts", "allow_roles",
        "preset", "active",
    ]
    # 没传的 JSON 列回空表，不是 null
    assert body["diagnosis_keywords"] == [] and body["allow_roles"] == []
    assert body["preset"] is False and body["active"] is True


# ------------------------------------------------- 问卷两个零覆盖端点
def test_问卷建与改的键集合一致(client, auth):
    """POST 与 PATCH 共用 `_q_out`，两条都得出同一形状；`items`/`abnormal_rules`
    是自由形状的 JSON（题型与规则类型决定字段），只钉外层。"""
    q = client.post(f"{B}/questionnaires", headers=auth,
                    json={"code": "fc_q", "name": "术后问卷", "scene": "surgery",
                          "items": [{"key": "pain", "label": "疼痛评分", "type": "number"}],
                          "abnormal_rules": [{"when": {"field": "pain", "op": ">=", "value": 7},
                                              "level": "high"}],
                          "track_dept": "普外科", "handle_role": "doctor"})
    assert q.status_code == 201, q.text[:400]
    keys = ["id", "code", "name", "scene", "items", "abnormal_rules",
            "track_dept", "handle_role", "preset", "active"]
    assert list(q.json()) == keys
    assert q.json()["items"][0]["key"] == "pain"

    patched = client.patch(f"{B}/questionnaires/{q.json()['id']}", headers=auth,
                           json={"name": "术后问卷v2"})
    assert patched.status_code == 200, patched.text[:400]
    assert list(patched.json()) == keys
    assert patched.json()["name"] == "术后问卷v2"
    # 没改的字段原样保留（PATCH 只认白名单里的键）
    assert patched.json()["abnormal_rules"][0]["level"] == "high"


# ------------------------------------------------- 自动匹配的两个分支
def test_自动匹配无可用方案时回note而不是scanned(client, auth, seeded):
    """`AutoMatchOut` 一个模型两条分支，靠 `exclude_unset` 各出各的键：

        没有配诊断关键词的方案 : matched, created, note      （scanned 不出现）
        正常扫描              : scanned, matched, created    （note 不出现）

    `scanned` 在最前、`note` 在最后，去掉任一个都不打乱其余键的顺序——这正是
    单模型能同时满足两分支的原因（`spd/assess` 的 scores-analysis 两分支顺序
    互斥，无解，那个端点因此刻意留在欠账里）。两条分支都要有序断言：把
    `scanned` 挪到中间，集合判等照样绿，字节却变了。
    """
    # 本模块此前只建过 surgery 场景的方案，checkup 场景一个都没有 → 走 note 分支
    empty = client.post(f"{B}/followup-plans/auto-match", headers=auth,
                        json={"scene": "checkup", "days": 7})
    assert empty.status_code == 200, empty.text[:400]
    assert list(empty.json()) == ["matched", "created", "note"]
    assert "scanned" not in empty.json()
    assert empty.json() == {"matched": 0, "created": 0,
                            "note": "没有配置了诊断关键词的可用方案"}

    # 配一个带诊断关键词的方案 → 走扫描分支
    client.post(f"{B}/followup-rules", headers=auth,
                json={"code": "fc_inp", "name": "出院随访", "scene": "inpatient",
                      "diagnosis_keywords": ["高血压"], "points": [7]})
    scanned = client.post(f"{B}/followup-plans/auto-match", headers=auth,
                          json={"scene": "inpatient", "days": 30})
    assert scanned.status_code == 200, scanned.text[:400]
    assert list(scanned.json()) == ["scanned", "matched", "created"]
    assert "note" not in scanned.json()


# ------------------------------------------------- 改随访任务
def test_改随访任务回完整记录且不注入action(client, auth, seeded):
    """PATCH 走 `_record_out`，它**不带** `action`——`action` 只在执行分支命中
    异常处置建议时才追加。端点带 `exclude_unset`，所以这里不该出现 `action: null`：
    客户端看到 null 会以为"有建议但内容为空"，实际是这条路径根本不产生建议。
    """
    rule = client.post(f"{B}/followup-rules", headers=auth,
                       json={"code": "fc_patch", "name": "改期用方案",
                             "scene": "outpatient", "points": [3]}).json()
    plan = client.post(f"{B}/followup-plans", headers=auth,
                       json={"patient_id": seeded["patient"], "rule_id": rule["id"],
                             "base_date": "2026-08-01", "org_id": seeded["org"]})
    assert plan.status_code == 201, plan.text[:400]
    assert list(plan.json()) == ["created", "items"]
    record_id = plan.json()["items"][0]["id"]

    patched = client.patch(f"{B}/followup-records/{record_id}", headers=auth,
                           json={"planned_at": "2026-09-01", "channel": "wechat"})
    assert patched.status_code == 200, patched.text[:400]
    body = patched.json()
    assert list(body) == [
        "id", "patient_id", "patient_name", "program_code", "rule_id",
        "questionnaire_code", "scene", "org_id", "dept", "planned_at",
        "executed_at", "channel", "executor_id", "answers", "abnormal_level",
        "result", "evidence", "status", "created_at",
    ]
    assert "action" not in body, "未执行的随访不该带处置建议键"
    assert body["planned_at"] == "2026-09-01" and body["channel"] == "wechat"
    # 未执行时是空串（String 列），不是 null——声明成 `str | None` 会改字节
    assert body["executed_at"] == ""
    # PATCH 走 `db.get` 后直接 `_record_out(record)`，没带患者名
    assert body["patient_name"] == ""


# ------------------------------------------------- 报告推送任务三个零覆盖端点
def test_报告任务列表与删除(client, auth, seeded):
    """建 → 列 → 删。`last_run_at` 从未跑过时是**空串**（handler 已折 None），
    删除回 204 **无正文**——204 上 `response_model` 无意义，棘轮的谓词已放宽。
    """
    tpl = client.post(f"{B}/report-templates", headers=auth,
                      json={"code": "fc_tpl", "name": "月报模板", "period": "monthly",
                            "scope_level": "center",
                            "sections": [{"key": "summary", "title": "概览"}],
                            "variables": {}})
    assert tpl.status_code == 201, tpl.text[:400]
    task = client.post(f"{B}/report-tasks", headers=auth,
                       json={"template_id": tpl.json()["id"], "name": "月报推送",
                             "frequency": "monthly", "push_time": "08:00",
                             "subscriber_ids": [seeded["user"]],
                             "org_ids": [seeded["org"]], "priority": 3})
    assert task.status_code == 201, task.text[:400]

    keys = ["id", "template_id", "name", "frequency", "push_time", "subscriber_ids",
            "org_ids", "valid_from", "valid_to", "priority", "status", "last_run_at"]
    assert list(task.json()) == keys

    rows = client.get(f"{B}/report-tasks", headers=auth).json()
    assert rows, "刚建的任务应该列得出来——空列表钉不住字段顺序"
    assert list(rows[0]) == keys
    assert rows[0]["last_run_at"] == "", "从未跑过应是空串而不是 null"
    assert rows[0]["subscriber_ids"] == [seeded["user"]]
    assert rows[0]["valid_from"] == "" and rows[0]["priority"] == 3

    gone = client.delete(f"{B}/report-tasks/{task.json()['id']}", headers=auth)
    assert gone.status_code == 204
    assert gone.content == b"", "204 不该有正文"
    assert client.get(f"{B}/report-tasks", headers=auth).json() == []


# ------------------------------------------------- 报告详情的字段插位
def test_报告详情把content插在org_id与created_at之间(client, auth, seeded):
    """`ReportInstanceDetailOut` **刻意不继承** `ReportInstanceOut`：它多出的
    `content` 与 `subscriber_ids` 落在 `org_id` 与 `created_at` **中间**，
    继承会把新字段排到末尾——那是另一串字节。

    "详情是列表的超集所以可以继承"是个只对了一半的判断：**继承对不对，取决于
    新字段在 handler 里是不是真的在最后**，不取决于键集合是不是超集。
    """
    # 自建模板，不借上一条用例造的——跨用例借数据会让本条单跑就红
    client.post(f"{B}/report-templates", headers=auth,
                json={"code": "fc_tpl2", "name": "详情模板", "period": "monthly",
                      "scope_level": "center",
                      "sections": [{"key": "summary", "title": "概览"}],
                      "variables": {}})
    inst = client.post(f"{B}/report-instances", headers=auth,
                       json={"template_code": "fc_tpl2", "org_id": seeded["org"],
                             "period_label": "2026-08"})
    assert inst.status_code == 201, inst.text[:400]
    assert list(inst.json()) == ["id", "title", "period_label", "content"]

    detail = client.get(f"{B}/report-instances/{inst.json()['id']}", headers=auth)
    assert detail.status_code == 200, detail.text[:400]
    assert list(detail.json()) == [
        "id", "title", "template_code", "period_label", "scope_level", "org_id",
        "content", "subscriber_ids", "created_at",
    ]

    listed = client.get(f"{B}/report-instances", headers=auth).json()
    assert listed and list(listed[0]) == [
        "id", "title", "template_code", "period_label", "scope_level", "org_id",
        "created_at",
    ]


# ------------------------------------------------- 健康日历
def test_健康日历三条线各自的形状(client, auth, seeded):
    """日历把随访 / 复诊 / 待办三张表并到一天里。三个列表都要有真数据——
    空表钉不住字段顺序，而这里恰好有个容易写错的：`revisits[].items` 是
    **String(512) 列**（逗号分隔的项目名），不是 JSON 数组。
    """
    day = "2026-08-15"
    with SessionLocal() as db:
        db.add_all([
            S.SpdRevisit(patient_id=seeded["patient"], plan_date=day, dept="心内科",
                         items="血压,血脂", source="manual", status="planned"),
            S.SpdTask(program_code="FC", patient_id=seeded["patient"],
                      task_type="followup", title="日历待办", org_id=seeded["org"],
                      status="pending", due_date=day),
        ])
        db.commit()
    rule = client.post(f"{B}/followup-rules", headers=auth,
                       json={"code": "fc_cal", "name": "日历方案", "scene": "outpatient",
                             "points": [0]}).json()
    client.post(f"{B}/followup-plans", headers=auth,
                json={"patient_id": seeded["patient"], "rule_id": rule["id"],
                      "base_date": day, "org_id": seeded["org"]})

    body = client.get(f"{B}/health-calendar", headers=auth,
                      params={"patient_id": seeded["patient"], "day": day}).json()
    assert list(body) == ["day", "followups", "revisits", "tasks"]
    assert body["day"] == day

    assert body["followups"], "当天应有随访——空表钉不住嵌套记录的字段"
    assert "action" not in body["followups"][0], "计划中的随访不带处置建议键"

    assert body["revisits"], "当天应有复诊"
    assert list(body["revisits"][0]) == ["id", "plan_date", "dept", "items", "status"]
    items = body["revisits"][0]["items"]
    assert items == "血压,血脂" and isinstance(items, str), (
        f"复诊项目是 String 列存的逗号串，不是数组：{items!r}"
    )

    assert body["tasks"], "当天应有待办"
    assert list(body["tasks"][0]) == ["id", "title", "task_type", "status"]
