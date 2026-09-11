"""慢专病任务流转必须校验机构归属（2026-09-11 实测取证后补）。

## 修的是什么

`/api/spd/tasks/{id}/…` 的七个流转端点原先一律**按 id 直取、不校验归属**。
实测取证（乙院 doctor，任务属甲院）：claim / urge / escalate / submit /
complete **全部 200**。

## 最该记的一点：注释断言的与代码相反

同文件的 `batch_tasks` 早就修过这个形状（P1-47），它的注释白纸黑字写着——

    「同文件的单条接口一直是校验的（见 `claim_task`）」

**而 `claim_task` 恰恰是没校验的那个。** 批量版照着单条版修，可单条版本身就没有；
注释这么写了之后，就再没人回头核过。同文件真正有守卫的是 `assign_task` 与
`advance_instance` 两个——**同一个对象、同一个文件，九个写端点两套口径**。

## 为什么闸门一直没报

横向越权闸门只看端点自身函数体里的 `db.get(…)`，而取行在 `_load_task` 里——
与 `clinical_docs`（P0-10）、`outpatient_docs`（P0-11）同一个盲区，同一条线上的第三族。

## 口径

`assert_org_writable`：`director` 属全域角色，**县级中心跨机构督办/审核照常**；
只有 `doctor` / `public_health` 被收到本机构。`org_id` 为 None 的任务不拦
（`assert_org_writable` 的既定语义）。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdTask


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def task_world(client):
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "任务甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "任务乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("tk_doc_a", a, "doctor"), ("tk_doc_b", b, "doctor"),
                             ("tk_dir_b", b, "director")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    patient = client.post("/api/patients",
                          json={"name": "任务患者", "id_card": "320000199407075566"},
                          headers=admin).json()
    return {"admin": admin, "a": a, "b": b, "patient": patient,
            "doc_a": _login(client, "tk_doc_a"), "doc_b": _login(client, "tk_doc_b"),
            "dir_b": _login(client, "tk_dir_b")}


_DEFAULT_ORG = object()   # 哨兵：None 是**有意义的取值**（无机构归属），不能当"没传"


def _new_task(w, org_id=_DEFAULT_ORG, **kw):
    """默认建在甲院名下；显式传 `None` 表示**无机构归属**的任务。

    哨兵不是讲究：第一版写的是 `org_id=None` + `if org_id is not None else 甲院`，
    于是"显式传 None"被当成"没传"，那条用例实际建在甲院名下、拿到 403，
    看起来像代码误拦——**其实是夹具把有意义的 None 吞了**。
    """
    with SessionLocal() as db:
        task = SpdTask(patient_id=w["patient"]["id"], title="甲院的待办",
                       org_id=w["a"]["id"] if org_id is _DEFAULT_ORG else org_id, **kw)
        db.add(task)
        db.commit()
        return task.id


ACTIONS = [
    ("claim", {}),
    ("urge", {}),
    ("escalate", {}),
    ("submit", {"form": {"x": 1}, "submit": True}),
    ("complete", {}),
]


@pytest.mark.parametrize("action,body", ACTIONS)
def test_本机构医师流转任务照常(client, task_world, action, body):
    """先证明**放行的那条路是通的**——否则下面的 403 可能只是"全关了"。"""
    tid = _new_task(task_world)
    r = client.post(f"/api/spd/tasks/{tid}/{action}", json=body, headers=task_world["doc_a"])
    assert r.status_code == 200, f"本院医师做 {action} 被挡住了：{r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("action,body", ACTIONS)
def test_无关机构不得流转别家的任务(client, task_world, action, body):
    tid = _new_task(task_world)
    r = client.post(f"/api/spd/tasks/{tid}/{action}", json=body, headers=task_world["doc_b"])
    assert r.status_code == 403, f"乙院 doctor 做成了 {action}：{r.status_code} {r.text[:200]}"


def test_无关机构不得审核别家的任务(client, task_world):
    """review 要先有已提交的任务，单列一条。"""
    tid = _new_task(task_world)
    assert client.post(f"/api/spd/tasks/{tid}/submit",
                       json={"form": {"x": 1}, "submit": True},
                       headers=task_world["doc_a"]).status_code == 200
    r = client.post(f"/api/spd/tasks/{tid}/review",
                    json={"passed": True, "note": "乙院越权审核"},
                    headers=task_world["doc_b"])
    assert r.status_code == 403, f"乙院 doctor 审核了甲院的任务：{r.status_code} {r.text[:200]}"


def test_全域角色跨机构督办照常(client, task_world):
    """`director` 是全域角色——县级中心督办乡镇任务是**设计如此**，不能被这次收口掐掉。

    这条与上面的 403 同样重要：隔离做过头会挡掉真实业务，
    那比不做隔离更糟——它会让人把整套隔离关掉。
    """
    tid = _new_task(task_world)
    r = client.post(f"/api/spd/tasks/{tid}/urge", json={}, headers=task_world["dir_b"])
    assert r.status_code == 200, f"乙院 director 督办甲院任务被挡住了：{r.text[:200]}"


def test_无机构归属的任务不受本条收口影响(client, task_world):
    """`org_id` 为 None 的任务按 `assert_org_writable` 的既定语义不拦——
    "org_id 为 None 的记录不在此列，由各接口自己定语义"。这条钉住那个语义没被改掉。
    """
    tid = _new_task(task_world, org_id=None)
    r = client.post(f"/api/spd/tasks/{tid}/urge", json={}, headers=task_world["doc_b"])
    assert r.status_code == 200, f"无机构归属的任务被误拦：{r.status_code} {r.text[:200]}"
