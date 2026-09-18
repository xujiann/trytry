"""P1-60 最后一批：医废收集 与 慢专病路径启动（2026-09-18 实测取证后补）。

## 实测取证（修之前）

    POST /api/medwaste           乙院 operator 替甲院记一条医废收集 → 201
                                 追溯码 MW-20260401-0001 落在**甲院**头上
    POST /api/spd/path-instances 乙院 doctor 给甲院的纳管档案启动路径 → 201
                                 连首节点任务一起生成、派给甲院的执行人

医废收集是**受监管的转移联单起点**（追溯码按机构编号），别家替你记一条，
等于往你的危废台账里塞了一笔来路不明的记录。

路径启动这条尤其典型：同文件的其余任务流转端点（claim / urge / escalate /
submit / review / complete，P0-12 那一批修的）**早就有归属校验**，
唯独"启动"没有——又一次"同一个文件两套口径"。

## ⚠️ 更正上一批的一处说法：它**有**角色门

2026-09-18 上一批的提交信息与 TECH_DEBT 里写过"`start_path_instance` 连角色门都没有、
任何已登录账号都能调"——**这是错的**。它是 `require_roles(*SERVICE_ROLES)`，
即 doctor / public_health / director。

错因是当时那份角色门扫描用 `re.findall(r"'([a-z_]+)'", ...)` 取角色名，
**遇到星号展开的常量会得到空集合**，于是被判成"没有角色门"。
`tests/test_clinical_write_org_guard.py` 里那条判定用例本来也有同一个毛病
（空集合 `<= {"admin","director"}` 恒真 → **为错误的理由通过**），
已同批改成解析模块级常量、解析不出来就留 `<未解析:…>` 让断言红掉。

结论不变：doctor / public_health 都是非全域角色，它仍然是真候选，只是"够得着的人"
比原先说的窄。**说错了就改，而不是让它留在文档里。**
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(client, username, password="pw123456"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    a = client.post("/api/organizations",
                    json={"name": "末批甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "末批乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("last_op_a", a, "operator"), ("last_op_b", b, "operator"),
                             ("last_doc_a", a, "doctor"), ("last_doc_b", b, "doctor")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    return {"admin": admin, "a": a, "b": b,
            "op_a": _login(client, "last_op_a"), "op_b": _login(client, "last_op_b"),
            "doc_a": _login(client, "last_doc_a"), "doc_b": _login(client, "last_doc_b")}


DENIED = {"detail": "无权以该机构名义写入数据"}
_seq = [0]


def _source_location(client, world):
    _seq[0] += 1
    resp = client.post("/api/medwaste/locations", headers=world["admin"],
                       json={"org_id": world["a"]["id"], "name": f"甲院产生点{_seq[0]}",
                             "location_type": "source"})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------- 医废


def test_别家operator不能替本院记医废收集(client, world):
    loc = _source_location(client, world)
    resp = client.post("/api/medwaste", headers=world["op_b"],
                       json={"org_id": world["a"]["id"], "source_location_id": loc["id"],
                             "waste_type": "infectious", "weight_kg": 3.5,
                             "collected_date": "2026-04-01"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


def test_本院operator照常记医废收集(client, world):
    loc = _source_location(client, world)
    resp = client.post("/api/medwaste", headers=world["op_a"],
                       json={"org_id": world["a"]["id"], "source_location_id": loc["id"],
                             "waste_type": "infectious", "weight_kg": 3.5,
                             "collected_date": "2026-04-02"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["org_id"] == world["a"]["id"]


def test_自报成自己家也不能借别家的点位(client, world):
    """守卫用的是 body 自报的 `org_id`，而"点位属于该机构"是另一道既有校验。

    乙院把 `org_id` 填成自己（过归属守卫），却引用甲院的点位——应当被那道
    既有校验以 422 拦下。两道各管各的，缺一不可。
    """
    loc = _source_location(client, world)
    resp = client.post("/api/medwaste", headers=world["op_b"],
                       json={"org_id": world["b"]["id"], "source_location_id": loc["id"],
                             "waste_type": "infectious", "weight_kg": 1.0,
                             "collected_date": "2026-04-03"})
    assert resp.status_code == 422, resp.text
    assert "点位不属于该机构" in resp.json()["detail"]


# ---------------------------------------------------------------- 慢专病路径


@pytest.fixture(scope="module")
def path_fixture(client, world):
    prog = client.post("/api/spd/programs", headers=world["admin"],
                       json={"code": "lastbatch", "name": "末批取证专病",
                             "category": "chronic"})
    assert prog.status_code == 201, prog.text
    tpl = client.post("/api/spd/path-templates", headers=world["admin"],
                      json={"program_id": prog.json()["id"], "code": "LB1",
                            "name": "末批取证路径"})
    assert tpl.status_code == 201, tpl.text
    client.post(f"/api/spd/path-templates/{tpl.json()['id']}/nodes", headers=world["admin"],
                json={"key": "n1", "name": "首节点", "seq": 1})
    client.post(f"/api/spd/path-templates/{tpl.json()['id']}/status", headers=world["admin"],
                json={"status": "published"})
    return {"template_id": tpl.json()["id"]}


def _enrollment(client, world, suffix):
    patient = client.post("/api/patients", headers=world["admin"],
                          json={"name": f"末批患者{suffix}",
                                "id_card": f"3300001990010166{suffix:02d}"}).json()
    resp = client.post("/api/spd/enrollments", headers=world["admin"],
                       json={"patient_id": patient["id"], "program_code": "lastbatch",
                             "org_id": world["a"]["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_别家doctor不能给本院纳管档案启动路径(client, world, path_fixture):
    enr = _enrollment(client, world, 1)
    resp = client.post("/api/spd/path-instances", headers=world["doc_b"],
                       json={"enrollment_id": enr["id"],
                             "template_id": path_fixture["template_id"]})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


def test_本院doctor照常启动路径(client, world, path_fixture):
    enr = _enrollment(client, world, 2)
    resp = client.post("/api/spd/path-instances", headers=world["doc_a"],
                       json={"enrollment_id": enr["id"],
                             "template_id": path_fixture["template_id"]})
    assert resp.status_code == 201, resp.text


def test_启动路径确实有角色门_不是任何已登录账号都能调(client, world, path_fixture):
    """钉住上面那处更正：`require_roles(*SERVICE_ROLES)` 真的在。

    用一个**不在** SERVICE_ROLES（doctor/public_health/director）里的角色试，
    应当被角色门挡下而不是被归属守卫挡下——两者的报错文案不同，据此可区分。
    """
    client.post("/api/users", headers=world["admin"],
                json={"username": "last_op_role", "password": "pw123456",
                      "full_name": "甲院经办", "role": "operator",
                      "org_id": world["a"]["id"]})
    op_a_same_org = _login(client, "last_op_role")
    enr = _enrollment(client, world, 3)
    resp = client.post("/api/spd/path-instances", headers=op_a_same_org,
                       json={"enrollment_id": enr["id"],
                             "template_id": path_fixture["template_id"]})
    assert resp.status_code == 403, resp.text
    # 同机构，所以拦下它的**只能**是角色门
    assert "角色" in resp.json()["detail"], resp.json()
