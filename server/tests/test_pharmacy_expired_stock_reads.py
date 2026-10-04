"""批次自然过期之后，读汇总的五处都当有货、发药同时 409（P2-1250，第三十六批扫描 U2-1）。

`DrugStock.quantity` 是可用汇总，效期没有事件可挂，静置过期的量一直留在里面（ADR-0013「已知边界」）。修前缺药预警
（`/api/pharmacy/alerts`）、待办（`/api/todos` 缺药节）、驾驶舱（`metrics.q_stock_alerts`）、供应风险
（`/api/medication/supply-risk`）都比 `汇总 < 阈值`，采购建议拿汇总合计算缺口，批次台账对过期批次印「状态 normal /
可用 100」；而发药按 FEFO 只取「正常且未过效期」的批次。实测阈值 20、唯一批次入库 100、业务日拨过效期：缺药预警
空、待办 0、驾驶舱 0、供应风险空，已计入 15 粒用量时采购建议也空——同一时刻发药 409「可发批次库存不足」。

修法照 ADR 写好的下一步在读侧现算可发量（`dispense.dispensable_by_drug` / `q_dispensable_shortage`，谓词与
`_fefo_batches` 共用 `dispensable_clause`），不动汇总、不动不变式；响应只增不改（`dispensable_quantity` /
`dispensable` / `dispensable_stock` / 台账 `expired`）。
"""
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import business_today, freeze_business_date

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

CODE = "P1250-AMOX"
RECALL_CODE = "P1250-RCL"
NO_THRESHOLD_CODE = "P1250-NOTH"


@pytest.fixture(scope="module")
def world(client, admin):
    """阈值 20、唯一批次入库 100（效期 +5 天）；一张 15 粒的处方计入近 30 天用量。

    另一味药同样只有一个快过期的批次但没配阈值（阈值 0 永不报），用来钉住「没配预警的照旧不报」。
    """
    today = business_today()
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1250 过期读侧卫生院", "org_type": "township", "level": "township"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1250 患者", "id_card": "330102199001011250"}).json()
    expire = (today + timedelta(days=5)).isoformat()
    for code, threshold in ((CODE, 20), (NO_THRESHOLD_CODE, 0)):
        r = client.post("/api/pharmacy/stocks", headers=admin, json={
            "org_id": org["id"], "drug_code": code, "drug_name": f"{code} 胶囊", "quantity": 0,
            "threshold": threshold})
        assert r.status_code == 200, r.text
        r = client.post("/api/pharmacy/batches", headers=admin, json={
            "org_id": org["id"], "drug_code": code, "drug_name": f"{code} 胶囊", "batch_no": f"{code}-B1",
            "expire_date": expire, "quantity": 100})
        assert r.status_code == 201, r.text
    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "急性咽炎",
        "items": [{"drug_code": CODE, "drug_name": f"{CODE} 胶囊", "daily_dose": 3, "days": 5}]})
    assert rx.status_code == 201, rx.text
    assert rx.json()["status"] == "auto_passed", rx.json()
    return {"org": org["id"], "rx": rx.json()["id"], "later": today + timedelta(days=10)}


def _reads(client, admin, org):
    """五处读侧 + 批次台账 + 驾驶舱下钻，一次取齐。"""
    alerts = {a["drug_code"]: a for a in client.get(f"/api/pharmacy/alerts?org_id={org}", headers=admin).json()}
    todo = next(i for i in client.get("/api/todos", headers=admin).json()["items"] if i["type"] == "stock_shortage")
    overview = client.get("/api/metrics/overview", headers=admin).json()["pharmacy"]["stock_alerts"]
    drill = client.get("/api/metrics/drilldown?metric=stock_alerts", headers=admin).json()
    risks = {r["drug_code"]: r for r in client.get("/api/medication/supply-risk", headers=admin).json()["risks"]}
    sugg = {s["drug_code"]: s for s in client.get("/api/pharmacy/purchase-suggestions", headers=admin).json()}
    batches = {b["drug_code"]: b for b in
               client.get(f"/api/pharmacy/batches?org_id={org}", headers=admin).json()}
    return alerts, todo, overview, drill, risks, sugg, batches


def test_未过期_五处读侧与修前一致_台账可发即可用(client, admin, world):
    alerts, todo, overview, drill, risks, sugg, batches = _reads(client, admin, world["org"])
    assert alerts == {} and todo["count"] == 0 and todo["list"] == []
    assert overview == 0 and drill["total"] == 0 and drill["items"] == []
    assert CODE not in risks
    assert CODE not in sugg                     # 用量 15 < 可发 100，不缺
    b1 = batches[CODE]
    assert (b1["status"], b1["available"], b1["expired"], b1["dispensable"]) == ("normal", 100, False, 100)


def test_唯一批次过期_四处都报缺药_采购建议给缺口_发药同时409(client, admin, world):
    with freeze_business_date(world["later"]):
        alerts, todo, overview, drill, risks, sugg, batches = _reads(client, admin, world["org"])
        dispense = client.post("/api/dispense", headers=admin, json={"prescription_id": world["rx"]})
    # 发药按 FEFO 不取过期批次：这一刻一片也发不出（修前修后都 409，读侧要跟它对上）
    assert dispense.status_code == 409, dispense.text
    # 缺药预警：汇总照旧 100（不改含义），可发 0（修前整条不出现）
    assert set(alerts) == {CODE}
    assert (alerts[CODE]["quantity"], alerts[CODE]["threshold"], alerts[CODE]["dispensable_quantity"]) == (100, 20, 0)
    # 待办：修前 count 0
    assert todo["count"] == 1
    assert [(r["drug_name"], r["quantity"], r["threshold"], r["dispensable"]) for r in todo["list"]] == [
        (f"{CODE} 胶囊", 100, 20, 0)]
    # 驾驶舱：overview 计数与下钻同一个构造（修前 0）；下钻行另起一列可发
    assert overview == 1 and drill["total"] == 1
    assert drill["fields"][-1] == "dispensable" and drill["columns"][-1] == "可发"
    assert (drill["items"][0]["quantity"], drill["items"][0]["dispensable"]) == (100, 0)
    # 供应风险：修前不出现
    assert risks[CODE]["low_stock_orgs"] == 1
    # 采购建议：缺口按可发量算（修前汇总 100 ≥ 用量 15，不建议采购）
    assert sugg[CODE] == {"drug_code": CODE, "drug_name": f"{CODE} 胶囊", "usage_30d": 15.0, "current_stock": 100,
                          "suggested_quantity": 15, "dispensable_stock": 0}
    # 台账：状态照旧 normal（它只表达召回），可用汇总照旧 100；过期与可发现算
    b1 = batches[CODE]
    assert (b1["status"], b1["available"], b1["expired"], b1["dispensable"]) == ("normal", 100, True, 0)
    # 阈值 0 是没配预警：过期了也照旧不报
    assert NO_THRESHOLD_CODE not in alerts and NO_THRESHOLD_CODE not in risks
    assert all(r["drug_name"] != f"{NO_THRESHOLD_CODE} 胶囊" for r in todo["list"])


def test_召回路径不变_余量当场退出汇总_台账可发为零(client, admin, world):
    org = world["org"]
    r = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": RECALL_CODE, "drug_name": "P1250 召回药", "quantity": 0, "threshold": 20})
    assert r.status_code == 200, r.text
    batch = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": RECALL_CODE, "drug_name": "P1250 召回药", "batch_no": "P1250-R1",
        "expire_date": (business_today() + timedelta(days=300)).isoformat(), "quantity": 50}).json()
    assert (batch["expired"], batch["dispensable"]) == (False, 50)
    assert RECALL_CODE not in {a["drug_code"] for a in
                               client.get(f"/api/pharmacy/alerts?org_id={org}", headers=admin).json()}
    recalled = client.post(f"/api/pharmacy/batches/{batch['id']}/recall", headers=admin, json={"reason": "厂家召回"})
    assert recalled.status_code == 200, recalled.text
    body = recalled.json()
    assert (body["status"], body["available"], body["blocked_quantity"], body["expired"], body["dispensable"]) == (
        "recalled", 0, 50, False, 0)
    stock = next(s for s in client.get(f"/api/pharmacy/stocks?org_id={org}", headers=admin).json()
                 if s["drug_code"] == RECALL_CODE)
    assert stock["quantity"] == 0               # 召回照旧同事务扣出汇总
    alert = next(a for a in client.get(f"/api/pharmacy/alerts?org_id={org}", headers=admin).json()
                 if a["drug_code"] == RECALL_CODE)
    assert (alert["quantity"], alert["dispensable_quantity"]) == (0, 0)


def test_近效期预警的响应形状不动():
    """台账加的 `expired` / `dispensable` 不得挤进近效期那份响应、也不得打乱它的键序（近效期另见 P2-34，本条不碰）。"""
    from app.routers.pharmacy import BatchOut, ExpiringBatchOut

    old = ["id", "org_id", "drug_code", "drug_name", "batch_no", "expire_date", "supplier", "quantity",
           "used_quantity", "remaining", "blocked_quantity", "available", "status", "recall_reason"]
    assert list(ExpiringBatchOut.model_fields) == [*old, "remaining_days", "expired"]
    assert list(BatchOut.model_fields) == [*old, "expired", "dispensable"]   # 只在末尾加


def test_页面缺药行与台账印可发量():
    """管理端药房页：缺药行同时印可发量、台账「可用」列印 dispensable 且过效期的标「已过期」、采购建议印可发；
    医生移动端待办卡片给 dispensable 配中文键名（否则卡片上是英文键）。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    body = core[core.index("async function renderPharmacy()"):]
    body = body[:body.index("\n}\n")]
    # 可发量取库存行自带的（P2-1360）：原先取缺药预警行的 `a.dispensable_quantity`，只有缺药行注得上
    assert "s.dispensable_quantity" in body and "${stockQty(s)}" in body
    assert "<td>${b.dispensable}</td>" in body and "<td>${b.available}</td>" not in body
    assert 'b.status === "normal" && b.expired' in body and "已过期" in body
    assert "g.dispensable_stock" in body
    # 召回回执报的是「退出可用汇总」的量：仍取 available（召回扣出汇总的正是它），不改成可发量
    assert "退出可用汇总 ${batch ? batch.available" in body
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    names = doctor[doctor.index("const FIELD_NAMES = {"):]
    names = names[:names.index("};")]
    assert 'dispensable: "可发"' in names
