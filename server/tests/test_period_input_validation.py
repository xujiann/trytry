"""月份口径入参的端到端回归（P1-34）：`2026-13` 不得再被受理。

缺陷现场：5 处 `period` 入参各写一遍 `pattern=r"^\\d{4}-\\d{2}$"`，正则只管形状
不管日历。实测改动前：

- `POST /api/fund/pools/{id}/periods`、`POST /api/mgmt/finance`、
  `POST /api/mgmt/payroll` 对 `2026-13` / `2026-00` / `9999-12` 一律 **201**，
  脏周期直接落库；
- `GET /api/quality/records/qc-summary?period=2026-13` **200**，返回一份
  `"period":"2026-13"` 的全零统计——没有任何一条记录的 `%Y-%m` 会等于它，
  所以它永远是"这个月什么都没发生"，与 D-3 那条静默消失的派驻同族。

落库那一半更难收场：薪酬按 `period` 等值查，一条 `2026-13` 的工资记录在任何
月份报表里都查不出来，却实实在在计在 `total_amount` 里。

本文件按**业务行为**断言（HTTP 状态码），单一真源的静态闸门在
`test_datestr_single_source.py`，两者缺一不可：闸门拦"别处再写一遍正则"，
这里拦"类型本身被换掉/绕开"。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

#: 形状对、日历不对（缺陷放行的那一批）；`9999-12` 另加一层：它的次月首日
#: 溢出 `datetime.max`，`fund` 的归集路径原本会 500。
INVALID_PERIODS = ["2026-13", "2026-00", "9999-12"]
#: 形状就不对（改动前后都应当 422，用来确认没有把拒绝面缩小）
MALFORMED_PERIODS = ["2026-1", "202612", "bad", "2026-01-01", ""]
#: 合法月份：必须照常受理，一个都不许被误伤
VALID_PERIODS = ["2026-01", "2026-12", "2024-02", "1999-01"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "月份口径县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def pool(client, admin):
    return client.post(
        "/api/fund/pools",
        json={"year": 2026, "insurance_type": "resident", "total_amount": 1000000},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def employee(client, admin, org):
    return client.post(
        "/api/mgmt/employees", json={"org_id": org["id"], "name": "月份口径员工"}, headers=admin
    ).json()


# --------------------------------------------------- 三个写接口：脏周期不得落库
@pytest.mark.parametrize("period", INVALID_PERIODS + MALFORMED_PERIODS)
def test_基金预结拒绝非法月份(client, admin, pool, period):
    resp = client.post(
        f"/api/fund/pools/{pool['id']}/periods",
        json={"period": period, "actual_amount": 1}, headers=admin,
    )
    assert resp.status_code == 422, f"{period!r} 被受理了：{resp.text[:200]}"


@pytest.mark.parametrize("period", INVALID_PERIODS + MALFORMED_PERIODS)
def test_财务台账拒绝非法月份(client, admin, org, period):
    resp = client.post(
        "/api/mgmt/finance",
        json={"org_id": org["id"], "period": period, "category": "income", "amount": 1},
        headers=admin,
    )
    assert resp.status_code == 422, f"{period!r} 被受理了：{resp.text[:200]}"


@pytest.mark.parametrize("period", INVALID_PERIODS + MALFORMED_PERIODS)
def test_薪酬录入拒绝非法月份(client, admin, employee, period):
    resp = client.post(
        "/api/mgmt/payroll",
        json={"employee_id": employee["id"], "period": period, "base_salary": 1},
        headers=admin,
    )
    assert resp.status_code == 422, f"{period!r} 被受理了：{resp.text[:200]}"


# --------------------------------------------- 两个统计接口：脏周期不得被"统计"
@pytest.mark.parametrize("period", INVALID_PERIODS + MALFORMED_PERIODS[:-1])
def test_病历质控统计拒绝非法月份(client, admin, period):
    resp = client.get(f"/api/quality/records/qc-summary?period={period}", headers=admin)
    assert resp.status_code == 422, f"{period!r} 被受理了：{resp.text[:200]}"
    # 文案是既有响应体，只允许把"受理范围"改小，不允许换措辞（CLAUDE.md 第 7 条）
    assert resp.json()["detail"] == "period 格式须为 YYYY-MM"


@pytest.mark.parametrize("period", INVALID_PERIODS + MALFORMED_PERIODS[:-1])
def test_运营月报导出拒绝非法月份(client, admin, period):
    """这里**必须连文案一起断言**，否则这条用例是空的。

    运营月报会调 `performance.org_scorecards`，那条链路上的 `deps.period_bounds`
    本来就对 `2026-13` 抛 422——所以把本文件的校验改回裸正则，只看状态码的话
    全绿（实测如此）。那份 422 是**顺带捡到的**：它来自另一个模块的另一条判定，
    CSV 行过滤那一半（`month_of(at) == period`）照旧把脏周期当成"这个月没有数据"。
    断言文案 = 断言"这个端点自己在门口挡住了它"。
    """
    resp = client.get(f"/api/reports/operations/export?period={period}", headers=admin)
    assert resp.status_code == 422, f"{period!r} 被受理了：{resp.text[:200]}"
    assert resp.json()["detail"] == "period 格式须为 YYYY-MM", (
        f"{period!r} 是被本端点自己挡下的吗？文案显示它落到了下游："
        f"{resp.text[:200]}"
    )


# --------------------------------------------------------- 合法月份一个都不许误伤
@pytest.mark.parametrize("period", VALID_PERIODS)
def test_合法月份照常受理(client, admin, org, employee, pool, period):
    assert client.post(
        f"/api/fund/pools/{pool['id']}/periods",
        json={"period": period, "actual_amount": 1}, headers=admin,
    ).status_code == 201
    assert client.post(
        "/api/mgmt/finance",
        json={"org_id": org["id"], "period": period, "category": "income", "amount": 1},
        headers=admin,
    ).status_code == 201
    assert client.post(
        "/api/mgmt/payroll",
        json={"employee_id": employee["id"], "period": period, "base_salary": 1},
        headers=admin,
    ).status_code == 201
    qc = client.get(f"/api/quality/records/qc-summary?period={period}", headers=admin)
    assert qc.status_code == 200 and qc.json()["period"] == period
    assert client.get(
        f"/api/reports/operations/export?period={period}", headers=admin
    ).status_code == 200


def test_period_缺省仍是累计口径(client, admin):
    """`period` 可以不传——不传是"累计"，不是"非法"。收紧校验最容易顺手打碎它。"""
    qc = client.get("/api/quality/records/qc-summary", headers=admin)
    assert qc.status_code == 200 and qc.json()["period"] == "累计"
    assert client.get("/api/reports/operations/export", headers=admin).status_code == 200
