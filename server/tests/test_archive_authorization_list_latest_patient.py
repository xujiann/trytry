"""患者主索引「档案调阅授权」清单只画最后一次查的那一位、写明是谁（P2-1791，第五十三批「页面的异步竞态与过期响应」扫描 AQ2-3）。

`core.js::renderPatients` 的 `drawAuths` 是「先清空 → await → 写」，不比对请求序号，清单只有编号、机构、范围、有效期、状态，
不写是谁；撤销确认框只写「撤销调阅授权」。两种时序都会让乙的号下挂着甲的授权、「撤销」按钮挂甲的患者号：
- 先查甲、立刻改查乙，甲的回包晚到——扫描实测框里最后查的是乙，点撤销发出 `/patients/<甲>/authorizations/<甲的>/revoke`；
- 给甲登记授权、授权请求在途时查乙——不用任何乱序：授权成功后的 `drawAuths(甲)` 一定排在乙的查询之后落地。
P2-1010 只修了「查不到」那一支（先清空）。

修法照 P2-1012 的序号写法：`drawAuths` 取序号，过期的回包（成功与出错两支）都丢弃；授权 / 撤销成功后的重画只在这期间没有
再查别人时才画；清单头写「患者 编号 的调阅授权」（清单接口不带姓名，不为此另开接口）；撤销确认框写明患者与被授权机构，
授权回执写明给谁登记的。授权、撤销接口与权限一概不动。

页面原样拿到 node 里跑（`patients_page.py`，请求转给真接口），页面的 `api` 外面包一层「扣住回包」：请求照发、回包照收，
地址以登记的片段结尾的那一次先不交给页面，等乙画完再放行——先后是确定的。
"""
import re
import shutil
from itertools import count

import pytest

from patients_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 扣住回包：页面的 `api` 包一层——请求照发、回包照收（管道按先后配对），地址以 `hold` 登记的片段结尾的那一次先不交给页面，
#: `release` 才放行；`flush` 等到连着 5 拍没有在途请求（被扣住的那次回包已经收到，不算在途）
HOLD = r"""
const gates = [];
let outstanding = 0;
const realApi = api;
api = (path, opts) => {
  outstanding += 1;
  const reply = realApi(path, opts);
  reply.then(() => { outstanding -= 1; }, () => { outstanding -= 1; });
  const gate = gates.find((g) => !g.taken && path.endsWith(g.tail));
  if (!gate) return reply;
  gate.taken = true;
  return gate.opened.then(() => reply);
};
function hold(tail) {
  const gate = { tail, taken: false };
  gate.opened = new Promise((resolve) => { gate.open = resolve; });
  gates.push(gate);
}
const release = (tail) => gates.find((g) => g.tail === tail).open();
const flush = async () => {
  for (let quiet = 0; quiet < 5;) { await new Promise((r) => setTimeout(r, 5)); quiet = outstanding ? 0 : quiet + 1; }
};
const submit = (sel, fields) => elements[sel].onsubmit({ preventDefault() {}, target: { ...fields, querySelectorAll: () => [] } });
await renderPatients();
const P = ARGS.params;
"""


_worlds = count(1)


@pytest.fixture()
def world(client, admin):
    """甲、乙各给同一家机构一条「全部档案」的有效授权。每条用例一份，撤销不串到别的用例。"""
    n = next(_worlds)
    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P21791-{n} 县人民医院", "org_type": "township", "level": "township"}).json()["id"]
    out = {"org": org, "n": n}
    for key, who, birth in (("a", "甲", "1960"), ("b", "乙", "1965")):
        resp = client.post("/api/patients", headers=admin, json={
            "name": f"P21791 {who}", "id_card": f"330102{birth}0101{n:02d}91"})
        assert resp.status_code == 201 and resp.json()["created"], resp.text
        pid = resp.json()["id"]
        grant = client.post(f"/api/patients/{pid}/authorizations", headers=admin,
                            json={"grantee_org_id": org, "scope": "all", "expire_date": "2099-12-31"})
        assert grant.status_code == 201, grant.text
        out[key], out[f"auth_{key}"] = pid, grant.json()["id"]
    return out


def _status(client, admin, pid):
    return {r["id"]: r["status"] for r in client.get(f"/api/patients/{pid}/authorizations", headers=admin).json()}


def test_甲的回包晚到_清单与撤销按钮仍是乙的_撤销撤的是乙(client, admin, world):
    a, b = world["a"], world["b"]
    out, _ = run(client, admin, HOLD + """
hold(`/api/patients/${P.a}/authorizations`);
const late = submit("#auth-list-form", { patient_id: String(P.a) });
await flush();
await submit("#auth-list-form", { patient_id: String(P.b) });
await flush();
const before = htmlOf("#auth-table");
release(`/api/patients/${P.a}/authorizations`);
await late;
await flush();
const after = htmlOf("#auth-table");
// 操作者以为这是乙（框里最后查的是乙）的清单，点「撤销」
const [, revoke, pid] = after.match(/data-revoke="(\\d+)" data-pid="(\\d+)"/);
const done = click({ revoke, pid });
await flush();
const modal = lastModal();
const intro = (modal.html.match(/<div class="desc"[^>]*>([^<]*)<\\/div>/) || [])[1];
await submitModal(modal);
await done;
await flush();
return { before, after, intro, writes: posts, redrawn: htmlOf("#auth-table") };
""", {"a": a, "b": b}, send_writes=True)
    assert f"<td>{world['auth_b']}</td>" in out["before"], out["before"]
    assert out["after"] == out["before"], "甲的回包晚到，盖掉了乙的授权清单"   # 修前首行换成甲的授权
    assert f"<td>{world['auth_a']}</td>" not in out["after"]
    assert f"患者 {b} 的调阅授权" in out["after"], out["after"]   # 修前清单里不写是谁
    assert set(re.findall(r'data-pid="(\d+)"', out["after"])) == {str(b)}   # 修前「撤销」挂甲的患者号
    # 撤销确认框写明患者与被授权机构（修前只写「撤销后该机构不能再凭此授权调阅患者档案……」）
    for text in (f"患者 {b}", f"「P21791-{world['n']} 县人民医院」", "全部档案", f"记录 {world['auth_b']}",
                 "须患者本人重新办理授权"):
        assert text in out["intro"], (text, out["intro"])
    assert out["writes"] == [["POST", f"/api/patients/{b}/authorizations/{world['auth_b']}/revoke", None]]   # 修前撤的是甲
    assert "已撤销" in out["redrawn"] and f"患者 {b} 的调阅授权" in out["redrawn"]   # 撤销后重画的仍是乙
    assert _status(client, admin, a) == {world["auth_a"]: "active"}
    assert _status(client, admin, b) == {world["auth_b"]: "revoked"}


def test_给甲登记授权在途时查乙_清单与撤销按钮仍是乙的_回执写明给谁登记的(client, admin, world):
    a, b = world["a"], world["b"]
    out, requests = run(client, admin, HOLD + """
// 给甲登记一条「就诊记录」授权；授权请求在途，经办接着查乙（不扣任何回包，一律按发出顺序交回）
const grant = submit("#auth-grant-form", { patient_id: String(P.a), grantee_org_id: String(P.org), scope: "encounter",
                                           expire_date: "2099-12-31" });
await submit("#auth-list-form", { patient_id: String(P.b) });
await grant;
await flush();
return { html: htmlOf("#auth-table"), msg: msgOf("#auth-msg") };
""", {"a": a, "b": b, "org": world["org"]}, send_writes=True)
    auths = [f"{m} {p}" for m, p in requests if "/authorizations" in p]
    assert auths == [f"POST /api/patients/{a}/authorizations", f"GET /api/patients/{b}/authorizations"], auths
    assert f"患者 {b} 的调阅授权" in out["html"], out["html"]
    assert re.findall(r"<td>(全部档案|就诊记录|检查报告)</td>", out["html"]) == ["全部档案"]   # 修前换成甲的两条
    assert set(re.findall(r'data-pid="(\d+)"', out["html"])) == {str(b)}   # 修前「撤销」挂甲的患者号
    assert out["msg"] == f"授权已登记（患者 {a}）"   # 清单停在乙，回执写明是给甲登记的
    assert sorted(_status(client, admin, a).values()) == ["active", "active"]   # 甲的授权照样登记上


def test_授权后没换人_照旧重画这一位的清单(client, admin, world):
    a = world["a"]
    out, _ = run(client, admin, HOLD + """
await submit("#auth-grant-form", { patient_id: String(P.a), grantee_org_id: String(P.org), scope: "exam",
                                   expire_date: "2099-12-31" });
await flush();
return { html: htmlOf("#auth-table"), msg: msgOf("#auth-msg") };
""", {"a": a, "org": world["org"]}, send_writes=True)
    assert f"患者 {a} 的调阅授权" in out["html"], out["html"]
    assert re.findall(r"<td>(全部档案|就诊记录|检查报告)</td>", out["html"]) == ["检查报告", "全部档案"]
    assert out["msg"] == f"授权已登记（患者 {a}）"


def test_过期的出错回包同样丢弃_不把甲的报错写在乙的清单旁(client, admin, world):
    """甲那次查失败（这里垫成查无此人：号敲错了）且晚到：修前出错那一支照样把原因写进消息行，乙的清单旁挂着甲的报错。"""
    b = world["b"]
    out, _ = run(client, admin, HOLD + """
hold("/api/patients/999999/authorizations");
const late = submit("#auth-list-form", { patient_id: "999999" });
await flush();
await submit("#auth-list-form", { patient_id: String(P.b) });
await flush();
const before = { html: htmlOf("#auth-table"), msg: msgOf("#auth-msg") };
release("/api/patients/999999/authorizations");
await late;
await flush();
return { before, after: { html: htmlOf("#auth-table"), msg: msgOf("#auth-msg") } };
""", {"b": b})
    assert f"<td>{world['auth_b']}</td>" in out["before"]["html"] and out["before"]["msg"] == "", out["before"]
    assert out["after"] == out["before"], out["after"]   # 修前消息行换成「患者不存在」
    assert f"患者 {b} 的调阅授权" in out["after"]["html"], out["after"]
