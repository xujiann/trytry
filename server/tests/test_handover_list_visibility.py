"""交接班清单不收调用方：任一登录账号翻得到全县各病区的交接班（P0-48）。

`GET /api/inpatient/handovers` 原先连 `user` 形参都没有，按 `ward_id` 或不带参数都照给；交班正文是自由文本，常写着
「几床谁病危、注意什么」。同文件其余读接口（病程、护理记录、体征、文书完整度）都按住院患者的可见性判并留痕，新建交接班
按病区所属机构判（`assert_obj_org_writable`）——只有这一条清单什么都不看。修前实测：与该院毫无关系的卫生院医生
带 `ward_id` 或不带参数都 200，读到别家病区的交班正文。

修法：只列本机构病区的（全域角色不限），与新建同一口径；点名别家的病区 403，不悄悄返回空（`scope_org_list` 同一句）。
"""
import pytest

B = "/api/inpatient"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    a = client.post("/api/organizations", headers=admin, json={
        "name": "P0048 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    b = client.post("/api/organizations", headers=admin, json={
        "name": "P0048 无关卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, org in (("p0048_doc_a", a), ("p0048_doc_b", b)):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": "doctor", "org_id": org})
        assert created.status_code == 201, created.text
    ward = client.post(f"{B}/wards", headers=admin, json={"org_id": a, "name": "P0048 外科病区"}).json()["id"]
    created = client.post(f"{B}/handovers", headers=admin, json={
        "ward_id": ward, "shift": "night", "handover_date": "2026-09-26", "from_staff": "白班", "to_staff": "夜班",
        "critical_count": 0, "content": "P0048 3床术后第一天病危，注意引流"})
    assert created.status_code == 201, created.text
    return {"ward": ward, "doc_a": _login(client, "p0048_doc_a"), "doc_b": _login(client, "p0048_doc_b")}


def test_无关机构点名别家病区_403(client, world):
    got = client.get(f"{B}/handovers", params={"ward_id": world["ward"]}, headers=world["doc_b"])
    assert got.status_code == 403, got.text   # 修前 200，交班正文原样给出
    assert "病危" not in got.text


def test_无关机构不带参数_看不到别家的交接班(client, world):
    got = client.get(f"{B}/handovers", headers=world["doc_b"])
    assert got.status_code == 200, got.text
    assert all("P0048" not in row["content"] for row in got.json())   # 修前全县的交接班都在


def test_本机构照常看(client, world):
    got = client.get(f"{B}/handovers", params={"ward_id": world["ward"]}, headers=world["doc_a"])
    assert got.status_code == 200, got.text
    assert [row["content"] for row in got.json()] == ["P0048 3床术后第一天病危，注意引流"]
    unscoped = client.get(f"{B}/handovers", headers=world["doc_a"])
    assert any(row["content"].startswith("P0048") for row in unscoped.json())
