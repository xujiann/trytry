"""冷链录温超温时，回执里的提示写进冷链面板的消息行（P2-1500，第四十四批「疫苗批次、冷链与 AEFI」扫描 AH1-2 的页面半边）。

模块口径 1：超温不自动封存批次，「平台把异常标出来……由人决定」——录温回执在超温时带一句 `hint`（「请核查该设备内疫苗
批次并决定是否封存（平台不自动封存）」），这是超温时平台给录温人的唯一一句话。修前页面「录入」走 `postAction`，成功即
整页重画、回执整个丢掉：扫描用页面原文的 `postAction` 跑一次 12℃ 录温，`#cc-msg` 为空、页面上哪儿都没有这句提示。

修法照 P2-1480 / P2-1432：直接调 `api()`，先 `await route()` 重画、再把 `hint` 写进 `#cc-msg`（超温，按警示色）；没超温的回执
不带 `hint`，不写。批次记不记存放设备、超温未处置期间批次怎么标 / 拦要业务拍板（AH1-2 的另一半），不在这一条。
这里把页面原样拿到 node 里跑（`vaccine_page.py`，`postAction` 用页面原文、`route()` 照整页重画把元素全部换新），
录温请求转给真接口。
"""
import shutil
from pathlib import Path

import pytest

from vaccine_page import run

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
HINT = "已超出允许区间，请核查该设备内疫苗批次并决定是否封存（平台不自动封存）"

STEPS = """
await goto(renderVaccineSupply);
await document.querySelector("#cc-form").onsubmit({ preventDefault() {}, target: form(ARGS.params.fields) });
await settled();
const msg = document.querySelector("#cc-msg");
return { msg: [msg.textContent, msg.className], routed, posts: requests.filter(([method]) => method !== "GET") };
"""


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21500 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _record(client, admin, org, device, temperature):
    fields = {"org_id": str(org), "device_name": device, "temperature": str(temperature), "min_allowed": "2",
              "max_allowed": "8", "recorded_at": "2026-10-01 09:00"}
    out = run(client, admin, STEPS, {"fields": fields})
    rows = [r for r in client.get("/api/vaccine-supply/cold-chain", headers=admin).json() if r["device_name"] == device]
    assert [(r["temperature"], r["exceeded"]) for r in rows] == [(float(temperature), temperature > 8)]   # 录上了
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_超温录温_先重画再把提示写进冷链面板的消息行(client, admin, org):
    out = _record(client, admin, org, "P21500 1号冰箱", 12)
    assert out["posts"] == [["POST", "/api/vaccine-supply/cold-chain"]]
    # 修前走 postAction：重画之后回执丢掉，消息行是空的
    assert out["msg"] == [HINT, "msg err"]
    assert out["routed"] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_正常温度_只重画_不写消息(client, admin, org):
    out = _record(client, admin, org, "P21500 2号冰箱", 5)
    assert out["msg"] == ["", ""] and out["routed"] == 1


def test_录温读回执的hint_不再走postAction():
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    body = page[page.index("async function renderVaccineSupply("):]
    handler = body[body.index('$("#cc-form").onsubmit'):]
    handler = handler[:handler.index("\n  };\n")]
    assert "postAction(" not in handler   # 修前：postAction 成功即重画、不读回执
    assert handler.index("await route();") < handler.index("if (r.hint)") < handler.index('setMsg("#cc-msg", r.hint, false)')
