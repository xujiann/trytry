"""互认方的申请单行打不开依据的那份报告与修订史，打印件也不写依据的是哪一张（P2-1200，第三十四批「跨机构协作的两端」
扫描 L2-11）。

互认不另出报告：乙院给同一患者开同项目检查时选了互认，新单直接「已互认」、只记 `recognized_from_id`。检查申请清单
（`GET /api/exams`）的 `report_id` 只按本单取（第十六批 T1-6），互认单恒为空；页面按 `report_id` 摆「打印报告 / 修订史」，
于是乙院那一行一个按钮都没有。开发库实测（修前代码）：乙院行 `{'status': 'recognized', 'report_id': None,
'recognized_from_id': 1}`；源报告随后被非危急修订为「右肺上叶结节 8mm…」，乙院行上照旧没有报告号；打印乙院的申请单，
「含源申请单号? False」。

修法：清单出参新增 `recognized_report_id`（取 `recognized_from_id` 那张源申请单的报告；`report_id` 仍只是本单自己的报告，
语义不变），页面据此给「查看依据报告」「依据报告修订史」；互认单的打印件写明「互认自 SQxxxxxxxx」。
"""
import re
from pathlib import Path

import pytest
from conftest import login

CORE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name in (("src", "P21200 甲院"), ("rec", "P21200 乙院")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21200_rec", "password": "passw0rd1", "full_name": "乙院医师", "role": "doctor",
        "org_id": orgs["rec"]})
    assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21200 患者", "id_card": "331782199004041200", "gender": "男"}).json()["id"]
    exam = {"patient_id": patient, "center_type": "imaging", "item_code": "DR-P21200", "item_name": "胸部DR(P2-1200)"}
    source = client.post("/api/exams", headers=admin, json={**exam, "from_org_id": orgs["src"]})
    assert source.status_code == 201, source.text
    assert client.post(f"/api/exams/{source.json()['id']}/claim", headers=admin).status_code == 200
    report = client.post(f"/api/exams/{source.json()['id']}/report", headers=admin, json={
        "finding": "双肺纹理清晰", "conclusion": "双肺未见明显异常", "critical": False, "reported_by": "影像科"})
    assert report.status_code in (200, 201), report.text
    recognized = client.post("/api/exams", headers=admin, json={
        **exam, "from_org_id": orgs["rec"], "accept_recognition_of": source.json()["id"]})
    assert recognized.status_code == 201 and recognized.json()["status"] == "recognized", recognized.text
    pending = client.post("/api/exams", headers=admin, json={**exam, "from_org_id": orgs["rec"],
                                                             "recognition_declined_reason": "患者要求本院复查"})
    assert pending.status_code == 201, pending.text
    return {"source": source.json()["id"], "report": report.json()["id"], "recognized": recognized.json()["id"],
            "pending": pending.json()["id"], "rec": login(client, "p21200_rec", "passw0rd1")}


def _row(client, headers, request_id):
    rows = client.get("/api/exams?limit=500", headers=headers)
    assert rows.status_code == 200, rows.text
    [row] = [r for r in rows.json() if r["id"] == request_id]
    return row


def test_互认单行带依据报告的报告号_本单报告号语义不变(client, world):
    row = _row(client, world["rec"], world["recognized"])
    assert (row["status"], row["report_id"], row["recognized_from_id"]) == ("recognized", None, world["source"])
    assert row["recognized_report_id"] == world["report"]   # 修前没有这一列
    source = _row(client, world["rec"], world["source"])
    assert (source["report_id"], source["recognized_report_id"]) == (world["report"], None)
    pending = _row(client, world["rec"], world["pending"])   # 不互认、还没出报告的：两列都空
    assert (pending["report_id"], pending["recognized_report_id"]) == (None, None)


def test_互认方照着这个报告号打得开依据的报告_修订后查得到修订史(client, admin, world):
    report_id = _row(client, world["rec"], world["recognized"])["recognized_report_id"]
    printed = client.get(f"/api/print/exam-reports/{report_id}", headers=world["rec"])
    assert printed.status_code == 200 and "双肺未见明显异常" in printed.text
    amended = client.patch(f"/api/exams/reports/{world['report']}", headers=admin, json={
        "conclusion": "右肺上叶结节 8mm，建议3个月复查CT", "reason": "复阅更正"})
    assert amended.status_code == 200, amended.text
    revisions = client.get(f"/api/exams/reports/{report_id}/revisions", headers=world["rec"])
    assert revisions.status_code == 200, revisions.text
    assert [r["prev_conclusion"] for r in revisions.json()] == ["双肺未见明显异常"]


def test_互认单的打印件写明互认自哪张申请单(client, world):
    html = client.get(f"/api/print/exam-requests/{world['recognized']}", headers=world["rec"])
    assert html.status_code == 200, html.text
    # 修前只有「当前状态 已互认」，看不出依据的是哪一张
    assert f'<td class="k">互认依据</td><td colspan="3">互认自 SQ{world["source"]:08d}</td>' in html.text
    assert '<td class="k">当前状态</td><td>已互认</td>' in html.text
    for other in ("source", "pending"):   # 不是互认单的打印件不多这一行
        assert "互认依据" not in client.get(f"/api/print/exam-requests/{world[other]}", headers=world["rec"]).text


def test_页面按依据报告号摆查看依据报告与修订史():
    start = CORE.index("async function renderExams()")
    page = CORE[start:CORE.index("\n}\n", start)]
    assert "if (r.recognized_report_id) {" in page   # 修前互认单那一行没有任何报告按钮
    buttons = re.findall(r'data-(printreport|revs)="\$\{esc\(r\.recognized_report_id\)\}">([^<]+)</button>', page)
    assert buttons == [("printreport", "查看依据报告"), ("revs", "依据报告修订史")], buttons
    assert '<td>${r.report_id ?? "—"}</td>' in page   # 「报告ID」一列仍只印本单自己的报告号
