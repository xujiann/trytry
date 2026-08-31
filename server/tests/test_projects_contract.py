"""项目管理 `/api/projects` 八个端点的**特征化网 + 响应契约**。

套路同 `test_rules_contract.py` / `test_dataquality_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

建模判断：

- `budget_amount` 是 **Money 列**（Numeric(14,2, asdecimal=False)）：整数预算读回
  来是 `int`（20000 不是 20000.0），声明 `int | float` 原样透传——本文件同时种
  整数与小数两种取值并逐个 `isinstance` 钉死（陷阱一，docs/接口标准与治理.md）。
  `total_budget` 是它们的 `round(sum(), 2)` 派生值，同理（空集为 `round(0,2)=0`，
  恒 int，由空库统计钉住）。
- `avg_progress_pct_active` 是 `round(真除法, 1)`：有在办项目时**恒 float**
  （整数均值也是 15.0），无在办项目为 None——两条分支都钉。
- 更新回执**不回带里程碑**（`_project_out(project, today, [])`）：即便项目已有
  里程碑，PATCH 回执里 `milestones` 恒为 `[]`、三个 milestone_* 计数恒 0——
  这是当前字节，如实钉住，不"顺手修"。
- 逾期按日期现算：GET 类端点用 `today` 覆盖参数钉死日期口径；创建/操作类回执
  用 2099/2001 这类远期日期保证与真实日期无关。
"""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


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


STATS_CALIBER = "平均进度只算在办项目（筹备+进行中），已完成与已中止不计入"


def test_空库统计精确_均值None_总预算int(client, admin):
    """必须最先跑（其余用例才开始造项目）：钉住零分支——

    `avg_progress_pct_active` 无在办项目时是 None（不是 0）；
    `total_budget` 是 `round(sum([]), 2) = 0`，**int 不是 0.0**（Money 陷阱一）。
    """
    body = client.get("/api/projects/stats/overview", headers=admin).json()
    assert body == {
        "total": 0,
        "by_status": {},
        "active": 0,
        "overdue": 0,
        "avg_progress_pct_active": None,
        "total_budget": 0,
        "caliber": STATS_CALIBER,
    }
    assert isinstance(body["total_budget"], int)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "项目契约县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


PROJECT_KEY_ORDER = [
    "id", "org_id", "name", "category", "owner_name", "start_date", "due_date",
    "status", "status_name", "progress_pct", "budget_amount", "description",
    "milestones", "milestone_done", "milestone_total", "milestone_overdue", "overdue",
]
MILESTONE_KEY_ORDER = ["id", "name", "due_date", "done", "done_date", "overdue", "note"]


@pytest.fixture(scope="module")
def seeded_projects(client, admin, org):
    first = client.post(
        "/api/projects",
        json={"org_id": org["id"], "name": "契约项目一", "category": "info",
              "owner_name": "王负责", "start_date": "2026-01-01", "due_date": "2099-06-30",
              "budget_amount": 20000, "description": "信息化改造"},
        headers=admin,
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/projects", json={"org_id": org["id"], "name": "契约项目二",
                               "budget_amount": 1234.56}, headers=admin
    )
    assert second.status_code == 201, second.text
    return {"first": first.json(), "second": second.json()}


def test_创建回执精确形状与键序(seeded_projects):
    body = seeded_projects["first"]
    assert list(body.keys()) == PROJECT_KEY_ORDER
    assert body == {
        "id": body["id"],
        "org_id": body["org_id"],
        "name": "契约项目一",
        "category": "info",
        "owner_name": "王负责",
        "start_date": "2026-01-01",
        "due_date": "2099-06-30",
        "status": "planning",
        "status_name": "筹备",
        "progress_pct": 0,
        "budget_amount": 20000,
        "description": "信息化改造",
        "milestones": [],
        "milestone_done": 0,
        "milestone_total": 0,
        "milestone_overdue": 0,
        "overdue": False,
    }
    # Money 陷阱一：整数预算读回来是 int（== 比不出 20000 与 20000.0，逐个钉类型）
    assert isinstance(body["budget_amount"], int)
    minimal = seeded_projects["second"]
    assert minimal == {
        "id": minimal["id"],
        "org_id": minimal["org_id"],
        "name": "契约项目二",
        "category": "general",
        "owner_name": "",
        "start_date": "",
        "due_date": "",
        "status": "planning",
        "status_name": "筹备",
        "progress_pct": 0,
        "budget_amount": 1234.56,
        "description": "",
        "milestones": [],
        "milestone_done": 0,
        "milestone_total": 0,
        "milestone_overdue": 0,
        "overdue": False,
    }
    assert isinstance(minimal["budget_amount"], float)


def test_列表与回执同形(client, admin, seeded_projects):
    rows = client.get("/api/projects", headers=admin).json()
    # id 倒序：后建的在前；此刻还没有里程碑，行与创建回执逐键相等
    assert rows == [seeded_projects["second"], seeded_projects["first"]]
    assert client.get("/api/projects?status=done", headers=admin).json() == []
    assert client.get(
        "/api/projects?overdue_only=true&today=2026-08-31", headers=admin
    ).json() == []


@pytest.fixture(scope="module")
def milestones(client, admin, seeded_projects):
    project_id = seeded_projects["first"]["id"]
    late = client.post(
        f"/api/projects/{project_id}/milestones",
        json={"name": "需求调研", "due_date": "2001-01-01", "note": "契约里程碑备注"},
        headers=admin,
    )
    assert late.status_code == 201, late.text
    open_ended = client.post(
        f"/api/projects/{project_id}/milestones", json={"name": "上线运行"}, headers=admin
    )
    assert open_ended.status_code == 201, open_ended.text
    return {"late": late.json(), "open_ended": open_ended.json()}


def test_里程碑回执精确形状与键序(milestones):
    body = milestones["late"]
    assert list(body.keys()) == MILESTONE_KEY_ORDER
    # 到期日在过去且未完成 → 逾期现算为 True（与真实日期无关：2001 恒在过去）
    assert body == {
        "id": body["id"],
        "name": "需求调研",
        "due_date": "2001-01-01",
        "done": False,
        "done_date": "",
        "overdue": True,
        "note": "契约里程碑备注",
    }
    # 无到期日不算逾期
    assert milestones["open_ended"] == {
        "id": milestones["open_ended"]["id"],
        "name": "上线运行",
        "due_date": "",
        "done": False,
        "done_date": "",
        "overdue": False,
        "note": "",
    }


def test_详情含里程碑精确(client, admin, seeded_projects, milestones):
    project = seeded_projects["first"]
    body = client.get(f"/api/projects/{project['id']}?today=2026-09-01", headers=admin).json()
    # 里程碑按 (due_date, id) 排序：空到期日排最前
    assert body == {
        **project,
        "milestones": [milestones["open_ended"], milestones["late"]],
        "milestone_done": 0,
        "milestone_total": 2,
        "milestone_overdue": 1,
    }


def test_完成与撤销回执精确(client, admin, milestones):
    milestone_id = milestones["late"]["id"]
    # 缺省分支：不传 done_date 时取服务端当日（值不可预测，钉格式与其余全键）
    done = client.post(f"/api/projects/milestones/{milestone_id}/done", headers=admin).json()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", done["done_date"])
    assert done == {**milestones["late"], "done": True, "done_date": done["done_date"],
                    "overdue": False}
    # 撤销完成：done_date 清空回空串（不是 null）
    reopened = client.post(
        f"/api/projects/milestones/{milestone_id}/reopen", headers=admin
    ).json()
    assert reopened == milestones["late"]
    # 显式日期分支
    redone = client.post(
        f"/api/projects/milestones/{milestone_id}/done?done_date=2026-03-15", headers=admin
    ).json()
    assert redone == {**milestones["late"], "done": True, "done_date": "2026-03-15",
                      "overdue": False}


def test_更新回执精确_不回带里程碑(client, admin, seeded_projects, milestones):
    project = seeded_projects["first"]
    body = client.patch(
        f"/api/projects/{project['id']}",
        json={"status": "ongoing", "progress_pct": 30},
        headers=admin,
    ).json()
    assert list(body.keys()) == PROJECT_KEY_ORDER
    # 当前行为：更新回执恒不带里程碑（milestones=[] 且三个计数为 0），
    # 即便该项目已有 2 个里程碑——契约如实镜像，不借治理改字节
    assert body == {
        **project, "status": "ongoing", "status_name": "进行中", "progress_pct": 30,
    }
    assert isinstance(body["budget_amount"], int)


def test_统计精确_均值恒float_预算混合为float(client, admin, seeded_projects):
    body = client.get("/api/projects/stats/overview?today=2026-09-01", headers=admin).json()
    assert list(body.keys()) == [
        "total", "by_status", "active", "overdue", "avg_progress_pct_active",
        "total_budget", "caliber",
    ]
    assert body == {
        "total": 2,
        # 键序按项目 id 升序首次出现：项目一（ongoing）在前
        "by_status": {"ongoing": {"count": 1, "name": "进行中"},
                      "planning": {"count": 1, "name": "筹备"}},
        "active": 2,
        "overdue": 0,
        "avg_progress_pct_active": 15.0,
        "total_budget": 21234.56,
        "caliber": STATS_CALIBER,
    }
    # 真除法均值恒 float（15.0 不得变 15）；整+小数预算求和为 float
    assert isinstance(body["avg_progress_pct_active"], float)
    assert isinstance(body["total_budget"], float)
    # 逾期分支：把"今天"拨到项目一到期日之后
    overdue = client.get("/api/projects/stats/overview?today=2099-12-31", headers=admin).json()
    assert overdue == {**body, "overdue": 1}
