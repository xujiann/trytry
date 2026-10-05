"""基金预付唯一的防线「超计划警告」在页面上从不出现；计划预付额为 0 的池子预付再多也不警告（P2-1480，第四十三批扫描
AG3-4 的①②）。

预付「超出计划预付额只警告不拦截」（`fund.add_prepayment` 的 docstring），回执里的 `warning` 是这道防线唯一的出口。修前两处断了：
① 页面「登记预付」走 `postAction`，成功即整页重画、回执整个丢掉——比例 70% 的池子预付 40 万，接口回执写着「累计预付 400000.0
元已超过计划预付额 350000.0 元…」，页面上什么都没有；② 判断写的是 `if out["planned_prepay"] and …`，计划额 0 被当成没有计划，
而编辑框写明「0 = 不预付」（P2-590）——扫描实测比例 0、筹资 50 万的池子预付 500 万，201、`warning: None`。

修法：计划额为 0 时任何一笔预付都给 warning，累计预付超过筹资总额另给一句，仍只警告不拦截；页面改为直接调 `api()`，先重画、
再把 warning 写进消息行（同 P2-1432 / P2-1013）。录错的预付怎么更正是业务口径（AG3-4 的③），不在这一条。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def director(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21480 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21480_dir", "password": "passw0rd1", "role": "director", "org_id": org})
    assert created.status_code == 201, created.text
    return login(client, "p21480_dir", "passw0rd1")


def _pool(client, director, year, total, ratio, insurance_type="resident"):
    created = client.post("/api/fund/pools", headers=director, json={
        "year": year, "insurance_type": insurance_type, "total_amount": total, "prepay_ratio_pct": ratio})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _prepay(client, director, pool, amount):
    resp = client.post(f"/api/fund/pools/{pool}/prepayments", headers=director, json={"amount": amount})
    assert resp.status_code == 201, resp.text   # 只警告不拦截
    return resp.json()


def test_计划预付额为0_任何一笔预付都给警告(client, director):
    pool = _pool(client, director, 2091, total=500000, ratio=0)
    first = _prepay(client, director, pool, 100)
    assert first["planned_prepay"] == 0
    # 修前没有 warning 这一键：计划额 0 被当成「没有计划」
    assert first["warning"] == "本池计划预付额为 0（不预付），累计预付 100.0 元，请确认是否为追加预拨"
    over = _prepay(client, director, pool, 4999900)   # 扫描那一例：比例 0、筹资 50 万，累计预付 500 万
    assert over["prepaid_amount"] == 5000000
    assert over["warning"] == ("本池计划预付额为 0（不预付），累计预付 5000000.0 元，请确认是否为追加预拨；"
                               "累计预付已超过筹资总额 500000.0 元，请核对")


def test_有计划的池子_没超不警告_超计划照旧_超筹资总额另给一句(client, director):
    pool = _pool(client, director, 2092, total=1000, ratio=50)   # 计划预付 500
    assert "warning" not in _prepay(client, director, pool, 400)   # 条件键：没超就不出现
    over_plan = _prepay(client, director, pool, 300)
    assert over_plan["warning"] == "累计预付 700.0 元已超过计划预付额 500.0 元，请确认是否为追加预拨"   # 措辞不变
    over_total = _prepay(client, director, pool, 400)
    assert over_total["warning"] == ("累计预付 1100.0 元已超过计划预付额 500.0 元，请确认是否为追加预拨；"
                                     "累计预付已超过筹资总额 1000.0 元，请核对")


# ---------- 页面：先重画、再把 warning 写进消息行 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: `$()` 按选择器给一个记 textContent / innerHTML 的假元素；`api()` 经管道转给真接口（GET / POST 都转），`settled()` 等到
#: 发出去的请求都回来（修前的提交不 await，要等它的 postAction 走完）；`route()` 照真页面整页重画的效果把消息行换成一个
#: 新元素——写在重画之前的回执会被冲掉（P2-1013）。`postAction` 用 pages-clinical.js 的原文
_HARNESS = r"""
let els = {};
let routed = 0;
let inflight = 0;
const DATA = JSON.parse(process.argv[1]);
const mk = () => ({ textContent: "", innerHTML: "", className: "", value: "", style: {},
  classList: { add() {}, remove() {}, toggle() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); },
  querySelectorAll() { return []; } };
globalThis.localStorage = { getItem(key) { return key === "medplat_fund_pool" ? String(DATA.pool) : null; }, setItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const posts = [];
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  if (method !== "GET") posts.push([method, path, body]);
  inflight += 1;
  process.stdout.write(JSON.stringify({ method, path, body }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  inflight -= 1;
  if (reply.status >= 400) throw new Error(reply.body.detail);
  return reply.body;
}
async function settled() { while (inflight) await new Promise((resolve) => setTimeout(resolve, 5)); }
async function route() { routed += 1; delete els["#fd-msg"]; }
function formJson() { return DATA.form; }
async function spdModal() { return null; }
"""


def _submit_prepay(client, headers, pool: int, form: dict) -> dict:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    consts = "".join(page[page.index(f"const {name} = "):page.index(";\n", page.index(f"const {name} = ")) + 2]
                     for name in ("INSURANCE_TYPES", "POOL_STATUS_TAG"))
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + "".join(_top_level(core, f"function {name}(") for name in ("table", "panel", "setMsg", "pickedId"))
              + _top_level(clinical, "async function postAction(")
              + consts + _top_level(page, "async function renderFund(") + _top_level(page, "function renderSettlement(")
              + "\n(async () => { await renderFund();\n"
                "  await els['#fd-prepay'].onsubmit({ preventDefault() {}, target: {} });\n"
                "  await settled();\n"
                "  const msg = document.querySelector('#fd-msg');\n"
                "  process.stdout.write(JSON.stringify({ result: { msg: [msg.textContent, msg.className], routed, posts } }) + '\\n');\n"
                "  rl.close(); })();\n")
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"pool": pool, "form": form})], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.request(message["method"], message["path"], headers=headers, json=message["body"])
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_登记预付先重画再把警告写进消息行(client, director):
    pool = _pool(client, director, 2093, total=1000000, ratio=0, insurance_type="employee")
    out = _submit_prepay(client, director, pool, {"batch_no": "P21480-1", "amount": 400000})
    assert out["posts"] == [["POST", f"/api/fund/pools/{pool}/prepayments", {"batch_no": "P21480-1", "amount": 400000}]]
    # 修前走 postAction：重画之后回执丢掉，消息行是空的
    assert out["msg"] == ["本池计划预付额为 0（不预付），累计预付 400000.0 元，请确认是否为追加预拨", "msg err"]
    assert out["routed"] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_没超计划只重画_不写消息(client, director):
    pool = _pool(client, director, 2094, total=1000000, ratio=70)
    out = _submit_prepay(client, director, pool, {"amount": 100000})
    assert out["msg"] == ["", ""] and out["routed"] == 1


def test_页面_登记预付读回执的warning_不再走postAction():
    page = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    body = _top_level(page, "async function renderFund(")
    handler = body[body.index('$("#fd-prepay").onsubmit'):]
    handler = handler[:handler.index("\n    };\n")]
    assert "postAction(" not in handler   # 修前：postAction 成功即重画、不读回执
    assert handler.index("await route();") < handler.index("if (r.warning)") < handler.index('setMsg("#fd-msg", r.warning')
