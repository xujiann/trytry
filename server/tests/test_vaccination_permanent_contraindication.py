"""长期禁忌带着日期就按日期失效：给过敏史这类长期禁忌顺手填了「有效期末日」，到那天禁忌就不拦了（P2-236）。

禁忌分长期（过敏史等，须人工解除）与暂时（发热等，有有效期），列注释写着 `valid_until` 是「暂时禁忌的有效期末日」。
登记接口只要求暂时禁忌必须给日期，长期禁忌给了日期照收；判定函数不分类型按日期算。页面上「有效期末日（暂时禁忌必填）」
一栏两种禁忌共用，登记严重过敏时顺手填了个复查日期，过了那天这条长期禁忌就 `blocking=false`，照样能接种。

修法：长期禁忌给日期 422；判定只让暂时禁忌按日期失效——存量里带日期的长期禁忌恢复拦截（要放行须人工解除，留下解除
理由），这是这类禁忌本来的规矩。
"""
import pytest

V = "/api/vaccination"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2236 接种门诊", "org_type": "township", "level": "township"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2236 受种者{i}", "id_card": f"33012720200101{2236 + i:04d}"}).json()["id"] for i in range(2)]
    return {"org": org, "patients": patients}


def test_长期禁忌带有效期_422(client, admin, world):
    resp = client.post(f"{V}/contraindications", headers=admin, json={
        "patient_id": world["patients"][0], "vaccine_code": "P2236-V", "reason": "对明胶严重过敏",
        "contra_type": "permanent", "valid_until": "2026-10-01"})
    assert resp.status_code == 422, resp.text   # 修前 201，到 10-02 就不拦了
    assert resp.json() == {"detail": "长期禁忌没有有效期、须人工解除：有效期末日只给暂时禁忌填"}


def test_存量带日期的长期禁忌照旧拦截_暂时禁忌过期照旧放行(client, admin, world):
    from app.database import SessionLocal
    from app.models import VaccineContraindication

    patient = world["patients"][1]
    with SessionLocal() as db:   # 修之前录进去的：长期禁忌带着一个早已过去的日期
        db.add_all([
            VaccineContraindication(patient_id=patient, vaccine_code="P2236-V", reason="对明胶严重过敏",
                                    contra_type="permanent", valid_until="2020-01-01"),
            VaccineContraindication(patient_id=patient, vaccine_code="P2236-T", reason="发热",
                                    contra_type="temporary", valid_until="2020-01-01"),
        ])
        db.commit()
    rows = {r["vaccine_code"]: r for r in client.get(f"{V}/contraindications?patient_id={patient}", headers=admin).json()}
    assert (rows["P2236-V"]["expired"], rows["P2236-V"]["blocking"]) == (False, True)   # 修前 (True, False)
    assert (rows["P2236-T"]["expired"], rows["P2236-T"]["blocking"]) == (True, False)
    body = {"patient_id": patient, "vaccine_name": "P2236 疫苗", "org_id": world["org"]}
    blocked = client.post(f"{V}/records", headers=admin, json={**body, "vaccine_code": "P2236-V"})
    assert blocked.status_code == 409 and "对明胶严重过敏" in blocked.json()["detail"], blocked.text   # 修前 201
    assert client.post(f"{V}/records", headers=admin, json={**body, "vaccine_code": "P2236-T"}).status_code == 201
