"""拼成时间戳去比 DateTime 列的日期筛选：非法值在真 PG 上是 500（P1-58）。

形状是 `query.filter(Model.created_at >= f"{date_from} 00:00:00")`。参数是裸 `str`
时，`abc` / `2026-02-31` 原样拼进去：

| 库 | `abc` | `2026-02-31` |
|---|---|---|
| SQLite | 200 空集（按字符串比较） | 200 空集 |
| PostgreSQL | **500** `InvalidDatetimeFormat` | **500** `DatetimeFieldOverflow` |

修复前实测如此。SQLite 上"看起来只是查不到"，生产库上是一个 500——
正是 CLAUDE.md §6 说的"别把 SQLite 绿了当成 PG 也对"。

**这一套要在真 PG 上跑才算数。** 默认跟 test-unit 跑 SQLite（那里验的是"非法值 422、
留空照旧"这一半）；`tests/test_postgres_real.py` 里有一条 integration 用例，
用 `MEDPLAT_DATEQ_PG_URL` 把本文件换到 PG 上再跑一遍——接法照抄
`test_billing_money_concurrency.py`。
"""
import os

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前。
# 只有 test_postgres_real.py 起的那个子进程会带上这个变量。
_PG_URL = os.environ.get("MEDPLAT_DATEQ_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402

from app.database import engine  # noqa: E402

if _PG_URL:
    assert engine.dialect.name == "postgresql", (
        "MEDPLAT_DATEQ_PG_URL 已给出，引擎却不是 PostgreSQL——"
        f"实际 {engine.dialect.name}，多半是 app.database 在本模块之前就被导入了"
    )

#: (路径, 参数, 必带的其他查询参数)。逐模块接一批、加一批。
#: 值写成 `PATIENT` 的，用例里换成本模块建的那个患者的 id。
PATIENT = "<patient>"
DATETIME_FILTERS = [
    ("/api/access-logs", "start", {}),
    ("/api/access-logs", "end", {}),
    ("/api/spd/case-reports", "date_from", {}),
    ("/api/spd/case-reports", "date_to", {}),
    ("/api/spd/measurements", "since", {"patient_id": PATIENT}),
    ("/api/spd/call-tasks", "date_from", {}),
    ("/api/spd/call-tasks", "date_to", {}),
    ("/api/spd/referrals-stats/closure", "date_from", {}),
    ("/api/spd/referrals-stats/closure", "date_to", {}),
]

#: 形状错、日历上不存在、不补零。前两类修复前在 PG 上是 500；
#: 第三类 PG 自己会解析（`2026-9-1 00:00:00` 是合法 timestamp）、SQLite 按字符串比较
#: 是错集——两库口径不一，统一收成 422（界面占位符写的就是 YYYY-MM-DD）。
BAD_VALUES = ["abc", "2026-02-31", "2026-9-1"]


@pytest.fixture(scope="module")
def client():
    """raise_server_exceptions=False：要断言的正是"会不会出 500"，异常抛进用例就看不到状态码了。"""
    from fastapi.testclient import TestClient

    from conftest import reset_database

    from app.main import app

    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def patient_id(client, admin):
    return client.post(
        "/api/patients", json={"name": "日期筛选探针", "id_card": "330782198701015555"},
        headers=admin,
    ).json()["id"]


def _fill(extra: dict, patient_id: int) -> dict:
    return {k: (patient_id if v == PATIENT else v) for k, v in extra.items()}


@pytest.mark.parametrize("path, param, extra", DATETIME_FILTERS)
def test_非法日期是422不是500(client, admin, patient_id, path, param, extra):
    extra = _fill(extra, patient_id)
    for bad in BAD_VALUES:
        resp = client.get(path, params={**extra, param: bad}, headers=admin)
        assert resp.status_code == 422, (engine.dialect.name, bad, resp.status_code, resp.text[:200])
        assert resp.json()["detail"].startswith(f"{param}："), (bad, resp.json())


@pytest.mark.parametrize("path, param, extra", DATETIME_FILTERS)
def test_留空等于不筛_合法日期照常(client, admin, patient_id, path, param, extra):
    extra = _fill(extra, patient_id)
    base = client.get(path, params=extra, headers=admin)
    blank = client.get(path, params={**extra, param: ""}, headers=admin)
    assert base.status_code == 200, base.text
    assert blank.status_code == 200, blank.text
    assert blank.json() == base.json(), "留空应与不带这个参数完全一致"
    assert client.get(path, params={**extra, param: "2026-09-01"}, headers=admin).status_code == 200


def test_调阅留痕按日界筛选_两库语义一致(client, admin):
    """校验挪到拼串之前，不能改变合法值的筛选结果：今天的留痕，`start=今天` 查得到、
    `start=明天` 查不到；`end=昨天` 查不到、`end=今天` 查得到。"""
    from datetime import date, timedelta

    patient = client.post(
        "/api/patients", json={"name": "留痕日界", "id_card": "330782198701016666"}, headers=admin
    ).json()
    # 针对单个患者的调阅记录查询本身会留一笔（见 access_logs._log_view）
    client.get("/api/access-logs", params={"patient_id": patient["id"]}, headers=admin)

    def rows(**params):
        resp = client.get("/api/access-logs", params={"patient_id": patient["id"], **params},
                          headers=admin)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def ids(**params):
        return [r["id"] for r in rows(**params)]

    # 按单个患者查调阅记录**本身也留痕**，所以每调一次就多一行——比对一份固定快照，
    # 不比两次调用的结果（后一次总比前一次多一条）。
    snapshot = rows()
    assert snapshot, "前提：至少有一条留痕"
    # 日界取留痕自己的时间戳（UTC），不取本地"今天"——别让用例在午夜两侧各算一天
    today = date.fromisoformat(snapshot[0]["at"][:10])
    seen = {r["id"] for r in snapshot}
    assert seen <= set(ids(start=today.isoformat()))
    assert ids(start=(today + timedelta(days=1)).isoformat()) == []
    assert ids(end=(today - timedelta(days=1)).isoformat()) == []
    assert seen <= set(ids(end=today.isoformat()))
