"""医保「转诊证明」只凭一个转诊记录号签发：平台转诊与慢专病转诊各自从 1 编号，照慢专病转诊单号来签，证明挂到了同号的另一位
患者的平台转诊上（P2-997，第二十八批「编号、单号与流水号」扫描 F3-3）。

`issue_referral_cert` 只收路径里的转诊号、`db.get(Referral, …)`，回执只有证明号与转诊号；页面表单只有一个「转诊记录ID」框，
成功只提示证明号。经办按慢专病转诊页上乙那张单的号去签，返回 200，证明挂在甲的平台转诊上，签错了看不出来；复签幂等，甲那张
早签过的话乙拿到的就是甲的证明号。县外就诊挂转诊单时同时收患者号，不符即 422「该转诊单不属于此患者」。

修法：签发接口可带 `patient_id`，带了就核对转诊单的患者（同一句 422）；页面一律带上。回执形状不变（契约用例钉着），不带的
老调用照旧。慢专病转诊能不能开医保转诊证明另行待裁定。
"""
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2998 {name}", "org_type": kind, "level": level}).json()["id"]
        for name, kind, level in (("卫生院", "township", "township"), ("县医院", "lead_hospital", "county"))]
    patients = {}
    for key, id_card in (("jia", "330106197101010098"), ("yi", "33010619720202009X")):
        made = client.post("/api/patients", headers=admin, json={"name": f"P2998 {key}", "id_card": id_card})
        assert made.status_code in (200, 201), made.text
        patients[key] = made.json()["id"]
    referral = client.post("/api/referrals", headers=admin, json={
        "patient_id": patients["jia"], "from_org_id": orgs[0], "to_org_id": orgs[1], "direction": "up",
        "reason": "P2998 上转"}).json()
    assert client.patch(f"/api/referrals/{referral['id']}/status", headers=admin,
                        json={"status": "accepted"}).status_code == 200
    return {"referral": referral["id"], **patients}


def test_带上患者号_不是这位患者的转诊单422(client, admin, world):
    wrong = client.post(f"/api/insurance/referral-certs/{world['referral']}", headers=admin,
                        params={"patient_id": world["yi"]})
    assert wrong.status_code == 422 and wrong.json()["detail"] == "该转诊单不属于此患者", wrong.text   # 修前 200
    right = client.post(f"/api/insurance/referral-certs/{world['referral']}", headers=admin,
                        params={"patient_id": world["jia"]})
    assert right.status_code == 200 and set(right.json()) == {"cert_no", "referral_id"}, right.text   # 回执形状不变


def test_页面签发一律带上患者号():
    form = PAGE[PAGE.index('<form class="inline" id="cert-form">'):]
    form = form[:form.index("</form>")]
    assert 'name="patient_id"' in form and "required" in form[form.index('name="patient_id"'):]
    handler = PAGE[PAGE.index('$("#cert-form").onsubmit'):]
    handler = handler[:handler.index("};")]
    assert "?patient_id=" in handler
