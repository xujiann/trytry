"""质控页录入测定值后的「尚有 N 个失控点未处理」写进测定面板自己的消息框，并随回执清掉（P2-1471，第四十三批扫描 AG1-11）。

修前录入回执里的警示（docstring：「失控未处理期间继续录入，响应给警示」，这是唯一的提醒）写在顶部「新建质控批号」面板的
`#labqc-msg`，离测定面板很远；而且只在有警示时写、没有时不清：处理完失控点、再录一个在控点（回执 alert 为空）、换到别的
批号，这一句都还挂着，测定面板自己的 `#meas-msg` 一直是空的。

修法：录入成功后先重画测定面板、再把回执里的警示写进 `#meas-msg`（先重画再写回执，P2-1013），回执里没有警示就清掉；测定面板
每次重画（换批号、处理失控点之后）都清空这一格；顶部 `#labqc-msg` 只留给建批号的回执。这里照扫描复现脚本
`r4_labqc_page_alert.py` 的 node 桩法（真接口回执 + 页面原代码，夹具见 `tests/labqc_page.py`），看录入后、处理后、再录（无
警示）、换批号四个时刻的两个消息框；整块重画按浏览器规矩换新子元素、按 r4 原样不换新，两种垫法都跑一遍。
"""
import shutil

import pytest

from labqc_page import responses, run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

ALERT = "该批号尚有 1 个失控点未处理，请先登记原因与纠正措施"
HANDLE = {"reason": "质控品失效", "corrective_action": "换新支质控品复测"}
REDRAW = pytest.mark.parametrize("rerender", [True, False], ids=["重画换新子元素", "r4原样不换新"])

STEPS = r"""
const out = [];
await renderLabQc();
await openLot(ARGS.params.a);
await submitMeasurement();                    // 回执带警示
out.push(["录入后", messages()]);
await handlePoint(ARGS.params.bad);
out.push(["处理后", messages()]);
await submitMeasurement();                    // 回执 alert 为空
out.push(["再录_无警示", messages()]);
await openLot(ARGS.params.b);
out.push(["换批号", messages()]);
return { out, posts: calls.filter((c) => c[0] === "POST") };
"""


@pytest.fixture(scope="module")
def world(client, admin):
    """K-1、K-2 两个批号；K-1 先录一个 1-3s 失控点，再录一点（回执带警示）、处理那个失控点、再录一点（回执无警示）。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21471 县检验中心", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    lots = []
    for no in ("P21471-K1", "P21471-K2"):
        made = client.post("/api/labqc/lots", headers=admin, json={
            "org_id": org, "item_code": "K", "item_name": "血清钾", "lot_no": no, "target_value": 5.0, "sd": 0.5})
        assert made.status_code == 201, made.text
        lots.append(made.json()["id"])
    a, b = lots
    bad = client.post(f"/api/labqc/lots/{a}/measurements", headers=admin, json={"value": 7.0}).json()   # z=+4 1-3s
    assert bad["out_of_control"], bad
    receipts = [client.post(f"/api/labqc/lots/{a}/measurements", headers=admin, json={"value": 5.1}).json(),
                client.post(f"/api/labqc/measurements/{bad['id']}/handle", headers=admin, json=HANDLE).json(),
                client.post(f"/api/labqc/lots/{a}/measurements", headers=admin, json={"value": 5.0}).json()]
    assert (receipts[0]["alert"], receipts[2]["alert"]) == (ALERT, ""), receipts   # 真回执：先有警示，处理后没有
    return {"a": a, "b": b, "bad": bad["id"], "receipts": receipts, "get": responses(client, admin, lots)}


@REDRAW
def test_警示写进测定面板_处理后再录与换批号都清掉_顶部不再挂(world, rerender):
    got = run(world["get"], STEPS, posts=world["receipts"], handle_form=HANDLE,
              params={"a": world["a"], "b": world["b"], "bad": world["bad"]}, rerender=rerender)
    # 修前四个时刻顶部都是这句警示、测定面板一直为空
    assert got["out"] == [
        ["录入后", {"top": "", "meas": ALERT}],
        ["处理后", {"top": "", "meas": ""}],
        ["再录_无警示", {"top": "", "meas": ""}],
        ["换批号", {"top": "", "meas": ""}],
    ]
    a = world["a"]
    assert got["posts"] == [["POST", f"/api/labqc/lots/{a}/measurements"],
                            ["POST", f"/api/labqc/measurements/{world['bad']}/handle"],
                            ["POST", f"/api/labqc/lots/{a}/measurements"]]


@REDRAW
def test_补录改判的说明同样写进测定面板(world, rerender):
    """回执的 alert 还会带补录改判的说明（P2-687）：同一个出口，写进测定面板、不写顶部。"""
    note = "这是补录点：插在 2026-09-23 08:00 那一点之前，该点改跟本点比，重判为「失控 2-2s」（原为「1-2s 警告」）"
    receipt = {**world["receipts"][2], "alert": note}
    got = run(world["get"], "await renderLabQc(); await openLot(ARGS.params.a); await submitMeasurement(); return messages();",
              posts=[receipt], params={"a": world["a"]}, rerender=rerender)
    assert got == {"top": "", "meas": note}
