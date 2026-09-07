"""P2-8 第一批：计费模块 8 个列表端点从硬编码 `.limit(N)` 切到 `deps.paginate`。

**这是修一个正确性问题，不是加功能。** 切之前这 8 个端点长这样：

    return [... for x in q.order_by(...).limit(500).all()]

数据没超过 500 条时看不出任何异常；一旦超过，接口**静默少返回**——
没有 `X-Total-Count`、没有 `offset`，调用方（前端表格）连"还有没有"都无从知道，
更别说翻页。列表少一半这件事，在页面上和"就这么多"长得一模一样。

切法是**兼容增强**：每个端点的 `limit` 默认值**取它原来那个硬编码值**
（500/300/200/30 各按原样），所以不带参数调用时**第一页与切之前逐条相同**；
新增的只有 `offset`/`limit` 两个可选查询参数与一个 `X-Total-Count` 响应头。
既有的 63 条 billing 用例一条没改就全绿，这是"没改行为"的第一重证据；
本文件补的是那三件**切之前根本做不到**的事：总数看得见、翻得动页、超量不会静默丢。

一个真踩到的坑记在这儿：`list_bill_details` 改签名时漏加 `response: Response`，
`paginate(..., response, ...)` 里的 `response` 直接 NameError——**模块用例当场报红**
（`app/routers/billing.py:451`）。切分页这件事机械但不能盲改，每个端点都要跑一遍。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "分页计费医院", "org_type": "lead_hospital",
                            "level": "county"}).json()
    client.post("/api/users", headers=admin,
                json={"username": "pg_op", "password": "pass123456",
                      "role": "operator", "org_id": org["id"]})
    op = login(client, "pg_op", "pass123456")
    codes = []
    for i in range(7):
        code = f"PG-{i:02d}"
        client.post("/api/billing/charge-items", headers=admin,
                    json={"code": code, "name": f"分页项目{i}", "category": "exam",
                          "price": 10.0 + i, "unit": "次"})
        codes.append(code)
    return {"org": org, "op": op, "codes": codes}


def test_不带参数时第一页与切之前一样(client, admin, setup):
    """`limit` 的默认值取的就是原来那个硬编码值，所以默认调用的结果不该变。"""
    resp = client.get("/api/billing/charge-items", headers=admin)
    assert resp.status_code == 200
    rows = resp.json()
    codes = [r["code"] for r in rows]
    # 原实现是 `q.order_by(ChargeItem.code).limit(500)`——顺序与范围都应保持
    assert codes == sorted(codes)
    assert set(setup["codes"]) <= set(codes)


def test_总数经响应头返回而不是猜(client, admin, setup):
    """`X-Total-Count` 是切分页最实的收益：调用方终于知道"还有没有"。"""
    resp = client.get("/api/billing/charge-items", headers=admin)
    assert "X-Total-Count" in resp.headers
    assert int(resp.headers["X-Total-Count"]) == len(resp.json())

    page = client.get("/api/billing/charge-items", headers=admin, params={"limit": 3})
    assert len(page.json()) == 3
    # 总数是**满足条件的总行数**，不是本页条数——这正是"超量不再静默"的那个信号
    assert int(page.headers["X-Total-Count"]) > 3


def test_翻页不重不漏(client, admin, setup):
    total = int(client.get("/api/billing/charge-items", headers=admin)
                .headers["X-Total-Count"])
    seen, offset = [], 0
    while offset < total:
        rows = client.get("/api/billing/charge-items", headers=admin,
                          params={"offset": offset, "limit": 2}).json()
        assert rows, "翻页翻到空页说明 offset 没生效"
        seen.extend(r["code"] for r in rows)
        offset += 2
    assert len(seen) == total == len(set(seen)), "翻页结果有重复或缺漏"


def test_筛选条件下总数也跟着筛(client, admin, setup):
    """`X-Total-Count` 必须是**筛过之后**的总数，否则翻页会翻出空页。"""
    resp = client.get("/api/billing/charge-items", headers=admin,
                      params={"category": "exam", "limit": 2})
    assert int(resp.headers["X-Total-Count"]) >= len(setup["codes"])
    none = client.get("/api/billing/charge-items", headers=admin,
                      params={"category": "根本不存在的类别"})
    assert none.json() == [] and none.headers["X-Total-Count"] == "0"


@pytest.mark.parametrize("path,params", [
    ("/api/billing/charge-items", {}),
    ("/api/billing/details", {}),
    ("/api/billing/settlements", {}),
    ("/api/billing/payments", {}),
    ("/api/billing/reconciliation", {}),
    ("/api/billing/deposits/alerts", {}),
])
def test_切过的端点都带上了总数头(client, admin, path, params):
    """一个都不许漏——漏掉的那个就是下一次"列表少了一半没人发现"。"""
    resp = client.get(path, headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    assert "X-Total-Count" in resp.headers, f"{path} 没带 X-Total-Count"
