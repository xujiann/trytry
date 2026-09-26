"""体检报告打印版的异常标注不瞎写方向、不自相矛盾（P2-205）。

分项的 `abnormal` 是不带方向的布尔（页面上就是一个「异常」勾），打印版却一律印「异常 ↑」——血红蛋白 95（参考 115–150）
偏低，纸上读成偏高；汇总的「异常项」串是选填的，分项标了异常、汇总没写时，下面的「异常项提示」又印「无」，同一张纸
上下矛盾。
"""
import pytest


@pytest.fixture(scope="module")
def checkup_id(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2205 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2205 受检者", "id_card": "330106199109091617"}).json()["id"]
    resp = client.post("/api/checkups", headers=admin, json={
        "patient_id": patient, "org_id": org, "exam_date": "2026-09-20", "summary": "贫血待查",
        "items": [{"item_code": "HB", "item_name": "血红蛋白", "result_value": "95", "unit": "g/L",
                   "ref_range": "115-150", "abnormal": True},
                  {"item_code": "WBC", "item_name": "白细胞", "result_value": "6.1", "unit": "10^9/L",
                   "ref_range": "3.5-9.5", "abnormal": False}]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_异常不带方向_异常项提示列出标了异常的分项(client, admin, checkup_id):
    html = client.get(f"/api/print/checkups/{checkup_id}", headers=admin).text
    assert "异常 ↑" not in html                                     # 修前偏低的血红蛋白也印「异常 ↑」
    assert '<span class="critical">异常</span>' in html
    tip = html[html.index("<h3>异常项提示</h3>"):]
    tip = tip[:tip.index("</div></div>")]
    assert "血红蛋白" in tip and "白细胞" not in tip                  # 修前汇总没写就印「无」
