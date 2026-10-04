"""代煎单清单看得出代煎 / 自煎，「流转」按钮写明下一步（P2-1407，第四十一批扫描 AE4-1）。

后端按开单时选的 `decoct` 分两条路走：已调配之后，代煎的去「已煎煮」，不代煎（自煎）的直接「配送中」；用户手册也写着
「不煎煮订单自动跳过煎煮环节」。修前中药房执行流转的人在页面上看不到这个分叉：清单只有 ID / 患者 / 饮片 / 剂数 / 状态 /
操作，不引用 `o.decoct`，按钮一律写「流转」——两张单同停在「已调配」时一模一样，代煎单点下去记成「已煎煮」（没煎也这么记），
自煎单点下去直接「配送中」。下单表单还把不代煎写成「免煎」（免煎通常指配方颗粒，后端没有颗粒这一说）。

修后出参 `DispenseOut` 末尾只增 `next_status_name`（下一步状态的中文，终态为 null；与流转同一个判据 `_next_status`，
按煎法选 `_DISPENSE_FLOW` / `_NO_DECOCT_FLOW`）；页面加「煎法」列，按钮按 `next_status_name` 写「标为……」，不另抄流转表；
表单的「免煎」改叫「自煎（不代煎）」。
"""
import json
import os
import shutil
import subprocess

import pytest

from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 修前代煎单出参的键与次序：只许在末尾加一个 `next_status_name`
ORDER_KEYS = ["patient_id", "from_org_id", "herbs", "doses", "decoct", "id", "status"]
#: 修后清单的表头
HEADER = '["ID", "患者", "饮片", "剂数", "煎法", "状态", "操作"]'


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _render_tcm() -> str:
    source = _read("pages-clinical.js")
    start = source.index("async function renderTcm()")
    return source[start:source.index("\nasync function ", start + 1)]


def _row_template() -> str:
    """清单行模板：`(o) =>` 起，到行尾的 ``</tr>` `` 止（原样拿去 node 里跑）。"""
    body = _render_tcm()
    begin = body.index(f"table({HEADER}, orders, (o) =>")
    arrow = body.index("(o) =>", begin)
    return body[arrow:body.index("</tr>`", arrow) + len("</tr>`")]


@pytest.fixture(scope="module")
def world(client, admin):
    """甲镇卫生院：医师开两张单（一张代煎、一张自煎），药师把两张都推到「已调配」。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21407 甲镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, role in (("p21407_doc", "doctor"), ("p21407_pha", "pharmacist")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": org})
        assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21407 患者", "id_card": "330106197001011407"})
    assert patient.status_code in (200, 201), patient.text
    doc, pha = login(client, "p21407_doc", "pw123456"), login(client, "p21407_pha", "pw123456")
    created = {}
    for decoct, herbs in ((True, "制附子15g(先煎) 干姜10g 炙甘草6g"), (False, "黄芪30g 白术15g 防风10g")):
        resp = client.post("/api/tcm/dispense-orders", headers=doc, json={
            "patient_id": patient.json()["id"], "from_org_id": org, "herbs": herbs, "doses": 7, "decoct": decoct})
        assert resp.status_code == 201, resp.text
        created[decoct] = resp.json()
    return {"org": org, "doc": doc, "pha": pha, "created": created}


def _advance(client, world, order_id):
    resp = client.post(f"/api/tcm/dispense-orders/{order_id}/advance", headers=world["pha"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_出参末尾只增下一步_两张单同在已调配时下一步不同(client, world):
    created = world["created"]
    # 新建回执：原有键与次序不动，下一步都是「已调配」
    assert [list(created[d]) for d in (True, False)] == [[*ORDER_KEYS, "next_status_name"]] * 2
    assert [created[d]["next_status_name"] for d in (True, False)] == ["已调配", "已调配"]
    # 流转回执同形：两张都到了「已调配」，下一步一张「已煎煮」、一张「配送中」（修前没有这个键，两张单看不出分别）
    advanced = {d: _advance(client, world, created[d]["id"]) for d in (True, False)}
    assert [list(advanced[d]) for d in (True, False)] == [[*ORDER_KEYS, "next_status_name"]] * 2
    assert [(advanced[d]["status"], advanced[d]["next_status_name"]) for d in (True, False)] == [
        ("dispensed", "已煎煮"), ("dispensed", "配送中")]
    # 清单行同一个口径
    rows = {r["id"]: r for r in client.get("/api/tcm/dispense-orders", headers=world["pha"]).json()}
    assert [list(rows[created[d]["id"]]) for d in (True, False)] == [[*ORDER_KEYS, "next_status_name"]] * 2
    assert [rows[created[d]["id"]]["next_status_name"] for d in (True, False)] == ["已煎煮", "配送中"]
    by_status = client.get("/api/tcm/dispense-orders", headers=world["pha"], params={"status": "dispensed"}).json()
    assert {r["id"]: r["next_status_name"] for r in by_status} == {
        created[True]["id"]: "已煎煮", created[False]["id"]: "配送中"}


def test_按钮写的下一步就是点下去真走的那一步_走到终态为空(client, world):
    created = world["created"]
    for decoct in (True, False):
        order = client.get("/api/tcm/dispense-orders", headers=world["pha"]).json()
        order = next(r for r in order if r["id"] == created[decoct]["id"])
        while order["next_status_name"] is not None:
            promised = order["next_status_name"]
            order = _advance(client, world, order["id"])
            assert {"dispensed": "已调配", "decocted": "已煎煮", "delivering": "配送中",
                    "delivered": "已送达"}[order["status"]] == promised
        assert order["status"] == "delivered"   # 终态：下一步为空，再推 409
        resp = client.post(f"/api/tcm/dispense-orders/{order['id']}/advance", headers=world["pha"])
        assert resp.status_code == 409 and resp.json()["detail"] == "状态 已送达 已是终态", resp.text


def test_页面加煎法列_按钮按后端给的下一步写字_表单不再写免煎():
    body = _render_tcm()
    row = _row_template()
    assert f"table({HEADER}, orders, (o) =>" in body   # 修前表头没有「煎法」
    assert '<td>${o.decoct ? "代煎" : "自煎"}</td>' in row   # 修前行模板不引用 o.decoct
    assert "o.next_status_name" in row and "标为${esc(o.next_status_name)}" in row
    assert ">流转</button>" not in row   # 修前一律写「流转」
    assert '<option value="false">自煎（不代煎）</option>' in body and ">免煎</option>" not in body
    # 页面不另抄一份流转表：下一步只取出参
    assert "decocted: \"delivering\"" not in body and "dispensed: \"delivering\"" not in body


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的行模板")
def test_两张同在已调配的单_页面按钮文字不同(client, world):
    """清单行模板原样拿到 node 里跑，喂接口原样返回的行：一张写「标为已煎煮」、一张写「标为配送中」，煎法列各写各的。"""
    def order(herbs, decoct, steps):
        resp = client.post("/api/tcm/dispense-orders", headers=world["doc"], json={
            "patient_id": world["created"][True]["patient_id"], "from_org_id": world["org"], "herbs": herbs,
            "doses": 3, "decoct": decoct})
        assert resp.status_code == 201, resp.text
        for _ in range(steps):
            _advance(client, world, resp.json()["id"])
        return resp.json()["id"]

    decocting, self_decoct = order("川芎10g", True, 1), order("当归10g", False, 1)   # 同在「已调配」
    delivered = order("红花6g", False, 3)                                               # 自煎三步到「已送达」
    wanted = {decocting, self_decoct, delivered}
    rows = [r for r in client.get("/api/tcm/dispense-orders", headers=world["pha"]).json() if r["id"] in wanted]
    assert len(rows) == 3
    body = _render_tcm()
    ds = body[body.index("const DS = "):body.index(";", body.index("const DS = ")) + 1]
    script = (
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + _read("shared.js") + "\n" + ds
        + f"\nconst row = {_row_template()};\n"
        + "const rows = JSON.parse(process.argv[1]);\n"
        "process.stdout.write(JSON.stringify(rows.map((o) => {\n"
        "  const html = row(o);\n"
        "  const cells = [...html.matchAll(/<td>([\\s\\S]*?)<\\/td>/g)].map((m) => m[1].trim());\n"
        "  const button = html.match(/data-adv=\"\\d+\">([^<]*)<\\/button>/);\n"
        "  return [o.id, o.decoct, cells[4], button ? button[1] : null];\n"
        "})));\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(rows, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    rendered = {oid: (decoct, method, button) for oid, decoct, method, button in json.loads(done.stdout)}
    assert rendered[decocting] == (True, "代煎", "标为已煎煮")
    assert rendered[self_decoct] == (False, "自煎", "标为配送中")   # 修前两行都是「流转」
    assert rendered[delivered] == (False, "自煎", None)             # 已送达：不摆按钮
