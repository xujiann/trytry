"""接种登记不送剂次时恒记「第 1 剂」，与接种前评估算出的「本次为第 N 剂」对不上（P2-987，第二十八批「编号、单号与流水号」
扫描 F3-2 / 「缺省值」扫描 F4-2）。

`RecordCreate.dose_no` 缺省 1，页面剂次框又预填 1、每次都送：第二、三针都存成、接种证都印成「第 1 剂」；接种前评估按
既往条数报「本次为第 N 剂」，系统里两个数自相矛盾。接种证是入托入学查验与补种判断的依据。对接方不带剂次时同样落 1。

修法：不送剂次就按「同一疫苗的既往剂次 + 1」取，与接种前评估共用一个算法（按比对键认编码，P1-219）；送了的照送的记；
页面剂次框不再预填。同一疫苗出现相同剂次号拦不拦（补种、无效剂次重种、加强剂）另行待裁定。
"""
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2987 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    child = client.post("/api/patients", headers=admin, json={
        "name": "P2987 儿童", "id_card": "330106202401020087", "birth_date": "2024-01-02"})
    assert child.status_code in (200, 201), child.text
    return {"org": org, "child": child.json()["id"]}


def _vaccinate(client, admin, world, day, code="HepB", **extra):
    resp = client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": world["child"], "vaccine_code": code, "vaccine_name": "乙肝疫苗", "org_id": world["org"],
        "vaccinated_date": day, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["dose_no"]


def test_不送剂次_按既往剂次加一_与接种前评估同一个数(client, admin, world):
    assert _vaccinate(client, admin, world, "2024-01-03") == 1
    assert _vaccinate(client, admin, world, "2024-02-03", code="hepb") == 2   # 修前 1；编码写法不同照样认（P1-219）
    check = client.get("/api/vaccination/pre-check", headers=admin,
                       params={"patient_id": world["child"], "vaccine_code": "HepB"}).json()
    assert (check["previous_doses"], check["next_dose_no"]) == (2, 3)
    assert _vaccinate(client, admin, world, "2024-07-03") == check["next_dose_no"]   # 修前 1


def test_送了剂次的照送的记(client, admin, world):
    assert _vaccinate(client, admin, world, "2024-08-03", dose_no=1) == 1


def test_页面剂次框不再预填1():
    form = PAGE[PAGE.index('<form class="inline" id="vac-form">'):]
    form = form[:form.index("</form>")]
    dose = form[form.index('name="dose_no"'):]
    dose = dose[:dose.index(">")]
    assert 'value="1"' not in dose and "留空" in dose, dose
