"""家医签约页写着「履约记录」，却只能记、不能看：这一户做过几次上门、几次随访，解约之后更是无从查起（P2-494）。

`GET /api/contracts/{contract_id}/services` 一直在（按协议患者判可见性并留痕），前端一个调用都没有（读动词棘轮 P2-475
登记在册）；出参也不带履约时刻。

修法：每份签约（含已解约的）一个「履约记录」，按次列出时间、类型、备注，记完履约即展开；出参 `ContractServiceOut`
补 `created_at`。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_contracts() -> str:
    source = (STATIC / "core.js").read_text(encoding="utf-8")
    start = source.index("async function renderContracts()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_每份签约都能看履约记录_解约了也能():
    body = _render_contracts()
    assert "api(`/api/contracts/${contractId}/services`)" in body   # 修前没有一个调用
    ops = body[body.index('<td><button class="btn secondary" data-svclist="${c.id}">履约记录</button>'):]
    assert ops.index('data-svclist="${c.id}"') < ops.index('c.status === "active"'), "「履约记录」不能挂在「履约中」的条件里"
    table = body[body.index('table(["时间", "类型", "备注"]'):]
    table = table[:table.index("</tr>`)")]
    for field in ("r.created_at", "r.service_type", "r.note"):
        assert field in table, field
    record = body[body.index("if (svc) {"):]
    record = record[:record.index("if (term) {")]
    assert "await drawServices(svc);" in record   # 记完即展开


def test_出参带履约时刻(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2494 患者", "id_card": "320981197006062477"}).json()["id"]
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2494 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    contract = client.post("/api/contracts", headers=admin, json={
        "patient_id": patient, "org_id": org, "doctor_name": "P2494 医生", "package": "basic"})
    assert contract.status_code == 201, contract.text
    cid = contract.json()["id"]
    created = client.post(f"/api/contracts/{cid}/services", headers=admin, json={"service_type": "visit", "note": "上门"})
    assert created.status_code == 201, created.text
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", created.json()["created_at"]), created.json()   # 修前没有这个键
    rows = client.get(f"/api/contracts/{cid}/services", headers=admin).json()
    assert [(r["service_type"], r["created_at"]) for r in rows] == [("visit", created.json()["created_at"])]
