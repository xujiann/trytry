"""体检分项在页面上录不了；接口一侧同一项目可以重复录（P2-1404，第四十一批扫描 AE3-3 的录入一半）。

模块 docstring 写「登记接口新增可选 items 分项列表…分项录完后由总检医师出总检结论」，后端 `CheckupCreate.items` 早就收，
可登记表单只有患者、机构、套餐、日期、汇总两栏：页面登记的体检分项永远是 0 条，端到端用例造分项也只能绕开页面直接调接口。
接口一侧同一次体检里 GLU 5.2 与 GLU 12.8 各收一条（修前实测 201），报告上并排印两行、哪个作数说不清。登记、分项、打印的
报错都写到页面最上方证明面板的 `#cert-msg`，中间隔着最多 200 行的证明表。

修后：登记表单可增删分项行（项目编码 / 名称 / 结果 / 单位 / 参考范围 / 是否异常，写法照开方明细 `RX_ITEM_ROW`，分项选填、
首屏不摆空行），提交时逐行组成 `items`；同一次体检里项目编码重复（前后空格不算区别）整单 422 点名、一条不落；体检的报错写在
体检面板自己的消息行 `#chk-msg`。登记后补录 / 更正分项、按参考范围自动判异常是业务口径，不在本条。端到端档另有一条在真
浏览器里开三行、看重复编码报在体检面板、删掉一行再登记、`/items` 回两条。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PAGE = (STATIC / "pages-public.js").read_text(encoding="utf-8")
CLINICAL = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")

#: 页面上填的表头（formJson 收的只有带 name 的这几栏；套餐留空不送，后端缺省「常规体检」）与两行分项
HEAD = {"patient_id": "0", "org_id": "0", "package_name": "", "exam_date": "2026-10-02", "summary": "血压偏高"}
ROWS = [{"item_code": "GLU", "item_name": "空腹血糖", "result_value": "5.2", "unit": "mmol/L", "ref_range": "3.9-6.1",
         "abnormal": False},
        {"item_code": "SBP", "item_name": "收缩压", "result_value": "182", "unit": "mmHg", "ref_range": "90-139",
         "abnormal": True}]


def _between(start: str, end: str, src: str = PAGE) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _render() -> str:
    return _between("async function renderCerts()", "\nasync function ")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21404 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21404 受检者", "id_card": "330127195002021404", "gender": "女"}).json()["id"]
    return {"org": org, "patient": patient}


def _create(client, admin, world, items, **head):
    return client.post("/api/checkups", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "exam_date": "2026-10-02", **head, "items": items})


def _count(client, admin, world):
    return len(client.get("/api/checkups", headers=admin, params={"patient_id": world["patient"]}).json())


@pytest.mark.parametrize("second", ["GLU", " GLU "], ids=["同一编码", "前后多了空格"])
def test_同一次体检分项编码重复_整单422点名_一条不落(client, admin, world, second):
    before = _count(client, admin, world)
    resp = _create(client, admin, world, [ROWS[0], {**ROWS[0], "item_code": second, "result_value": "12.8"}, ROWS[1]])
    assert resp.status_code == 422, resp.text                       # 修前 201：GLU 5.2 与 12.8 各收一条
    assert resp.json()["detail"] == "同一次体检里项目编码重复：GLU（每个项目只录一行，核对哪一行作数后再登记）"
    assert _count(client, admin, world) == before


def test_编码不同的照收(client, admin, world):
    resp = _create(client, admin, world, ROWS)
    assert resp.status_code == 201, resp.text
    items = client.get(f"/api/checkups/{resp.json()['id']}/items", headers=admin).json()
    assert [(i["item_code"], i["abnormal"]) for i in items] == [("GLU", False), ("SBP", True)]


def test_登记表单有可增删的分项行_六栏都在():
    form = _between('<form class="inline" id="chk-form">', "</form>")
    assert '<div id="chk-items" style="flex-basis:100%"></div>' in form              # 分项选填：首屏不摆空行
    assert '<button type="button" class="btn secondary" id="chk-add-item">添加分项</button>' in form
    assert "item_code" not in form                                                  # 修前表单上根本没有分项框
    row = _between("const CHK_ITEM_ROW = `", "</div>`;")
    for field in ("item_code", "item_name", "result_value", "unit", "ref_range"):
        assert row.count(f'data-item="{field}"') == 1, field
    assert '<input data-item="abnormal" type="checkbox">' in row
    assert " name=" not in row                                     # 带 name 的框会被 formJson 摊成请求体的顶层字段
    assert '<button type="button" class="btn secondary" data-chkdelrow>删除本行</button>' in row
    body = _render()
    assert '$("#chk-add-item").onclick = () => chkRows.insertAdjacentHTML("beforeend", CHK_ITEM_ROW);' in body
    assert 'e.target.closest(".chk-item").remove();' in body


def test_体检的报错写在体检面板自己的消息行():
    body = _render()
    form_panel = _between('<form class="inline" id="chk-form">', "${abnormal.length ? panel(", body)
    assert '<p class="msg" id="chk-msg"></p>' in form_panel
    submit = _between('$("#chk-form").onsubmit', "\n  };\n", body)
    assert '"#chk-msg"' in submit and '"#cert-msg"' not in submit   # 修前登记的报错写到页面最上方的 #cert-msg
    click = _between('$("#page-body").onclick', "\n  };\n", body)
    assert 'const msgSel = chkitems || chkreview || printchk ? "#chk-msg" : "#cert-msg";' in click
    assert "catch (err) { setMsg(msgSel, err.message, false); }" in click   # 修前分项、打印的报错也写到 #cert-msg


@pytest.fixture(scope="module")
def page_payload():
    """把页面的提交处理原样拿到 node 里跑一遍：表头 + 两行分项，看它送出的请求体。

    formJson 用 pages-clinical.js 里的原件；假 FormData 只给带 name 的表头几栏（浏览器里不带 name 的分项框不进 FormData——
    这一半由端到端档在真浏览器里钉）。假表单认页面用到的两样：逐行取值的 `querySelectorAll(".chk-item")` 与每行的
    `[data-item="…"]`。`postAction` 记下地址、请求体与消息行。
    """
    if shutil.which("node") is None:
        pytest.skip("没有 node 可执行这段前端处理")
    form_json = _between("function formJson(form, numFields = []) {", "\n}\n", CLINICAL) + "\n}\n"
    items_fn = _between("function chkItems(form) {", "\n}\n") + "\n}\n"
    handler = _between('$("#chk-form").onsubmit', "\n  };\n", _render()) + "\n  };\n"
    script = (
        "const [head, rows] = JSON.parse(process.argv[1]);"
        "const fakeRow = (r) => ({ querySelector: (sel) => {"
        "  const v = r[/data-item=\"(\\w+)\"/.exec(sel)[1]]; return { value: v, checked: v }; } });"
        "const form = { querySelectorAll: (sel) => (sel === '.chk-item' ? rows.map(fakeRow) : []) };"
        "class FormData { entries() { return Object.entries(head); } }"
        "const handlers = {}; const $ = (sel) => (handlers[sel] = handlers[sel] || {});"
        "let sent = null; const postAction = (path, body, msgSel) => { sent = { path, body, msgSel }; };"
        + form_json + items_fn + handler +
        "handlers['#chk-form'].onsubmit({ preventDefault() {}, target: form });"
        "console.log(JSON.stringify(sent));"
    )
    out = subprocess.run(["node", "-e", script, json.dumps([HEAD, ROWS], ensure_ascii=False)], capture_output=True,
                         text=True, check=True, timeout=60).stdout
    return json.loads(out)


def test_跑一遍提交处理_表单上几行分项就送几项(page_payload):
    assert (page_payload["path"], page_payload["msgSel"]) == ("/api/checkups", "#chk-msg")
    assert page_payload["body"] == {
        "patient_id": 0, "org_id": 0, "exam_date": "2026-10-02", "summary": "血压偏高",   # 套餐留空不送
        "items": ROWS,                                                                   # 修前请求体里没有 items
    }, page_payload["body"]


def test_页面送出的两行分项进同一次体检_清单标出异常项(client, admin, world, page_payload):
    body = {**page_payload["body"], "patient_id": world["patient"], "org_id": world["org"]}
    made = client.post("/api/checkups", headers=admin, json=body)
    assert made.status_code == 201, made.text
    assert made.json()["has_abnormal"] is True and made.json()["package_name"] == "常规体检"
    items = client.get(f"/api/checkups/{made.json()['id']}/items", headers=admin).json()
    assert [(i["item_code"], i["result_value"], i["abnormal"]) for i in items] == [
        ("GLU", "5.2", False), ("SBP", "182", True)]                                  # 修前页面登记的分项永远是 0 条
    rows = client.get("/api/checkups", headers=admin, params={"patient_id": world["patient"]}).json()
    assert [r["abnormal_text"] for r in rows if r["id"] == made.json()["id"]] == ["收缩压"]
