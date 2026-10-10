"""接种页三块按患者号查的面板只画最后一次查的那一位、写明是谁（P2-1790，第五十三批「页面的异步竞态与过期响应」扫描 AQ2-1）。

`pages-clinical.js::renderVaccination` 的接种前评估、禁忌清单（`drawContras`，含解除后的重画）、接种史三处都是「先清空 →
await → 写」，不比对请求序号，结果里也不写是谁。患者号敲错一位、改号后一个往返内再点查询，第一次的回包比第二次晚到：
- 禁忌清单换成甲的，「解除」按钮挂甲的患者号——扫描实测框里最后查的是乙，点解除发出 `/contraindications/<甲的>/lift`，
  甲（仍在发热）的接种前评估随即放行，乙照旧拦着；
- 接种前评估被无禁忌的丁的「可以接种，本次为第 1 剂」盖掉，表单里是有禁忌的乙；
- 接种史换成甲的各剂次，「上报 AEFI」按行上的甲的患者号跳过去。
P2-1009 / P2-378 只修了「查询失败」那一支（先清空、写原因），出错的回包晚到照样盖掉乙的结果。

修法照同文件 AEFI 剂次下拉的 `aefiDoseSeq`（P2-1012 同一修法）：三处各取序号，过期的回包（成功与出错两支）都丢弃；
解除在途时又查了别人，回来不再重画解除的那一位；清单头 / 评估结论写「患者 编号」（这几个接口都不带姓名，不为此另开接口）；
解除确认框写明是谁的哪条禁忌（同 P2-1774）。

这里把页面原样拿到 node 里跑（`vaccine_page.py`，请求转给真接口），页面的 `api` 外面包一层「扣住回包」：请求照发、回包照收，
地址以登记的片段结尾的那一次先不交给页面，等乙画完再放行——先后是确定的（同端到端 P2-1012 用 Playwright 扣请求）。
静态钉见 `test_panel_fetch_latest_wins.py`。
"""
import re
import shutil
from datetime import timedelta
from itertools import count

import pytest

from conftest import business_today
from vaccine_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 扣住回包：页面的 `api` 包一层——请求照发、回包照收（管道按先后配对），地址以 `hold` 登记的片段结尾的那一次先不交给页面，
#: `release` 才放行。`race` 先提交 first（被扣住）、再提交 second，second 画完记下结果区，放行 first 后再取一次
RACE = r"""
const gates = [];
const realApi = api;
api = (path, opts) => {
  const reply = realApi(path, opts);
  const gate = gates.find((g) => !g.taken && path.endsWith(g.tail));
  if (!gate) return reply;
  gate.taken = true;
  reply.catch(() => {});   // 扣着的期间回包出错，不算没人接
  return gate.opened.then(() => reply);
};
function hold(tail) {
  const gate = { tail, taken: false };
  gate.opened = new Promise((resolve) => { gate.open = resolve; });
  gates.push(gate);
}
/** 等到连着 5 拍没有在途请求（被扣住的那次回包已经收到，不算在途）、页面的后续代码跑完 */
const flush = async () => {
  for (let quiet = 0; quiet < 5;) { await new Promise((r) => setTimeout(r, 5)); quiet = inflight ? 0 : quiet + 1; }
};
const submit = (sel, fields) => document.querySelector(sel).onsubmit({ preventDefault() {}, target: form(fields) });
async function race(formSel, resultSel, tail, first, second, extra = {}) {
  hold(tail);
  const late = submit(formSel, { patient_id: String(first), ...extra });
  await flush();
  await submit(formSel, { patient_id: String(second), ...extra });
  await flush();
  const before = document.querySelector(resultSel).innerHTML;
  gates.find((g) => g.tail === tail).open();
  await late;
  await flush();
  return { before, after: document.querySelector(resultSel).innerHTML };
}
await goto(renderVaccination);
const P = ARGS.params;
"""

_cards = count(1)


def _patient(client, admin, name):
    card = f"33010220240{next(_cards):03d}1790"
    resp = client.post("/api/patients", headers=admin, json={"name": name, "id_card": card, "birth_date": "2024-01-01"})
    assert resp.status_code == 201 and resp.json()["created"], resp.text
    return resp.json()["id"]


def _contra(client, admin, pid, reason):
    resp = client.post("/api/vaccination/contraindications", headers=admin, json={
        "patient_id": pid, "vaccine_code": "HepB", "reason": reason, "contra_type": "temporary",
        "valid_until": (business_today() + timedelta(days=7)).isoformat()})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21790 接种门诊", "org_type": "township", "level": "township"}).json()["id"]


@pytest.fixture()
def pair(client, admin, org):
    """甲、乙各一条拦截中的 HepB 暂时禁忌、各一剂卡介苗；丁没有禁忌。每条用例一份，解除不串到别的用例。"""
    tag = next(_cards)
    a, b, d = (_patient(client, admin, f"P21790-{tag} {who}宝") for who in "甲乙丁")
    contras = {a: _contra(client, admin, a, f"P21790-{tag} 甲 发热 38.6℃，暂缓接种"),
               b: _contra(client, admin, b, f"P21790-{tag} 乙 发热 38.2℃，暂缓接种")}
    for pid, who in ((a, "甲"), (b, "乙")):
        resp = client.post("/api/vaccination/records", headers=admin, json={
            "patient_id": pid, "vaccine_code": "BCG", "vaccine_name": f"P21790-{tag} {who}的卡介苗", "org_id": org,
            "vaccinated_date": "2024-01-02"})
        assert resp.status_code == 201, resp.text
    return {"a": a, "b": b, "d": d, "tag": tag, "contras": contras}


def _allowed(client, admin, pid):
    return client.get(f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=HepB", headers=admin).json()["allowed"]


def test_禁忌清单_甲的回包晚到_清单与解除按钮仍是乙的_解除解的是乙(client, admin, pair):
    a, b, tag = pair["a"], pair["b"], pair["tag"]
    out = run(client, admin, RACE + """
const got = await race("#contra-list", "#contra-result", `contraindications?patient_id=${P.a}`, P.a, P.b);
// 操作者以为这是乙（框里最后查的是乙）的清单，点「解除」
const [, lift, pid] = got.after.match(/data-lift="(\\d+)" data-pid="(\\d+)"/);
const modals = [];
const realModal = spdModal;
spdModal = (title, fields, opts = {}) => { modals.push({ title, intro: opts.intro || "" }); return realModal(title, fields, opts); };
await document.querySelector("#contra-result").onclick({ target: { dataset: { lift, pid } } });
await flush();
return { ...got, modals, writes: requests.filter(([m]) => m !== "GET"),
         redrawn: document.querySelector("#contra-result").innerHTML };
""", {"a": a, "b": b}, modal={"lift_reason": "P21790 复测体温已正常"})
    assert f"P21790-{tag} 乙 发热" in out["before"], out["before"]
    assert out["after"] == out["before"], "甲的回包晚到，盖掉了乙的禁忌清单"   # 修前换成甲的「38.6℃」
    assert f"P21790-{tag} 甲" not in out["after"]
    assert f"患者 {b} 的接种禁忌" in out["after"], out["after"]   # 修前清单里不写是谁
    assert set(re.findall(r'data-pid="(\d+)"', out["after"])) == {str(b)}   # 修前「解除」挂甲的患者号
    (modal,) = out["modals"]
    assert modal["title"] == "解除接种禁忌"
    # 写明是谁的哪条禁忌（同 P2-1774）；后果照旧写着
    for text in (f"患者 {b}", "「HepB」", f"P21790-{tag} 乙 发热 38.2℃，暂缓接种", f"记录 {pair['contras'][b]}",
                 "不再因这一条拦截"):
        assert text in modal["intro"], (text, modal["intro"])
    assert out["writes"] == [["POST", f"/api/vaccination/contraindications/{pair['contras'][b]}/lift"]]   # 修前解的是甲的
    assert f"患者 {b} 的接种禁忌" in out["redrawn"] and "已解除" in out["redrawn"]   # 解除后重画的仍是乙
    assert (_allowed(client, admin, a), _allowed(client, admin, b)) == (False, True)   # 修前甲放行、乙照旧拦着


def test_接种前评估_无禁忌的丁晚到_结论仍是乙的禁止接种(client, admin, pair):
    b, d, tag = pair["b"], pair["d"], pair["tag"]
    out = run(client, admin, RACE + """
return await race("#vac-check", "#vac-check-result", `pre-check?patient_id=${P.d}&vaccine_code=HepB`, P.d, P.b,
                  { vaccine_code: "HepB" });
""", {"b": b, "d": d})
    assert "禁止接种" in out["before"] and f"P21790-{tag} 乙 发热" in out["before"], out["before"]
    assert out["after"] == out["before"], "丁的「可以接种」晚到，盖掉了乙的「禁止接种」"   # 修前「可以接种，本次为第 1 剂」
    # 结论写明是谁（修前只有「禁止接种：…」）
    assert out["after"] == f'<p class="msg err">患者 {b} 禁止接种：P21790-{tag} 乙 发热 38.2℃，暂缓接种</p>', out["after"]


def test_接种史_甲的回包晚到_表与上报AEFI按钮仍是乙的(client, admin, pair):
    a, b, tag = pair["a"], pair["b"], pair["tag"]
    out = run(client, admin, RACE + """
return await race("#vac-hist", "#vac-hist-result", `records?patient_id=${P.a}`, P.a, P.b);
""", {"a": a, "b": b})
    assert f"P21790-{tag} 乙的卡介苗" in out["before"], out["before"]
    assert out["after"] == out["before"], "甲的接种史晚到，盖掉了乙的"
    assert f"患者 {b} 的接种史" in out["after"], out["after"]   # 修前表上不写是谁
    assert set(re.findall(r'data-aefi-pid="(\d+)"', out["after"])) == {str(b)}   # 修前「上报 AEFI」挂甲的患者号


@pytest.mark.parametrize(("form", "result", "tail", "extra"), [
    ("#contra-list", "#contra-result", "/api/vaccination/contraindications?patient_id={pid}", {}),
    ("#vac-check", "#vac-check-result", "/api/vaccination/pre-check?patient_id={pid}&vaccine_code=HepB",
     {"vaccine_code": "HepB"}),
    ("#vac-hist", "#vac-hist-result", "/api/vaccination/records?patient_id={pid}", {}),
])
def test_过期的出错回包同样丢弃_不盖掉乙的结果(client, admin, pair, form, result, tail, extra):
    """甲那次查失败（这里垫成无权 403）且晚到：修前出错那一支照样把原因写进结果区，乙的结论 / 清单被冲掉。"""
    a, b = pair["a"], pair["b"]
    out = run(client, admin, RACE + """
return await race(P.form, P.result, P.tail, P.a, P.b, P.extra);
""", {"a": a, "b": b, "form": form, "result": result, "tail": tail.format(pid=a).removeprefix("/api/vaccination/"),
      "extra": extra}, overrides={tail.format(pid=a): (403, {"detail": "P21790 无权调阅该患者档案"})})
    assert "P21790 无权" not in out["before"], out["before"]
    assert out["after"] == out["before"], out["after"]   # 修前换成「P21790 无权调阅该患者档案」
    assert f"患者 {b}" in out["after"], out["after"]
