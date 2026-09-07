"""P2-8 第五批 5b：15 个**纯分页整改**的端点切 `deps.paginate`（基线 117 → 102）。

与 5a（修两处预警截断）分开提交。这 15 个是第五批 21 个 A 类候选里，
六项检查**全部过关**的那些：limit 就在返回列表的查询上、查询后没有依赖全部行的
计算、没有取行后的 Python 过滤、排序末位键唯一、要登录、已有收口。

两处不是照抄就能过的：

1. **`appointments:list_slots` 补了 `id` 尾键**。它按 `(slot_date, slot_time)` 排，
   而号源表的唯一索引是 `(org_id, employee_id, resource_type, resource_name,
   slot_date, slot_time)`——同一个「日期+时段」上**按设计**并排着各机构各资源的号源。
   居民端同一张表的 `/me/slots` 在第二批已经补过，这里是它的业务端孪生：
   **同一张表的两个入口，缺陷也是成对的**。
2. **`surveillance:list_pathogens` 显式传 `max_limit=1000`**。它的原硬编码上限是
   1000，**高于** `paginate` 默认的 `max_limit=500`——照默认切，不带参数调用时
   第一页会从 1000 条缩到 500 条，那是**减少**了返回内容，不是兼容增强。
   这是本轮唯一一个原上限高于 500 的端点。
"""
import pytest

MIGRATED = [
    ("/api/appointments", {}),
    ("/api/appointments/slots", {}),
    ("/api/blood/requests", {}),
    ("/api/certs", {}),
    ("/api/checkups", {}),
    ("/api/contracts", {}),
    ("/api/cssd/requests", {}),
    ("/api/dispense", {}),
    ("/api/eldercare/assessments", {}),
    ("/api/labqc/lots", {}),
    ("/api/medwaste", {}),
    ("/api/medwaste/locations", {}),
    ("/api/spd/groups", {}),
    ("/api/surgery/rooms", {}),
    ("/api/surveillance/pathogens", {}),
]


@pytest.mark.parametrize("path,params", MIGRATED)
def test_切过的端点都带上了总数头(client, admin, path, params):
    """一个都不许漏——漏掉的那个就是下一次「列表少了一半没人发现」。"""
    resp = client.get(path, headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    assert "X-Total-Count" in resp.headers, f"{path} 没带 X-Total-Count"


def test_号源列表翻页不重不漏(client, admin):
    """号源按 `(slot_date, slot_time)` 排，同一时段并排着各机构各资源的号源。

    诚实边界：**SQLite 上这条即使不补 id 尾键也会绿**（并列行按 rowid 稳定返回），
    真正钉死这件事的是静态闸门 `test_pagination_sort_stability.py`。
    这条是行为侧的下限。
    """
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "5b 号源院", "org_type": "lead_hospital",
                            "level": "county"}).json()
    for i in range(7):
        client.post("/api/appointments/slots", headers=admin,
                    json={"org_id": org["id"], "resource_type": "outpatient",
                          "resource_name": f"并列科室{i}", "slot_date": "2026-11-11",
                          "slot_time": "09:00", "capacity": 5})
    total = int(client.get("/api/appointments/slots", headers=admin,
                           params={"org_id": org["id"]}).headers["X-Total-Count"])
    assert total >= 7
    seen, offset = [], 0
    while offset < total:
        rows = client.get("/api/appointments/slots", headers=admin,
                          params={"org_id": org["id"], "offset": offset,
                                  "limit": 3}).json()
        assert rows, "翻页翻到空页说明 offset 没生效"
        seen.extend(r["id"] for r in rows)
        offset += 3
    assert len(seen) == total == len(set(seen)), "翻页结果有重复或缺漏"


def test_原上限高于500的端点第一页不许缩水(client, admin):
    """`surveillance:list_pathogens` 原硬编码上限 1000 > `paginate` 默认 max_limit=500。

    照默认切，不带参数调用会从 1000 条缩到 500 条——**那是减少返回内容**，
    不是兼容增强。显式传 `max_limit=1000` 才保得住。本轮只此一处。
    """
    import inspect

    from app.routers import surveillance

    src = inspect.getsource(surveillance.list_pathogens)
    assert "1000)" in src and "paginate(" in src, "max_limit 没显式传，第一页会缩水"
    resp = client.get("/api/surveillance/pathogens", headers=admin,
                      params={"limit": 1000})
    assert resp.status_code == 200
