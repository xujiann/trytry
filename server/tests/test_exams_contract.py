"""共享诊断中心 `/api/exams` 三个待治理端点（互认检查/催办/互认统计）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。其余 18 个端点此前已治理，仅作种子。

本簇的建模判断（都以此处的精确断言为依据）：

- `recognition-check` 是**条件键**三分支：无报告只有 `recognizable` 一键；
  目录阻断出 `recognizable+reason`；可互认出 `recognizable+request_id+
  item_name+conclusion`。按出键序声明（reason 在 request_id 前，两键从不同
  分支出现互不打架）+ `response_model_exclude_unset=True`，三分支各钉一遍
  ——尤其"键**整个不在**"那一半，只钉出现分支等于没钉。
- `recognition_ratio_pct` 恒 float：`round(x/total*100, 1)` 与兜底字面量
  `0.0` 都是浮点，零分支在造数前单独钉。本簇无 Money 出参，其余数值全 int。
- 催办行的 `reported_at` 是 handler 里 `isoformat()` 过的**字符串**（非
  datetime 透传），与 DB 值逐字符回绑；空清单与 today 覆盖两分支都钉。
- `log_patient_access(recognition)` 的跨机构留痕语义（按设计不拦、只留痕）
  不在本网改动范围，治理不许动。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import ExamReport

UNACK_ROW_KEYS = ["report_id", "request_id", "conclusion", "reported_by", "reported_at", "critical_status"]
STATS_KEYS = ["recognized_total", "reported_total", "recognition_ratio_pct", "saved_exams", "by_item"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "诊断契约卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约受检者", "id_card": "330281198804047716", "gender": "男",
              "birth_date": "1988-04-04"},
        headers=admin,
    ).json()
    return {"org": org, "patient": patient}


def test_互认统计与催办清单_零分支精确(client, admin):
    """放在最前：无任何申请单/报告时，0.0 兜底与空清单才钉得住。"""
    body = client.get("/api/exams/recognition-stats", headers=admin).json()
    assert list(body.keys()) == STATS_KEYS
    assert body == {
        "recognized_total": 0,
        "reported_total": 0,
        "recognition_ratio_pct": 0.0,
        "saved_exams": 0,
        "by_item": [],
    }
    assert isinstance(body["recognition_ratio_pct"], float)
    assert client.get("/api/exams/critical/unacknowledged", headers=admin).json() == []


def test_互认检查_无报告分支只有一键(client, admin, base):
    body = client.get(
        f"/api/exams/recognition-check?patient_id={base['patient']['id']}&item_code=CT001",
        headers=admin,
    ).json()
    # 条件键的另一半：reason/request_id/item_name/conclusion **整个不在**，不是 null
    assert list(body.keys()) == ["recognizable"]
    assert body == {"recognizable": False}


@pytest.fixture(scope="module")
def reported(client, admin, base):
    """CT001 走完 申请→领取→出报告；再建一张互认单与一张危急值检验单。"""
    rq = client.post(
        "/api/exams",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["org"]["id"],
              "center_type": "imaging", "item_code": "CT001", "item_name": "胸部CT平扫"},
        headers=admin,
    ).json()
    assert client.post(f"/api/exams/{rq['id']}/claim", headers=admin).status_code == 200
    rep = client.post(
        f"/api/exams/{rq['id']}/report",
        json={"finding": "未见异常", "conclusion": "胸部CT未见明显异常", "reported_by": "读片医师"},
        headers=admin,
    )
    assert rep.status_code == 201, rep.text
    return {"rq": rq, "rep": rep.json()}


def test_互认检查_可互认分支精确形状与键序(client, admin, base, reported):
    body = client.get(
        f"/api/exams/recognition-check?patient_id={base['patient']['id']}&item_code=CT001",
        headers=admin,
    ).json()
    assert list(body.keys()) == ["recognizable", "request_id", "item_name", "conclusion"]
    assert body == {
        "recognizable": True,
        "request_id": reported["rq"]["id"],
        "item_name": "胸部CT平扫",
        "conclusion": "胸部CT未见明显异常",
    }
    assert type(body["request_id"]) is int


@pytest.fixture(scope="module")
def recognized(client, admin, base, reported):
    rq2 = client.post(
        "/api/exams",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["org"]["id"],
              "center_type": "imaging", "item_code": "CT001", "item_name": "胸部CT平扫",
              "accept_recognition_of": reported["rq"]["id"]},
        headers=admin,
    ).json()
    assert rq2["status"] == "recognized"
    return rq2


def test_互认统计精确_比率恒float(client, admin, recognized):
    body = client.get("/api/exams/recognition-stats", headers=admin).json()
    assert list(body.keys()) == STATS_KEYS
    assert body == {
        "recognized_total": 1,
        "reported_total": 1,
        "recognition_ratio_pct": 50.0,
        "saved_exams": 1,
        "by_item": [{"item_code": "CT001", "item_name": "胸部CT平扫", "recognized_count": 1}],
    }
    assert isinstance(body["recognition_ratio_pct"], float)
    assert type(body["by_item"][0]["recognized_count"]) is int


@pytest.fixture(scope="module")
def critical(client, admin, base):
    rq3 = client.post(
        "/api/exams",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["org"]["id"],
              "center_type": "lab", "item_code": "K001", "item_name": "血钾"},
        headers=admin,
    ).json()
    rep3 = client.post(
        f"/api/exams/{rq3['id']}/report",
        json={"finding": "K 7.2", "conclusion": "血钾危急", "critical": True, "reported_by": "检验师"},
        headers=admin,
    )
    assert rep3.status_code == 201, rep3.text
    return {"rq3": rq3, "rep3": rep3.json()}


def test_催办清单精确_reported_at回绑DB(client, admin, critical):
    # 默认 30 分钟窗口：刚出的报告未超时 → 空清单（该分支同样要钉）
    assert client.get("/api/exams/critical/unacknowledged", headers=admin).json() == []
    rows = client.get("/api/exams/critical/unacknowledged?today=2027-01-01", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [UNACK_ROW_KEYS]
    with SessionLocal() as db:
        db_iso = db.get(ExamReport, critical["rep3"]["id"]).reported_at.isoformat()
    assert rows == [{
        "report_id": critical["rep3"]["id"],
        "request_id": critical["rq3"]["id"],
        "conclusion": "血钾危急",
        "reported_by": "检验师",
        "reported_at": db_iso,  # handler 手工 isoformat 的字符串，逐字符回绑
        "critical_status": "notified",
    }]
    assert client.get(
        "/api/exams/critical/unacknowledged?today=bad", headers=admin
    ).status_code == 422


def test_互认检查_目录阻断分支精确(client, admin, base, reported):
    """放在最后：一旦目录配置过项目，目录外项目全被阻断（改变全局判定）。"""
    created = client.post(
        "/api/exams/recognition-items",
        json={"item_code": "MR001", "item_name": "头颅MR", "center_type": "imaging"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    body = client.get(
        f"/api/exams/recognition-check?patient_id={base['patient']['id']}&item_code=CT001",
        headers=admin,
    ).json()
    assert list(body.keys()) == ["recognizable", "reason"]
    assert body == {"recognizable": False, "reason": "该项目不在互认目录内"}
