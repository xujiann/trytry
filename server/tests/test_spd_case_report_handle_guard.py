"""慢专病个案上报的处置：已完成 / 已关闭的不能再改；空备注不抹掉已写的处置备注（P2-213）。

原先处置接口不看状态：已由甲在 09-20 办结的上报，乙 09-26 再发一次「关闭」照样 200，处置人与处置时间改写成乙与那一刻；
页面上「开始处置」写了「已联系家属嘱急诊」，「处置完成」时备注留空，原先就被抹成空串。本文件的「结束咨询」（P2-75）、
慢专病入组申请（「该申请已处理」）早就这样拦。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def report_id(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2213 患者", "id_card": "330106196411111659"}).json()["id"]
    resp = client.post(f"{B}/case-reports", headers=admin, json={
        "patient_id": patient, "report_type": "dispose", "content": "血压 190/110，头痛"})
    assert resp.status_code == 201, resp.text
    return {"id": resp.json()["id"], "patient": patient}


def _row(client, admin, report):
    rows = client.get(f"{B}/case-reports?patient_id={report['patient']}", headers=admin).json()
    return next(r for r in rows if r["id"] == report["id"])


def test_空备注不抹掉已写的处置备注(client, admin, report_id):
    assert client.post(f"{B}/case-reports/{report_id['id']}/handle", headers=admin,
                       json={"status": "handling", "handle_note": "已联系家属嘱急诊"}).status_code == 200
    assert client.post(f"{B}/case-reports/{report_id['id']}/handle", headers=admin,
                       json={"status": "done"}).status_code == 200
    row = _row(client, admin, report_id)
    assert (row["status"], row["handle_note"]) == ("done", "已联系家属嘱急诊")          # 修前备注被抹成空串


def test_办结后不能再改处置结果(client, admin, report_id):
    again = client.post(f"{B}/case-reports/{report_id['id']}/handle", headers=admin,
                        json={"status": "closed", "handle_note": "改个结论"})
    assert again.status_code == 409, again.text                                        # 修前 200
    row = _row(client, admin, report_id)
    assert (row["status"], row["handle_note"]) == ("done", "已联系家属嘱急诊")
