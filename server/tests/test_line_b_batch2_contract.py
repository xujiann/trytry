"""线 B 第二批（`education` / `admin_mgmt` / `quality` / `spd/tasks`）的**响应契约**。

四个模块 80 个端点一次做完，套件级捕获一轮比对。本文件钉住捕获**证明不了**
或**特别容易写错**的地方。

## 逐字节比对抓到的一处真回归

`GET /api/mgmt/budgets/execution` 的 `year` 是**查询参数原样回显**，
handler 签名是 `year: str`。我按"年份是数字"声明成 `int`，Pydantic 悄悄把
`"2026"` 变成 `2026`——**键集合与顺序全没变，只有那一个值的引号没了**。
用例不会红（`"2026" != 2026` 没人断言），OpenAPI 也说得通，只有逐字节比对
抓得到。契约不该顺手改类型，那是行为变更。

隔离实验确认过三种状态：无契约 `"2026"` → 声明 int `2026` → 改回 str
`"2026"`（与无契约一致）。

## 本轮反复踩到的：名字像计数、实为明细

`swept` / `skipped` / `closed` / `evidence_urls` 四个字段，名字都像个数字或
字符串表，实际全是明细结构。前两个声明成 `int` 会让端点直接 500。
详见 `docs/接口标准与治理.md` 陷阱四。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Encounter, Organization, Patient, User
from app.security import hash_password


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        org = Organization(name="批二契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        db.add(User(username="b2dir", password_hash=hash_password("B2-dir-2026!"),
                    full_name="批二主任", role="director", org_id=org.id))
        # 直播申请只认 doctor/operator/public_health——director 会被 403 挡住，
        # 那是 §8 的角色约束在生效，不是接口缺陷
        db.add(User(username="b2doc", password_hash=hash_password("B2-doc-2026!"),
                    full_name="批二医师", role="doctor", org_id=org.id))
        # record-qc 会校验抽检对象存在（"抽检对象不存在"→404），要有真就诊
        patient = Patient(ehc_no="EHC-B2-001", name="批二患者", gender="male",
                          birth_date="1980-01-01", id_card="330102198001010033",
                          phone="13700000033")
        db.add(patient)
        db.flush()
        enc = Encounter(patient_id=patient.id, org_id=org.id, doctor_name="批二医师",
                        encounter_type="outpatient", diagnosis_code="I10",
                        diagnosis_name="高血压", summary="首诊")
        db.add(enc)
        db.commit()
        return {"org": org.id, "encounter": enc.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "b2dir",
                              "password": "B2-dir-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def doc_auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "b2doc",
                              "password": "B2-doc-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------- 逐字节比对抓到的回归
def test_预算执行的year是原样回显的字符串(client, auth, seeded):
    """handler 签名 `year: str`，响应里就该是带引号的 `"2026"`。

    声明成 int 会让它变成 `2026`——**这是改响应字节**，属破坏性变更
    （治理第 7 条：治理不得改响应字节）。只断言相等是抓不到的，
    Python 里 `"2026" != 2026` 虽然为真，但没人会去写这一条；
    这里用 `isinstance` 明确钉住类型。
    """
    client.post("/api/mgmt/budgets", headers=auth,
                json={"org_id": seeded["org"], "year": "2026",
                      "category": "income", "amount": 1000000})
    body = client.get("/api/mgmt/budgets/execution", headers=auth,
                      params={"org_id": seeded["org"], "year": "2026"}).json()
    assert list(body) == ["org_id", "year", "income", "expense"]
    assert body["year"] == "2026" and isinstance(body["year"], str), (
        f"year 被契约改成了 {body['year']!r}——那是改字节，不是治理"
    )
    # 未编预算的类别：执行率是 null 而不是 0
    assert list(body["expense"]) == ["budget", "actual", "execution_pct"]
    assert body["expense"]["execution_pct"] is None


# ------------------------------------------------- 名字像计数、实为明细
def test_任务汇总的swept是四项明细不是计数(client, auth, seeded):
    """`swept` 是 `sweep_overdue()` 的四项明细，不是"扫了几条"。
    声明成 int 会 500。"""
    body = client.get("/api/spd/tasks/summary", headers=auth).json()
    assert list(body) == ["by_status", "open_by_type", "open_total", "overdue",
                          "escalated", "due_today", "swept"]
    assert isinstance(body["swept"], dict), f"swept 是明细字典：{body['swept']!r}"
    assert set(body["swept"]) == {"overdue", "escalated", "revisits", "followups"}
    # 两个 by_* 的键由数据决定，没数据时就是空字典，不该硬塞 0
    assert body["by_status"] == {} and body["open_by_type"] == {}


def test_批量处理的skipped是逐条明细不是计数(client, auth, seeded):
    """`skipped` 回的是**哪几条、为什么**——只回一个数字，调用方除了重跑
    全量别无办法。声明成 int 会 500。"""
    body = client.post("/api/spd/tasks/batch", headers=auth,
                       json={"task_ids": [999999], "action": "claim"})
    assert body.status_code == 200, body.text[:300]
    assert list(body.json()) == ["processed", "skipped"]
    assert isinstance(body.json()["skipped"], list)


# ------------------------------------------------- quality 的两处列类型
def test_不良事件的经办人是姓名不是用户id(client, auth, seeded):
    """`reviewed_by` / `rectified_by` 是 `VARCHAR(64)` 存**姓名**，
    未处理时是空串。声明成 `int | None` 会 500（空串解析不成整数）。"""
    made = client.post("/api/quality/adverse-events", headers=auth,
                       json={"event_type": "fall", "level": "III",
                             "description": "契约用例", "org_id": seeded["org"]})
    assert made.status_code in (200, 201), made.text[:300]
    body = made.json()
    assert body["reviewed_by"] == "" and isinstance(body["reviewed_by"], str)
    assert body["rectified_by"] == "" and isinstance(body["rectified_by"], str)


def test_病历质控的两个defects同名不同型(client, auth, seeded):
    """两个字段都叫 defects，**类型不同**，就差一个前缀：

        `RecordQc.defects`          VARCHAR(1024)  文本
        `MedicalRecord.qc_defects`  JSON           数组

    照名字建模必错一个。这与 `care` 的 `RevisitOut.items` 是同一个陷阱：
    看着该是数组的 String 列。
    """
    made = client.post("/api/quality/record-qc", headers=auth,
                       json={"target_type": "encounter", "target_id": seeded["encounter"],
                             "score": 90, "grade": "甲", "defects": "主诉不完整"})
    assert made.status_code in (200, 201), made.text[:300]
    d = made.json()["defects"]
    assert d == "主诉不完整" and isinstance(d, str), f"RecordQc.defects 是文本：{d!r}"
    # 分数是 INTEGER 列，不是 Float——90 不该变 90.0
    assert made.json()["score"] == 90 and isinstance(made.json()["score"], int)


def test_质控分组的key在两组里是不同类型(client, auth, doc_auth, seeded):
    """`by_org` 的 key 是 `org_id`（int），`by_doctor` 的 key 是姓名（str）——
    同一个 `group()` 建出来的两组，分组字段类型不同。声明成 str 会让机构分组
    500。这是**真多态**，不是拿宽类型偷懒。"""
    # 必须先造出真病历——空表下 by_org 一行都没有，key 的类型根本没被验到。
    # 变异验证实测：把 `key: int | str` 改成 `str`，空表版本照样绿。
    # 「空集钉不住字段」这一条，本文件初稿自己就犯了。
    # 写病历只认医师角色（director 会被 403），用 doc_auth
    made = client.post("/api/quality/records", headers=doc_auth,
                       json={"encounter_id": seeded["encounter"],
                             "chief_complaint": "头晕三天",
                             "present_illness": "三天前无明显诱因出现头晕",
                             "past_history": "高血压病史",
                             "physical_exam": "血压 150/95mmHg",
                             "diagnosis_basis": "结合病史与体征",
                             "treatment_plan": "降压治疗"})
    assert made.status_code in (200, 201), made.text[:300]

    body = client.get("/api/quality/records/qc-summary", headers=auth).json()
    assert list(body) == ["period", "total", "avg_score", "grade_distribution",
                          "grade_a_pct", "by_org", "by_doctor"]
    assert body["total"] >= 1, "应有病历——空表钉不住 key 的类型"
    assert body["by_org"] and body["by_doctor"]

    org_key = body["by_org"][0]["key"]
    doc_key = body["by_doctor"][0]["key"]
    assert isinstance(org_key, int) and not isinstance(org_key, bool), (
        f"by_org 的 key 是机构 id（int）：{org_key!r}"
    )
    assert isinstance(doc_key, str), f"by_doctor 的 key 是医师姓名（str）：{doc_key!r}"
    assert list(body["by_org"][0]) == ["key", "name", "total", "avg_score",
                                       "grade_a", "grade_b", "grade_c", "grade_a_pct"]
    assert body["period"] == "累计"


def test_临床指标只有一个带uncollected(client, auth, seeded):
    """`uncollected` 是**只有"术前术后诊断符合率"带**的条件键，且夹在
    `rate_pct` 与 `caliber` **中间**——去掉它剩下的顺序恰好是其余指标的顺序，
    故一个模型 + `exclude_unset` 能同时满足。

    若哪天所有指标都补上这个键，本条会红——那正是"可以把它改成必填"的时机。
    """
    body = client.get("/api/quality/clinical-indicators", headers=auth).json()
    assert list(body) == ["period", "org_id", "group_id", "indicators"]
    withs = [i for i in body["indicators"] if "uncollected" in i]
    withouts = [i for i in body["indicators"] if "uncollected" not in i]
    assert len(withs) == 1, f"带 uncollected 的指标应只有一个，实为 {len(withs)}"
    assert withs[0]["key"] == "preop_postop_match"
    # 位置在 rate_pct 与 caliber 之间，不是追加在末尾
    assert list(withs[0]) == ["key", "name", "dimension", "numerator",
                              "denominator", "rate_pct", "uncollected", "caliber"]
    assert withouts, "其余指标不该带这个键"
    assert list(withouts[0]) == ["key", "name", "dimension", "numerator",
                                 "denominator", "rate_pct", "caliber"]


# ------------------------------------------------- education 的两处
def test_直播评价无人评时平均分是null(client, auth, doc_auth, seeded):
    """一条评价都没有时 `avg_rating` 回 **null**，不是 0——
    兜底成 0 会让"没人评"看起来像"全给了 0 分"。"""
    live = client.post("/api/education/live-sessions", headers=doc_auth,
                       json={"title": "契约直播", "speaker": "讲者"})
    assert live.status_code == 201, live.text[:300]
    assert list(live.json()) == ["id", "title", "status"]
    body = client.get(f"/api/education/live-sessions/{live.json()['id']}/feedback",
                      headers=auth).json()
    assert list(body) == ["session_id", "count", "avg_rating", "feedbacks"]
    assert body["count"] == 0 and body["avg_rating"] is None
    assert body["feedbacks"] == []
