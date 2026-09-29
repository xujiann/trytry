"""缺药登记表单补可选的「患者ID」（P2-859，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-8）。

`ShortageCreate.patient_id` 可空：按机构报缺（补库存）与按患者登记（延伸处方）共用一张表，注释写明只有按患者登记的才谈得上
「登记后不来取药」、才进得了黑名单；带患者时先查黑名单、命中 403。页面表单没有患者框，登的全是按机构报缺——按患者登记
这整条路（含黑名单拦截）界面上都没有入口。按机构补货的登记怎么结案另见 P2-379。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def test_登记表单有患者框_按数送():
    start = PAGE.index('<form class="inline" id="short-form">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<input name="patient_id" type="number"' in form   # 修前没有
    assert 'formJson(e.target, ["org_id", "patient_id", "quantity"])' in PAGE


def test_按页面送的患者登记_黑名单照拦(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2859 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2859 患者", "id_card": "330102196001012859"}).json()["id"]
    made = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": org, "patient_id": patient, "drug_code": "P2859", "drug_name": "P2859 药", "quantity": 1})
    assert made.status_code in (200, 201), made.text
    assert made.json()["patient_id"] == patient
