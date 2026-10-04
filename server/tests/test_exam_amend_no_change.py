"""报告修订同值重送不算修订：送来的每一项都与现值相同时 422「没有改动」（P2-1365，第四十批扫描 AD3-10「同值应拦下」那一半）。

`exams.amend_report` 只判「三项都没送」才 422（文案「修订须至少改结论、所见或危急值标记中的一项」——要的是「改」），送来的值
与现值完全相同照样写一条修订史；仍是危急值的，把已处置的闭环复位成「已通知」、重发通知、重进待办与超时催办，打印件印
「本报告已修订 N 次，以本版为准」。页面修订框（`core.js`）选了「是危急值」就送 `critical:true`、所见非空就送，原样提交就能走到。
修前实测：头颅 CT 危急值已处置，PATCH `{critical: true}`、把结论原样重送都 200，各写一条与原值一字不差的修订史，闭环 resolved →
notified，申请机构医师多收两条「危急值（报告修订）」，待办「待确认危急值 1」，打印件印「本报告已修订 2 次」。

修法：送来的每一项都与现值相同（没送的不算）→ 422「没有改动」，不写修订史、不复位闭环、不发通知；判在归属校验之后。有任何一项
真改了，行为不变。「结论与危急标记都没变、只改所见时要不要复位已处置的危急值」是另一句业务口径，本条不动（最后一条用例钉着现状）。
"""
import pytest

from conftest import login

CONCLUSION = "左侧硬膜下血肿"
FINDING = "左额颞部新月形高密度影"


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21365 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town_a, town_b = (client.post("/api/organizations", headers=admin, json={
        "name": f"P21365 {name}乡卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
        for name in ("甲", "乙"))
    for username, org, full_name in (("p21365_a", town_a, "甲医生"), ("p21365_b", town_b, "乙医生"),
                                     ("p21365_c", county, "县中心李医生")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": full_name})
        assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21365 张三", "id_card": "330106197211133017", "gender": "男"})
    assert patient.status_code in (200, 201), patient.text
    return {"town_a": town_a, "patient": patient.json()["id"],
            "a": login(client, "p21365_a", "passw0rd1"), "b": login(client, "p21365_b", "passw0rd1"),
            "c": login(client, "p21365_c", "passw0rd1")}


def _report(client, world, *, critical, finding=FINDING, resolve=False):
    """甲院开单、县中心领取并出报告；危急值的可再由甲院确认接收、处置反馈到「已处置」。"""
    req = client.post("/api/exams", headers=world["a"], json={
        "patient_id": world["patient"], "from_org_id": world["town_a"], "center_type": "imaging",
        "item_code": "CT-HEAD", "item_name": "头颅CT平扫"}).json()
    assert client.post(f"/api/exams/{req['id']}/claim", headers=world["c"]).status_code == 200
    rep = client.post(f"/api/exams/{req['id']}/report", headers=world["c"], json={
        "conclusion": CONCLUSION, "finding": finding, "critical": critical})
    assert rep.status_code == 201, rep.text
    report_id = rep.json()["id"]
    if resolve:
        assert client.post(f"/api/exams/reports/{report_id}/acknowledge", headers=world["a"]).status_code == 200
        resolved = client.post(f"/api/exams/reports/{report_id}/resolve", headers=world["a"], json={"note": "已转神经外科"})
        assert resolved.json()["critical_status"] == "resolved", resolved.text
    return report_id


def _state(client, world, report_id):
    """修订史条数、闭环状态、处置轨迹条数、甲院医师的消息条数、打印件有没有修订标记。"""
    revisions = client.get(f"/api/exams/reports/{report_id}/revisions", headers=world["c"]).json()
    critical = {r["id"]: r["critical_status"] for r in client.get("/api/exams/critical", headers=world["a"]).json()}
    actions = client.get(f"/api/exams/reports/{report_id}/critical-actions", headers=world["c"]).json()
    notes = [n for n in client.get("/api/notifications", headers=world["a"]).json() if n["link_id"] == report_id]
    printed = client.get(f"/api/print/exam-reports/{report_id}", headers=world["c"]).text
    return {"revisions": len(revisions), "critical_status": critical.get(report_id), "actions": len(actions),
            "notices": len(notes), "amended_mark": "本报告已修订" in printed}


@pytest.mark.parametrize("body, named", [
    ({"critical": True}, "危急值标记"),   # 页面修订框选「是危急值」、别的不动
    ({"conclusion": CONCLUSION}, "结论"),
    ({"conclusion": CONCLUSION, "finding": FINDING, "critical": True, "reason": "复核无误"}, "结论、所见、危急值标记"),
])
def test_已处置的危急值同值重送_422_修订史闭环通知都不动(client, world, body, named):
    report_id = _report(client, world, critical=True, resolve=True)
    before = _state(client, world, report_id)
    resp = client.patch(f"/api/exams/reports/{report_id}", headers=world["c"], json=body)
    assert resp.status_code == 422, resp.text   # 修前 200
    assert resp.json()["detail"] == f"没有改动：送来的{named}与报告现值相同"
    assert _state(client, world, report_id) == before == {
        "revisions": 0, "critical_status": "resolved", "actions": 3, "notices": 1, "amended_mark": False}
    todo = next(i for i in client.get("/api/todos", headers=world["a"]).json()["items"] if i["type"] == "critical_ack")
    assert report_id not in [r["id"] for r in todo["list"]]   # 修前重进「待确认危急值」
    overdue = client.get("/api/exams/critical/unacknowledged?timeout_minutes=0", headers=world["a"]).json()
    assert report_id not in [r["report_id"] for r in overdue]   # 修前重进超时催办


def test_非危急报告同值重送也拦下_改判危急值照旧(client, world):
    report_id = _report(client, world, critical=False, finding="")
    for body, named in (({"critical": False}, "危急值标记"), ({"finding": ""}, "所见")):
        resp = client.patch(f"/api/exams/reports/{report_id}", headers=world["c"], json=body)
        assert (resp.status_code, resp.json()["detail"]) == (422, f"没有改动：送来的{named}与报告现值相同"), resp.text
    assert client.get(f"/api/exams/reports/{report_id}/revisions", headers=world["c"]).json() == []
    changed = client.patch(f"/api/exams/reports/{report_id}", headers=world["c"], json={"critical": True, "finding": ""})
    assert changed.status_code == 200, changed.text   # 有一项真改了（改判危急值），照旧
    assert changed.json()["critical_status"] == "notified"


def test_别家医生同值重送_先判归属_403不是422(client, world):
    """拿送来的值与现值比在归属校验之后：无关机构不能凭 422「没有改动」试出别家报告的结论。"""
    report_id = _report(client, world, critical=True, resolve=True)
    resp = client.patch(f"/api/exams/reports/{report_id}", headers=world["b"], json={"conclusion": CONCLUSION})
    assert resp.status_code == 403, resp.text
    assert client.get(f"/api/exams/reports/{report_id}/revisions", headers=world["c"]).json() == []


def test_有一项真改了照旧修订_只改所见仍复位闭环是现状(client, world):
    """只改所见（结论与危急标记原样送）是真改了：照旧记修订史、仍是危急值的复位成「已通知」并重发通知——要不要复位是另一句
    业务口径，本条不动，这里钉着现状。"""
    report_id = _report(client, world, critical=True, resolve=True)
    resp = client.patch(f"/api/exams/reports/{report_id}", headers=world["c"], json={
        "conclusion": CONCLUSION, "finding": FINDING + "，中线右移 3 mm", "critical": True})
    assert resp.status_code == 200, resp.text
    revisions = client.get(f"/api/exams/reports/{report_id}/revisions", headers=world["c"]).json()
    assert [(r["prev_conclusion"], r["prev_finding"], r["prev_critical"]) for r in revisions] == [
        (CONCLUSION, FINDING, True)]
    state = _state(client, world, report_id)
    assert (state["critical_status"], state["notices"], state["amended_mark"]) == ("notified", 2, True)
