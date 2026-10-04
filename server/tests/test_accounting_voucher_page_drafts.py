"""会计核算页的凭证清单只看得到第一页：一个月过了 50 张，最早录的草稿在页面上没有行（P1-250，第四十二批扫描 AF3-1）。

修前 `renderAccounting` 取 `/api/accounting/vouchers?period=…`，不带 limit / offset / status——后端 `list_vouchers` 缺省一页
50 张、按 id 倒序，总数只放在 `X-Total-Count` 响应头里，页面的 `api()` 读不到。「过账」「明细」只画在这一页的行上：一个月
录到第 51 张，最早录的那批草稿就挤出窗口，过不了账、看不了明细，月末集中过账时它们一直进不了试算平衡与合并报表；标题照样
写「凭证（50）」。扫描实测：同一期间 60 张，页面那个地址只回 50 行、`X-Total-Count = 60`，把看得到的 50 张全部过账后，页面
全是「已过账」，库里还有 10 张草稿，试算平衡借方 5000（应为 6000）。

修法同 P2-456 / P2-1310：本期草稿用 `fetchAllPages` 续页取全、`actionableFirst` 排在最前，草稿行上的「过账」「明细」照旧；
已过账 / 已作废的整期不封顶（全域账号看的是全县各家的凭证），照旧只取最新一页（页长 50 写明在请求里）。标题不再写截断后的
行数：最新一页没取满就是整期的实数，取满了写「本期草稿 N 张（全列）、其余显示最新 M 张」。

这里把 `renderAccounting` 原样拿到 node 里跑，页面的 `api` 经管道转给真接口，看画出来的凭证面板。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 一个满月：61 张凭证，最早的 11 张还是草稿、最新的 50 张已过账——最新一页（50 张）里一张草稿都没有
FULL = "2025-03"
#: 一个没满一页的月份
SMALL = "2025-04"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _accounting_section() -> str:
    """pages-mgmt.js 里「会计核算」一节（常量 + `renderAccounting`），到「成本核算」一节为止。"""
    source = _read("pages-mgmt.js")
    start = source.index("/* ---------------- 会计核算 ---------------- */")
    return source[start:source.index("/* ---------------- 成本核算 ---------------- */", start)]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`localStorage` 从参数里读；`api()` 经标准输出把地址交给
#: 测试进程、从标准输入读回真接口的响应（`fetchAllPages` 拼出来的翻页地址也照样转）
_HARNESS = r"""
const els = {};
const rows = [];
const STORE = JSON.parse(process.argv[1]);
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", style: {}, kids: {}, dataset: {},
  classList: { add() {}, remove() {} },
  querySelector(sel) { return (this.kids[sel] ||= mk()); },
  appendChild(child) { rows.push(child); return child; } });
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= mk()); },
  querySelectorAll(sel) { return sel === ".entry-row" ? rows : []; },
  createElement() { return mk(); } };
globalThis.localStorage = { getItem: (k) => (k in STORE ? STORE[k] : null),
  setItem(k, v) { STORE[k] = String(v); }, removeItem(k) { delete STORE[k]; } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requested = [];
async function api(path) {
  requested.push(path);
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function currentRole() { return STORE.medplat_role || ""; }
function route() {}
function postAction() {}
function formJson() { return {}; }
async function spdModal() { return null; }
"""


def _render(client, headers, period: str) -> dict:
    core = _read("core.js")
    script = (
        _HARNESS + _read("shared.js")
        + _top_level(core, "function table(") + _top_level(core, "function panel(")
        + _top_level(core, "function actionableFirst(") + _top_level(core, "function setMsg(")
        + _accounting_section()
        + "\n(async () => { await renderAccounting();\n"
        "  process.stdout.write(JSON.stringify({ result: { body: els['#page-body'].innerHTML, requested } }) + '\\n');\n"
        "  rl.close(); })();\n"
    )
    store = {"medplat_acc_period": period, "medplat_role": "admin"}
    proc = subprocess.Popen(["node", "-e", script, json.dumps(store)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _voucher_panel(html: str, period: str) -> tuple[str, list[tuple[int, str, str]]]:
    """凭证面板：(标题, [(凭证 id, 凭证号, 这一行其余的 HTML)])。"""
    start = html.index(f"<h3>{period} 凭证")
    panel = html[start:html.index("</table>", start)]
    title = panel[len("<h3>"):panel.index("</h3>")]
    rows = [(int(vid), no, rest) for vid, no, rest in re.findall(r"<tr><td>(\d+)</td><td>(.*?)</td>(.*?)</tr>", panel, re.S)]
    return title, rows


def _voucher(client, headers, org: int, no: str, day: str) -> int:
    resp = client.post("/api/accounting/vouchers", headers=headers, json={
        "org_id": org, "voucher_no": no, "voucher_date": day, "summary": f"{no} 收门诊款",
        "entries": [{"subject_code": "1002", "debit": 100}, {"subject_code": "4001", "credit": 100}]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1250 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ids = [_voucher(client, admin, org, f"P1250-{i:03d}", f"{FULL}-{1 + i % 28:02d}") for i in range(1, 62)]
    for vid in ids[11:]:   # 最新的 50 张过账；最早录的 11 张还是草稿
        posted = client.post(f"/api/accounting/vouchers/{vid}/post", headers=admin)
        assert posted.status_code == 200, posted.text
    small = [_voucher(client, admin, org, f"P1250-S{i}", f"{SMALL}-0{i}") for i in range(1, 4)]
    assert client.post(f"/api/accounting/vouchers/{small[0]}/post", headers=admin).status_code == 200
    return {"ids": ids, "drafts": ids[:11], "small": small}


def test_缺省一页取不到最早的草稿_按状态续页取得到(client, admin, world):
    """页面靠 `?status=draft` 续页取全：缺省一页（最新 50 张）里一张草稿都没有，按状态取得到全部 11 张。"""
    page = client.get("/api/accounting/vouchers", headers=admin, params={"period": FULL})
    assert page.headers["X-Total-Count"] == "61"
    assert {v["status"] for v in page.json()} == {"posted"} and len(page.json()) == 50   # 修前页面上就是这 50 行
    drafts = client.get("/api/accounting/vouchers", headers=admin, params={"period": FULL, "status": "draft"})
    assert sorted(v["id"] for v in drafts.json()) == world["drafts"]


def test_满月_最早录的草稿在页面上_行上有过账_标题写的数与实际一致(client, admin, world):
    out = _render(client, admin, FULL)
    title, rows = _voucher_panel(out["body"], FULL)
    shown = {vid: rest for vid, _no, rest in rows}
    first = world["drafts"][0]   # 记 P1250-001：最早录的那张
    assert first in shown, out["requested"]                                    # 修前不在页面上
    assert f'data-post="{first}">过账</button>' in shown[first]
    assert f'data-detail="{first}">明细</button>' in shown[first]
    posts = sorted(vid for vid, rest in shown.items() if "data-post=" in rest)
    assert posts == world["drafts"]                                             # 11 张草稿一张不落，都能过账
    assert [vid for vid, _no, _rest in rows[:11]] == sorted(world["drafts"], reverse=True)   # 排在最前
    assert len(rows) == len(shown) == 61                                        # 按 id 去重
    # 修前标题「2025-03 凭证（50）」：截断后的行数。现在草稿是全的、其余只是最新一页
    assert title == f"{FULL} 凭证：本期草稿 11 张（全列）、其余显示最新 50 张"
    drafts_shown = len(posts)
    others_shown = len(rows) - drafts_shown
    assert (drafts_shown, others_shown) == (11, 50)                            # 标题里的两个数就是页面上的行数
    assert f"/api/accounting/vouchers?period={FULL}&status=draft&limit=500&offset=0" in out["requested"]


def test_没满一页的月份_标题照旧写整期张数(client, admin, world):
    out = _render(client, admin, SMALL)
    title, rows = _voucher_panel(out["body"], SMALL)
    assert title == f"{SMALL} 凭证（3）"
    assert sorted(vid for vid, _no, _rest in rows) == sorted(world["small"])
    assert sum("data-post=" in rest for _vid, _no, rest in rows) == 2          # 两张草稿可过账
    assert sum("data-void=" in rest for _vid, _no, rest in rows) == 1          # 已过账那张可作废
