"""医嘱执行可补填执行时刻、出参与页面印本地时刻，医嘱单印开立 / 停止时刻（P2-1694，第五十批扫描 AN4-2）。

修前（实测，`TZ=Asia/Shanghai`）：执行登记 `ExecutionCreate` 只收说明与皮试结果，执行时刻只能是点按钮那一刻——夜班事后补登的
给药只能记成补登那一刻；出参 `executed_at` 是落库的 naive UTC，住院页执行记录把它截成「YYYY-MM-DD HH:MM」照印：同一刻登记的
皮试执行显示 10:30，关联它的护理记录（不填时刻落 `clock.now_local()`，P2-455）显示 18:30。医嘱单表头「ID / 类型 / 内容 /
状态 / 开立 / 操作」，看不出医嘱哪天开、哪天停（临时医嘱又一直「执行中」，P2-281）。

修法照 P2-1633（门急诊处置执行时间）：执行登记收可选的执行时刻（与护理记录同一写法、同一校验类型 `OptionalDateTimeStr`，填的
是本地时刻，换成 naive UTC 落进 `executed_at` 列），不填即此刻；执行出参末尾追加 `executed_at_shown`（本地时刻，照
`clinical_docs._shown_time`），`executed_at` 原样；医嘱出参末尾追加 `created_at_shown` / `stopped_at_shown`（同一个帮手）。
页面执行记录印 `executed_at_shown`、登记执行框可填执行时刻，医嘱单补「开立时刻」「停止时刻」两列。补录的上下界（早于开立、
晚于停止、将来）不在本条，随 P2-1135 一并定。
"""
import re
import shutil
import time
from datetime import datetime

import pytest
from conftest import login
from inpatient_page import run

#: 钉住的此刻：本地（东八区）10:30 即 UTC 02:30
FIXED_LOCAL = datetime(2026, 10, 9, 10, 30)
FIXED_UTC = datetime(2026, 10, 9, 2, 30)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture
def shanghai(monkeypatch):
    """进程时区换成东八区（服务端在同一进程里，`astimezone()` 按它换算），用完复原。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21694 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for username, role, name in (("p21694_doc", "doctor", "甲医生"), ("p21694_nurse", "operator", "甲护士")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org, "full_name": name})
        assert created.status_code in (200, 201), created.text
    doctor, nurse = login(client, "p21694_doc", "passw0rd1"), login(client, "p21694_nurse", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21694 内科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21694-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21694 患者", "id_card": "330106196606061694", "gender": "男"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=doctor, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return {"adm": adm.json()["id"], "doctor": doctor, "nurse": nurse}


def _order(client, world, content, order_type="temp"):
    resp = client.post("/api/inpatient/orders", headers=world["doctor"], json={
        "admission_id": world["adm"], "order_type": order_type, "content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _executions(client, world, order_id):
    resp = client.get(f"/api/inpatient/orders/{order_id}/executions", headers=world["nurse"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_不填执行时刻_落此刻_与同一刻的护理记录显示一致(client, world, shanghai, monkeypatch):
    from app.routers import clinical_docs, inpatient

    monkeypatch.setattr(inpatient, "utcnow", lambda: FIXED_UTC)
    monkeypatch.setattr(clinical_docs, "now_local", lambda: FIXED_LOCAL)
    order = _order(client, world, "P21694 青霉素皮试")
    executed = client.post(f"/api/inpatient/orders/{order['id']}/executions", headers=world["nurse"],
                           json={"note": "已做", "skin_test_result": "negative"})
    nursing = client.post(f"/api/inpatient/admissions/{world['adm']}/nursing-records", headers=world["nurse"],
                          json={"content": "皮试后观察 20 分钟无不适", "inpatient_order_id": order["id"]})
    assert (executed.status_code, nursing.status_code) == (201, 201), (executed.text, nursing.text)
    assert executed.json()["executed_at"] == "2026-10-09T02:30:00"   # 落库口径（naive UTC）原样
    # 修前页面拿 executed_at 截成「2026-10-09 02:30」，护理记录印「2026-10-09 10:30」
    assert executed.json()["executed_at_shown"] == nursing.json()["recorded_at"] == "2026-10-09 10:30"
    assert [x["executed_at_shown"] for x in _executions(client, world, order["id"])] == ["2026-10-09 10:30"]


def test_填了执行时刻照填的落(client, world, shanghai):
    order = _order(client, world, "P21694 呋塞米 20mg iv st")
    resp = client.post(f"/api/inpatient/orders/{order['id']}/executions", headers=world["nurse"],
                       json={"note": "夜班补登", "executed_at": "2026-10-09 03:15"})
    assert resp.status_code == 201, resp.text
    # 修前请求里的执行时刻被忽略，记成补登那一刻
    assert (resp.json()["executed_at"], resp.json()["executed_at_shown"]) == ("2026-10-08T19:15:00", "2026-10-09 03:15")
    assert [x["executed_at_shown"] for x in _executions(client, world, order["id"])] == ["2026-10-09 03:15"]


def test_执行时刻形状不对_422不落库(client, world):
    order = _order(client, world, "P21694 形状")
    resp = client.post(f"/api/inpatient/orders/{order['id']}/executions", headers=world["nurse"],
                       json={"executed_at": "2026/10/09 03:15"})
    assert resp.status_code == 422, resp.text   # 修前被忽略、照样 201
    assert _executions(client, world, order["id"]) == []


def test_执行出参新键接在末尾_原有键与次序不变(client, world):
    order = _order(client, world, "P21694 键序")
    resp = client.post(f"/api/inpatient/orders/{order['id']}/executions", headers=world["nurse"], json={})
    assert list(resp.json()) == ["id", "inpatient_order_id", "executed_by", "executed_by_name", "executed_at", "note",
                                 "skin_test_result", "nursing_record_count", "executed_at_shown"]


def test_医嘱出参末尾带开立与停止的本地时刻(client, world, shanghai, monkeypatch):
    from app.routers import inpatient

    monkeypatch.setattr(inpatient, "utcnow", lambda: FIXED_UTC)
    order = _order(client, world, "P21694 一级护理", order_type="long")
    assert list(order)[-2:] == ["created_at_shown", "stopped_at_shown"]
    assert (order["created_at_shown"], order["stopped_at_shown"]) == (_local(order["created_at"]), None)
    stopped = client.post(f"/api/inpatient/orders/{order['id']}/stop", headers=world["doctor"])
    assert stopped.status_code == 200, stopped.text
    assert (stopped.json()["stopped_at"], stopped.json()["stopped_at_shown"]) == ("2026-10-09T02:30:00", "2026-10-09 10:30")
    listed = client.get(f"/api/inpatient/orders?admission_id={world['adm']}", headers=world["doctor"]).json()
    assert [(o["created_at_shown"], o["stopped_at_shown"]) for o in listed if o["id"] == order["id"]] == [
        (order["created_at_shown"], "2026-10-09 10:30")]


def _local(iso_utc: str) -> str:
    """落库的 naive UTC（ISO）换成本地「YYYY-MM-DD HH:MM」——东八区加 8 小时。"""
    from datetime import timedelta
    return (datetime.fromisoformat(iso_utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


# ---------- 页面（原样拿到 node 里跑，夹具见 tests/inpatient_page.py） ----------


@needs_node
def test_页面_执行记录印本地时刻_医嘱单有开立与停止时刻两列(client, world, shanghai):
    order = _order(client, world, "P21694 页面 头孢呋辛 1.5g ivgtt bid", order_type="long")
    assert client.post(f"/api/inpatient/orders/{order['id']}/executions", headers=world["nurse"],
                       json={"note": "P21694 页面执行", "executed_at": "2026-10-09 03:15"}).status_code == 201
    stopped = client.post(f"/api/inpatient/orders/{order['id']}/stop", headers=world["doctor"]).json()
    steps = """
      await renderInpatient();
      await click({ orders: String(ARGS.params.adm) });
      const orders = htmlOf("#inp-orders");
      await click({ execList: String(ARGS.params.order) });
      return { orders, exec: htmlOf("#inp-exec") };
    """
    html = run(client, world["doctor"], steps, {"adm": world["adm"], "order": order["id"]})
    assert "<th>开立时刻</th><th>停止时刻</th>" in html["orders"]   # 修前没有这两列
    row = re.search(rf'<tr><td>{order["id"]}</td>.*?</tr>', html["orders"], re.S).group(0)
    assert f"<td>{order['created_at_shown']}</td><td>{stopped['stopped_at_shown']}</td>" in row
    # 修前印 executed_at 截出来的「2026-10-08 19:15」（UTC）
    assert "<td>2026-10-09 03:15</td>" in html["exec"] and "2026-10-08 19:15" not in html["exec"]


@needs_node
def test_页面_登记执行框可填执行时刻_填了照送(client, world):
    order = _order(client, world, "P21694 页面 登记执行")
    steps = """
      await renderInpatient();
      await click({ orders: String(ARGS.params.adm) });
      const pending = click({ execAdd: String(ARGS.params.order) });
      await tick();
      const modal = lastModal();
      await submitModal(modal, { executed_at: "2026-10-09 03:15" });
      await pending;
      return { html: modal.html, posts };
    """
    got = run(client, world["nurse"], steps, {"adm": world["adm"], "order": order["id"]})
    assert 'name="executed_at"' in got["html"]   # 修前框里只有说明与皮试结果
    assert got["posts"] == [["POST", f"/api/inpatient/orders/{order['id']}/executions",
                             {"note": "", "executed_at": "2026-10-09 03:15"}]]
