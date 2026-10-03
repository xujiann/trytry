"""网页开方表单只有一组药品框：一张方只开得出一味药，同方相互作用、同方重复两条审方规则在页面上永远触发不到（P2-1214，
第三十五批扫描 T4-5）。

「集中审方」页的开方面板标题写着「开方（单药演示）」，表单里药品编码、名称、日剂量、天数各只有一个框，提交时 `items`
只有一项；而接口本来收多行（`PrescriptionCreate.items` 是列表），用户手册写的也是「集中审方页开方（可多药品行）」。没有
HIS 的村卫生室只能用这一页开方：华法林、布洛芬只能分两张开，两张都系统审通过（修前实测 auto_passed ×2）；同样两味写进
一张方——修前只有接口做得到——才转药师审「药物相互作用：华法林 与 布洛芬」。系统审不跨方比是 P1-225（待裁定），不在本条。

修法：表单改成可增删的多行明细（「添加一行」「删除本行」，只剩一行时不摆删除），提交时逐行组成 `items` 全部送出；天数
留空沿用原写法不送、后端缺省 1。端到端档另有一条在真浏览器里开两行、看回执转药师审。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")

#: 页面上开的两行：华法林的种子规则把布洛芬列为相互作用药（`data/drug_rules_seed.py`）；第二行天数留空
HEAD = {"patient_id": "0", "org_id": "0", "diagnosis_name": "心房颤动;腰痛"}
ROWS = [{"drug_code": "B01AA03", "drug_name": "华法林", "daily_dose": "3", "days": "7"},
        {"drug_code": "M01AE01", "drug_name": "布洛芬", "daily_dose": "1200", "days": ""}]


def _between(start: str, end: str, src: str = PAGE) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _render() -> str:
    return _between("async function renderRx()", "\n}\n")


def test_开方面板不再是单药演示_药品框挪进可重复的一行():
    assert 'panel("开方", `' in PAGE and "单药演示" not in PAGE
    form = _between('<form class="inline" id="rx-form">', "</form>")
    assert '<div id="rx-items" style="flex-basis:100%">${RX_ITEM_ROW}</div>' in form   # 首屏一行
    assert 'name="drug_code"' not in form                                           # 修前药品框直接写在表单里，只有一组
    row = _between("const RX_ITEM_ROW = `", "</div>`;")
    for field in ('name="drug_code"', 'name="drug_name"', 'name="daily_dose"', 'name="days"'):
        assert row.count(field) == 1, field


def test_添加一行与删除本行_最后一行不能删():
    form = _between('<form class="inline" id="rx-form">', "</form>")
    row = _between("const RX_ITEM_ROW = `", "</div>`;")
    body = _render()
    # 两个按钮都写明 type="button"：缺省是提交按钮，在框里按回车会先「点」到它
    assert '<button type="button" class="btn secondary" id="rx-add-item">添加一行</button>' in form
    assert '<button type="button" class="btn secondary" data-rxdelrow>删除本行</button>' in row
    assert ('$("#rx-add-item").onclick = () => { rxRows.insertAdjacentHTML("beforeend", RX_ITEM_ROW); syncRxDel(); };'
            in body)
    assert 'rxRows.children.length < 2) return;' in body and 'e.target.closest(".rx-item").remove();' in body
    assert 'b.style.display = dels.length > 1 ? "" : "none";' in body                # 只剩一行时不摆「删除本行」
    assert "\n  syncRxDel();\n" in body                                              # 首屏只有一行：画完就收起删除


@pytest.fixture(scope="module")
def page_payload():
    """把页面的提交处理原样拿到 node 里跑一遍：表单两行，看它送出的请求体。

    假表单只认页面用到的两样：逐行取值的 `querySelectorAll(".rx-item")` 与每行的 `[name="…"]`；`FormData.get` 与浏览器
    同一口径——同名的框有几个都只取第一个（修前的写法正是这样只送出第一行）。
    """
    if shutil.which("node") is None:
        pytest.skip("没有 node 可执行这段前端处理")
    items_fn = _between("function rxItems(form) {", "\n}\n") + "\n}\n"
    handler = _between('$("#rx-form").onsubmit', "\n  };\n", _render()) + "\n  };\n"
    script = (
        "const [head, rows] = JSON.parse(process.argv[1]);"
        "const fakeRow = (r) => ({ querySelector: (sel) => ({ value: r[/name=\"(\\w+)\"/.exec(sel)[1]] }) });"
        "const form = { querySelectorAll: (sel) => (sel === '.rx-item' ? rows.map(fakeRow) : []) };"
        "class FormData { get(k) { return k in head ? head[k] : k in rows[0] ? rows[0][k] : null; } }"
        "const handlers = {}; const $ = (sel) => (handlers[sel] = handlers[sel] || {});"
        "let sent = null; const msgs = [];"
        "const api = async (url, opts) => { sent = { url, method: opts.method, body: JSON.parse(opts.body) };"
        "  return { status: 'pending_review', review_comment: '药物相互作用', advisories: [] }; };"
        "const route = async () => {}; const setMsg = (...args) => msgs.push(args);"
        + items_fn + handler +
        "handlers['#rx-form'].onsubmit({ preventDefault() {}, target: form })"
        "  .then(() => console.log(JSON.stringify({ sent, msgs })));"
    )
    out = subprocess.run(["node", "-e", script, json.dumps([HEAD, ROWS], ensure_ascii=False)], capture_output=True,
                         text=True, check=True, timeout=60).stdout
    return json.loads(out)


def test_跑一遍提交处理_表单上几行就送几项(page_payload):
    sent = page_payload["sent"]
    assert (sent["url"], sent["method"]) == ("/api/prescriptions", "POST")
    assert sent["body"]["items"] == [
        {"drug_code": "B01AA03", "drug_name": "华法林", "daily_dose": 3, "days": 7},
        {"drug_code": "M01AE01", "drug_name": "布洛芬", "daily_dose": 1200},   # 天数留空不送，后端缺省 1
    ], sent["body"]["items"]                                                     # 修前只有第一项
    assert page_payload["msgs"] == [["#rx-msg", "转入药师审核：药物相互作用", False]]


def test_页面送出的两行进同一张方_转药师审命中相互作用(client, admin, page_payload):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21214 村卫生室", "org_type": "village", "level": "village"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21214 房颤患者", "id_card": "330106199001012147", "gender": "男"})
    assert patient.status_code == 201, patient.text
    body = {**page_payload["sent"]["body"], "patient_id": patient.json()["id"], "org_id": org}
    made = client.post("/api/prescriptions", headers=admin, json=body)
    assert made.status_code == 201, made.text
    got = made.json()
    assert got["status"] == "pending_review", got                                # 修前页面只开得出一味：auto_passed
    assert "药物相互作用：华法林 与 布洛芬" in got["review_comment"], got["review_comment"]
    assert [(i["drug_code"], i["days"]) for i in got["items"]] == [("B01AA03", 7), ("M01AE01", 1)]
