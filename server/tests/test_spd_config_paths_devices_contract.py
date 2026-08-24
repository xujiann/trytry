"""慢专病配置域 · 标准路径与设备/数据源的**响应契约**（paths 7 + devices 9）。

`spd/config` 的第二批。两处判断：

1. **`_template_out` 出三种形状**，靠两个条件键区分——列表只带 `node_count`、
   详情/新建带 `nodes` + `node_count`、复制/改状态两个都不带。故
   `response_model_exclude_unset=True`，且 `nodes` 必须声明在 `node_count`
   之前（序列化按声明顺序走，顺序不同即改字节）。
2. **`success_rate` 是 Float 列 + `round(..., 2)`**：满分也是 `100.0`。

paths 的两个 `DELETE` 返回 **204 无响应体**，`response_model` 对它们没有意义——
与 CSV 下载同理，判据已放宽为「204 也算声明了契约」，见
`test_api_contract_governance.py` 的 `_declares_empty_body` 与那条钉住清单的守卫。

**一条没写进变异清单的记录**：把 `last_sync_at` 从 `str` 改成 `str | None` 时
**零处转红**——handler 永远给值（None 折成空串），可空与否根本不影响字节。
那不是"守卫失效"，是这个选择本来就不由字节决定：声明成可空会在 OpenAPI 上
公告一个实际不会出现的 null，理由是契约要诚实，不是字节会变。
两类判断要分清，别把不咬人的变异当成证据。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.security import hash_password
from app.spd import models as S


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        org = Organization(name="路径契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        db.add(User(username="pathct", password_hash=hash_password("Path-ct-2026!"),
                    full_name="路径管理员", role="admin", org_id=org.id))
        prog = S.SpdProgram(code="PC-HTN", name="路径契约病种", active=True)
        db.add(prog)
        db.flush()
        draft = S.SpdPathTemplate(program_id=prog.id, code="PC-D", name="草稿路径",
                                  scene="outpatient", version="v1", status="draft",
                                  scope="region", description="说明", created_by="pathct")
        pub = S.SpdPathTemplate(program_id=prog.id, code="PC-P", name="已发布路径",
                                scene="followup", version="v1", status="published",
                                scope="org", org_id=org.id, created_by="pathct")
        db.add_all([draft, pub])
        db.flush()
        node = S.SpdPathNode(template_id=draft.id, key="n1", name="首诊", stage="stable",
                             seq=1, dept="心内科", exec_role="doctor",
                             service_type="followup", enter_condition=[],
                             complete_condition=[], next_key="n2", due_days=7,
                             timeout_action="remind", form_code="F1", note="备注")
        db.add(node)
        db.add(S.SpdPathNode(template_id=draft.id, key="n2", name="复诊", seq=2,
                             service_type="revisit", enter_condition=[],
                             complete_condition=[], due_days=30))
        db.add(S.SpdPathNode(template_id=pub.id, key="p1", name="已发布节点", seq=1,
                             enter_condition=[], complete_condition=[], due_days=7))
        # 设备：一台挂了机构且已绑定，一台两者皆空（照出可空列）
        db.add(S.SpdDevice(sn="PC-SN1", device_type="bp", model="BP-100", org_id=org.id,
                           bound_patient_id=1, status="bound"))
        dev_free = S.SpdDevice(sn="PC-SN2", device_type="glucose", model="", status="idle")
        db.add(dev_free)
        src = S.SpdDataSource(code="PC-HIS", name="HIS接口", source_type="HIS",
                              org_id=org.id, endpoint="http://his/api", freq_minutes=60,
                              scope="门诊", active=True, status="idle")
        db.add(src)
        db.commit()
        return {"draft": draft.id, "pub": pub.id, "prog": prog.id, "org": org.id,
                "node": node.id, "src": src.id, "dev_free": dev_free.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "pathct",
                              "password": "Path-ct-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


B = "/api/spd"
TEMPLATE_BASE = {"id", "program_id", "code", "name", "scene", "risk_level", "version",
                 "status", "scope", "org_id", "team_id", "description", "copied_from_id",
                 "created_by"}
NODE_KEYS = {"id", "template_id", "key", "name", "stage", "seq", "dept", "exec_role",
             "service_type", "enter_condition", "complete_condition", "next_key",
             "due_days", "timeout_action", "require_form", "require_evidence",
             "form_code", "note"}


# ------------------------------------------------- 三种形状
def test_路径模板列表只带node_count不带nodes(client, auth, seeded):
    rows = {t["code"]: t for t in client.get(f"{B}/path-templates", headers=auth).json()}
    row = rows["PC-D"]
    assert set(row) == TEMPLATE_BASE | {"node_count"}
    assert "nodes" not in row and row["node_count"] == 2
    # node_count 排在最后：`{**_template_out(t), "node_count": ...}`
    assert list(row)[-1] == "node_count"


def test_路径模板详情带nodes且nodes排在node_count之前(client, auth, seeded):
    body = client.get(f"{B}/path-templates/{seeded['draft']}", headers=auth).json()
    assert set(body) == TEMPLATE_BASE | {"nodes", "node_count"}
    assert list(body)[-2:] == ["nodes", "node_count"]
    assert body["node_count"] == len(body["nodes"]) == 2
    assert set(body["nodes"][0]) == NODE_KEYS
    assert body["nodes"][0]["key"] == "n1"          # 按 seq 排序
    assert body["org_id"] is None and body["copied_from_id"] is None


def test_复制与改状态两个键都不出现而不是null(client, auth, seeded):
    """本批最要紧的守卫：去掉 `exclude_unset`，这两条响应会冒出
    `"nodes": null, "node_count": null`——客户端照着 null 去读长度就是 TypeError。"""
    copied = client.post(f"{B}/path-templates/{seeded['pub']}/copy", headers=auth, json={})
    assert copied.status_code == 201
    assert set(copied.json()) == TEMPLATE_BASE
    assert copied.json()["copied_from_id"] == seeded["pub"]
    assert copied.json()["status"] == "draft"       # 复制出来一律是草稿

    changed = client.post(f"{B}/path-templates/{seeded['draft']}/status", headers=auth,
                          json={"status": "disabled"})
    assert changed.status_code == 200
    assert set(changed.json()) == TEMPLATE_BASE
    assert "nodes" not in changed.json() and "node_count" not in changed.json()


# ------------------------------------------------- 节点写侧
def test_节点增改的形状与已发布路径的拒绝(client, auth, seeded):
    added = client.post(f"{B}/path-templates/{seeded['draft']}/nodes", headers=auth,
                        json={"key": "n9", "name": "宣教", "service_type": "edu", "seq": 9})
    assert added.status_code == 201 and set(added.json()) == NODE_KEYS
    assert added.json()["enter_condition"] == [] and added.json()["complete_condition"] == []

    patched = client.patch(f"{B}/path-nodes/{seeded['node']}", headers=auth,
                           json={"name": "首诊(改)", "due_days": 14})
    assert patched.status_code == 200 and set(patched.json()) == NODE_KEYS
    assert patched.json()["name"] == "首诊(改)" and patched.json()["due_days"] == 14

    # 已发布路径不许直接加节点（在跑的实例会突然多出任务）
    blocked = client.post(f"{B}/path-templates/{seeded['pub']}/nodes", headers=auth,
                          json={"key": "px", "name": "往已发布加"})
    assert blocked.status_code == 409 and set(blocked.json()) == {"detail"}


def test_删除节点回204且响应体为空(client, auth, seeded):
    """204 的契约就是"没有响应体"。`response_model` 对它没有意义——
    函数直接返回 `Response(204)`，FastAPI 不会走模型。棘轮的判据据此放宽。"""
    added = client.post(f"{B}/path-templates/{seeded['draft']}/nodes", headers=auth,
                        json={"key": "ndel", "name": "待删节点"}).json()
    resp = client.delete(f"{B}/path-nodes/{added['id']}", headers=auth)
    assert resp.status_code == 204
    assert resp.content == b""
    assert client.delete(f"{B}/path-nodes/999999", headers=auth).status_code == 404


# ------------------------------------------------- 设备与数据源
def test_设备的两个可空列在无值时是null(client, auth, seeded):
    rows = {d["sn"]: d for d in client.get(f"{B}/devices", headers=auth).json()}
    assert set(rows["PC-SN1"]) == {"id", "sn", "device_type", "model", "org_id",
                                   "bound_patient_id", "status", "last_sync_at"}
    free = rows["PC-SN2"]
    assert free["org_id"] is None and free["bound_patient_id"] is None
    # 从未同步过：**空串**，不是 null（handler 自己折的）
    assert free["last_sync_at"] == ""


def test_绑定与解绑走同一形状(client, auth, seeded):
    bound = client.post(f"{B}/devices/{seeded['dev_free']}/bind", headers=auth,
                        json={"patient_id": 5}).json()
    assert bound["bound_patient_id"] == 5 and bound["status"] == "bound"
    unbound = client.post(f"{B}/devices/{seeded['dev_free']}/bind", headers=auth,
                          json={}).json()
    assert unbound["bound_patient_id"] is None and unbound["status"] == "idle"
    assert set(bound) == set(unbound)


SOURCE_KEYS = {"id", "code", "name", "source_type", "org_id", "endpoint", "freq_minutes",
               "scope", "active", "status", "last_sync_at", "last_rows",
               "last_latency_ms", "success_rate"}


def test_数据源成功率是float且同步登记连带回快照(client, auth, seeded):
    listed = {s["code"]: s for s in client.get(f"{B}/data-sources", headers=auth).json()}
    assert set(listed["PC-HIS"]) == SOURCE_KEYS
    assert listed["PC-HIS"]["last_sync_at"] == ""      # 尚未同步

    recorded = client.post(f"{B}/data-sources/{seeded['src']}/sync-logs", headers=auth,
                           json={"rows": 120, "latency_ms": 300, "success": True})
    assert recorded.status_code == 201
    assert set(recorded.json()) == {"id", "source"}
    source = recorded.json()["source"]
    assert set(source) == SOURCE_KEYS
    # 一次全成功 → 100.0，不是 100：Float 列 + round(..., 2)
    assert source["success_rate"] == 100.0 and isinstance(source["success_rate"], float)
    assert source["status"] == "running" and source["last_rows"] == 120

    failed = client.post(f"{B}/data-sources/{seeded['src']}/sync-logs", headers=auth,
                         json={"rows": 0, "latency_ms": 50, "success": False,
                               "message": "连接超时"}).json()
    assert failed["source"]["status"] == "failed"
    assert failed["source"]["success_rate"] == 50.0

    logs = client.get(f"{B}/data-sources/{seeded['src']}/sync-logs", headers=auth).json()
    assert set(logs[0]) == {"id", "started_at", "rows", "latency_ms", "success", "message"}


def test_接入总览的动态状态字典(client, auth):
    body = client.get(f"{B}/data-sources-monitor", headers=auth).json()
    assert set(body) == {"total", "by_status", "stale_over_24h", "avg_success_rate"}
    # by_status 只出现**实际存在**的状态，没有 failed 时不该硬塞 "failed": 0
    assert all(isinstance(v, int) for v in body["by_status"].values())
    assert sum(body["by_status"].values()) == body["total"]
    assert isinstance(body["avg_success_rate"], float)
    assert all(set(s) == SOURCE_KEYS for s in body["stale_over_24h"])
