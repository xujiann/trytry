"""节点准入检查不核对节点是否属于这条实例的模板：拿别的路径的节点照样回「满足进入条件」（P2-1598，第四十七批扫描 AK3-5）。

`GET /api/spd/path-nodes/{node_id}/enter-check?instance_id=` 修前只查两样都存在、实例看得见，就拿节点的进入条件对实例
求值——路径甲的实例配路径乙的节点，回 `200 {"allowed": true, "conditions": [乙的条件]}`。这条路径永远走不到那个节点，
回答却是「满足」。同文件的 `_resume_paused` 与 `service.advance_path` 都只在 `instance.template_id` 的节点里取。

修法：可见性判定之后加一道 `node.template_id != instance.template_id` → 404「该节点不在这条路径上」；顺序不变——
看不见的实例照旧 403，别给它当节点归属的神谕。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    own = client.post("/api/organizations", headers=admin, json={
        "name": "P21598 纳管卫生院", "org_type": "township", "level": "township"}).json()["id"]
    other = client.post("/api/organizations", headers=admin, json={
        "name": "P21598 无关卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, org in (("p21598_doc_own", own), ("p21598_doc_other", other)):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": "doctor", "org_id": org})
        assert created.status_code == 201, created.text
    condition = [{"field": "risk_level", "op": "==", "value": "high"}]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        path_a = SpdPathTemplate(program_id=program.id, code="P21598_A", name="P21598 路径甲", status="published")
        path_b = SpdPathTemplate(program_id=program.id, code="P21598_B", name="P21598 路径乙", status="published")
        db.add_all([path_a, path_b])
        db.flush()
        own_node = SpdPathNode(template_id=path_a.id, key="a2", name="甲·强化管理", seq=2, enter_condition=condition)
        foreign_node = SpdPathNode(template_id=path_b.id, key="b1", name="乙·首诊", seq=1, enter_condition=condition)
        db.add_all([SpdPathNode(template_id=path_a.id, key="a1", name="甲·首诊", seq=1), own_node, foreign_node])
        db.commit()
        template_id, own_node_id, foreign_node_id = path_a.id, own_node.id, foreign_node.id
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21598 患者", "id_card": "330127197309091598"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": own})
    assert enrollment.status_code == 201, enrollment.text
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment.json()["id"], "template_id": template_id})
    assert started.status_code == 201, started.text
    return {"instance": started.json()["id"], "enrollment": enrollment.json()["id"],
            "own_node": own_node_id, "foreign_node": foreign_node_id,
            "doc_own": _login(client, "p21598_doc_own"), "doc_other": _login(client, "p21598_doc_other")}


def _check(client, world, node_key, who):
    return client.get(f"{B}/path-nodes/{world[node_key]}/enter-check",
                      params={"instance_id": world["instance"]}, headers=world[who])


def test_别的路径的节点_404(client, admin, world):
    # 让条件对这位患者成立：修前跨模板也回 allowed=true，最能看出「答非所问」
    assert client.patch(f"{B}/enrollments/{world['enrollment']}", headers=admin,
                        json={"risk_level": "high"}).status_code == 200
    got = _check(client, world, "foreign_node", "doc_own")
    assert got.status_code == 404, got.text   # 修前 200 {"allowed": true, "conditions": [乙的条件]}
    assert got.json()["detail"] == "该节点不在这条路径上"


def test_本路径的节点照常判(client, admin, world):
    assert client.patch(f"{B}/enrollments/{world['enrollment']}", headers=admin,
                        json={"risk_level": "low"}).status_code == 200
    got = _check(client, world, "own_node", "doc_own")
    assert got.status_code == 200, got.text
    assert got.json()["allowed"] is False and got.json()["conditions"][0]["value"] == "high"


def test_看不见的实例照旧403_先于节点归属(client, world):
    # 别家机构的人拿跨模板的节点来问：仍是 403，不因节点不在这条路径上先回 404 而泄露实例的模板
    assert _check(client, world, "foreign_node", "doc_other").status_code == 403
    assert _check(client, world, "own_node", "doc_other").status_code == 403
