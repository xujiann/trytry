"""修订过的检查报告，打印版写明「已修订 N 次、以本版为准」与最后一次修订的人、时间、原因（P1-150）。

修订（`PATCH /api/exams/reports/{id}`）改的是所见 / 结论 / 危急值，报告医师与报告时间仍是首次出具的；打印版原先只印
这两项——改判后的结论署着原报告医师的名字与原来的时间，前后两次打印同一个单据编号、结论相反，两张都能验真为
「有效」（ADR-0015 明确接受单张不可吊销），纸面上看不出哪张是修订版。修订史表（M-6）早就记了修订人与时间。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def report(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1150 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1150 患者", "id_card": "330106197808081507"}).json()
    for username in ("p1150_wang", "p1150_li"):
        client.post("/api/users", headers=admin, json={
            "username": username, "password": "pass123456", "role": "doctor", "org_id": org["id"],
            "full_name": {"p1150_wang": "王医师", "p1150_li": "李医师"}[username]})
    wang, li = login(client, "p1150_wang", "pass123456"), login(client, "p1150_li", "pass123456")
    req = client.post("/api/exams", headers=wang, json={
        "patient_id": patient["id"], "from_org_id": org["id"], "center_type": "imaging",
        "item_code": "CT-CHEST", "item_name": "胸部 CT"}).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=wang)
    rep = client.post(f"/api/exams/{req['id']}/report", headers=wang, json={
        "finding": "双肺纹理清晰", "conclusion": "未见异常", "critical": False, "reported_by": "王医师"})
    assert rep.status_code in (200, 201), rep.text
    return {"id": rep.json()["id"], "li": li}


def test_未修订的报告不印修订标记(client, admin, report):
    html = client.get(f"/api/print/exam-reports/{report['id']}", headers=admin).text
    assert "未见异常" in html and "已修订" not in html


def test_修订后打印写明修订次数与最后一次修订的人和原因(client, admin, report):
    for conclusion, reason in (("右肺上叶结节，建议复查", "复阅发现结节"), ("右肺上叶结节 8mm，建议 3 个月复查", "补充大小")):
        resp = client.patch(f"/api/exams/reports/{report['id']}", headers=report["li"], json={
            "conclusion": conclusion, "reason": reason})
        assert resp.status_code == 200, resp.text
    html = client.get(f"/api/print/exam-reports/{report['id']}", headers=admin).text
    assert "右肺上叶结节 8mm，建议 3 个月复查" in html
    assert "本报告已修订 2 次，以本版为准" in html          # 修前没有任何修订标记
    assert "最后一次修订：李医师" in html and "原因：补充大小" in html
    assert "报告医师：王医师" in html                       # 首次出具的署名照旧保留
