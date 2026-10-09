"""居民端住院费用清单明细过 500 条只列最早的 500 条、标题照印「明细（500）」；宣教文章第 51 篇之后翻不到（P2-1778，第五十二批
扫描 AP1-4）。

修前（实测 `ap1/r3_adm_bill_items.py`）：`/api/portal/me/admissions/{id}/bill` 的明细按收费先后（id 正序）分页、缺省 500 条，
「费用合计」按全部明细算；`m.js` 只取第一页、不读总数，520 条明细的住院标题印「明细（500）」、费用合计 ¥3120.00，而列出来的
500 条加起来是 ¥3000——最新的 20 笔看不到，与同卡片的合计对不上（同形 P2-1550，`m.js` 早有 `withTotal`）。宣教文章接口一页
50 篇、docstring 写着「切分页的目的是让第 51 篇之后翻得到」，页面同样不带 offset、不读总数、不提示截断。

修法：费用清单按 X-Total-Count 续页取全（`withAllBillItems`；对账用的清单列一半对不上合计，一次住院的明细以住院天数封顶），
标题写明细条数、万一没取全写「已列 N / 共 M」；宣教文章读总数，列不全时写「已列 N / 共 M 篇」并给「更多」按页续取（点了才取，
接口把匿名单次可取量压在 50 篇）。后端排序与出参不动。

页面那几条把 `m.js` 的 `api()` 与取数、渲染函数原文放进 node 跑，`fetch` 经管道转给真接口（居民本人的身份），响应头照真接口
给；DOM 用一个够这几段用的小替身，`querySelectorAll` 从 innerHTML 里认出带标记的按钮。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import BillDetail, User

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

PHONE = "13900017780"
#: 明细条数：比接口一页（500）多 20 条；最后 20 条（最新的收费）名字不同，修前不在页上
N, NEWEST = 520, 20
ARTICLES = 60


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21778 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21778 长住院居民", "id_card": "330782195001011778", "gender": "男", "birth_date": "1950-01-01",
        "phone": PHONE})
    assert patient.status_code == 201, patient.text
    pid = patient.json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21778 康复病区"}).json()
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": "08"}).json()
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": pid, "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "脑梗死恢复期"})
    assert adm.status_code == 201, adm.text
    adm_id = adm.json()["id"]
    item = client.post("/api/billing/charge-items", headers=admin, json={
        "code": "P21778INJ", "name": "静脉输液", "category": "treatment", "price": 6})
    assert item.status_code == 201, item.text
    with SessionLocal() as db:   # 逐条走接口太慢，直接落库：按收费先后，最后 20 条是最新的那几笔
        uid = db.query(User).filter(User.username == "admin").one().id
        db.execute(insert(BillDetail), [{
            "patient_id": pid, "admission_id": adm_id, "item_code": "P21778INJ",
            "item_name": "最新一笔输液" if i >= N - NEWEST else "静脉输液",
            "unit_price": 6, "quantity": 1, "amount": 6, "created_by": uid} for i in range(N)])
        db.commit()
    for i in range(ARTICLES):
        art = client.post("/api/education/articles", headers=admin, json={
            "title": f"P21778 宣教第{i + 1:02d}篇", "category": "chronic", "content": "限盐、运动、规律服药"})
        assert art.status_code == 201, art.text
        assert client.post(f"/api/education/articles/{art.json()['id']}/publish", headers=admin).status_code == 200
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    resident = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code})
    assert resident.status_code == 200 and resident.json()["bound"], resident.text
    return {"admission": adm_id, "resident": {"Authorization": f"Bearer {resident.json()['access_token']}"}}


def test_接口照旧_合计按全部明细算_明细一页500条(client, world):
    resp = client.get(f"/api/portal/me/admissions/{world['admission']}/bill", headers=world["resident"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (resp.headers["X-Total-Count"], body["total_amount"], len(body["items"])) == (str(N), N * 6, 500)
    assert "最新一笔输液" not in {i["item_name"] for i in body["items"]}   # 最新的那几笔不在第一页


PRELUDE = r"""
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const OUT = { requests: [], alerts: [] };
const registry = {};
function attrsOf(text) {
  const attrs = {};
  for (const m of text.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[m[1]] = m[2] ?? "";
  return attrs;
}
function makeEl(attrs = {}) {
  const dataset = {};
  Object.entries(attrs).forEach(([k, v]) => {
    if (k.startsWith("data-")) dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
  });
  const el = { attrs, dataset, innerHTML: "", textContent: "", listeners: {},
    addEventListener(type, fn) { el.listeners[type] = fn; },
    querySelector(sel) { return pick(el, sel)[0] || null; },
    querySelectorAll(sel) { return pick(el, sel); },
    scrollIntoView() {} };
  return el;
}
/* 从 innerHTML 里认出匹配的元素（`.类名`、`[data-属性]`、`.类名[data-属性]`）；innerHTML 没变时回同一批，挂的监听取得回来 */
function pick(parent, sel) {
  const m = sel.match(/^(?:\.([\w-]+))?(?:\[([\w-]+)\])?$/);
  const cache = (parent.picked ||= new Map());
  const key = `${sel}\u0000${parent.innerHTML}`;
  if (!cache.has(key)) {
    cache.set(key, [...parent.innerHTML.matchAll(/<(\w+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>/g)]
      .map((t) => attrsOf(t[2]))
      .filter((a) => (!m[1] || (a.class || "").split(/\s+/).includes(m[1])) && (!m[2] || m[2] in a))
      .map((a) => makeEl(a)));
  }
  return cache.get(key);
}
globalThis.document = { cookie: "", addEventListener() {}, querySelector: (sel) => (registry[sel] ||= makeEl()) };
globalThis.alert = (text) => { OUT.alerts.push(text); };
/* 浏览器的 fetch：经管道转给真接口，响应头只垫 X-Total-Count（大小写不敏感，照 Headers.get） */
globalThis.fetch = async (path) => {
  OUT.requests.push(path);
  process.stdout.write(JSON.stringify({ path }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  return { status: reply.status, ok: reply.status < 400, json: async () => reply.body,
    headers: { get: (name) => (name.toLowerCase() === "x-total-count" ? reply.total : null) } };
};
/* 登录态请求：Cookie 会话由管道那头补上居民本人的令牌 */
async function authApi(path, options = {}) { return api(path, options); }
let viewingPatientId = null;
"""

#: 从 m.js 原文取的声明；打 * 的是这次新加的，修前没有就取成空串（页面照修前的样子跑，断言在内容上红）
HEADS = ("async function api(", "function kv(", "function svcQuery(", "const CATEGORY_TEXT = ",
         "*async function withAllBillItems(", "async function renderInpatient(", "async function loadArticles(")


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，其余取这一行。"""
    if head.startswith("*"):
        head = head[1:]
        if head not in source:
            return ""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    return source[start:source.index("\n", start) + 1]


def run_page(client, headers: dict, steps: str):
    """在 node 里加载 shared.js 与 `m.js` 的取数 / 渲染函数，跑 `steps`（async 函数体，return 一个可 JSON 化的值）。"""
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    script = (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
              + "".join(_top(source, head) for head in HEADS)
              + f"\n(async () => {{\n{steps}\n}})().then("
              + "(r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
              + " (e) => { console.error(e); process.exit(1); });\n")
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["path"], headers=headers)
            reply = {"status": resp.status_code, "body": resp.json(), "total": resp.headers.get("x-total-count")}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)


def test_费用清单明细取全_标题条数与合计对得上(client, world):
    out = run_page(client, world["resident"], """
      const box = makeEl();
      await renderInpatient(box);
      await box.querySelector(".bill-detail[data-adm]").listeners.click();
      return { html: $("#adm-bill").innerHTML, requests: OUT.requests, alerts: OUT.alerts };
    """)
    assert out["alerts"] == [], out["alerts"]
    html = out["html"]
    assert re.findall(r'<div class="sec-title">明细（([^）]*)）</div>', html) == [str(N)], html[:600]   # 修前「明细（500）」
    amounts = re.findall(r"¥(\d+\.\d{2})</span></div>\s*<div class=\"kv\"><span class=\"k\">单价×数量", html)
    assert len(amounts) == N and f"<b>¥{N * 6:.2f}</b>" in html   # 列出来的条数与合计同一口径
    assert round(sum(float(a) for a in amounts), 2) == N * 6     # 修前 500 条加起来 ¥3000，合计 ¥3120
    assert html.count("最新一笔输液") == NEWEST                   # 最新的那几笔看得到
    bill = f"/api/portal/me/admissions/{world['admission']}/bill"
    assert out["requests"][-2:] == [bill, f"{bill}?offset=500"]   # 按页续取


def test_宣教文章读总数_列不全给更多_点了取下一页(client, world):
    out = run_page(client, {}, """
      const box = $("#edu-list");
      await loadArticles();
      const first = box.innerHTML;
      const more = box.querySelector("[data-edu-more]");
      if (more) await more.listeners.click();
      return { first, second: box.innerHTML, requests: OUT.requests };
    """)
    titles = lambda html: re.findall(r"<h3>(P21778 宣教第\d+篇)</h3>", html)   # noqa: E731
    first, second = out["first"], out["second"]
    assert len(titles(first)) == 50
    assert f"已列 50 / 共 {ARTICLES} 篇" in first and "data-edu-more" in first   # 修前不提示、没有「更多」
    assert titles(second) == [f"P21778 宣教第{i:02d}篇" for i in range(ARTICLES, 0, -1)]   # 新的在前、不重不漏
    assert "已列" not in second and "data-edu-more" not in second
    assert out["requests"] == ["/api/portal/health-articles", "/api/portal/health-articles?offset=50"]
