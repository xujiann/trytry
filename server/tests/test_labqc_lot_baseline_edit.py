"""室内质控批号的靶值 / SD 建好后改不了：SD 多敲一位，之后永远判不出失控（P2-1368，第四十批扫描 AD3-8）。

模型注释写靶值与标准差取「定值质控品说明书或前 20 次测定累积均值/SD」，改批号的 `PATCH /api/labqc/lots/{id}` 却只收
`active`。实测（修前）：血钾批号 SD 应为 0.1、误录成 1.0，测得 4.45（真实 z=+4.5）判为在控；`PATCH {sd: 0.1, active: true}`
返回 200、sd 照旧是 1.0（多余字段被静默忽略）；停用后按同一批号重建撞唯一约束 409——只能编一个假批号，批号追溯就断了。

修法：`LotPatch` 加可选的靶值 / SD（取值约束照建批号的 `LotCreate`：有限值，SD 须大于 0）。该批号**还没有任何测定点**时
照改（「没有测定点」与改写压进同一条 UPDATE）；已有测定点时 409，文案写明「已有测定点，改靶值要先定既往判定怎么处理」——
既往判定怎么处理（未处理的点按新靶值重判、已处理的保留原判定，还是改唯一约束允许同批号重建）是业务口径，登记待裁定，本条不做
重判。与现值相同的不算改；一项都没送的 422；只改 `active` 的照旧（有没有测定点都能启停）。请求体里认不得的键照本文件其余请求
模型的缺省口径忽略（不单为这一个模型改成 422）。

页面入口：批号 L-J 面板上，还没有测定点的摆「改靶值 / SD」——框里预填现值、由框自己提交（打开页面之后别人先录了点的，
409 的原话写在框里），改完重画页面，台账里的靶值 / SD 一并换成新的；有测定点的不摆。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import login

from app.database import SessionLocal
from app.models import QcLot

BASELINE_LOCKED = "已有测定点，改靶值要先定既往判定怎么处理"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21368 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _lot(client, admin, org, lot_no, target=4.0, sd=1.0):
    made = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "K", "item_name": "血钾", "lot_no": lot_no, "target_value": target, "sd": sd})
    assert made.status_code == 201, made.text
    return made.json()


def _measure(client, admin, lot_id, value, measured_at):
    made = client.post(f"/api/labqc/lots/{lot_id}/measurements", headers=admin,
                       json={"value": value, "measured_at": measured_at})
    assert made.status_code == 201, made.text
    return made.json()


def _stored(lot_id):
    with SessionLocal() as db:
        lot = db.get(QcLot, lot_id)
        return lot.target_value, lot.sd, lot.active


def test_没有测定点时改SD生效_之后的测定按新SD判(client, admin, org):
    lot = _lot(client, admin, org, "P21368-TYPO", target=4.0, sd=1.0)   # SD 应为 0.1，录成了 1.0
    fixed = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json={"sd": 0.1, "active": True})
    assert fixed.status_code == 200, fixed.text
    assert (fixed.json()["target_value"], fixed.json()["sd"], fixed.json()["active"]) == (4.0, 0.1, True)
    assert _stored(lot["id"]) == (4.0, 0.1, True)   # 修前 200 而 sd 照旧 1.0
    point = _measure(client, admin, lot["id"], 4.45, "2026-10-04 08:00")   # z=(4.45-4.0)/0.1=+4.5
    assert (point["out_of_control"], point["violated_rules"]) == (True, "1-3s"), point   # 修前按 SD 1.0 判成在控
    lj = client.get(f"/api/labqc/lots/{lot['id']}/levey-jennings", headers=admin).json()
    assert (lj["sd"], lj["lines"]["sd3_upper"], lj["points"][0]["z"]) == (0.1, 4.3, 4.5)


def test_没有测定点时改靶值生效(client, admin, org):
    """按「前 20 次测定累积均值」定靶的另起一个试测批号；这里只钉靶值本身改得动、L-J 参考线跟着走。"""
    lot = _lot(client, admin, org, "P21368-TARGET", target=5.0, sd=0.5)
    moved = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json={"target_value": 5.2})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["target_value"], moved.json()["sd"], moved.json()["active"]) == (5.2, 0.5, True)
    lj = client.get(f"/api/labqc/lots/{lot['id']}/levey-jennings", headers=admin).json()
    assert lj["lines"]["mean"] == 5.2


def test_已有测定点时改靶值或SD一律409_靶值SD与启停都不动(client, admin, org):
    lot = _lot(client, admin, org, "P21368-MEASURED", target=4.0, sd=1.0)
    point = _measure(client, admin, lot["id"], 4.45, "2026-10-04 08:00")
    for body in ({"target_value": 4.2}, {"sd": 0.1}, {"sd": 0.1, "active": False}, {"target_value": 4.0, "sd": 0.1}):
        refused = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json=body)
        assert refused.status_code == 409, (body, refused.text)
        assert BASELINE_LOCKED in refused.json()["detail"], refused.text
        assert _stored(lot["id"]) == (4.0, 1.0, True), body   # 整条不落：连一起送来的停用也不生效
    points = client.get(f"/api/labqc/lots/{lot['id']}/measurements", headers=admin).json()
    assert [(p["id"], p["out_of_control"]) for p in points] == [(point["id"], False)]   # 既往判定不重判（待裁定）


def test_与现值相同的不算改_有测定点照样200(client, admin, org):
    lot = _lot(client, admin, org, "P21368-SAME", target=4.0, sd=1.0)
    _measure(client, admin, lot["id"], 4.1, "2026-10-04 08:00")
    same = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json={"target_value": 4.0, "sd": 1.0, "active": False})
    assert same.status_code == 200, same.text
    assert _stored(lot["id"]) == (4.0, 1.0, False)


def test_只改启停的行为不变_有测定点也能停用与重新启用(client, admin, org):
    lot = _lot(client, admin, org, "P21368-ACTIVE")
    _measure(client, admin, lot["id"], 4.2, "2026-10-04 08:00")
    off = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json={"active": False})
    assert off.status_code == 200 and off.json()["active"] is False, off.text
    refused = client.post(f"/api/labqc/lots/{lot['id']}/measurements", headers=admin, json={"value": 4.0})
    assert refused.status_code == 409   # 停用的批号照旧不收测定值
    on = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json={"active": True})
    assert on.status_code == 200 and on.json()["active"] is True, on.text
    assert _stored(lot["id"]) == (4.0, 1.0, True)


@pytest.mark.parametrize("n, body", list(enumerate([
    {"sd": 0}, {"sd": -0.1}, {"sd": "NaN"}, {"target_value": "Infinity"}, {"target_value": None, "sd": None}, {},
    {"SD": 0.1},   # 认不得的键照本文件的缺省口径忽略，忽略完一项都没有，照样 422
])))
def test_取值约束照建批号_一项都没送的422(client, admin, org, n, body):
    lot = _lot(client, admin, org, f"P21368-BAD-{n}")
    bad = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json=body)
    assert bad.status_code == 422, (body, bad.text)
    assert _stored(lot["id"]) == (4.0, 1.0, True)


def test_别家机构改靶值SD_403且不动(client, admin, org):
    other = client.post("/api/organizations", headers=admin, json={
        "name": "P21368 乙县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21368_other", "password": "passw0rd1", "role": "doctor", "org_id": other})
    assert created.status_code in (200, 201), created.text
    lot = _lot(client, admin, org, "P21368-OWN")
    resp = client.patch(f"/api/labqc/lots/{lot['id']}", headers=login(client, "p21368_other", "passw0rd1"),
                        json={"sd": 0.1})
    assert resp.status_code == 403, resp.text
    assert _stored(lot["id"]) == (4.0, 1.0, True)


# ---------- 页面入口：还没有测定点的批号摆「改靶值 / SD」 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML、登记监听的假元素；`api()` 记下每次调用、按路径回给定的数据；
#: `spdModal()` 记下标题与字段，照框自己提交的写法把字段（SD 改成 0.1、其余原样）交给 `submit`；`route()` 记次数
_HARNESS = """
const els = {};
const calls = [];
let modal = null;
let routed = 0;
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
const DATA = JSON.parse(process.argv[1]);
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  return opts.method === "PATCH" ? { ...DATA.lot, ...JSON.parse(opts.body) } : DATA.get[path];
}
async function spdModal(title, fields, opts = {}) {
  modal = { title, fields, intro: opts.intro || "" };
  return opts.submit(Object.fromEntries(fields.map((f) => [f.name, f.name === "sd" ? 0.1 : f.value])));
}
function route() { routed += 1; }
function formJson() { return {}; }
function postAction() {}
"""


def _run_page(client, admin, lot_id: int) -> dict:
    """照真接口的返回渲染质控页：打开批号的 L-J 面板，面板上有「改靶值 / SD」就点一下。"""
    lot = next(row for row in client.get("/api/labqc/lots", headers=admin).json() if row["id"] == lot_id)
    get = {path: client.get(path, headers=admin).json() for path in (
        "/api/labqc/lots", f"/api/labqc/lots/{lot_id}/levey-jennings", f"/api/labqc/lots/{lot_id}/measurements")}
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(page, "async function renderLabQc(")
              + "(async () => { await renderLabQc();\n"
              f"  await els['#page-body'].listeners.click({{ target: {{ dataset: {{ lot: '{lot_id}' }} }} }});\n"
              "  const detail = els['#lot-detail'].innerHTML;\n"
              f"  if (detail.includes('data-baseline=')) await els['#lot-detail'].onclick("
              f"{{ target: {{ dataset: {{ baseline: '{lot_id}' }} }} }});\n"
              "  process.stdout.write(JSON.stringify({ detail, modal, calls, routed })); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps({"lot": lot, "get": get}, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_没有测定点的批号摆改靶值SD_预填现值_交给PATCH_改完重画(client, admin, org):
    lot = _lot(client, admin, org, "P21368-PAGE", target=4.0, sd=1.0)
    out = _run_page(client, admin, lot["id"])
    assert f'data-baseline="{lot["id"]}">改靶值 / SD</button>' in out["detail"]   # 修前页面上无处可改
    assert out["modal"]["title"] == "改靶值 / SD"
    assert [(f["name"], f["type"], f["value"], f["required"]) for f in out["modal"]["fields"]] == [
        ("target_value", "number", 4.0, True), ("sd", "number", 1.0, True)]   # 预填现值
    assert "P21368-PAGE" in out["modal"]["intro"]
    assert out["calls"][-1] == ["PATCH", f"/api/labqc/lots/{lot['id']}", {"target_value": 4.0, "sd": 0.1}]
    assert out["routed"] == 1   # 改完重画：台账里的靶值 / SD 换成新的
    # 页面交的这一份，接口照收（没有测定点）
    saved = client.patch(f"/api/labqc/lots/{lot['id']}", headers=admin, json=out["calls"][-1][2])
    assert saved.status_code == 200 and _stored(lot["id"]) == (4.0, 0.1, True), saved.text


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_有测定点的批号不摆改靶值SD(client, admin, org):
    lot = _lot(client, admin, org, "P21368-PAGE-MEASURED", target=4.0, sd=1.0)
    _measure(client, admin, lot["id"], 4.1, "2026-10-04 08:00")
    out = _run_page(client, admin, lot["id"])
    assert "data-baseline=" not in out["detail"] and "改靶值" not in out["detail"]   # 后端 409，入口一并不摆
    assert out["modal"] is None and not any(method == "PATCH" for method, _, _ in out["calls"])
