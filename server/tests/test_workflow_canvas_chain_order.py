"""流程画布存出去的节点顺序、定义表的「节点链」与实例实际起步的节点对不上（P2-1473，第四十三批扫描 AG4-1 的前端半边）。

实例发起时落在定义数组的第 0 个节点（`start_instance` 的 `definition.nodes[0]`），而画布按数组顺序摆节点、没有「改节点」：
要改首节点（比如换「科室申请」的角色）只能删了重加，新节点 push 到数组末尾，「保存为定义」原样提交——存出去是
[药学审核→院长审批, 院长审批(终态), 科室申请→药学审核]，前后端校验都过、201，医生发起后实例直接落在「药学审核」，
「科室申请」从没发生。定义表的「节点链」又按数组顺序拼接，印成「药学审核 → 院长审批 → 科室申请」，看上去申请只是
最后一步；终态节点先画的（[归档, 申请, 院长审批]）印成「归档 → 申请 → 院长审批」，实例却一推就「已完成」。

修法只改页面（后端「首节点 / 可达」的口径随待裁定 P2-1033 定，这一条不改后端、不加 422）：
- 画布保存前沿 next 从链头重排：链头是唯一一个没有入边的节点；只有链头唯一、且沿 next 走得到全部节点时才重排，
  有环、有走不到的节点就保持原顺序、不拦；「产出的 JSON」预览按保存时的顺序印；
- 定义表从实际起点 nodes[0] 沿 next 打印，走回已经过的节点写「回到」，起点走不到的节点另起一行标「起点走不到」。

这里把画布那一段与 `renderWorkflows` 原样拿到 node 里跑（夹具见 `workflow_page.py`）：载入定义 → 删首节点 → 加回来 →
连线 → 保存，看提交出去的 JSON 并真交给后端发起一次；定义表喂真接口取回的定义，看「节点链」一格与实例实际起步的节点。
"""
import json
import re
import shutil

import pytest

from conftest import login
from workflow_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面脚本")

TAG = '<br><span class="tag red">起点走不到</span> '

DEFINITIONS = {
    # 种子里那条「新药引进审批」的正常顺序：画布从它载入改编
    "p21473_drug": ("P21473 新药引进审批", [
        {"key": "apply", "name": "科室申请", "role": "doctor", "next": "pharmacy"},
        {"key": "pharmacy", "name": "药学审核", "role": "pharmacist", "next": "approve"},
        {"key": "approve", "name": "院长审批", "role": "director", "next": ""}]),
    # 修前的画布删了重加「科室申请」之后存出去的样子（扫描复现的那一份）
    "p21473_bad": ("P21473 删了重加的新药引进", [
        {"key": "pharmacy", "name": "药学审核", "role": "pharmacist", "next": "approve"},
        {"key": "approve", "name": "院长审批", "role": "director", "next": ""},
        {"key": "apply", "name": "科室申请", "role": "doctor", "next": "pharmacy"}]),
    # 终态节点先画的采购流程
    "p21473_purchase": ("P21473 采购审批", [
        {"key": "archive", "name": "归档", "role": "", "next": ""},
        {"key": "apply", "name": "申请", "role": "doctor", "next": "approve"},
        {"key": "approve", "name": "院长审批", "role": "director", "next": "archive"}]),
    # 插入的审核节点没人指向（两个没有入边的节点）
    "p21473_leave": ("P21473 请假审批", [
        {"key": "apply", "name": "申请", "role": "doctor", "next": "approve"},
        {"key": "review", "name": "科室审核", "role": "pharmacist", "next": "approve"},
        {"key": "approve", "name": "院长审批", "role": "director", "next": ""}]),
    # 并发用例里那种环形定义：a→b→a，另有一个够不着的终态让后端放行
    "p21473_loop": ("P21473 环形复核", [
        {"key": "loop_a", "name": "初审", "role": "", "next": "loop_b"},
        {"key": "loop_b", "name": "复核", "role": "", "next": "loop_a"},
        {"key": "loop_end", "name": "归档", "role": "", "next": ""}]),
}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21473 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21473_doc", "password": "pw123456", "role": "doctor", "org_id": org, "full_name": "P21473 医生"})
    assert resp.status_code == 201, resp.text
    for key, (name, nodes) in DEFINITIONS.items():
        resp = client.post("/api/workflows/definitions", headers=admin, json={"key": key, "name": name, "nodes": nodes})
        assert resp.status_code == 201, resp.text   # 前提：后端照收，首节点就是数组第 0 个
    definitions = client.get("/api/workflows/definitions", headers=admin).json()
    return {"org": org, "doc": login(client, "p21473_doc", "pw123456"), "definitions": definitions}


def _start(client, world, key):
    resp = client.post("/api/workflows/instances", headers=world["doc"], json={
        "definition_key": key, "business_type": "p21473", "title": f"P21473 {key}", "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _save_from_canvas(world, key, edits=""):
    """画布载入 `key` 这份定义、做完 `edits` 里的操作后点「保存为定义」，回提交出去的节点与预览。"""
    return run(f"""
      wfCanvasInit(DATA.definitions);
      $("#wfc-load").onchange({{ target: {{ value: {key!r} }} }});
      $("#wfc-meta").key.value = "p21473_canvas";
      {edits}
      const preview = $("#wfc-json").textContent;
      await $("#wfc-save").onclick(EV);
      return {{ preview, posted: POSTED }};
    """, {"get": {"/api/users/roles": {}}, "definitions": world["definitions"]})


# ---------------------------------------------------------------- 画布保存前沿 next 从链头重排
def test_删了首节点再加回来_存出去的首项仍是链头_实例从申请起步(client, admin, world):
    out = _save_from_canvas(world, "p21473_drug", """
      const loaded = WF_SELECTED;                    // 载入后选中的就是首节点「科室申请」
      if (loaded !== "apply") throw new Error(`载入后选中的是 ${loaded}`);
      $("#wfc-del").onclick(EV);
      MODAL.push({ key: "apply", name: "科室申请", role: "doctor" });
      await $("#wfc-add").onclick(EV);              // 加回来：push 到数组末尾、并选中它
      $("#wfc-link").onclick(EV);
      NODE_EL.pharmacy.onclick();                    // 连线：科室申请 → 药学审核
    """)
    (posted,) = out["posted"]
    assert posted["path"] == "/api/workflows/definitions"
    nodes = posted["body"]["nodes"]
    # 修前提交的是 [pharmacy, approve, apply]：实例发起后落在药学审核，科室申请从没发生
    assert [n["key"] for n in nodes] == ["apply", "pharmacy", "approve"], nodes
    assert nodes == DEFINITIONS["p21473_drug"][1], "重排只换顺序，节点的字段原样"
    assert json.loads(out["preview"]) == nodes, "预览写着「提交给后端的就是它」，顺序要与提交的一致"

    # 真交给后端：存得下，发起后落在科室申请
    saved = client.post("/api/workflows/definitions", headers=admin, json=posted["body"])
    assert saved.status_code == 201, saved.text
    assert _start(client, world, "p21473_canvas")["current_node"] == "apply"


def test_终态节点先画的_保存时也排回链头起步(world):
    nodes = _save_from_canvas(world, "p21473_purchase")["posted"][0]["body"]["nodes"]
    assert [n["key"] for n in nodes] == ["apply", "approve", "archive"]   # 修前原样存出去，一推就「已完成」


@pytest.mark.parametrize("key", ["p21473_loop", "p21473_leave"])
def test_有环或有走不到的节点_保存时保持原顺序_也不拦(world, key):
    """环形定义唯一没有入边的是够不着的终态「归档」，排到前面就成了一推即完成的流程；两个链头的分不出谁先——都不动。"""
    out = _save_from_canvas(world, key)
    (posted,) = out["posted"]   # 照常提交：后端怎么判随 P2-1033 定，页面不拦
    assert posted["body"]["nodes"] == DEFINITIONS[key][1]


# ---------------------------------------------------------------- 定义表按实际路径打印
def _chain_cells(world) -> dict:
    out = run("""
      await renderWorkflows();
      return { html: $("#page-body").innerHTML };
    """, {"get": {"/api/workflows/definitions": world["definitions"], "/api/workflows/instances": [],
                  "/api/workflows/my-tasks": {"count": 0, "tasks": []}}})
    cells = {}
    for key in DEFINITIONS:
        m = re.search(rf"<tr><td>{key}</td><td>[^<]*</td>\s*<td>(.*?)</td>\s*<td>启用</td></tr>", out["html"], re.S)
        assert m, f"定义表里没找到 {key} 那一行"
        cells[key] = m.group(1)
    return cells


def test_定义表的节点链从实际起点沿next打印_走不到的另起一行标出(client, world):
    cells = _chain_cells(world)
    # 一条直链：与原先按数组拼的一字不差
    assert cells["p21473_drug"] == "科室申请(doctor) → 药学审核(pharmacist) → 院长审批(director)"
    # 修前印「药学审核(pharmacist) → 院长审批(director) → 科室申请(doctor)」，看上去申请只是最后一步
    assert cells["p21473_bad"] == f"药学审核(pharmacist) → 院长审批(director){TAG}科室申请(doctor)"
    assert cells["p21473_purchase"] == f"归档{TAG}申请(doctor)、院长审批(director)"
    assert cells["p21473_leave"] == f"申请(doctor) → 院长审批(director){TAG}科室审核(pharmacist)"
    assert cells["p21473_loop"] == f"初审 → 复核 → 回到 初审{TAG}归档"
    # 打印的起点就是实例实际落下的节点
    assert _start(client, world, "p21473_bad")["current_node_name"] == "药学审核"
    assert _start(client, world, "p21473_purchase")["current_node_name"] == "归档"


def test_节点链一格照样转义(world):
    """节点名与角色由管理员录入，打印换了写法也一律 esc()。"""
    definitions = [{"id": 1, "key": "p21473_xss", "name": "x", "active": True, "nodes": [
        {"key": "a", "name": "<b>申请</b>", "role": "doc\"tor", "next": "b"},
        {"key": "b", "name": "审批&归档", "role": "", "next": ""},
        {"key": "c", "name": "<i>没人指向</i>", "role": "", "next": "b"}]}]
    out = run("""
      await renderWorkflows();
      return { html: $("#page-body").innerHTML };
    """, {"get": {"/api/workflows/definitions": definitions, "/api/workflows/instances": [],
                  "/api/workflows/my-tasks": {"count": 0, "tasks": []}}})
    assert (f"&lt;b&gt;申请&lt;/b&gt;(doc&quot;tor) → 审批&amp;归档{TAG}&lt;i&gt;没人指向&lt;/i&gt;"
            in out["html"]), out["html"]
