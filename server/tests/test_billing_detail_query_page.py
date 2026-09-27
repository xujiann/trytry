"""「计费与结算」只能计、不能查：结算前看不到这次住院 / 就诊挂着哪些未结清的明细（P2-493）。

`GET /api/billing/details` 一直在（按患者可见性收口，可按患者 / 住院单 / 就诊 / 是否已结算筛），前端一个调用都没有
（读动词棘轮 P2-475 登记在册）；计错了数量、挂错了住院单，要到结算单的总额对不上才发现。

修法：「计费与结算」一块补「计费明细查询」：按患者 / 住院单 / 就诊之一查（一项都不填不查，不在页面上拉全县明细），
缺省只看未结清；列出项目、单价、数量、金额与结算状态，写明条数与合计，截到 500 条时明说。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_billing() -> str:
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderBilling()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_计费明细有查询入口_缺省看未结清():
    body = _render_billing()
    form = body[body.index('<form class="inline" id="bd-query">'):]
    form = form[:form.index("</form>")]
    for name in ("patient_id", "admission_id", "encounter_id", "settled"):
        assert f'name="{name}"' in form, name
    assert form.index('<option value="false">未结清</option>') < form.index('<option value="">全部</option>')   # 缺省未结清
    handler = body[body.index('$("#bd-query").onsubmit'):]
    assert "api(`/api/billing/details?${query}`)" in handler   # 修前没有一个调用
    assert '["patient_id", "admission_id", "encounter_id"].some((k) => query.has(k))' in handler   # 不拉全县
    assert "已截到 500 条" in handler


def test_按住院单查未结清(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2493 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2493 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2493-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2493 患者", "id_card": "320981197006062477", "gender": "男"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"}).json()["id"]
    assert client.post("/api/billing/charge-items", headers=admin, json={
        "code": "P2493-1", "name": "P2493 雾化", "category": "treatment", "price": 12.5}).status_code == 201
    for qty in (2, 1):
        assert client.post("/api/billing/details", headers=admin, json={
            "patient_id": patient, "admission_id": adm, "item_code": "P2493-1", "quantity": qty}).status_code == 201
    rows = client.get("/api/billing/details", headers=admin, params={"admission_id": adm, "settled": "false"}).json()
    assert sorted(r["amount"] for r in rows) == [12.5, 25.0] and not any(r["settled"] for r in rows)
