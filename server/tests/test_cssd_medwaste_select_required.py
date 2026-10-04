"""消毒供应两个撤不回的动作、医废入暂存的下拉不再悄悄落在第一项（P2-1443，第四十二批扫描 AF4-2）。

修前 `spdModal` 的下拉不给空项，浏览器落在第一项：
- 消毒供应「发放」的接收机构列全部机构（`/api/organizations` 按 id 升序），不动下拉点确定就发给第一家，多半是中心自己；
  发放之后没有改去向的路（`PATCH` / `redispatch` 都 404），再点一下就「已回收」；
- 「以批次响应」的批次缺省是最新一个已灭菌批次，不管什么物品：「缝合包×5」的申领不动下拉，挂上的是「换药包」那一批
  （后端不查物品，P2-189 另案）；申领一经响应即定批次，没有反向端点；
- 医废「入暂存间」有多间暂存间时落在第一间；
- 申领表单的机构下拉缺省第一家，非全域经办不改它直接 403「无权以该机构名义写入数据」。

修法：`spdModal` 的 select 给了 `placeholder` 就首项为空、必选（按需开启，别的调用方不变）——没选就点确定，把提示写在框里、
框不关、不发请求；三处弹窗都用上。申领表单首项为空、必选（页面不知道登录者挂哪家机构，不缺省成本人机构）。后端判定不动。
回归照 P2-1411 的 node 渲染法：页面函数与 `spdModal` 原样拿到 node 里跑（夹具见 `tests/cssd_medwaste_page.py`）。
"""
import shutil

import pytest

from conftest import business_today
from cssd_medwaste_page import responses, run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    """县医院（消毒供应中心）+ 东镇、西镇；中心两批已灭菌（缝合包较早、换药包最新）；西镇申领缝合包×5；
    东镇两间暂存间、一包已收集的医废。"""
    orgs = {}
    for key, name, level, kind in (("center", "P21443 县人民医院（消毒供应中心）", "county", "lead_hospital"),
                                   ("east", "P21443 东镇卫生院", "township", "township"),
                                   ("west", "P21443 西镇卫生院", "township", "township")):
        resp = client.post("/api/organizations", headers=admin, json={"name": name, "org_type": kind, "level": level})
        assert resp.status_code in (200, 201), resp.text
        orgs[key] = resp.json()["id"]
    batches = {}
    for batch_no, item in (("P21443-01", "缝合包"), ("P21443-02", "换药包")):
        made = client.post("/api/cssd/batches", headers=admin, json={
            "batch_no": batch_no, "center_org_id": orgs["center"], "item_name": item, "quantity": 20})
        assert made.status_code == 201, made.text
        assert client.post(f"/api/cssd/batches/{made.json()['id']}/advance", headers=admin).status_code == 200
        batches[item] = made.json()["id"]
    request = client.post("/api/cssd/requests", headers=admin, json={
        "org_id": orgs["west"], "item_name": "缝合包", "quantity": 5})
    assert request.status_code == 201, request.text
    rooms = {}
    for name, kind in (("P21443 东楼暂存间", "storage"), ("P21443 西楼暂存间", "storage"), ("P21443 外科病区", "source")):
        loc = client.post("/api/medwaste/locations", headers=admin, json={
            "org_id": orgs["east"], "name": name, "location_type": kind})
        assert loc.status_code == 201, loc.text
        rooms[name] = loc.json()["id"]
    waste = client.post("/api/medwaste", headers=admin, json={
        "org_id": orgs["east"], "waste_type": "infectious", "weight_kg": 1.2,
        "collected_date": business_today().isoformat(), "source_location_id": rooms["P21443 外科病区"]})
    assert waste.status_code == 201, waste.text
    return {"orgs": orgs, "batches": batches, "request": request.json()["id"], "rooms": rooms,
            "waste": waste.json()["id"],
            "responses": responses(client, admin, "renderCssd", "drawCssdCosts", "renderMedwaste")}


#: 点按钮开框 → 下拉不动直接点确定 → 再选一项点确定。`ARGS.params`：render（页面函数名）、attr / dataset（点哪个按钮）、
#: field（框里的下拉）、pick（第二次选的值）
MODAL_STEPS = r"""
const P = ARGS.params;
await globalThis[P.render]();
const pending = click(P.attr, P.dataset);
await tick();
const modal = lastModal();
const options = modalSelects(modal)[P.field];
await submitModal(modal);
const untouchedRun = { posts: posts.splice(0), closed: modal.removed, msg: modalMsg(modal) };
await submitModal(modal, { [P.field]: P.pick });
await pending;
return { options, untouched: untouchedRun, picked: { posts, closed: modal.removed } };
"""


def _modal(world, render, attr, dataset, field, pick):
    return run(world["responses"], "admin", MODAL_STEPS, {
        "render": render, "attr": attr, "dataset": dataset, "field": field, "pick": pick})


def _assert_blank_first(options, placeholder):
    assert options[0] == {"value": "", "label": placeholder, "selected": False}, options   # 修前首项就是第一家 / 第一批
    assert not any(o["selected"] for o in options), options                                # 缺省就是这个空项


def test_发放弹窗的接收机构首项为空_不选不发请求_框里提示(world):
    batch, west = world["batches"]["缝合包"], world["orgs"]["west"]
    got = _modal(world, "renderCssd", "data-adv", {"adv": str(batch), "next": "sterile"}, "dispatched_to_org_id", west)
    # 修前：不动下拉就发出 advance?dispatched_to_org_id=<机构表第一家，即中心自己>，框也关了
    assert got["untouched"] == {"posts": [], "closed": False, "msg": "请选择接收机构"}
    _assert_blank_first(got["options"], "请选择接收机构")
    assert [o["label"] for o in got["options"][1:]] == [
        "P21443 县人民医院（消毒供应中心）", "P21443 东镇卫生院", "P21443 西镇卫生院"]
    assert got["picked"] == {"posts": [["POST", f"/api/cssd/batches/{batch}/advance?dispatched_to_org_id={west}", None]],
                             "closed": True}


def test_以批次响应的批次首项为空_不选不发请求_框里提示(world):
    request, batch = world["request"], world["batches"]["缝合包"]
    got = _modal(world, "renderCssd", "data-creqful", {"creqful": str(request)}, "batch_id", batch)
    # 修前：不动下拉就以最新一批「换药包」响应了缝合包的申领
    assert got["untouched"] == {"posts": [], "closed": False, "msg": "请选择响应批次"}
    _assert_blank_first(got["options"], "请选择响应批次")
    assert [o["label"] for o in got["options"][1:]] == ["P21443-02｜换药包×20", "P21443-01｜缝合包×20"]
    assert got["picked"] == {"posts": [["POST", f"/api/cssd/requests/{request}/fulfill?batch_id={batch}", None]],
                             "closed": True}


def test_入暂存间的暂存间首项为空_不选不发请求_框里提示(world):
    waste, room = world["waste"], world["rooms"]["P21443 西楼暂存间"]
    got = _modal(world, "renderMedwaste", "data-store", {"store": str(waste), "org": str(world["orgs"]["east"])},
                 "storage_location_id", room)
    assert got["untouched"] == {"posts": [], "closed": False, "msg": "请选择暂存间"}   # 修前记进第一间
    _assert_blank_first(got["options"], "请选择暂存间")
    assert [o["label"] for o in got["options"][1:]] == ["P21443 东楼暂存间", "P21443 西楼暂存间"]
    assert got["picked"] == {"posts": [["POST", f"/api/medwaste/{waste}/store", {"storage_location_id": room}]],
                             "closed": True}


def test_申领表单的机构首项为空必选_不选不发请求(world):
    west = world["orgs"]["west"]
    got = run(world["responses"], "admin", r"""
await renderCssd();
const options = selectsIn(pageHtml()).org_id;
const form = elements["#creq-form"];
const fields = (orgId) => ({ org_id: orgId, item_name: "缝合包", quantity: "5" });
await form.onsubmit({ preventDefault() {}, target: fields(untouched(options)) });
const untouchedRun = { posts: posts.splice(0), msg: $("#creq-msg").textContent };
await form.onsubmit({ preventDefault() {}, target: fields(String(ARGS.params.west)) });
return { options, required: /<select name="org_id" required>/.test(pageHtml()), untouched: untouchedRun, picked: posts };
""", {"west": west})
    # 修前：不动下拉就以机构表第一家（中心）的名义申领——非全域经办 403
    assert got["untouched"] == {"posts": [], "msg": "请选择申领机构"}
    _assert_blank_first(got["options"], "请选择申领机构")
    assert got["required"] is True
    assert got["picked"] == [["POST", "/api/cssd/requests", {"org_id": west, "item_name": "缝合包", "quantity": 5}]]


def test_不给placeholder的下拉照旧(world):
    """按需开启：别的调用方没给 placeholder 的下拉照旧落在第一项、框里也不多一行消息。"""
    got = run({}, "admin", r"""
const pending = spdModal("别的弹窗", [{ name: "k", label: "K", type: "select",
  options: [{ value: "a", label: "甲" }, { value: "b", label: "乙" }] }]);
await tick();
const modal = lastModal();
const options = modalSelects(modal).k;
const hasMsg = modal.html.includes("data-modal-msg");
await submitModal(modal);
return { options, hasMsg, value: await pending, closed: modal.removed };
""")
    assert got == {"options": [{"value": "a", "label": "甲", "selected": False},
                               {"value": "b", "label": "乙", "selected": False}],
                   "hasMsg": False, "value": {"k": "a"}, "closed": True}
