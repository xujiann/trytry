"""出报告、标本核收这一侧看不到申请单上的临床资料与患者号；病理标本流转表不显示申请单号（P2-1367，第四十批扫描 AD3-7）。

申请单出参本来就带 `clinical_info`（开单医生写的检查目的）与 `patient_id`，病理标本出参本来就带 `request_id`，页面没印：

* 共享诊断中心（`core.js renderExams`）申请单行只有 ID / 患者号 / 中心 / 项目 / 状态，临床资料只出现在开单框与申请单打印件上；
  「出报告」框只有结论 / 所见 / 危急值三个框——同一项目十几张单时，出报告的医师只能凭申请单号对着写，要看检查目的得另开打印页；
* 医生移动端（`m/doctor.js loadExams`）出报告卡片只有申请单 / 中心 / 项目 / 状态，连患者号都没有，临床资料无处可看；
* 病理标本页（`pages-clinical.js renderPathology`）标本流转表没有申请单号，送检登记手输「病理申请单ID」、走 `postAction` 成功即
  重画、什么都不回显——单号敲错一位，标本就挂到别人的病理申请上（拒收原因里的「申请单信息不符」前提是能比对申请单）。

修法（只改页面）：桌面申请单行与移动端出报告卡片印出临床资料（空的不印）与患者号（桌面端本来就有患者号，不重复加），桌面「出报告」
框头印出申请单、项目与临床资料（移动端的出报告表单开在卡片里，卡片上这几行就在表单上方）；标本表加申请单号、项目两列，项目按申请单号
对上——病理申请与标本清单同一趟取，不逐行请求；送检登记成功后先重画、再在提示里回显申请单号（回执里有）与项目（按页面已取到的申请单
对上）。插进 innerHTML 的一律 `esc()`。患者姓名随 P2-681 待裁定，这里只印患者号。

这里把三处页面函数原样拿到 node 里跑（页面辅助函数取自 shared.js / core.js / doctor.js 原文），页面的 `api` 经管道转给真接口；
申请单的项目与临床资料带 HTML 特殊字符，漏一处 `esc()` 原样片段就会出现。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 插进 innerHTML 的用户数据带上 HTML 特殊字符：转义漏一处，下面断言里的原样片段就会出现
CLINICAL_INFO = '发热3天<img src=x onerror="alert(1)">查感染指标'
PATHOLOGY_ITEM = '胃镜活检<b>&"x"'
PATHOLOGY_INFO = "胃窦溃疡待排恶性"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


def _read(*parts: str) -> str:
    return STATIC.joinpath(*parts).read_text(encoding="utf-8")


def _block(src: str, head: str, end: str = "\n}\n") -> str:
    start = src.index(head)
    return src[start:src.index(end, start) + len(end)]


def _const(src: str, name: str) -> str:
    """页面文件里一行写完的常量（`const NAME = {...};`）。"""
    return re.search(rf"^const {name} = .*;$", src, re.M).group(0) + "\n"


def _esc(text: str) -> str:
    """与 shared.js 的 `esc()` 同一张表。"""
    return "".join({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}.get(c, c) for c in text)


SHARED, CORE, CLINICAL, DOCTOR = _read("shared.js"), _read("core.js"), _read("pages-clinical.js"), _read("m", "doctor.js")

#: 页面辅助函数原文（转义、状态标签、表格、面板、待办在前）
HELPERS = "".join([
    _block(SHARED, "function esc(value) {"),
    _block(SHARED, "function statusTag(map, key) {"),
    _block(SHARED, "async function fetchAllPages(get, path) {"),   # 待诊断 / 诊断中两种续页取全（P2-1711）
    _block(CORE, "function table(cols, rows, renderRow) {"),
    _block(CORE, 'function panel(title, body, { accent = "" } = {}) {'),
    _block(CORE, "function actionableFirst(recent, ...actionable) {"),
])

#: 假 DOM 与页面依赖：`$` 记下页面写进去的 innerHTML 与挂上的处理函数；`api` 经标准输入输出转给 Python 侧的真接口
PRELUDE = r"""
const args = JSON.parse(process.argv[1]);
const els = {};
const $ = (sel) => (els[sel] = els[sel] || { innerHTML: "", textContent: "", dataset: {}, listeners: {},
  addEventListener(type, fn) { this.listeners[type] = fn; } });
const msgs = []; const setMsg = (sel, text, ok = true) => { msgs.push([sel, text, ok]); };
const modals = [];
const spdModal = async (title, fields, opts = {}) => {
  modals.push({ title, fields: fields.map((f) => f.name), intro: opts.intro || "" });
  return null;   // 当作点了取消：只看框头
};
let routed = 0; const route = async () => { routed += 1; };
const pollTodos = () => {};
const formJson = (form) => ({ ...form.values });
const requested = [];
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  requested.push(`${method} ${path}`);
  process.stdout.write(JSON.stringify({ req: { path, method, body: opts.body ? JSON.parse(opts.body) : null } }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) throw new Error(JSON.stringify(resp.body.detail));
  return resp.body;
}
"""


def _run(client, headers, code: str, main: str, args: dict | None = None) -> dict:
    """在 node 里跑页面代码 `code`，再执行 `main`（async 函数体，return 结果）；页面的请求转给真接口。

    node 写完结果不自己关输入：页面里不等的请求（修前 `postAction` 就不等）可能排在结果前面还没答，这边按行序答完、读到结果
    再关输入，node 随之退出。
    """
    script = (
        PRELUDE + code
        + f"\n(async () => {{ {main} }})()"
        ".then((out) => { process.stdout.write(JSON.stringify({ done: out }) + '\\n'); })"
        ".catch((err) => { process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + '\\n'); });\n"
    )
    proc = subprocess.Popen(["node", "-e", script, json.dumps(args or {}, ensure_ascii=False)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(50):
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "done" in message:
                return message["done"]
            assert "error" not in message, message["error"]
            req = message["req"]
            resp = client.request(req["method"], req["path"], headers=headers, json=req["body"])
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        raise AssertionError("页面请求停不下来")
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _row(html: str, opener: str) -> str:
    """表格里以 `opener` 开头的那一行（到 `</tr>`）。"""
    start = html.index(opener)
    return html[start:html.index("</tr>", start)]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21367 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21367 患者", "id_card": "330106197003031367", "gender": "男"})
    assert patient.status_code == 201, patient.text
    pid = patient.json()["id"]

    def exam(center_type, item_code, item_name, clinical_info=""):
        made = client.post("/api/exams", headers=admin, json={
            "patient_id": pid, "from_org_id": org, "center_type": center_type, "item_code": item_code,
            "item_name": item_name, "clinical_info": clinical_info})
        assert made.status_code == 201, made.text
        return made.json()["id"]

    lab = exam("lab", "P21367-CBC", "P21367 血常规", CLINICAL_INFO)
    bare = exam("imaging", "P21367-DR", "P21367 胸部DR")   # 没写临床资料
    assert client.post(f"/api/exams/{bare}/claim", headers=admin).status_code == 200   # 诊断中的也在移动端「待审」里
    pathology = exam("pathology", "P21367-BX", PATHOLOGY_ITEM, PATHOLOGY_INFO)
    specimen = client.post("/api/pathology/specimens", headers=admin, json={"request_id": pathology, "site": "胃窦"})
    assert specimen.status_code == 201, specimen.text
    return {"org": org, "patient": pid, "lab": lab, "bare": bare, "pathology": pathology, "specimen": specimen.json()}


# ---------- 共享诊断中心（管理端） ----------

EXAMS_PAGE = (
    HELPERS + "".join(_const(CORE, name) for name in ("CENTER_NAMES", "EXAM_STATUS", "SAMPLE_NEXT", "SAMPLE_STATUS"))
    + _block(CORE, "async function renderExams() {")
)


@pytest.fixture(scope="module")
def exams_page(client, admin, world):
    return _run(client, admin, EXAMS_PAGE, """
      await renderExams();
      const body = els["#page-body"].innerHTML;
      await els["#page-body"].onclick({ target: { dataset: { report: String(args.lab) } } });
      return { body, modals };
    """, {"lab": world["lab"]})


def test_桌面申请单行印出临床资料_转义_空的不印(exams_page, world):
    row = _row(exams_page["body"], f"<tr><td>{world['lab']}</td><td>{world['patient']}</td>")   # 患者号本来就在第二列
    assert f"临床资料：{_esc(CLINICAL_INFO)}" in row, row   # 修前这一行没有临床资料
    assert CLINICAL_INFO not in exams_page["body"] and "<img" not in exams_page["body"]   # 进 innerHTML 的过了 esc()
    bare = _row(exams_page["body"], f"<tr><td>{world['bare']}</td><td>{world['patient']}</td>")
    assert "临床资料" not in bare, bare   # 没写临床资料的不印空标签
    page = _block(CORE, "async function renderExams() {")
    assert len(re.findall(r"\br\.patient_id\b", page)) == 1   # 桌面行本来就有患者号一列，没有再加一处


def test_桌面出报告框头印出申请单_项目与临床资料(exams_page, world):
    [modal] = exams_page["modals"]
    assert modal["title"] == "出报告" and modal["fields"] == ["conclusion", "finding", "critical"]
    # 框头是纯文本，由 spdModal 自己 esc() 之后放进框里（见下一条）；修前框头为空
    assert f"申请单 {world['lab']}" in modal["intro"] and f"患者 {world['patient']}" in modal["intro"], modal
    assert "项目：P21367 血常规（P21367-CBC）" in modal["intro"], modal
    assert f"临床资料：{CLINICAL_INFO}" in modal["intro"], modal


def test_框头由spdModal转义():
    modal = _block(_read("pages-spd.js"), "function spdModal(title, fields, opts = {}) {")
    assert '${opts.intro ? `<div class="desc" style="white-space:pre-wrap;font-size:12px">${esc(opts.intro)}</div>` : ""}' \
        in modal


# ---------- 医生移动端 ----------

DOCTOR_PAGE = (
    HELPERS + _block(DOCTOR, "function kv(k, v) {") + _block(DOCTOR, 'function card(inner, ops = "") {')
    + _const(DOCTOR, "CENTER_NAMES") + _const(DOCTOR, "EXAM_STATUS") + _block(DOCTOR, "async function loadExams() {")
)


def test_移动端出报告卡片印出患者号与临床资料_转义_空的不印(client, admin, world):
    body = _run(client, admin, DOCTOR_PAGE, 'await loadExams(); return els["#exam-list"].innerHTML;')
    cards = {}
    for part in body.split('<div class="m-card">')[1:]:   # 一张卡片一段，开头就是申请单号那一行
        cards[int(re.match(r'<div class="kv"><span class="k">申请单</span><span>(\d+)</span></div>', part).group(1))] = part
    lab, bare = cards[world["lab"]], cards[world["bare"]]
    for card in (lab, bare):   # 修前卡片上连患者号都没有
        assert f'<span class="k">患者号</span><span>{world["patient"]}</span>' in card, card
    assert f'<span class="k">临床资料</span><span>{_esc(CLINICAL_INFO)}</span>' in lab, lab
    assert "临床资料" not in bare, bare
    assert CLINICAL_INFO not in body and "<img" not in body


# ---------- 病理标本 ----------

PATHOLOGY_PAGE = HELPERS + _block(CLINICAL, "async function postAction(path, body, msgSel, method) {") + _block(
    CLINICAL, "async function renderPathology() {")


@pytest.fixture(scope="module")
def pathology_page(client, admin, world):
    """渲染一遍病理页，送检登记两次：一次挂在页面取到的那张病理申请上；一次挂在页面打开之后才开的病理申请上（`route` 是桩，
    页面不重画，处理函数手里还是打开时取到的那份申请单清单）。"""
    late = {"patient_id": world["patient"], "from_org_id": world["org"], "center_type": "pathology",
            "item_code": "P21367-LATE", "item_name": "P21367 页面打开之后才开的单"}
    return _run(client, admin, PATHOLOGY_PAGE, """
      await renderPathology();
      const body = els["#page-body"].innerHTML;
      const loads = requested.slice();
      await els["#sp-form"].onsubmit({ preventDefault() {}, target: { values: { request_id: args.request, site: "胃体" } } });
      const first = { msgs: msgs.splice(0), routed, submitted: requested.slice(loads.length) };
      const late = await api("/api/exams", { method: "POST", body: JSON.stringify(args.late) });
      await els["#sp-form"].onsubmit({ preventDefault() {}, target: { values: { request_id: late.id } } });
      return { body, loads, first, late: { id: late.id, msgs: msgs.splice(0) } };
    """, {"request": world["pathology"], "late": late})


def test_标本流转表加申请单号与项目两列_项目按申请单号对上_转义(pathology_page, world):
    body = pathology_page["body"]
    assert "<th>标本号</th><th>申请单号</th><th>项目</th><th>部位</th>" in body   # 修前没有这两列
    row = _row(body, f"<tr><td>{world['specimen']['specimen_no']}</td>")
    assert f"<td>{world['pathology']}</td><td>{_esc(PATHOLOGY_ITEM)}</td>" in row, row
    assert "<b>" not in body


def test_项目与标本清单同一趟取_不逐行请求(pathology_page):
    exams = [r for r in pathology_page["loads"] if r.startswith("GET /api/exams")]
    assert exams == ["GET /api/exams?center_type=pathology&limit=500"], pathology_page["loads"]


def test_送检登记成功先重画_再回显申请单号与项目(pathology_page, world):
    first = pathology_page["first"]
    assert first["submitted"] == ["POST /api/pathology/specimens"] and first["routed"] == 1
    [(sel, text, ok)] = first["msgs"]   # 修前走 postAction：成功即重画，什么都不说
    assert sel == "#sp-msg" and ok is True
    # 回显写进消息行的 textContent（不是 innerHTML），原样印出、不转义
    assert f"申请单 {world['pathology']}" in text and f"项目 {PATHOLOGY_ITEM}" in text, text


def test_送检登记回显的项目对不上时照实说(pathology_page):
    """回执只带申请单号；页面取到的病理申请里没有这一张（页面打开之后才开的单、或在最新 500 张之外），回显照实说、不编项目。"""
    late = pathology_page["late"]
    [(sel, text, ok)] = late["msgs"]
    assert sel == "#sp-msg" and ok is True
    assert f"申请单 {late['id']}" in text and "项目未对上（本页取到的病理申请里没有这一张）" in text, text
