"""服务包项目编码不许重复（P2-631，第十三批「拆分之和 vs 总额」扫描 Q3-8）。

扣减按编码找项目，建 / 改服务包原先照收同一编码两条（BP 2 次、BP 3 次）：绑定后剩余次数按各条相加显示 5，扣减却只认
第一条——扣满 2 次之后「该项目剩余次数不足」，另外 3 次永远用不上，居民端还一直显示剩 3 次。

修法：建 / 改服务包拒收重复编码（说清楚是哪个编码、怎么改）；存量里已经重复的，扣减扣在第一条还够扣的那条上。
"""
import pytest

B = "/api/spd"
ITEMS = [{"code": "BP", "name": "测血压", "times": 2}, {"code": "GLU", "name": "测血糖", "times": 1},
         {"code": "BP", "name": "测血压", "times": 3}]
REFUSED = "服务包项目编码重复：BP（同一项目请合成一条、次数相加）"


def test_建服务包拒收重复编码(client, admin):
    resp = client.post(f"{B}/service-packages", headers=admin,
                       json={"code": "P2631_NEW", "name": "P2631 新包", "items": ITEMS})
    assert (resp.status_code, resp.json()) == (422, {"detail": REFUSED}), resp.text   # 修前 201


def test_改服务包同样拒收(client, admin):
    created = client.post(f"{B}/service-packages", headers=admin,
                          json={"code": "P2631_EDIT", "name": "P2631 改包", "items": ITEMS[:2]})
    assert created.status_code == 201, created.text
    resp = client.patch(f"{B}/service-packages/{created.json()['id']}", headers=admin, json={"items": ITEMS})
    assert (resp.status_code, resp.json()) == (422, {"detail": REFUSED}), resp.text   # 修前 200


@pytest.fixture(scope="module")
def legacy_binding(client, admin):
    """修前建出的重复编码服务包，绑在一份在管档案上。"""
    from app.database import SessionLocal
    from app.spd.models import SpdServicePackage

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2631 服务包院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2631 患者", "id_card": "330106197001012631", "gender": "男", "birth_date": "1970-01-01"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin,
                             json={"patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    with SessionLocal() as db:
        legacy = SpdServicePackage(code="P2631_LEGACY", name="P2631 存量重复包", active=True, items=ITEMS)
        db.add(legacy)
        db.commit()
        package_id = legacy.id
    bound = client.post(f"{B}/enrollments/{enrollment.json()['id']}/packages", headers=admin,
                        json={"package_id": package_id})
    assert bound.status_code == 201 and bound.json()["remaining"] == 6, bound.text
    return bound.json()["id"]


def test_存量重复编码_扣满第一条之后接着扣第二条(client, admin, legacy_binding):
    url = f"{B}/package-bindings/{legacy_binding}/usages"
    for _ in range(2):
        assert client.post(url, headers=admin, json={"item_code": "BP"}).status_code == 201
    third = client.post(url, headers=admin, json={"item_code": "BP", "qty": 3})
    assert third.status_code == 201, third.text   # 修前 409「该项目剩余次数不足」，显示的剩余却是 3
    binding = third.json()["binding"]
    assert binding["remaining"] == 1 and [i["used"] for i in binding["items"] if i["code"] == "BP"] == [2, 3]
    over = client.post(url, headers=admin, json={"item_code": "BP"})
    assert (over.status_code, over.json()["detail"]) == (409, "该项目剩余次数不足")
