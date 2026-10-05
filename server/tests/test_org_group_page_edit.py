"""机构协作分组页只有「停用 / 启用」一个改档入口：名称、类型、牵头机构、备注建错了只能停用（P2-1533，第四十四批扫描 AH4-2 的界面那半，接 P2-1511）。

`PATCH /api/org-groups/{id}` 能改名称、类型（P2-1511 补上）、牵头机构（传 null 即清空，P2-351）和备注，分组页
（`pages-mgmt.js::renderOrgGroups`）的分组表每行却只有「管理成员」「停用 / 启用」，页面只送 `{active}`。修前实测（node 渲染页面）：
行上没有改档按钮，点 `data-ogedit` 什么也不发生——新建表单的类型下拉缺省「片区/分片」，想建专科联盟忘了改就落成片区；名称唯一，
想用原名重建得先改旧组的名字，页面上也改不了，只能停用。

修法：分组表每行加「改档」，点了弹页内表单（`spdModal`，照 pages-mgmt.js「编辑基金池」的写法）：预填名称、类型（选项取 `GROUP_TYPES`）、
牵头机构（首项「无牵头机构」、值为空）、备注；确定后只送和预填值不同的项，牵头机构改成「无」送 null，一项都没变在消息行提示、不发
请求；页面约束与后端一致（名称必填、最多 64 字、不能全是空白，备注最多 256 字），拦下的不发请求；成功重画，失败把后端的话写进 `#og-msg`。

页面函数原样拿到 node 里跑（写法照 test_performance_dimension_headers 的 `_HARNESS`），`api` 经管道转给真接口；`spdModal` 换成桩：
记下框里的字段与预填值，按用例给的改动交回（与 spdModal 一样去首尾空白），`answer` 为 None 即点了取消。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from app.routers.org_groups import GROUP_TYPE_NAMES

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _const(source: str, name: str) -> str:
    found = re.search(rf"^const {name} = .*?;\n", source, re.M | re.S)
    assert found, f"找不到常量 {name}"
    return found.group(0)


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`api()` 经标准输出把请求交给测试进程、从标准输入读回真接口的
#: 状态码与响应（失败照 core.js 的 `api` 抛 `errorText` 的话）；`spdModal()` 记下字段、按 `args.answer` 改几项交回；
#: `setMsg()` / `route()` 记下调用
_HARNESS = r"""
const args = JSON.parse(process.argv[1]);
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const calls = [];
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push([method, path, body]);
  process.stdout.write(JSON.stringify({ req: { method, path, body } }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) throw new Error(errorText(resp.body.detail, `请求失败(${resp.status})`));
  return resp.body;
}
let routed = 0;
function route() { routed += 1; }
const msgs = [];
function setMsg(sel, text, ok = true) { msgs.push([sel, text, ok]); }
let modal = null;
async function spdModal(title, fields, opts = {}) {
  modal = { title, intro: opts.intro || "", fields: fields.map((f) => ({ ...f })) };
  if (args.answer === null) return null;
  const out = {};
  for (const f of fields) {
    const raw = String(f.name in args.answer ? args.answer[f.name] : (f.value ?? "")).trim();
    out[f.name] = f.type === "number" ? Number(raw || 0) : raw;
  }
  return out;
}
"""

_RUN = r"""
(async () => {
  await renderOrgGroups();
  const body = els["#page-body"].innerHTML;
  await els["#page-body"].onclick({ target: { dataset: { ogedit: String(args.gid) } } });
  process.stdout.write(JSON.stringify({ result: { body, modal, calls, msgs, routed } }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _run_page(client, headers, gid: int, answer: dict | None) -> dict:
    """渲染分组页、点分组 `gid` 行上的「改档」；框里按 `answer` 改几项点确定（None 即点了取消）。"""
    core, page = _read("core.js"), _read("pages-mgmt.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in ("function table(", "function panel(", "function pickedId("))
              + _const(page, "GROUP_TYPES") + _top_level(page, "async function renderOrgGroups(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"gid": gid, "answer": answer}, ensure_ascii=False)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            assert "error" not in message, message["error"]
            req = message["req"]
            resp = client.request(req["method"], req["path"], headers=headers, json=req["body"])
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _writes(out: dict) -> list:
    return [call for call in out["calls"] if call[0] != "GET"]


def _row(body: str, gid: int) -> str:
    """分组表里分组 `gid` 那一行（按「管理成员」按钮认）。"""
    at = body.index(f'data-ogpick="{gid}"')
    return body[body.rindex("<tr>", 0, at):body.index("</tr>", at)]


@pytest.fixture(scope="module")
def world(client, admin):
    def org(name: str) -> int:
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "lead_hospital", "level": "county"})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return {"lead": org("P21533 县人民医院"), "other": org("P21533 县中医院")}


def _group(client, admin, name: str, **extra) -> int:
    resp = client.post("/api/org-groups", headers=admin, json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _saved(client, admin, gid: int) -> dict:
    return next(g for g in client.get("/api/org-groups", headers=admin).json() if g["id"] == gid)


def test_分组表每行有改档_点了框里预填原值_取消不发请求(client, admin, world):
    name, note = 'P21533 胸痛<专科>联盟&"A"', '原备注<b>&"x"'   # 带 HTML 特殊字符：进框的是原值，由 spdModal 转义（见末尾）
    gid = _group(client, admin, name, lead_org_id=world["lead"], note=note)   # 类型缺省落成片区
    out = _run_page(client, admin, gid, None)
    row = _row(out["body"], gid)
    assert f'<button data-ogedit="{gid}">改档</button>' in row, row   # 修前每行只有「管理成员」「停用 / 启用」
    assert out["modal"] is not None, "点「改档」没有打开改档表单"   # 修前点了什么也不发生
    fields = out["modal"]["fields"]
    assert [(f["name"], f["type"], f["value"]) for f in fields] == [
        ("name", "text", name), ("group_type", "select", "zone"),
        ("lead_org_id", "select", world["lead"]), ("note", "text", note)]   # 原值、不预先转义（否则框里显示 &amp;）
    by_name = {f["name"]: f for f in fields}
    assert by_name["name"].get("required") is True   # 名称必填，同后端 min_length=1
    # 类型选项取页面的 GROUP_TYPES，与后端的取值、名称同一份
    assert by_name["group_type"]["options"] == [{"value": k, "label": v} for k, v in GROUP_TYPE_NAMES.items()]
    leads = by_name["lead_org_id"]["options"]
    assert leads[0] == {"value": "", "label": "无牵头机构"}
    assert {"value": world["lead"], "label": "P21533 县人民医院"} in leads
    assert {"value": world["other"], "label": "P21533 县中医院"} in leads
    assert name in out["modal"]["title"]
    assert _writes(out) == [] and out["routed"] == 0 and out["msgs"] == []   # 点了取消：不发请求、不重画
    # 分组名、备注、机构名都是录入的数据：标题、字段值、选项由 spdModal 自己 esc() 之后才拼进框里
    modal = _top_level(_read("pages-spd.js"), "function spdModal(title, fields, opts = {}) {")
    for piece in ("<h3>${esc(title)}</h3>", 'value="${esc(val)}"', '<option value="${esc(o.value)}"',
                  ">${esc(o.label)}</option>"):
        assert piece in modal, piece


def test_改类型提交_只送类型_读回新类型并重画(client, admin, world):
    gid = _group(client, admin, "P21533 卒中专科联盟", lead_org_id=world["lead"], note="原备注")
    out = _run_page(client, admin, gid, {"group_type": "alliance"})
    assert _writes(out) == [["PATCH", f"/api/org-groups/{gid}", {"group_type": "alliance"}]]   # 只送改了的那一项
    assert out["routed"] == 1 and out["msgs"] == []
    saved = _saved(client, admin, gid)
    assert (saved["group_type"], saved["group_type_name"], saved["name"], saved["lead_org_id"], saved["note"]) == (
        "alliance", "专科联盟", "P21533 卒中专科联盟", world["lead"], "原备注")


def test_牵头机构改成无_送null_读回清空(client, admin, world):
    gid = _group(client, admin, "P21533 东片区", lead_org_id=world["lead"], note="原备注")
    out = _run_page(client, admin, gid, {"lead_org_id": ""})
    assert _writes(out) == [["PATCH", f"/api/org-groups/{gid}", {"lead_org_id": None}]]   # 不是空串、不是 0
    assert out["routed"] == 1
    saved = _saved(client, admin, gid)
    assert (saved["lead_org_id"], saved["name"], saved["note"]) == (None, "P21533 东片区", "原备注")


def test_改名_换牵头机构_清空备注_三项照送(client, admin, world):
    gid = _group(client, admin, "P21533 网格一", group_type="grid", lead_org_id=world["lead"], note="原备注")
    out = _run_page(client, admin, gid, {"name": "P21533 网格一（改）", "lead_org_id": str(world["other"]), "note": ""})
    assert _writes(out) == [["PATCH", f"/api/org-groups/{gid}", {
        "name": "P21533 网格一（改）", "lead_org_id": world["other"], "note": ""}]]   # 牵头机构送数字；备注清空照送
    saved = _saved(client, admin, gid)
    assert (saved["name"], saved["group_type"], saved["lead_org_id"], saved["note"]) == (
        "P21533 网格一（改）", "grid", world["other"], "")


def test_什么都没改_不发请求_消息行提示(client, admin, world):
    gid = _group(client, admin, "P21533 西片区", lead_org_id=world["lead"], note="原备注")
    out = _run_page(client, admin, gid, {})
    assert out["modal"] is not None
    assert _writes(out) == [] and out["routed"] == 0   # 后端此时也会 422「请至少改一项」
    (msg,) = out["msgs"]
    assert msg[0] == "#og-msg" and msg[2] is False and "没有改动" in msg[1], msg


def test_后端拒收_原话写进消息行_不重画(client, admin, world):
    _group(client, admin, "P21533 南片区")
    gid = _group(client, admin, "P21533 北片区")
    out = _run_page(client, admin, gid, {"name": "P21533 南片区"})   # 名称唯一
    assert _writes(out) == [["PATCH", f"/api/org-groups/{gid}", {"name": "P21533 南片区"}]]
    assert out["msgs"] == [["#og-msg", "同名分组已存在", False]] and out["routed"] == 0
    assert _saved(client, admin, gid)["name"] == "P21533 北片区"


@pytest.mark.parametrize("n, answer, said, body", [
    (0, {"name": "   "}, "不能只填空格", {"name": "   "}),
    (1, {"name": "​"}, "不能只填空格", {"name": "​"}),   # 只有零宽空格：后端 NON_BLANK 同样不收（P2-1148）
    (2, {"name": "名" * 65}, "最多 64 个字", {"name": "名" * 65}),
    (3, {"note": "备" * 257}, "最多 256 个字", {"note": "备" * 257}),
])
def test_页面约束与后端一致_拦下的不发请求(client, admin, world, n, answer, said, body):
    gid = _group(client, admin, f"P21533 约束-{n}", note="原备注")
    out = _run_page(client, admin, gid, answer)
    assert _writes(out) == [] and out["routed"] == 0
    (msg,) = out["msgs"]
    assert msg[0] == "#og-msg" and msg[2] is False and said in msg[1], msg
    # 页面拦下的，后端同样 422
    assert client.patch(f"/api/org-groups/{gid}", headers=admin, json=body).status_code == 422


def test_边界值页面放过_后端也收(client, admin, world):
    gid = _group(client, admin, "P21533 边界")
    name, note = "名" * 64, "备" * 256
    out = _run_page(client, admin, gid, {"name": name, "note": note})
    assert _writes(out) == [["PATCH", f"/api/org-groups/{gid}", {"name": name, "note": note}]]
    assert out["routed"] == 1 and out["msgs"] == []
    saved = _saved(client, admin, gid)
    assert (saved["name"], saved["note"]) == (name, note)
