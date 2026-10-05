"""流程引擎页只取「流转中」的实例：办完或被终止的单子整个从页面消失，发起人查不到自己的单子批没批（P2-1474，
第四十三批扫描 AG4-3 + AG4-4 的留痕半边）。

页面实例面板写死 `instances?status=running`，「流转记录」按钮又只在这张表里：三级审批办完后，发起人、经手医生、admin 的
「流转中实例」「我的待办」都是空的，谁批的、意见是什么只有接口 `?status=completed` 取得到；药师终止了医生甲的单子（原因
「同类药品已有两种在用，不予引进」），医生甲两张表都是空的，原因只在接口里。引擎不回写业务表，这一页是唯一看审批结论的
地方。发起这一步又不进流转记录，发起人、发起时间哪个接口都不出（库里 created_by 有）。

修法：
- `list_instances` 只增可选的 `mine=true`（只看本人发起的），在原有可见范围之内再收窄；原有参数语义不变；
- `WorkflowInstanceOut` 末尾只增 `status_name`（文案取自后端 INSTANCE_STATUS_NAMES）、`created_by_name`
  （full_name or username，按本页 created_by 批量取）、`created_at`——原有键与次序不动；发起这一步不往流转记录里补行
  （那会改历史接口的输出），发起人要不要持首节点角色是业务口径，不在这一条；
- 页面实例面板加状态筛选（流转中 / 已完成 / 已终止 / 全部状态，缺省仍是流转中）与「只看我发起的」，改筛选只重画实例表，
  每一行都有「流转记录」。页面一侧把 `renderWorkflows` 原样拿到 node 里跑（夹具见 `workflow_page.py`）。
"""
import re
import shutil
from pathlib import Path

import pytest

from conftest import login
from workflow_page import run

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")

#: 修前 WorkflowInstanceOut 的十一个键，次序照旧；新键只许补在末尾
OLD_KEYS = ["id", "definition_key", "business_type", "business_id", "title", "org_id", "current_node",
            "current_node_name", "current_node_role", "status", "updated_at"]
NEW_KEYS = ["status_name", "created_by_name", "created_at"]
#: 之后又只在末尾补的（P2-1475：流程名；待办行再多一个机构名）
LATER_KEYS = ["definition_name"]


@pytest.fixture(scope="module")
def world(client, admin):
    """一家卫生院的三级审批：医生甲发起的一张办完、一张被药师终止；医生丁发起的一张还在流转；经办（没填姓名）发起一张。
    另一家卫生院的医生与这几张都没有关系。"""
    orgs = {}
    for key, name in (("own", "P21474 卫生院"), ("other", "P21474 别家卫生院")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
    for username, role, org, full_name in (("p21474_doc", "doctor", "own", "P21474 医生甲"),
                                           ("p21474_doc2", "doctor", "own", "P21474 医生丁"),
                                           ("p21474_ph", "pharmacist", "own", "P21474 药师乙"),
                                           ("p21474_dir", "director", "own", "P21474 院长丙"),
                                           ("p21474_op", "operator", "own", ""),
                                           ("p21474_far", "doctor", "other", "P21474 别家医生")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": orgs[org], "full_name": full_name})
        assert resp.status_code == 201, resp.text
    h = {u: login(client, f"p21474_{u}", "pw123456") for u in ("doc", "doc2", "ph", "dir", "op", "far")}
    resp = client.post("/api/workflows/definitions", headers=admin, json={
        "key": "p21474_drug", "name": "P21474 新药引进审批",
        "nodes": [{"key": "apply", "name": "科室申请", "role": "doctor", "next": "pharmacy"},
                  {"key": "pharmacy", "name": "药学审核", "role": "pharmacist", "next": "approve"},
                  {"key": "approve", "name": "院长审批", "role": "director", "next": ""}]})
    assert resp.status_code == 201, resp.text

    def start(who, title):
        resp = client.post("/api/workflows/instances", headers=h[who], json={
            "definition_key": "p21474_drug", "business_type": "drug_intro", "title": title, "org_id": orgs["own"]})
        assert resp.status_code == 201, resp.text
        return resp.json()

    def step(iid, who, verb, comment):
        resp = client.post(f"/api/workflows/instances/{iid}/{verb}", headers=h[who], json={"comment": comment})
        assert resp.status_code == 200, resp.text
        return resp.json()

    started = start("doc", "P21474 引进某长效降压药")
    done = started["id"]
    advanced = step(done, "doc", "advance", "临床需要")
    step(done, "ph", "advance", "药学评估通过")
    step(done, "dir", "advance", "同意引进")
    cancelled = start("doc", "P21474 引进某抗凝药")["id"]
    step(cancelled, "doc", "advance", "临床需要")
    step(cancelled, "ph", "cancel", "同类药品已有两种在用，不予引进")
    running = start("doc2", "P21474 引进某降糖药")["id"]
    by_op = start("op", "P21474 经办代发起")
    return {"orgs": orgs, "h": h, "done": done, "cancelled": cancelled, "running": running, "by_op": by_op,
            "started": started, "advanced": advanced}


def _list(client, headers, **params):
    resp = client.get("/api/workflows/instances", headers=headers, params={"limit": 500, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 接口
def test_办完的实例_发起人按已完成且只看我发起的取得到_能打开流转记录(client, world):
    rows = _list(client, world["h"]["doc"], status="completed", mine="true")
    assert [r["id"] for r in rows] == [world["done"]]
    (row,) = rows
    assert (row["status"], row["status_name"], row["created_by_name"]) == ("completed", "已完成", "P21474 医生甲")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?", row["created_at"]), row["created_at"]
    assert row["created_at"] <= row["updated_at"]
    history = client.get(f"/api/workflows/instances/{world['done']}/history", headers=world["h"]["doc"])
    assert history.status_code == 200, history.text
    assert [(h["from_node"], h["to_node"], h["comment"]) for h in history.json()] == [
        ("apply", "pharmacy", "临床需要"), ("pharmacy", "approve", "药学评估通过"), ("approve", "", "同意引进")]


def test_被终止的实例同理_终止原因打得开(client, world):
    rows = _list(client, world["h"]["doc"], status="cancelled", mine="true")
    assert [(r["id"], r["status_name"]) for r in rows] == [(world["cancelled"], "已终止")]
    history = client.get(f"/api/workflows/instances/{world['cancelled']}/history", headers=world["h"]["doc"]).json()
    assert [(h["action"], h["comment"]) for h in history][-1] == ("cancel", "同类药品已有两种在用，不予引进")


def test_只看我发起的只在可见范围之内收窄(client, world):
    mine = {r["id"] for r in _list(client, world["h"]["doc2"], mine="true")}
    assert world["running"] in mine and not {world["done"], world["cancelled"]} & mine   # 修前 mine 被忽略，全都回来
    everyone = {r["id"] for r in _list(client, world["h"]["doc2"])}
    assert {world["done"], world["cancelled"], world["running"], world["by_op"]["id"]} <= everyone   # 不带它照旧
    for params in ({}, {"mine": "true"}):   # 别家机构的医生：本来就看不到，带不带 mine 都一样
        assert not {r["id"] for r in _list(client, world["h"]["far"], **params)} & everyone
    assert _list(client, world["h"]["doc"], status="running", mine="true") == []   # 状态与 mine 两个条件同时成立


def test_出参末尾补三键_原有键与次序不动_没填姓名的发起人回落账号(client, world):
    rows = _list(client, world["h"]["doc"])
    assert all(list(r) == OLD_KEYS + NEW_KEYS + LATER_KEYS for r in rows), [list(r) for r in rows][:1]
    by_op = next(r for r in rows if r["id"] == world["by_op"]["id"])
    assert (by_op["created_by_name"], by_op["status_name"]) == ("p21474_op", "流转中")   # 修前没有这两个键
    tasks = client.get("/api/workflows/my-tasks", headers=world["h"]["doc"]).json()["tasks"]
    assert tasks and all(list(t) == OLD_KEYS + NEW_KEYS + LATER_KEYS + ["org_name"] for t in tasks)
    # 发起与推进的回执同一个出参
    for body in (world["started"], world["advanced"], world["by_op"]):
        assert list(body) == OLD_KEYS + NEW_KEYS + LATER_KEYS
    assert (world["started"]["created_by_name"], world["advanced"]["created_by_name"]) == ("P21474 医生甲",) * 2
    assert world["advanced"]["created_at"] == world["started"]["created_at"]   # 推进不改发起时间


def test_发起这一步照旧不进流转记录(client, world):
    """补发起人、发起时间走实例出参，不往流转记录里补一行——那会改历史接口的输出。"""
    resp = client.get(f"/api/workflows/instances/{world['by_op']['id']}/history", headers=world["h"]["op"])
    assert resp.status_code == 200 and resp.json() == []


# ---------------------------------------------------------------- 页面
def test_页面实例面板不再只取流转中():
    body = PAGE[PAGE.index("async function renderWorkflows("):PAGE.index("const SR_FILTER")]
    assert 'api("/api/workflows/instances?status=running")' not in PAGE   # 修前写死只取流转中
    assert body.count("api(`/api/workflows/instances${wfInstanceQuery()}`)") == 2   # 首屏与改筛选同一个取法
    assert 'const WF_INSTANCE_FILTER = { status: "running", mine: "" };' in PAGE   # 缺省照旧是流转中


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面脚本")
def test_页面按已完成或已终止且只看我发起的筛_每一行都能打开流转记录(client, world):
    doc = world["h"]["doc"]
    paths = ["/api/workflows/definitions", "/api/workflows/instances?status=running",
             "/api/workflows/instances?status=completed",
             "/api/workflows/instances?status=completed&mine=true", "/api/workflows/instances?status=cancelled&mine=true",
             "/api/workflows/my-tasks", f"/api/workflows/instances/{world['done']}/history",
             f"/api/workflows/instances/{world['cancelled']}/history"]
    get = {}
    for path in paths:
        resp = client.get(path, headers=doc)
        assert resp.status_code == 200, (path, resp.text)
        get[path] = resp.json()
    out = run(f"""
      await renderWorkflows();
      const first = $("#page-body").innerHTML;
      const filter = $("#wf-inst-filter");
      await filter.onchange({{ target: {{ name: "status", type: "select-one", value: "completed" }} }});
      await filter.onchange({{ target: {{ name: "mine", type: "checkbox", checked: true }} }});
      const completed = $("#wf-inst-list").innerHTML;
      await $("#page-body").onclick({{ target: {{ dataset: {{ history: "{world['done']}" }} }} }});
      const doneTrail = $("#wf-history-body").innerHTML;
      await filter.onchange({{ target: {{ name: "status", type: "select-one", value: "cancelled" }} }});
      const cancelled = $("#wf-inst-list").innerHTML;
      await $("#page-body").onclick({{ target: {{ dataset: {{ history: "{world['cancelled']}" }} }} }});
      return {{ first, completed, doneTrail, cancelled, cancelTrail: $("#wf-history-body").innerHTML,
               calls: CALLS.map((c) => c.path), routed: ROUTED, msg: $("#wf-inst-msg").textContent }};
    """, {"get": get})
    calls = out["calls"]
    assert calls[:3] == ["/api/workflows/definitions", "/api/workflows/instances?status=running",
                         "/api/workflows/my-tasks"], calls   # 首屏缺省照旧只看流转中
    assert "/api/workflows/instances?status=completed&mine=true" in calls
    assert "/api/workflows/instances?status=cancelled&mine=true" in calls
    assert out["routed"] == 0, "改筛选只重画实例表，不整页重画（画布、填了一半的表单不被冲掉）"
    first = out["first"][out["first"].index("<h3>流程实例</h3>"):]   # 首屏的实例面板：只有流转中的
    assert f"<tr><td>{world['running']}</td>" in first and "<td>流转中</td>" in first, first
    assert f"<tr><td>{world['done']}</td>" not in first and f"<tr><td>{world['cancelled']}</td>" not in first
    assert (f"<tr><td>{world['done']}</td>" in out["completed"] and "<td>P21474 医生甲</td>" in out["completed"]
            and "<td>已完成</td>" in out["completed"]
            and f'data-history="{world["done"]}">流转记录</button>' in out["completed"]), out["completed"]
    assert f"<td>{world['cancelled']}</td>" not in out["completed"]
    assert "同意引进" in out["doneTrail"] and "药学评估通过" in out["doneTrail"], out["doneTrail"]
    assert (f"<tr><td>{world['cancelled']}</td>" in out["cancelled"] and "<td>已终止</td>" in out["cancelled"]
            and f'data-history="{world["cancelled"]}">流转记录</button>' in out["cancelled"]), out["cancelled"]
    assert "同类药品已有两种在用，不予引进" in out["cancelTrail"], out["cancelTrail"]
    assert out["msg"] == ""
