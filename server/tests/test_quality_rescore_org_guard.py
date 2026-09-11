"""病历复评必须校验机构（2026-09-11 实测取证后补）。

## 实测取证

    GET /api/quality/records/{id}/qc   乙院 doctor   → 200
    GET /api/quality/records/{id}/qc   乙院 operator → 200

甲院那条病历的 `qc_score` 从 **100 改成 43**、`qc_grade` 从**甲改成丙**——
质控等级是考核依据。端点原先连 `user` 形参都没有。

## 第八次"同一个文件、两套口径"，而且这次注释早就写明了风险

同文件的 `upsert_medical_record` 走的是**同一条 `_apply_qc` 路径**，它的注释白纸黑字写着：

    「这条路径会同步跑 _apply_qc 算缺陷扣分，写进去的内容直接进乙院的质控成绩」

它因此加了 `assert_org_writable`。而复评这个入口一直什么都不校验。
又一次：**风险被写下来了，只写在了其中一条路径上。**

## 闸门为什么直到今天才看见

它是个**会写库的 GET**（`db.commit()` 回写快照），而写侧几道静态闸门只扫
post/put/patch/delete，所以一直落在写侧判据之外。读侧闸门此前只跟进一层本模块
helper，而这里取到患者数据在第三跳：

    rescore_medical_record → _apply_qc → evaluate_record → _record_context
                                                            └ db.get(Encounter, …)

把跟进改成**传递闭包**（P2-36）之后才现形。"会写库的 GET"本身登记为 P2-37，
改成 POST 属破坏性变更，未做。
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
                    json={"name": "复评甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "复评乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("qc_doc_a", a, "doctor"), ("qc_doc_b", b, "doctor"),
                             ("qc_op_b", b, "operator"), ("qc_dir_b", b, "director")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    patient = client.post("/api/patients",
                          json={"name": "复评患者", "id_card": "330782199505055588"},
                          headers=admin).json()
    return {"admin": admin, "a": a, "b": b, "patient": patient,
            "doc_a": _login(client, "qc_doc_a"), "doc_b": _login(client, "qc_doc_b"),
            "op_b": _login(client, "qc_op_b"), "dir_b": _login(client, "qc_dir_b")}


def _seed(world):
    """造一条甲院名下、评分快照为满分的病历。"""
    db = SessionLocal()
    try:
        encounter = M.Encounter(patient_id=world["patient"]["id"], org_id=world["a"]["id"])
        db.add(encounter)
        db.flush()
        record = M.MedicalRecord(encounter_id=encounter.id, org_id=world["a"]["id"],
                                 created_by=1, chief_complaint="甲院主诉",
                                 qc_score=100, qc_grade="甲")
        db.add(record)
        db.commit()
        return record.id
    finally:
        db.close()


def _snapshot(record_id):
    db = SessionLocal()
    try:
        record = db.get(M.MedicalRecord, record_id)
        return record.qc_score, record.qc_grade
    finally:
        db.close()


def test_别家机构复评不了病历(client, world):
    record_id = _seed(world)
    resp = client.get(f"/api/quality/records/{record_id}/qc", headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构的经办也复评不了(client, world):
    """doctor 与 operator 两个角色都实测过 200，所以两个都钉。"""
    record_id = _seed(world)
    assert client.get(f"/api/quality/records/{record_id}/qc",
                      headers=world["op_b"]).status_code == 403


def test_被拒之后快照没有被改写(client, world):
    """403 不只是拦住响应——回写也必须没发生。

    这条端点是**会写库的 GET**：判定若排在 `_apply_qc` 之后，接口会返回 403，
    而分数已经被改掉了。所以单独验一次库里的值。
    """
    record_id = _seed(world)
    before = _snapshot(record_id)
    assert before == (100, "甲")
    assert client.get(f"/api/quality/records/{record_id}/qc",
                      headers=world["doc_b"]).status_code == 403
    assert _snapshot(record_id) == before, "被拒了，但快照被改写了"


def test_本机构与全域角色照常(client, world):
    """没有这一条，"修好了"可能只是"谁都复评不了了"。"""
    record_id = _seed(world)
    assert client.get(f"/api/quality/records/{record_id}/qc",
                      headers=world["doc_a"]).status_code == 200
    assert client.get(f"/api/quality/records/{record_id}/qc",
                      headers=world["dir_b"]).status_code == 200
    assert client.get(f"/api/quality/records/{record_id}/qc",
                      headers=world["admin"]).status_code == 200
    # 复评确实写回了快照（否则上面的 200 可能只是没做事）
    assert _snapshot(record_id) != (100, "甲")
