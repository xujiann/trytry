"""决策驾驶舱 `/api/metrics` 五个端点的**特征化网 + 响应契约**（接口标准与治理）。

按 `docs/接口标准与治理.md` 的既定套路：**先补网钉住键集合与类型 → 再加
`response_model` → 加完网照样绿**（治理不得改响应字节，CLAUDE.md §11）。

本簇的三处建模难点，都是先实测再决定、不是读代码推理的：

1. `overview` 的四个 `*_pct` 字段**恒为 float**——`pct()` 分母为 0 时返回字面量
   `0.0`，非 0 时是 `round(part * 100.0 / total, 2)`，两条分支都走浮点。
   （这一条必须实测：同类接口里 `round(x, 2)` 在 x 为整数时会返回 **int**，
   `cssd.total_cost` 就是那样，声明成 float 会把 `0` 变 `0.0` 即改字节。）
2. `overview.chronic_management.by_level` 的键是**慢病分级的实际取值**（数据决定），
   只能 `dict[str, int]`，不能逐字段写死。
3. `drilldown.items` 是**真多态**——八个 metric 各有各的行渲染函数，
   除 `id` 外字段完全不同，故 `list[dict[str, Any]]`。这不是偷懒：同一响应里的
   `fields` 数组就是这批行的字段清单，契约在运行期由它自描述，本文件有用例钉住
   "`items` 的键集合必须与 `fields` 一致"。
"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import (
    ChronicPatient,
    DrugStock,
    Encounter,
    ExamReport,
    ExamRequest,
    InfectiousCase,
    MedicalWaste,
    Prescription,
    Referral,
    User,
)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def seeded(client, admin):
    """八类可下钻指标各造一行——**空响应什么都钉不住**，本网的前提就是每段有数据。

    直接落库而不走各自的建单接口：那八张表各有各的流程（领取、出报告、处置闭环…），
    绕开更省事也更稳，与 `test_performance_orgs_contract.py` 同法。
    """
    org = client.post(
        "/api/organizations",
        json={"name": "驾驶舱契约院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "驾驶舱契约患者", "id_card": "330281199009094513"},
        headers=admin,
    ).json()
    overdue = str(date.today() - timedelta(days=9))
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        db.add(Encounter(patient_id=patient["id"], org_id=org["id"], encounter_type="outpatient",
                         diagnosis_name="高血压", diagnosis_code="I10", doctor_name="张医生"))
        request = ExamRequest(patient_id=patient["id"], from_org_id=org["id"],
                              center_type="imaging", item_code="CT", item_name="胸部CT",
                              status="reported", created_by=doctor.id)
        db.add(request)
        db.flush()
        db.add(ExamReport(request_id=request.id, conclusion="见结节", critical=True,
                          critical_status="notified", reported_by="李医生",
                          reported_at=datetime.utcnow()))
        db.add(DrugStock(org_id=org["id"], drug_code="MC-LOW", drug_name="驾驶舱短缺药",
                         quantity=1, threshold=50))
        db.add(ChronicPatient(patient_id=patient["id"], disease="hypertension", level=3,
                              managed_by_org_id=org["id"], next_due=overdue))
        db.add(MedicalWaste(org_id=org["id"], waste_type="infectious", weight_kg=2.5,
                            status="stored", trace_code="MC-TRACE-1", collected_date=overdue))
        db.add(InfectiousCase(org_id=org["id"], disease_code="A01", disease_name="伤寒",
                              category="乙类", onset_date=str(date.today())))
        db.add(Referral(patient_id=patient["id"], from_org_id=org["id"], to_org_id=org["id"],
                        direction="up", reason="上转", status="pending", created_by=doctor.id))
        db.add(Prescription(patient_id=patient["id"], org_id=org["id"], diagnosis_name="高血压",
                            status="rejected", review_comment="剂量超限", created_by=doctor.id))
        db.commit()
    return {"org_id": org["id"], "patient_id": patient["id"]}


def _get(client, admin, url):
    resp = client.get(url, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ /trends
def test_trends_键集合与类型(client, admin, seeded):
    body = _get(client, admin, "/api/metrics/trends?months=3")
    assert set(body) == {"months", "series"}
    assert [type(m).__name__ for m in body["months"]] == ["str"] * 3
    assert set(body["series"]) == {"encounters", "exam_reports", "referrals", "prescriptions"}
    for name, values in body["series"].items():
        assert len(values) == 3, f"{name} 的点数应与月份数一致"
        assert all(isinstance(v, int) for v in values), f"{name} 含非 int"


def test_trends_的series键是静态的_加新序列必须同步契约(client, admin, seeded):
    """`series` 建模成逐字段而不是 `dict[str, list[int]]`——键在代码里写死，
    逐字段是更强的契约。代价是：**加第五条序列而不改契约，会被 response_model
    静默过滤掉**。这条用例把"代码产出的序列"与"契约声明的字段"对齐，
    加序列忘了改契约就转红，而不是悄悄少一条曲线。
    """
    from app.routers.metrics import TrendSeries

    body = _get(client, admin, "/api/metrics/trends?months=1")
    assert set(body["series"]) == set(TrendSeries.model_fields), (
        "接口产出的序列与契约声明的字段对不上——加了序列没同步契约，"
        "response_model 会把它静默丢掉"
    )


# ------------------------------------------------------------------ /alerts
def test_alerts_键集合与类型(client, admin, seeded):
    body = _get(client, admin, "/api/metrics/alerts")
    assert set(body) == {"total", "items"}
    assert isinstance(body["total"], int)
    assert body["items"], "五类风险都造了数据，items 不该为空（空列表什么都钉不住）"
    for item in body["items"]:
        assert set(item) == {"type", "label", "count"}
        assert isinstance(item["type"], str) and isinstance(item["label"], str)
        assert isinstance(item["count"], int)


def test_alerts_只列计数大于零的项且total是全部之和(client, admin, seeded):
    body = _get(client, admin, "/api/metrics/alerts")
    assert all(i["count"] > 0 for i in body["items"]), "列出了计数为 0 的项"
    assert body["total"] == sum(i["count"] for i in body["items"])


# -------------------------------------------------------- /drilldown-metrics
def test_drilldown_metrics_是列表且键集合固定(client, admin, seeded):
    body = _get(client, admin, "/api/metrics/drilldown-metrics")
    assert isinstance(body, list) and body
    for row in body:
        assert set(row) == {"metric", "label", "page", "count"}
        assert isinstance(row["count"], int)
        assert isinstance(row["metric"], str) and isinstance(row["page"], str)


# ---------------------------------------------------------------- /drilldown
DRILLDOWN_TOP_KEYS = {
    "metric", "label", "page", "columns", "fields", "total", "offset", "limit", "items",
}


def _all_metrics(client, admin):
    return [row["metric"] for row in _get(client, admin, "/api/metrics/drilldown-metrics")]


def test_drilldown_顶层键集合对每个指标都一样(client, admin, seeded):
    for metric in _all_metrics(client, admin):
        body = _get(client, admin, f"/api/metrics/drilldown?metric={metric}")
        assert set(body) == DRILLDOWN_TOP_KEYS, f"{metric} 的顶层键集合不一致"
        assert isinstance(body["total"], int)
        assert isinstance(body["offset"], int) and isinstance(body["limit"], int)
        assert all(isinstance(c, str) for c in body["columns"])
        assert all(isinstance(f, str) for f in body["fields"])


def test_drilldown_行的键集合必须与fields一致(client, admin, seeded):
    """`items` 是真多态（八种行形状），契约只能是 `list[dict[str, Any]]`。

    但它不是无契约：同一响应里的 `fields` 就是这批行的字段清单，前端按它渲染。
    这条把"自描述"钉死——行里多一个键或少一个键，前端表格就会错位。
    """
    checked = 0
    for metric in _all_metrics(client, admin):
        body = _get(client, admin, f"/api/metrics/drilldown?metric={metric}")
        for row in body["items"]:
            assert set(row) == set(body["fields"]), (
                f"{metric} 的行键集合与 fields 对不上：{sorted(set(row) ^ set(body['fields']))}"
            )
            checked += 1
    assert checked >= 8, f"只校到 {checked} 行，八类指标没都造出数据，本用例失去区分力"


def test_drilldown_列名与字段数一一对应(client, admin, seeded):
    for metric in _all_metrics(client, admin):
        body = _get(client, admin, f"/api/metrics/drilldown?metric={metric}")
        assert len(body["columns"]) == len(body["fields"]), (
            f"{metric} 的表头数与字段数不等，前端表格必然错列"
        )


def test_drilldown_未知指标422(client, admin, seeded):
    resp = client.get("/api/metrics/drilldown?metric=不存在的指标", headers=admin)
    assert resp.status_code == 422
    assert "未知指标" in resp.json()["detail"]


def test_drilldown_带总数响应头(client, admin, seeded):
    """`X-Total-Count` 是分页的依据，加契约不能把响应头弄丢。"""
    resp = client.get("/api/metrics/drilldown?metric=critical_values", headers=admin)
    assert resp.headers.get("X-Total-Count") == str(resp.json()["total"])


# ----------------------------------------------------------------- /overview
OVERVIEW_SHAPE = {
    "resources": {"organizations", "patients"},
    "service_division": {
        "encounters_total", "grassroots_encounters", "grassroots_encounter_ratio_pct",
    },
    "remote_diagnosis": {
        "reported_total", "recognized_total", "recognition_ratio_pct", "critical_values",
    },
    "referrals": {"up", "down", "completed"},
    "prescription_review": {"total", "auto_pass_ratio_pct", "rejected", "pending_review"},
    "chronic_management": {"total", "by_level"},
    "pharmacy": {"stock_alerts"},
}
PCT_FIELDS = [
    ("service_division", "grassroots_encounter_ratio_pct"),
    ("remote_diagnosis", "recognition_ratio_pct"),
    ("prescription_review", "auto_pass_ratio_pct"),
]


def test_overview_七段结构与键集合(client, admin, seeded):
    body = _get(client, admin, "/api/metrics/overview")
    assert set(body) == set(OVERVIEW_SHAPE)
    for section, keys in OVERVIEW_SHAPE.items():
        assert set(body[section]) == keys, f"{section} 段键集合漂移"


def test_overview_比率字段恒为float(client, admin, seeded):
    """这条是**实测得来**的，不是从代码推的。

    `pct()` 两条分支都返回 float（分母 0 时是字面量 `0.0`），所以契约可以安全地
    声明 float。同类接口里 `round(x, 2)` 对整数入参会返回 **int**
    （`cssd.total_cost` 即是），那种地方声明 float 就是改字节——这类判断只能一个个
    实测，不能照抄结论。
    """
    body = _get(client, admin, "/api/metrics/overview")
    for section, field in PCT_FIELDS:
        value = body[section][field]
        assert isinstance(value, float), f"{section}.{field} 是 {type(value).__name__}，不是 float"


def test_overview_计数字段恒为int(client, admin, seeded):
    body = _get(client, admin, "/api/metrics/overview")
    pct_names = {f for _, f in PCT_FIELDS}
    for section, keys in OVERVIEW_SHAPE.items():
        for key in keys:
            if key in pct_names or key == "by_level":
                continue
            assert isinstance(body[section][key], int), f"{section}.{key} 不是 int"


def test_overview_分级分布是动态键(client, admin, seeded):
    """`by_level` 的键是慢病分级的**实际取值**，由数据决定，只能建成宽键字典。"""
    by_level = _get(client, admin, "/api/metrics/overview")["chronic_management"]["by_level"]
    assert isinstance(by_level, dict) and by_level, "seeded 造了一个 level=3 的慢病档案"
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in by_level.items())
    assert sum(by_level.values()) == _get(
        client, admin, "/api/metrics/overview"
    )["chronic_management"]["total"]
