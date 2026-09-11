"""归属隔一跳的四个慢专病写端点必须校验机构（2026-09-11 实测取证后补）。

## 实测取证（乙院 doctor，对象全在甲院名下）

    PATCH /api/spd/interventions/{id}              → 200  办结了甲院的干预任务、替它填反馈
    POST  /api/spd/package-bindings/{id}/usages    → 201  扣了甲院服务包的次数
    POST  /api/spd/package-bindings/{id}/unbind    → 200  把甲院的服务包解绑了
    PATCH /api/spd/path-instances/{id}             → 200  取消了甲院的临床路径实例

四个端点里有三个**连 `user` 形参都没有**。

## 为什么闸门此前看不见

三张表（`spd_interventions` / `spd_package_bindings` / `spd_path_instances`）
**都没有机构列**，归属要经 `enrollment_id` 回到 `spd_enrollments.org_id`。
而横向越权闸门的分母是「**直接取的那张表**带机构列」，于是整族掉在分母外。
现由 `ONEHOP_UNGUARDED_WRITES` 数着（P1-54）。

⚠️ `adjust_path_instance` 曾被记成「不是不想修是修不了：`SpdPathInstance`
没有机构列」。没有机构列是真的，「所以校验不了」是错的。
**一张表没有机构列，不等于这个对象没有归属。**

## 口径来源

三个文件里 `assert_org_writable` 分别已用了 2 / 15 / 5 处——又是"同一个文件、
两套口径"，第五次。本轮只是把既有口径补到漏掉的端点上，没换机制。
"""
import pytest

from app import models as M
from app.database import SessionLocal


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client):
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "一跳甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "一跳乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("hop_doc_a", a, "doctor"), ("hop_doc_b", b, "doctor"),
                             ("hop_dir_b", b, "director")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    patient = client.post("/api/patients",
                          json={"name": "一跳患者", "id_card": "330782199505055566"},
                          headers=admin).json()
    return {"admin": admin, "a": a, "b": b, "patient": patient,
            "doc_a": _login(client, "hop_doc_a"), "doc_b": _login(client, "hop_doc_b"),
            "dir_b": _login(client, "hop_dir_b")}


#: `spd_enrollments` 上有 `(patient_id, program_code)` 唯一索引，所以每次造档案
#: 都换一个病种码——同一位患者同一病种只能在管一次，那是业务不变式，不该为了
#: 造夹具去绕开它。
_PROGRAM_SEQ = iter(f"ctp_probe_{i}" for i in range(1, 999))


def _seed(world, *, with_enrollment=True):
    """直接落库造出"甲院名下"的三类对象。

    走 ORM 而不是走接口：这几类对象的建立各有一串前置（方案、路径模板、服务包目录），
    而本文件要证的是**改**它们时判不判归属，不是建它们的流程。
    """
    db = SessionLocal()
    try:
        enrollment = None
        if with_enrollment:
            enrollment = M.SpdEnrollment(patient_id=world["patient"]["id"],
                                         program_code=next(_PROGRAM_SEQ),
                                         org_id=world["a"]["id"])
            db.add(enrollment)
            db.flush()
        eid = enrollment.id if enrollment else None
        intervention = M.SpdIntervention(patient_id=world["patient"]["id"], enrollment_id=eid)
        db.add(intervention)
        objs = {"intervention": intervention}
        if with_enrollment:
            binding = M.SpdPackageBinding(
                enrollment_id=eid, package_id=1, status="bound",
                items=[{"code": "bp", "name": "血压", "total": 4, "used": 0, "price": 5}],
            )
            instance = M.SpdPathInstance(enrollment_id=eid, template_id=1, status="running")
            db.add_all([binding, instance])
            objs.update(binding=binding, instance=instance)
        db.commit()
        return {k: v.id for k, v in objs.items()}
    finally:
        db.close()


# ------------------------------------------------ 一个方向：无关机构一律 403


def test_别家机构办不了干预任务(client, world):
    ids = _seed(world)
    resp = client.patch(f"/api/spd/interventions/{ids['intervention']}",
                        json={"status": "done", "feedback": "乙院填的"},
                        headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构扣不了服务包次数(client, world):
    ids = _seed(world)
    resp = client.post(f"/api/spd/package-bindings/{ids['binding']}/usages",
                       json={"item_code": "bp", "qty": 1}, headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构解不了服务包(client, world):
    ids = _seed(world)
    resp = client.post(f"/api/spd/package-bindings/{ids['binding']}/unbind",
                       headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构调不了路径实例(client, world):
    ids = _seed(world)
    resp = client.patch(f"/api/spd/path-instances/{ids['instance']}",
                        json={"status": "cancelled"}, headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


# ------------------------------------------------ 另一个方向：不能变成"全关了"


def test_本机构照常(client, world):
    """没有这一条，"修好了"可能只是"谁都动不了了"。"""
    ids = _seed(world)
    assert client.patch(f"/api/spd/interventions/{ids['intervention']}",
                        json={"status": "done"}, headers=world["doc_a"]).status_code == 200
    assert client.post(f"/api/spd/package-bindings/{ids['binding']}/usages",
                       json={"item_code": "bp", "qty": 1},
                       headers=world["doc_a"]).status_code == 201
    assert client.patch(f"/api/spd/path-instances/{ids['instance']}",
                        json={"note": "本院调整"}, headers=world["doc_a"]).status_code == 200
    assert client.post(f"/api/spd/package-bindings/{ids['binding']}/unbind",
                       headers=world["doc_a"]).status_code == 200


def test_全域角色跨机构照常(client, world):
    """`director` 属 GLOBAL_ROLES：县级中心统筹在管档案是设计内的。

    隔离做过头会挡掉真实业务，那比不做隔离更糟——它会让人把整套隔离关掉。
    """
    ids = _seed(world)
    assert client.patch(f"/api/spd/path-instances/{ids['instance']}",
                        json={"note": "县级统筹"}, headers=world["dir_b"]).status_code == 200


# ------------------------------------------------ 顺序与边界


def test_对别家先403不泄露状态(client, world):
    """归属判定排在业务状态机之前。

    否则 409「该服务包已解绑」/「已完成的路径不可调整」这类措辞，
    会把别家档案的当前状态说出来——拒绝本身不该是一条信息通道。
    """
    ids = _seed(world)
    assert client.post(f"/api/spd/package-bindings/{ids['binding']}/unbind",
                       headers=world["doc_a"]).status_code == 200
    again = client.post(f"/api/spd/package-bindings/{ids['binding']}/usages",
                        json={"item_code": "bp", "qty": 1}, headers=world["doc_b"])
    assert again.status_code == 403, "本院对已解绑的才该是 409，别家机构应当先被 403 挡下"


def test_没有档案归属的干预不拦(client, world):
    """`spd_interventions.enrollment_id` 可空 → 拿不到归属。

    沿用 `assert_org_writable` 的既定语义（"org_id 为 None 的记录不在此列，
    由各接口自己定语义"）：**不拦**，并把这条语义钉在这里。
    改成拦之前请先想清楚谁来建这类无归属记录——现状是它们确实存在。
    """
    ids = _seed(world, with_enrollment=False)
    resp = client.patch(f"/api/spd/interventions/{ids['intervention']}",
                        json={"status": "done"}, headers=world["doc_b"])
    assert resp.status_code == 200, resp.text
