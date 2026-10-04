"""建机构给了上级时按层级阶梯拦；「新增机构」表单的层级与上级不给缺省（P1-247，第三十九批扫描 AC3-1）。

修前：`organizations.create_organization` 只查上级存不存在，同文件写好的阶梯 `_PARENT_LEVELS`（ADR-0005：村的上级只能是乡，
乡的上级只能是县或市）只有体检在用——村挂县、村挂村、乡挂乡、乡挂村一律 201。转诊审核链从此错位：挂在县医院下的村，县医院
做了「卫生院审核」之后「县级医院接收」谁做都 403；挂在村下的村由村卫生室做「卫生院审核」、县医院从未经手；而机构建好之后
上级改不了（P2-441），错一次就永久错着。「新增机构」表单的类型 / 层级 / 上级三个下拉缺省「牵头医院 / 县级 / 无上级机构」，
只改了类型的村卫生室落成一家县级树根（体检照样报「已满足」）；上级下拉列出全部机构、不按层级筛。

修后：给了上级就按 `_PARENT_LEVELS` 校验父机构层级，不在允许集合内 422（点名本机构层级、父机构层级与允许的层级）、不落库；
不给上级的孤儿照旧 201——全仓造数大量用「不挂上级的乡镇卫生院」，建时要不要拦另登待裁定。表单的层级必选，上级随层级重列：
乡、村两级只列阶梯允许的层级且必选，县、市级照旧可选「无上级机构」。类型与层级的配套（村卫生室↔村级）不校验，另登待裁定。
端到端用例在 `tests/e2e/test_flows.py`（test_新增机构的层级与上级不给缺省_上级按层级筛）。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routers.organizations import _PARENT_LEVELS

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
CORE = (STATIC / "core.js").read_text(encoding="utf-8")


def _mk(client, admin, name, level, org_type, parent_id=None):
    return client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": org_type, "level": level, "parent_id": parent_id})


def _names(client, admin) -> set[str]:
    return {o["name"] for o in client.get("/api/organizations", headers=admin).json()}


@pytest.fixture(scope="module")
def tree(client, admin):
    """一棵合法的县→乡→村，错挂的用例从里面挑上级。"""
    county = _mk(client, admin, "P1247 县人民医院", "county", "lead_hospital")
    assert county.status_code == 201, county.text
    town = _mk(client, admin, "P1247 甲镇卫生院", "township", "township", county.json()["id"])
    assert town.status_code == 201, town.text
    village = _mk(client, admin, "P1247 东村卫生室", "village", "village", town.json()["id"])
    assert village.status_code == 201, village.text
    return {"county": county.json(), "township": town.json(), "village": village.json()}


@pytest.mark.parametrize(("level", "parent_key", "expected", "parent_label"), [
    ("village", "county", "村级机构的上级只能是乡级机构", "是县级"),          # 村挂县
    ("village", "village", "村级机构的上级只能是乡级机构", "是村级"),         # 村挂村
    ("township", "township", "乡级机构的上级只能是市级或县级机构", "是乡级"),  # 乡挂乡
    ("township", "village", "乡级机构的上级只能是市级或县级机构", "是村级"),   # 乡挂村
], ids=["村挂县", "村挂村", "乡挂乡", "乡挂村"])
def test_给了上级的四种错挂_422且不落库(client, admin, tree, level, parent_key, expected, parent_label):
    parent = tree[parent_key]
    name = f"P1247 错挂{level}挂{parent_key}"
    resp = _mk(client, admin, name, level, level, parent["id"])
    assert resp.status_code == 422, resp.text   # 修前 201
    detail = resp.json()["detail"]
    assert expected in detail and f"所选上级「{parent['name']}」{parent_label}" in detail, detail
    assert name not in _names(client, admin)


def test_合法的县乡村逐级挂_201(client, admin):
    county = _mk(client, admin, "P1247 合法县医院", "county", "lead_hospital")
    assert county.status_code == 201, county.text
    town = _mk(client, admin, "P1247 合法卫生院", "township", "township", county.json()["id"])
    assert town.status_code == 201, town.text
    village = _mk(client, admin, "P1247 合法村卫生室", "village", "village", town.json()["id"])
    assert village.status_code == 201, village.text
    assert (town.json()["parent_id"], village.json()["parent_id"]) == (county.json()["id"], town.json()["id"])
    # 乡的上级也可以是市级（市级牵头架构，见 _PARENT_LEVELS）
    city = _mk(client, admin, "P1247 合法市医院", "city", "lead_hospital")
    assert city.status_code == 201, city.text
    town_city = _mk(client, admin, "P1247 市属卫生院", "township", "township", city.json()["id"])
    assert town_city.status_code == 201, town_city.text


@pytest.mark.parametrize(("level", "org_type"), [("township", "township"), ("village", "village")])
def test_不给上级的孤儿照旧201_特征化(client, admin, level, org_type):
    """特征化，不是认可：非县、市级缺上级即孤儿，会卡转诊审核（体检报 orphans）。建时要不要拦另登待裁定——全仓 500 多个
    用例按「不挂上级的乡镇卫生院」造数，本条只管给了上级时挂得对不对。"""
    resp = _mk(client, admin, f"P1247 孤儿{level}", level, org_type)
    assert resp.status_code == 201, resp.text
    assert resp.json()["parent_id"] is None


# ---------------------------------------------------------------- 页面「新增机构」表单


def _render_orgs() -> str:
    start = CORE.index("async function renderOrgs()")
    return CORE[start:CORE.index("\n}\n", start)]


def _const(name: str) -> str:
    match = re.search(rf"^const {name} = \{{[^\n]*\}};$", CORE, re.M)
    assert match, f"core.js 里找不到 {name}"
    return match.group(0)


def test_前端上级层级表与后端同一份():
    body = _const("ORG_PARENT_LEVELS").split("=", 1)[1].strip().rstrip(";")
    front = json.loads(re.sub(r"(\w+):", r'"\1":', body))
    assert {k: set(v) for k, v in front.items()} == _PARENT_LEVELS


def test_表单的层级与上级不给缺省():
    form = _render_orgs()
    form = form[form.index('<form class="inline" id="org-form">'):form.index("</form>")]
    # 层级：首项是空值占位、下拉必选（修前缺省落在第一项「县级」）
    assert '<select name="level" required><option value="">选择层级</option>' in form
    # 上级：先选层级才可选，不再一上来就落在「无上级机构」（修前缺省）
    assert '<select name="parent_id" disabled><option value="">先选层级</option></select>' in form
    assert "无上级机构" not in form


def _level_handler() -> str:
    page = _render_orgs()
    start = page.index("$(\"#org-form\").elements.level.onchange = (e) => {")
    body_start = page.index("{", start) + 1
    return page[body_start:page.index("\n  };\n", start)]


def test_上级下拉随层级重列_按阶梯筛():
    handler = _level_handler()
    assert "ORG_PARENT_LEVELS[level]" in handler
    assert "orgs.filter((o) => allowed.includes(o.level))" in handler
    assert "parentSelect.required = Boolean(allowed)" in handler


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端逻辑")
def test_跑一遍层级切换_上级只列允许的层级():
    """把页面的层级切换处理原样拿出来跑：村级只列乡级且必选，乡级只列县 / 市级且必选，县、市级照旧可不挂上级。"""
    shared = (STATIC / "shared.js").read_text(encoding="utf-8")
    esc = shared[shared.index("function esc(value) {"):shared.index("\n}\n", shared.index("function esc(value) {")) + 2]
    orgs = [{"id": 1, "name": "市医院", "level": "city"}, {"id": 2, "name": "县医院", "level": "county"},
            {"id": 3, "name": "甲镇卫生院", "level": "township"}, {"id": 4, "name": "东村卫生室", "level": "village"}]
    script = "\n".join([
        esc, _const("LEVELS"), _const("ORG_PARENT_LEVELS"),
        f"const orgs = {json.dumps(orgs, ensure_ascii=False)};",
        "const parentSelect = { innerHTML: '', disabled: true, required: false };",
        f"const handler = (e) => {{{_level_handler()}\n}};",
        "const out = {};",
        "for (const level of ['', 'village', 'township', 'county', 'city']) {",
        "  handler({ target: { value: level } });",
        "  const values = [...parentSelect.innerHTML.matchAll(/<option value=\"([^\"]*)\">([^<]*)</g)].map((m) => [m[1], m[2]]);",
        "  out[level || 'none'] = { values, disabled: parentSelect.disabled, required: parentSelect.required };",
        "}",
        "console.log(JSON.stringify(out));",
    ])
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True,
                                    timeout=60).stdout)
    assert out["none"] == {"values": [["", "先选层级"]], "disabled": True, "required": False}
    assert out["village"]["values"] == [["", "选择上级机构（乡级）"], ["3", "甲镇卫生院"]]
    assert out["township"]["values"] == [["", "选择上级机构（市级 / 县级）"], ["1", "市医院"], ["2", "县医院"]]
    assert out["village"]["required"] is out["township"]["required"] is True
    for level in ("county", "city"):   # 链路终点：照旧可选「无上级机构」，上级不限
        assert out[level]["values"][0] == ["", "无上级机构"] and len(out[level]["values"]) == 1 + len(orgs)
        assert out[level]["required"] is False and out[level]["disabled"] is False
