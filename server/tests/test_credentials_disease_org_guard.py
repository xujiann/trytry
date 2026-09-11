"""凭据处置与专病路径推进必须校验机构（2026-09-11 实测取证后补）。

## 实测取证（乙院对甲院名下对象）

    POST /api/credentials/{id}/recycle                  → 200  回收了甲院发的卡
    POST /api/credentials/{id}/void                     → 200  作废了甲院发的卡
    POST /api/disease-programs/enrollments/{id}/records → 201  给甲院的病例记路径节点
    POST /api/disease-programs/enrollments/{id}/exit    → 200  替甲院的病例出组并下疗效结论

## 互认是"认别家的卡"，不是"处置别家的卡"

`credentials.py` 的读侧（`/lookup/{no}`、`/resolve`）按设计跨机构——那是互认。
而回收与作废是**发卡机构对自己那张卡**的生命周期管理：作废之后该凭据立即不可用于
核验，被别家机构作废掉，等于让别人吊销了你发出去的证件。
`issue_credential` 一直是按 `user.org_id` 发卡的，处置这一半却谁都能做。

## 专病：第七次"同一个文件、两套口径"

`enroll` 一直 `assert_org_writable(db, user, body.org_id)`，而记节点与出组不校验。
出组要写疗效评价（治愈 / 好转 / 死亡），那是本院随访的结论，不该由别家机构下。

## `update_program` 为什么是豁免而不是补守卫

它挂 `require_admin`（`user.role != "admin"` 即 403），而 `admin` 属
`GLOBAL_ROLES`——`assert_org_writable` 对全域角色**恒放行**。
也就是说给它加机构守卫是一句**永远不会触发**的代码：闸门变绿，行为一点没变。

**一句永远不会触发的守卫比没有守卫更糟**——它让闸门不再问这个端点，
而问题（如果有）原封不动。所以登记成带理由的豁免，并用下面那条用例
钉住豁免的**前提**：哪天角色放宽到非全域角色，这条用例就红，豁免要重新判。
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
                    json={"name": "凭据甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "凭据乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    users = (("cd_op_a", a, "operator"), ("cd_doc_a", a, "doctor"), ("cd_ph_a", a, "public_health"),
             ("cd_op_b", b, "operator"), ("cd_doc_b", b, "doctor"), ("cd_ph_b", b, "public_health"))
    for uname, org, role in users:
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    patients = [
        client.post("/api/patients",
                    json={"name": f"凭据患者{n}", "id_card": f"33078219950505{n:04d}"},
                    headers=admin).json()
        for n in (1, 2, 3, 4, 5, 6)
    ]
    return {"admin": admin, "a": a, "b": b, "patients": patients,
            **{u.split("cd_")[1]: _login(client, u) for u, _, _ in users}}


_SEQ = iter(range(1, 999))


def _seed(world):
    """造一张甲院发的凭据 + 一条甲院名下的在管病例。

    `disease_enrollments` 上 `(program_id, patient_id)` 唯一，所以每次换一位患者；
    凭据号也逐次递增——两条都是业务不变式，不绕开。
    """
    db = SessionLocal()
    try:
        n = next(_SEQ)
        org_id = world["a"]["id"]
        patient = world["patients"][n % len(world["patients"])]
        program = M.DiseaseProgram(code=f"dp_guard_{n}", name=f"甲院专病{n}", org_id=org_id,
                                   path_nodes=[{"key": "n1", "name": "首诊"}], active=True)
        credential = M.VisitCredential(patient_id=patient["id"], credential_no=f"CRED-G-{n}",
                                       org_id=org_id, credential_type="card", status="active")
        db.add_all([program, credential])
        db.flush()
        enrollment = M.DiseaseEnrollment(program_id=program.id, patient_id=patient["id"],
                                         org_id=org_id, status="enrolled")
        db.add(enrollment)
        db.commit()
        return {"credential": credential.id, "enrollment": enrollment.id,
                "program": program.id}
    finally:
        db.close()


# ------------------------------------------------ 一个方向：无关机构一律 403


def test_别家机构回收不了凭据(client, world):
    ids = _seed(world)
    resp = client.post(f"/api/credentials/{ids['credential']}/recycle",
                       json={"reason": "乙院回收"}, headers=world["op_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构作废不了凭据(client, world):
    """作废后该凭据立即不可用于核验——等于让别人吊销了你发出去的证件。"""
    ids = _seed(world)
    resp = client.post(f"/api/credentials/{ids['credential']}/void",
                       json={"reason": "乙院说挂失"}, headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构记不了路径节点(client, world):
    ids = _seed(world)
    resp = client.post(f"/api/disease-programs/enrollments/{ids['enrollment']}/records",
                       json={"node_key": "n1", "note": "乙院记的"}, headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构不能替病例出组(client, world):
    """出组要写疗效评价（治愈/好转/死亡），那是本院随访的结论。"""
    ids = _seed(world)
    resp = client.post(f"/api/disease-programs/enrollments/{ids['enrollment']}/exit",
                       json={"status": "completed", "outcome": "cured"}, headers=world["ph_b"])
    assert resp.status_code == 403, resp.text


# ------------------------------------------------ 另一个方向：不能变成"全关了"


def test_本机构照常(client, world):
    ids = _seed(world)
    assert client.post(f"/api/disease-programs/enrollments/{ids['enrollment']}/records",
                       json={"node_key": "n1"}, headers=world["doc_a"]).status_code == 201
    assert client.post(f"/api/disease-programs/enrollments/{ids['enrollment']}/exit",
                       json={"status": "completed", "outcome": "cured"},
                       headers=world["ph_a"]).status_code == 200
    assert client.post(f"/api/credentials/{ids['credential']}/recycle",
                       json={"reason": "患者交回"}, headers=world["op_a"]).status_code == 200


def test_互认读侧不受影响(client, world):
    """读侧（按卡号查）按设计跨机构——这正是互认。**本轮没有动它。**

    没有这一条，"把处置收口"很容易被下一个人误读成"把互认也关了"。
    """
    ids = _seed(world)
    db = SessionLocal()
    try:
        no = db.get(M.VisitCredential, ids["credential"]).credential_no
    finally:
        db.close()
    assert client.get(f"/api/credentials/lookup/{no}", headers=world["op_b"]).status_code == 200


# ------------------------------------------------ 豁免的前提，钉住它


def test_目录配置端点只有全域角色够得着(client, world):
    """`update_program` 判为豁免的**前提**：它挂 `require_admin`，而 `admin` 属
    `GLOBAL_ROLES`，机构守卫对全域角色恒放行——加上去是一句永远不触发的代码。

    这条用例钉的是那个前提本身：哪天角色放宽到非全域角色，它就红，
    豁免要重新判。豁免的理由会过期，理由的**前提**得有人守着。
    """
    from app.visibility import GLOBAL_ROLES

    assert "admin" in GLOBAL_ROLES
    ids = _seed(world)
    for role_headers in (world["doc_b"], world["op_b"], world["ph_b"], world["doc_a"]):
        assert client.patch(f"/api/disease-programs/{ids['program']}",
                            json={"name": "改名"}, headers=role_headers).status_code == 403
    assert client.patch(f"/api/disease-programs/{ids['program']}",
                        json={"name": "管理员改名"}, headers=world["admin"]).status_code == 200
