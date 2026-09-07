"""P2-8 第三批：管理/药房/住院/质控四个模块 10 个列表端点切 `deps.paginate`。

切法与前两批一致（`limit` 默认值取原硬编码值，第一页不变），既有 125 条用例
一条没改就全绿。本文件补的是切之前做不到的事，外加**一处真缺陷的回归**。

**这一批只切了「有机构收口」的那些。** 同四个文件里另有 11 个端点
（`list_docs`/`list_rosters`/`list_qc`/`list_staff_contracts`/`list_payroll`、
`list_beds`/`list_orders`/`list_order_executions`、`list_adverse_events`/
`list_record_qc`、`batch_dispense_trace`）**函数体里根本没有 `scope_org_list`
或 `scope_patient_list`**。给它们切分页会把任何登录用户的可枚举面从"最多 200 行"
放大成"整表可翻"，还附送一个精确的全域总数头——那是**扩大暴露面**，
需要业务裁定谁该看见什么，不是这一批该顺手做的决定。已登记为 P1-49。

**修掉的真缺陷：近效期预警其实早就不预警了。**
`GET /api/pharmacy/batches/expiring` 原实现是

    rows = [b for b in q.order_by(expire_date, id).limit(500).all()
            if b.quantity - b.used_quantity > 0]

——**先取 500 行、再用 Python 筛掉发完的**。而上面的过滤只有上界没有下界
（docstring 明写"含已过期"），排序又是按到期日**升序**，所以 `.limit(500)` 砍掉的
恰好是「即将到期」那一端，留下的是「早就过期、也早就发完」那一端。发完的批次
不会删行（只累加 `used_quantity`），真实库里这 500 行大半是零余量，于是
**最该预警的批次一条都出不来**，页面上和「没有近效期批次」长得一模一样。

把条件下推 SQL 之后两件事同时成立：预警恢复有效，且 `X-Total-Count` 与响应体
数的是同一批行。留在外面则两者对不上——头说有 4000、翻到底只收得上来几十行，
按「`len(page) < limit` 即最后一页」翻页的调用方会在第一页就早停，
**那是把静默截断换成静默早停**，比原缺陷更难发现。
"""
import pytest

from app.database import SessionLocal
from app.models import DrugBatch, Employee, Organization
from conftest import login


@pytest.fixture(scope="module")
def seeded(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "三批医院", "org_type": "lead_hospital",
                            "level": "county"}).json()
    with SessionLocal() as db:
        db.add_all([Employee(org_id=org["id"], name=f"三批员工{i}", title="初级")
                    for i in range(6)])
        # 近效期预警的取证数据：520 条**早就过期且已发完**的批次（原实现会把
        # 500 个名额全占掉），外加 3 条**即将到期且仍有余量**的——后者正是
        # 预警该报的那些，在修之前一条都出不来。
        db.add_all([
            DrugBatch(org_id=org["id"], drug_code="EXP-OLD", batch_no=f"OLD-{i:04d}",
                      expire_date="2020-01-01", quantity=10, used_quantity=10)
            for i in range(520)
        ])
        db.add_all([
            # 到期日排在那 520 条之后、但仍落在 days=3650 的窗口内
            DrugBatch(org_id=org["id"], drug_code="EXP-SOON", batch_no=f"SOON-{i}",
                      expire_date="2030-01-01", quantity=100, used_quantity=0)
            for i in range(3)
        ])
        db.commit()
    return {"org": org["id"]}


MIGRATED = [
    ("/api/mgmt/employees", {}),
    ("/api/mgmt/assets", {}),
    ("/api/pharmacy/batches", {}),
    ("/api/pharmacy/batches/expiring", {"days": 3650}),
    ("/api/pharmacy/purchase-orders", {}),
    ("/api/pharmacy/stock-takes", {}),
    ("/api/inpatient/wards", {}),
    ("/api/inpatient/admissions", {}),
    ("/api/quality/infection-reports", {}),
    ("/api/quality/records", {}),
]


@pytest.mark.parametrize("path,params", MIGRATED)
def test_切过的端点都带上了总数头(client, admin, seeded, path, params):
    resp = client.get(path, headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    assert "X-Total-Count" in resp.headers, f"{path} 没带 X-Total-Count"


def test_翻页不重不漏(client, admin, seeded):
    total = int(client.get("/api/mgmt/employees", headers=admin)
                .headers["X-Total-Count"])
    assert total >= 6
    seen, offset = [], 0
    while offset < total:
        rows = client.get("/api/mgmt/employees", headers=admin,
                          params={"offset": offset, "limit": 2}).json()
        assert rows, "翻页翻到空页说明 offset 没生效"
        seen.extend(r["id"] for r in rows)
        offset += 2
    assert len(seen) == total == len(set(seen)), "翻页结果有重复或缺漏"


def test_不带参数时第一页与切之前一样(client, admin, seeded):
    rows = client.get("/api/mgmt/employees", headers=admin).json()
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids), "原实现是 Employee.id 升序，顺序不该变"
    assert len(rows) <= 500, "原硬编码上限是 500，默认调用不该一次给出更多"


# ------------------------------------------------- 近效期预警的真缺陷
def test_近效期预警不再被已发完的过期批次挤掉(client, admin, seeded):
    """种子里 520 条早已过期且已发完的批次 + 3 条即将到期且有余量的。

    修之前：按到期日升序取前 500 条，全被 2020 年那批占满，Python 再筛掉发完的
    → **返回空列表**，与"没有近效期批次"完全无法区分。
    修之后：余量条件下推 SQL，那 520 条根本进不了结果集，3 条该预警的正常返回。
    """
    resp = client.get("/api/pharmacy/batches/expiring", headers=admin,
                      params={"days": 3650})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    soon = [r for r in rows if r["batch_no"].startswith("SOON-")]
    assert len(soon) == 3, f"该预警的批次没出来（这正是修之前的症状）：{len(rows)} 行"
    assert not [r for r in rows if r["batch_no"].startswith("OLD-")], (
        "已发完的批次不该出现在「仍有余量」的预警里"
    )


def test_预警的总数头与响应体数的是同一批行(client, admin, seeded):
    """余量条件留在 Python 里时，头按「含已发完」计数、体只剩有余量的，两者对不上。

    对不上的后果不是少显示几行，而是**静默早停**：调用方按
    `len(page) < limit 即最后一页` 翻页，第一页就会停住。
    """
    resp = client.get("/api/pharmacy/batches/expiring", headers=admin,
                      params={"days": 3650, "limit": 2})
    total = int(resp.headers["X-Total-Count"])
    assert total == 3, f"总数把已发完的 520 条也算进去了：{total}"
    seen, offset = [], 0
    while offset < total:
        page = client.get("/api/pharmacy/batches/expiring", headers=admin,
                          params={"days": 3650, "offset": offset, "limit": 2}).json()
        assert page, "翻到空页——头与体数的不是同一批行"
        seen.extend(r["batch_no"] for r in page)
        offset += 2
    assert sorted(seen) == sorted(f"SOON-{i}" for i in range(3))


def test_未切的十一个端点仍按原样返回(client, admin):
    """这一批**故意没切**没有机构收口的那些，别被误当成漏迁。

    切它们会把可枚举面从「最多 200 行」放大成「整表可翻」，还附送精确总数——
    那是扩大暴露面，需要业务裁定（P1-49），不是分页整改顺手能做的决定。
    """
    for path in ("/api/mgmt/docs", "/api/inpatient/beds", "/api/quality/adverse-events"):
        resp = client.get(path, headers=admin)
        assert resp.status_code == 200, resp.text
        assert "X-Total-Count" not in resp.headers, (
            f"{path} 被切了分页——它没有机构收口，切之前需要先裁定 P1-49"
        )
