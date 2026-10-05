"""流程引擎页给审批人、发起人看的几栏是编码或空白（P2-1475，第四十三批扫描 AG4-6）。

- 「流程」一栏印 definition_key（leave），不是流程名；
- 待办表没有机构栏：管理层（全域角色）的待办混着甲、乙两院的「院长审批」，分不出是哪家的；
- 流转记录的「从 / 到」印节点编码，「动作」印 advance / cancel，终止那一行的「到」印「终态」（单子并没有走到终态）；
- 操作人只取 full_name，账号没填姓名（开通时非必填）就是一格空白——同仓惯例是 full_name or username。

修法：实例与待办出参末尾补 `definition_name`，待办再补 `org_name`；流转记录末尾补 `from_node_name` / `to_node_name` /
`action_name`（advance=推进、cancel=终止，名称表只在后端 TRANSITION_ACTION_NAMES 一处）与 `actor_name`（full_name or
username）——原有键与次序不动，`actor` 原样只给 full_name。页面改印名称，终止那一行的「到」写「（已终止）」。
事项标题要不要必填是业务口径，不在这一条。页面一侧把 `renderWorkflows` 原样拿到 node 里跑（夹具见 `workflow_page.py`）。
"""
import re
import shutil

import pytest

from conftest import login
from workflow_page import run

INSTANCE_KEYS = ["id", "definition_key", "business_type", "business_id", "title", "org_id", "current_node",
                 "current_node_name", "current_node_role", "status", "updated_at",
                 "status_name", "created_by_name", "created_at",   # P2-1474
                 "definition_name"]
TRANSITION_KEYS = ["id", "from_node", "to_node", "action", "comment", "actor", "created_at",
                   "from_node_name", "to_node_name", "action_name", "actor_name"]


@pytest.fixture(scope="module")
def world(client, admin):
    """甲、乙两院各一位医生（甲院那位没填姓名）提请假单，甲院的管理层王（全域角色）待办里两院的「院长审批」都有；
    另有一张不挂机构的全县流程。管理层王先取一次待办，再终止乙院那张（材料不全）、批完甲院那张。"""
    orgs = {}
    for key, name in (("a", "P21475 甲卫生院"), ("b", "P21475 乙卫生院")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
    for username, role, org, full_name in (("p21475_doc_a", "doctor", "a", ""),
                                           ("p21475_doc_b", "doctor", "b", "乙院医生"),
                                           ("p21475_mgr", "director", "a", "管理层王")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": orgs[org], "full_name": full_name})
        assert resp.status_code == 201, resp.text
    h = {u: login(client, f"p21475_{u}", "pw123456") for u in ("doc_a", "doc_b", "mgr")}
    resp = client.post("/api/workflows/definitions", headers=admin, json={
        "key": "p21475_leave", "name": "P21475 请假审批",
        "nodes": [{"key": "apply", "name": "提交申请", "role": "doctor", "next": "approve"},
                  {"key": "approve", "name": "院长审批", "role": "director", "next": ""}]})
    assert resp.status_code == 201, resp.text

    def start(who, org, title):
        resp = client.post("/api/workflows/instances", headers=h[who], json={
            "definition_key": "p21475_leave", "business_type": "leave", "title": title,
            "org_id": orgs[org] if org else None})
        assert resp.status_code == 201, resp.text
        return resp.json()

    def step(iid, who, verb, comment=""):
        resp = client.post(f"/api/workflows/instances/{iid}/{verb}", headers=h[who], json={"comment": comment})
        assert resp.status_code == 200, resp.text
        return resp.json()

    ia = start("doc_a", "a", "")   # 标题留空：页面那一格不是必填
    ib = start("doc_b", "b", "P21475 请假3天")
    county = start("doc_a", None, "P21475 全县联合请假")
    advanced = step(ia["id"], "doc_a", "advance")
    step(ib["id"], "doc_b", "advance")
    step(county["id"], "doc_a", "advance")
    tasks = client.get("/api/workflows/my-tasks", headers=h["mgr"])
    assert tasks.status_code == 200, tasks.text
    running = client.get("/api/workflows/instances?status=running", headers=h["mgr"])
    assert running.status_code == 200, running.text
    step(ib["id"], "mgr", "cancel", "材料不全")
    step(ia["id"], "mgr", "advance", "同意")
    history = {}
    for key, inst in (("a", ia), ("b", ib)):
        resp = client.get(f"/api/workflows/instances/{inst['id']}/history", headers=h["mgr"])
        assert resp.status_code == 200, resp.text
        history[key] = resp.json()
    definitions = client.get("/api/workflows/definitions", headers=h["mgr"]).json()
    return {"orgs": orgs, "h": h, "ia": ia, "ib": ib, "county": county, "advanced": advanced,
            "tasks": tasks.json(), "running": running.json(), "history": history, "definitions": definitions}


# ---------------------------------------------------------------- 接口
def test_实例与待办出参末尾补流程名_待办再补机构名_原有键与次序不动(client, world):
    for body in (world["ia"], world["ib"], world["advanced"]):   # 发起、推进的回执
        assert list(body) == INSTANCE_KEYS and body["definition_name"] == "P21475 请假审批"
    listed = client.get("/api/workflows/instances", headers=world["h"]["mgr"], params={"limit": 500}).json()
    assert listed and all(list(r) == INSTANCE_KEYS for r in listed)
    tasks = {t["id"]: t for t in world["tasks"]["tasks"]}
    assert all(list(t) == INSTANCE_KEYS + ["org_name"] for t in tasks.values())
    got = {iid: (tasks[iid]["definition_name"], tasks[iid]["org_name"])
           for iid in (world["ia"]["id"], world["ib"]["id"], world["county"]["id"])}
    # 修前两行只看得到 leave 与同一个「院长审批」，分不出是哪家的
    assert got == {world["ia"]["id"]: ("P21475 请假审批", "P21475 甲卫生院"),
                   world["ib"]["id"]: ("P21475 请假审批", "P21475 乙卫生院"),
                   world["county"]["id"]: ("P21475 请假审批", "")}


def test_流转记录补起止节点名_动作名与操作人_actor原样只给姓名(world):
    for rows in world["history"].values():
        assert all(list(r) == TRANSITION_KEYS for r in rows)
    shape = [(r["from_node"], r["from_node_name"], r["to_node"], r["to_node_name"], r["action"], r["action_name"],
              r["actor"], r["actor_name"], r["comment"]) for r in world["history"]["a"]]
    assert shape == [("apply", "提交申请", "approve", "院长审批", "advance", "推进", "", "p21475_doc_a", ""),
                     ("approve", "院长审批", "", "", "advance", "推进", "管理层王", "管理层王", "同意")]
    shape = [(r["from_node_name"], r["to_node"], r["to_node_name"], r["action"], r["action_name"], r["actor_name"],
              r["comment"]) for r in world["history"]["b"]]
    assert shape == [("提交申请", "approve", "院长审批", "advance", "推进", "乙院医生", ""),
                     ("院长审批", "", "", "cancel", "终止", "管理层王", "材料不全")]


# ---------------------------------------------------------------- 页面
@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面脚本")
def test_页面印流程名_机构名_节点名_动作名与操作人_终止行不印终态(world):
    ia, ib = world["ia"]["id"], world["ib"]["id"]
    get = {"/api/workflows/definitions": world["definitions"], "/api/workflows/my-tasks": world["tasks"],
           "/api/workflows/instances?status=running": world["running"],
           f"/api/workflows/instances/{ia}/history": world["history"]["a"],
           f"/api/workflows/instances/{ib}/history": world["history"]["b"]}
    out = run(f"""
      await renderWorkflows();
      const page = $("#page-body").innerHTML;
      await $("#page-body").onclick({{ target: {{ dataset: {{ history: "{ib}" }} }} }});
      const cancelled = $("#wf-history-body").innerHTML;
      await $("#page-body").onclick({{ target: {{ dataset: {{ history: "{ia}" }} }} }});
      return {{ page, cancelled, completed: $("#wf-history-body").innerHTML }};
    """, {"get": get})
    tasks = out["page"][:out["page"].index("流程图形化编排")]
    assert "<th>机构</th>" in tasks
    for iid, org in ((ia, "P21475 甲卫生院"), (ib, "P21475 乙卫生院"), (world["county"]["id"], "全县流程")):
        assert re.search(rf"<tr><td>{iid}</td><td>P21475 请假审批</td>\s*<td>{org}</td>", tasks), (iid, tasks)
    assert "<td>p21475_leave</td>" not in tasks   # 修前「流程」一栏印编码
    instances = out["page"][out["page"].index("<h3>流程实例</h3>"):]
    assert f"<tr><td>{world['county']['id']}</td><td>P21475 请假审批</td>" in instances, instances
    assert "<td>p21475_leave</td>" not in instances

    rows = re.findall(r"<tr>(.*?)</tr>", out["cancelled"].split("<tbody>")[1], re.S)
    cells = [re.findall(r"<td>(.*?)</td>", r, re.S) for r in rows]
    assert [c[:5] for c in cells] == [["提交申请", "院长审批", "推进", "", "乙院医生"],
                                      ["院长审批", "（已终止）", "终止", "材料不全", "管理层王"]], cells
    rows = re.findall(r"<tr>(.*?)</tr>", out["completed"].split("<tbody>")[1], re.S)
    cells = [re.findall(r"<td>(.*?)</td>", r, re.S) for r in rows]
    # 没填姓名的医生回落账号（修前空白）；办完那一行的「到」照旧印终态
    assert [c[:5] for c in cells] == [["提交申请", "院长审批", "推进", "", "p21475_doc_a"],
                                      ["院长审批", "终态", "推进", "同意", "管理层王"]], cells
