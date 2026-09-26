"""机构分组改档：显式传 null 清不掉可空的牵头机构（P2-351）。

`patchtypes` 的约定：改档分「不传（不改）/ 传值（改成它）/ 传 null（清空）」三种，可空的列三种都合法。
`PATCH /api/org-groups/{id}` 却对所有字段一律跳过 null——牵头机构（可空）设上之后，接口上再也清不掉。
不可空的名称 / 备注 / 启用传 null 照旧不改（不写进 NOT NULL 列）。
"""


def test_牵头机构传null即清空_其余字段传null不改(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2351 牵头县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    group = client.post("/api/org-groups", headers=admin, json={"name": "P2351 片区", "lead_org_id": org})
    assert group.status_code == 201, group.text
    gid = group.json()["id"]
    resp = client.patch(f"/api/org-groups/{gid}", headers=admin,
                        json={"lead_org_id": None, "name": None, "note": None, "active": None})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lead_org_id"] is None          # 修前仍是 org
    assert (body["name"], body["active"]) == ("P2351 片区", True)


def test_不传牵头机构不改(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2351 牵头县医院二", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    gid = client.post("/api/org-groups", headers=admin, json={"name": "P2351 片区二", "lead_org_id": org}).json()["id"]
    resp = client.patch(f"/api/org-groups/{gid}", headers=admin, json={"note": "改备注"})
    assert resp.status_code == 200 and resp.json()["lead_org_id"] == org, resp.text
