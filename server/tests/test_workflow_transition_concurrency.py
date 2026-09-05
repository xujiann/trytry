"""流程实例跃迁的并发闸门（P1-30 · `workflow_transitions`）。

这张表的不变式**不长在自己身上**，而长在父行 `workflow_instances` 上：
"一个实例离开某个节点恰好一次"，流转行只是那一次跃迁的留痕。所以守法不是唯一索引，
而是父行条件 UPDATE（`WHERE status='running' AND current_node=读到的节点`），
留痕只在 rowcount 命中后、同一个事务里追加。

为什么**不**建 `(instance_id, from_node)` 唯一索引：`_validate_nodes` 不拒环
（a→b→a 只要另有终态就存得下），环形定义里合法的第二圈会撞索引，单子从此既推不动
也终止不了——`test_环形定义里的第二圈仍然照常留痕` 就是这条理由的可执行版本。

SQLite 上**测不出**这条竞态：库级写锁把两路请求排成前后脚，晚到的那一路是在赢家
提交之后才 `db.get`，它读到的是新节点，于是合法地推进下一步——那是顺序语义，不是
竞态。所以本档的防拆卸网是"两个 Session 手工制造过期读"这条确定性用例
（`test_条件更新对过期读拦得住`）与静态钉（`test_推进与终止必须走条件UPDATE`），
真并发的证据在 `tests/test_workflow_transition_unique_races.py`（真 PG）。
"""
import ast
from pathlib import Path

import pytest

ROUTER_PATH = Path(__file__).resolve().parents[1] / "app" / "routers" / "workflows.py"

#: 节点被别人推走、实例还在流转中——顺序路径下不存在的一句（晚到的人看到的是下一个节点）
STALE_MOVE_DETAIL = "当前节点刚被其他人推进，请刷新后重试"

LINEAR_NODES = [
    {"key": "apply", "name": "科室申请", "role": "", "next": "pharmacy"},
    {"key": "pharmacy", "name": "药学审核", "role": "", "next": "approve"},
    {"key": "approve", "name": "院长审批", "role": "", "next": ""},
]

# 环形定义：a→b→a，另有一个够不着的终态节点让 _validate_nodes 放行。
# 这正是"不建 (instance_id, from_node) 唯一索引"的原因——它的第二圈合法。
CYCLIC_NODES = [
    {"key": "loop_a", "name": "初审", "role": "", "next": "loop_b"},
    {"key": "loop_b", "name": "复核", "role": "", "next": "loop_a"},
    {"key": "loop_end", "name": "归档", "role": "", "next": ""},
]


@pytest.fixture(scope="module")
def definitions(client, admin):
    for key, name, nodes in (
        ("wf_race_linear", "并发闸门直线流程", LINEAR_NODES),
        ("wf_race_cyclic", "并发闸门环形流程", CYCLIC_NODES),
    ):
        resp = client.post(
            "/api/workflows/definitions",
            json={"key": key, "name": name, "nodes": nodes},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
    return {"linear": "wf_race_linear", "cyclic": "wf_race_cyclic"}


def _start(client, admin, definition_key, title):
    resp = client.post(
        "/api/workflows/instances",
        json={"definition_key": definition_key, "business_type": "wf_race",
              "business_id": 0, "title": title},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ================================================================ 行为不变（特征化）


def test_顺序重复推进与重复终止的409逐字未变(client, admin, definitions):
    """条件 UPDATE 顶替读改写之后，顺序路径上的每一句 409 必须一字不差。

    这四句是历史契约（前端直接把 `detail` 弹给人看），改了就是破坏性变更。
    """
    iid = _start(client, admin, definitions["linear"], "顺序推到底")
    for _ in range(3):
        assert client.post(
            f"/api/workflows/instances/{iid}/advance", json={"comment": "同意"}, headers=admin
        ).status_code == 200
    late = client.post(f"/api/workflows/instances/{iid}/advance", json={}, headers=admin)
    assert late.status_code == 409
    assert late.json()["detail"] == "当前状态 completed 不可推进"
    late_cancel = client.post(f"/api/workflows/instances/{iid}/cancel", json={}, headers=admin)
    assert late_cancel.status_code == 409
    assert late_cancel.json()["detail"] == "当前状态 completed 不可终止"

    other = _start(client, admin, definitions["linear"], "顺序终止")
    assert client.post(
        f"/api/workflows/instances/{other}/cancel", json={"comment": "撤回"}, headers=admin
    ).json() == {"id": other, "status": "cancelled"}
    twice = client.post(f"/api/workflows/instances/{other}/cancel", json={}, headers=admin)
    assert twice.status_code == 409
    assert twice.json()["detail"] == "当前状态 cancelled 不可终止"
    after = client.post(f"/api/workflows/instances/{other}/advance", json={}, headers=admin)
    assert after.status_code == 409
    assert after.json()["detail"] == "当前状态 cancelled 不可推进"


def test_环形定义里的第二圈仍然照常留痕(client, admin, definitions):
    """同一实例从同一个节点走第二次是**合法**的，闸门不许把它拦掉。

    这条用例就是"为什么不在 workflow_transitions 上建 (instance_id, from_node)
    唯一索引"的证据：建了的话，第二圈 a→b 撞索引，这张单子既推不动（每次推进都要
    写留痕）也终止不了（终止那条留痕的 from_node 同样会撞），一条良性的多余行被换成
    一份卡死的单子。条件 UPDATE 认的是"当前位置"，对环形定义照样成立。
    """
    iid = _start(client, admin, definitions["cyclic"], "转两圈")
    for expected in ("loop_b", "loop_a", "loop_b"):
        resp = client.post(
            f"/api/workflows/instances/{iid}/advance", json={"comment": "转一格"}, headers=admin
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["current_node"] == expected
        assert resp.json()["status"] == "running"

    history = client.get(f"/api/workflows/instances/{iid}/history", headers=admin).json()
    assert [h["from_node"] for h in history] == ["loop_a", "loop_b", "loop_a"]
    assert [h["to_node"] for h in history] == ["loop_b", "loop_a", "loop_b"]
    # 同一 (instance_id, from_node) 上确实并存两行——合法的多行
    assert sum(1 for h in history if h["from_node"] == "loop_a") == 2


def test_跃迁没命中时推进与终止各自给出自己的409(client, admin, definitions, monkeypatch):
    """把 rowcount=0 那条路走一遍：接线（动词、状态码、文案）不靠并发也要测得到。

    真并发的证据在真 PG 档；这里用桩把"UPDATE 没命中"确定性地造出来，钉住的是
    `advance`/`cancel` 各自把哪个动词交给 `_stale_move_409`，以及两种分支的措辞。
    """
    from sqlalchemy import update

    from app.database import SessionLocal
    from app.models import WorkflowInstance
    from app.routers import workflows as workflows_router

    def _lose(flip_status=None):
        def fake(db, instance_id, from_node, **values):
            if flip_status is not None:  # 模拟"赢家已经把状态改走并提交"
                other = SessionLocal()
                try:
                    other.execute(
                        update(WorkflowInstance)
                        .where(WorkflowInstance.id == instance_id)
                        .values(status=flip_status)
                    )
                    other.commit()
                finally:
                    other.close()
            return False
        return fake

    # ① 状态还是 running、只是节点被推走了 → 新增的那句，推进与终止共用
    iid = _start(client, admin, definitions["linear"], "抢输的推进")
    monkeypatch.setattr(workflows_router, "_move_instance", _lose())
    lost_advance = client.post(f"/api/workflows/instances/{iid}/advance", json={}, headers=admin)
    assert lost_advance.status_code == 409
    assert lost_advance.json()["detail"] == STALE_MOVE_DETAIL
    lost_cancel = client.post(f"/api/workflows/instances/{iid}/cancel", json={}, headers=admin)
    assert lost_cancel.status_code == 409
    assert lost_cancel.json()["detail"] == STALE_MOVE_DETAIL

    # ② 状态被赢家改走了 → 复用顺序请求那句，调用方分辨不出"撞车"与"晚了一步"
    monkeypatch.setattr(workflows_router, "_move_instance", _lose("cancelled"))
    beaten = client.post(f"/api/workflows/instances/{iid}/advance", json={}, headers=admin)
    assert beaten.status_code == 409
    assert beaten.json()["detail"] == "当前状态 cancelled 不可推进"

    finished = _start(client, admin, definitions["linear"], "抢输的终止")
    monkeypatch.setattr(workflows_router, "_move_instance", _lose("completed"))
    beaten_cancel = client.post(f"/api/workflows/instances/{finished}/cancel", json={}, headers=admin)
    assert beaten_cancel.status_code == 409
    assert beaten_cancel.json()["detail"] == "当前状态 completed 不可终止"

    # 抢输的那几路一条留痕都不许留下（回滚把它们连同 UPDATE 一起退掉）
    assert client.get(f"/api/workflows/instances/{iid}/history", headers=admin).json() == []
    assert client.get(f"/api/workflows/instances/{finished}/history", headers=admin).json() == []


def test_条件更新对过期读拦得住(client, admin, definitions):
    """防拆卸网：把 `_move_instance` 的条件删掉（或改回读改写），这条立刻变红。

    行为用例分辨不出闸门在不在——顺序请求下预检就给 409，SQLite 的库级写锁又让线程
    探针对拆卸不敏感。这里手工造出竞态里真实发生的那件事：两个 Session 都读到同一个
    节点，一个先提交，另一个的 UPDATE 必须**一行都不命中**，并按库里现状措辞。
    """
    from app.database import SessionLocal
    from app.models import WorkflowInstance
    from app.routers.workflows import _move_instance, _stale_move_409

    iid = _start(client, admin, definitions["linear"], "两个会话抢同一格")
    session_a, session_b = SessionLocal(), SessionLocal()
    try:
        instance_a = session_a.get(WorkflowInstance, iid)
        instance_b = session_b.get(WorkflowInstance, iid)  # B 与 A 读到同一个节点
        assert instance_a.current_node == instance_b.current_node == "apply"

        assert _move_instance(session_a, iid, "apply", current_node="pharmacy") is True
        session_a.commit()

        # B 拿着过期读再推同一格：条件里的 current_node='apply' 已经不成立
        assert _move_instance(session_b, iid, "apply", current_node="pharmacy") is False
        stale = _stale_move_409(session_b, instance_b, "推进")
        assert stale.status_code == 409 and stale.detail == STALE_MOVE_DETAIL

        # 赢家接着终止；B 再来一次，这次状态被改走了，措辞回到顺序路径那一句
        assert _move_instance(session_a, iid, "pharmacy", status="cancelled") is True
        session_a.commit()
        assert _move_instance(session_b, iid, "pharmacy", current_node="approve") is False
        beaten = _stale_move_409(session_b, instance_b, "推进")
        assert beaten.status_code == 409 and beaten.detail == "当前状态 cancelled 不可推进"
        assert _stale_move_409(session_b, instance_b, "终止").detail == "当前状态 cancelled 不可终止"
    finally:
        session_b.rollback()
        session_a.rollback()
        session_b.close()
        session_a.close()


# ================================================================ 防拆卸静态钉


def _functions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _calls(func: ast.FunctionDef, name: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def _method_calls(func: ast.FunctionDef, obj: str, attr: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
        and isinstance(n.func.value, ast.Name) and n.func.value.id == obj
    ]


def test_条件更新的where里状态与节点一个都不能少():
    """`_move_instance` 的两个条件各挡一种抢跑，掉一个都等于把洞放回去。

    掉 `status='running'`：推进能盖掉别人的终止（终止过的单子又活过来）。
    掉 `current_node=读到的节点`：两路推进都命中，同一个节点被批两次。
    """
    move = _functions().get("_move_instance")
    assert move is not None, "_move_instance 没了——父行条件 UPDATE 是这张表唯一的闸门"
    compared = {
        node.left.attr: node
        for node in ast.walk(move)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute)
        and isinstance(node.left.value, ast.Name) and node.left.value.id == "WorkflowInstance"
    }
    assert "status" in compared, "WHERE 里没有 status 条件：推进会盖掉并发的终止"
    assert "current_node" in compared, "WHERE 里没有 current_node 条件：同一节点会被推两次"
    running = compared["status"].comparators[0]
    assert isinstance(running, ast.Constant) and running.value == "running", (
        "status 条件不再钉在 'running' 上"
    )
    assert isinstance(compared["current_node"].comparators[0], ast.Name), (
        "current_node 必须与 UPDATE 之前读好的局部变量比对——"
        "UPDATE 之后再读 instance.current_node 拿到的是新值，留痕会把 from_node 写成 to_node"
    )


@pytest.mark.parametrize("handler,verb", [("advance_instance", "推进"), ("cancel_instance", "终止")])
def test_推进与终止必须走条件UPDATE而不是读改写(handler, verb):
    """两个写入点的形状钉死：先条件 UPDATE，命中了才留痕，中间不许 commit。

    回到 `db.add(留痕)` → `instance.status = ...` → commit 的老写法，或者把留痕挪到
    UPDATE 前面、在两者之间插一次 commit，这条都会红——那些写法都会让并发的两路各写
    一条留痕。
    """
    func = _functions()[handler]

    assigned = {
        target.attr
        for node in ast.walk(func) if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
        and target.value.id == "instance"
    }
    assert not assigned & {"status", "current_node", "updated_at"}, (
        f"{handler} 又在 Python 侧给 instance.{assigned} 赋值了——那是 check-then-act"
    )

    moves = _calls(func, "_move_instance")
    assert len(moves) == 1, f"{handler} 必须恰好走一次 _move_instance"
    stale = _calls(func, "_stale_move_409")
    assert len(stale) == 1, f"{handler} 没有处理 rowcount=0 的分支"
    assert [a.value for a in stale[0].args if isinstance(a, ast.Constant)] == [verb], (
        f"{handler} 交给 _stale_move_409 的动词必须是 {verb!r}——"
        "动词串了行，抢输者会看到另一个接口的措辞"
    )

    adds = _method_calls(func, "db", "add")
    commits = _method_calls(func, "db", "commit")
    assert len(adds) == 1 and len(commits) == 1, f"{handler} 的写入点应当只有一处 db.add + 一次 commit"
    assert moves[0].lineno < adds[0].lineno < commits[0].lineno, (
        f"{handler} 的留痕必须写在条件 UPDATE 命中之后、同一个事务里 commit 之前"
    )
