"""只按分项标了异常的体检，在异常清单与体检清单上是一个空的红标签（P2-422）。

异常口径是「汇总异常串非空 **或** 任一分项异常」（`create_checkup`），而两张清单显示的是录入的汇总串
`abnormal_items`——录了分项、没另写汇总的体检，清单上标着异常却看不出哪项异常。打印件早就按「汇总没写时列出
标了异常的分项」印（P2-205）。修后两张清单多回一个 `abnormal_text`（与打印件同一个帮手），页面显示它；
`abnormal_items` 仍原样回显录入的汇总串。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def ctx(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2422 体检中心", "org_type": "lead_hospital", "level": "county"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2422 受检者", "id_card": "330127198801012422", "gender": "女"}).json()
    client.post("/api/users", headers=admin, json={
        "username": "p2422_ph", "password": "pass123456", "role": "public_health", "org_id": org["id"]})
    ph = login(client, "p2422_ph", "pass123456")

    def checkup(date, items, abnormal_items=""):
        resp = client.post("/api/checkups", headers=ph, json={
            "patient_id": patient["id"], "org_id": org["id"], "exam_date": date,
            "abnormal_items": abnormal_items, "items": items})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    item = lambda code, name, abnormal: {  # noqa: E731
        "item_code": code, "item_name": name, "result_value": "1", "abnormal": abnormal}
    ids = {
        "items_only": checkup("2026-09-01", [item("HB", "血红蛋白", True), item("GLU", "空腹血糖", True),
                                             item("ALT", "丙氨酸氨基转移酶", False)]),
        "summary": checkup("2026-09-02", [item("HB", "血红蛋白", True)], abnormal_items="贫血待查"),
        "normal": checkup("2026-09-03", [item("HB", "血红蛋白", False)]),
    }
    return {"ph": ph, "patient": patient["id"], "ids": ids}


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_异常清单_只按分项标了异常的列出分项名(client, ctx):
    rows = _by_id(client.get("/api/checkups/abnormal", headers=ctx["ph"]).json())
    row = rows[ctx["ids"]["items_only"]]
    assert row["abnormal_text"] == "血红蛋白、空腹血糖", row   # 修前没有这个键，页面上是空的红标签
    assert row["abnormal_items"] == ""                           # 录入的汇总串原样回显
    assert rows[ctx["ids"]["summary"]]["abnormal_text"] == "贫血待查"   # 写了汇总的照汇总
    assert ctx["ids"]["normal"] not in rows


def test_体检清单_同一口径(client, ctx):
    rows = _by_id(client.get("/api/checkups", headers=ctx["ph"], params={"patient_id": ctx["patient"]}).json())
    assert rows[ctx["ids"]["items_only"]]["abnormal_text"] == "血红蛋白、空腹血糖"
    assert rows[ctx["ids"]["summary"]]["abnormal_text"] == "贫血待查"
    assert rows[ctx["ids"]["normal"]]["abnormal_text"] == ""


def test_打印件与清单同一个帮手(client, ctx):
    html = client.get(f"/api/print/checkups/{ctx['ids']['items_only']}", headers=ctx["ph"]).text
    assert "血红蛋白、空腹血糖" in html
