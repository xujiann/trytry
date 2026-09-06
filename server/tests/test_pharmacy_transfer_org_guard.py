"""ADR-0020 的回归：药品余缺调拨必须校验**调出机构**的归属。

**实测出来的洞**（修之前）：建甲、乙、丙三院与一个挂甲院的 `operator`，乙院备货 200，
甲院 operator 调用 `POST /api/pharmacy/transfers`，`from_org_id=乙院`、`to_org_id=丙院`——

    ① 甲院 operator 调乙院的药给丙院 -> 201
       库存 org=2 PB01 qty=160      ← 乙院被搬走 40
       库存 org=3 PB01 qty=40       ← 丙院凭空多出 40

调用者与这笔调拨**两端都没有关系**，照样把别人的药搬走了。
角色守卫是 `require_roles("operator", "pharmacist")`，两个都不在 `GLOBAL_ROLES` 里。

比 ADR-0019 那例（目标池分发）更重：目标池改挂还能改回来，**调拨搬的是实物账**
——批次跟着一起搬，调出方账上少的那些在盘点前不会有人发现，
而缺药预警看的正是这个汇总。

**只校验调出方，不校验调入方**（ADR-0020 方案 B）：减少谁的库存就要能写谁；
收货不减少任何人的库存，而"甲把药调给乙"正是本接口的主用法——两端都要求可写
等于只剩 admin/director 能调拨。这与 ADR-0019 的取舍**故意不同**，
理由是那里的"去向"是改归属、这里是收货。
"""
from app.database import SessionLocal
from app.models import DrugStock
from conftest import login


def _org(client, admin, name, level="township"):
    return client.post("/api/organizations", headers=admin,
                       json={"name": name,
                             "org_type": "lead_hospital" if level == "county" else "township",
                             "level": level}).json()


def _stock(client, admin, org_id, code, qty):
    """备货：汇总 + 批次都要有，否则 `_fefo_batches` 挑不到可发批次。"""
    client.post("/api/pharmacy/stocks", headers=admin,
                json={"org_id": org_id, "drug_code": code, "drug_name": "调拨用例药",
                      "quantity": qty, "threshold": 1})
    client.post("/api/pharmacy/batches", headers=admin,
                json={"org_id": org_id, "drug_code": code, "drug_name": "调拨用例药",
                      "batch_no": f"TB-{code}", "expire_date": "2027-12-31", "quantity": qty})


def _qty(org_id, code):
    with SessionLocal() as db:
        row = (db.query(DrugStock)
               .filter(DrugStock.org_id == org_id, DrugStock.drug_code == code).first())
        return row.quantity if row else 0


def test_调别家的药必须403且对方库存一片不少(client, admin):
    a = _org(client, admin, "调拨甲院", "county")
    b = _org(client, admin, "调拨乙院")
    c = _org(client, admin, "调拨丙院")
    client.post("/api/users", headers=admin,
                json={"username": "tr_op_a", "password": "pass123456",
                      "role": "operator", "org_id": a["id"]})
    op_a = login(client, "tr_op_a", "pass123456")
    _stock(client, admin, b["id"], "TR01", 100)
    before = _qty(b["id"], "TR01")

    resp = client.post("/api/pharmacy/transfers", headers=op_a,
                       json={"drug_code": "TR01", "from_org_id": b["id"],
                             "to_org_id": c["id"], "quantity": 40})
    assert resp.status_code == 403, resp.text
    assert _qty(b["id"], "TR01") == before, "403 之后调出方库存不许少一片"
    assert _qty(c["id"], "TR01") == 0, "调入方也不该凭空多出来"


def test_从本机构调出照常放行(client, admin):
    """守卫不能把主用法挡了——「我把我的药调给别家」仍然 201。"""
    a = _org(client, admin, "本院调出甲", "county")
    b = _org(client, admin, "本院调出乙")
    client.post("/api/users", headers=admin,
                json={"username": "tr_op_own", "password": "pass123456",
                      "role": "operator", "org_id": a["id"]})
    op_a = login(client, "tr_op_own", "pass123456")
    _stock(client, admin, a["id"], "TR02", 100)
    before_a = _qty(a["id"], "TR02")

    resp = client.post("/api/pharmacy/transfers", headers=op_a,
                       json={"drug_code": "TR02", "from_org_id": a["id"],
                             "to_org_id": b["id"], "quantity": 30})
    assert resp.status_code == 201, resp.text
    assert _qty(a["id"], "TR02") == before_a - 30
    assert _qty(b["id"], "TR02") == 30


def test_全域角色跨机构调拨仍然放行(client, admin):
    """admin/director 在 GLOBAL_ROLES 里，中心统筹调拨是设计内的，不能被误伤。"""
    b = _org(client, admin, "全域调出乙")
    c = _org(client, admin, "全域调入丙")
    _stock(client, admin, b["id"], "TR03", 100)

    resp = client.post("/api/pharmacy/transfers", headers=admin,
                       json={"drug_code": "TR03", "from_org_id": b["id"],
                             "to_org_id": c["id"], "quantity": 20})
    assert resp.status_code == 201, resp.text
    assert _qty(c["id"], "TR03") == 20
