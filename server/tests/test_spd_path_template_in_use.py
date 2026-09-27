"""有患者走过的路径模板页面上只读（P2-599，第十二批「按钮 vs 状态机」扫描 Z2-9）。

有任何实例引用的路径，节点增改删一律 409（P2-97 / P2-569，不论模板此刻什么状态），模板也只能停用、不能删；页面却只看
「已发布」：停用或改回草稿的旧路径照样给「加节点」「编辑」「删除节点」，每一行都给「删除模板」，已发布的也给「加节点」
——点下去才 409。清单与详情带上 `in_use`，页面据此只读。
"""
from pathlib import Path

import pytest

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    hyp = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2599 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    tpl = {}
    for tag in ("在用", "未用"):
        created = client.post(f"{B}/path-templates", headers=admin, json={
            "program_id": hyp["id"], "code": f"P2599_{'USED' if tag == '在用' else 'FREE'}", "name": f"P2599 {tag}路径"})
        assert created.status_code == 201, created.text
        tpl[tag] = created.json()["id"]
        node = client.post(f"{B}/path-templates/{tpl[tag]}/nodes", headers=admin, json={"key": "n1", "name": "首诊", "seq": 1})
        assert node.status_code == 201, node.text
        tpl[f"{tag}节点"] = node.json()["id"]
    assert client.post(f"{B}/path-templates/{tpl['在用']}/status", headers=admin,
                       json={"status": "published"}).status_code == 200
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2599 患者", "id_card": "330127197705052599"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org}).json()["id"]
    started = client.post(f"{B}/path-instances", headers=admin, json={"enrollment_id": enrollment,
                                                                      "template_id": tpl["在用"]})
    assert started.status_code in (200, 201), started.text
    # 改回草稿：原先页面只看「已发布」，草稿就给加节点、编辑、删除
    assert client.post(f"{B}/path-templates/{tpl['在用']}/status", headers=admin,
                       json={"status": "draft"}).status_code == 200
    return tpl


def test_清单与详情带上是否有患者走过(client, admin, world):
    rows = {t["id"]: t for t in client.get(f"{B}/path-templates", params={"limit": 100}, headers=admin).json()}
    assert (rows[world["在用"]]["in_use"], rows[world["未用"]]["in_use"]) == (True, False)   # 修前没有这一键
    assert client.get(f"{B}/path-templates/{world['在用']}", headers=admin).json()["in_use"] is True


def test_有患者走过的_改节点与删模板照旧409(client, admin, world):
    used, node = world["在用"], world["在用节点"]
    for resp in (
        client.post(f"{B}/path-templates/{used}/nodes", headers=admin, json={"key": "n2", "name": "复诊", "seq": 2}),
        client.patch(f"{B}/path-nodes/{node}", headers=admin, json={"name": "改名"}),
        client.delete(f"{B}/path-nodes/{node}", headers=admin),
    ):
        assert resp.status_code == 409 and "复制新版本" in resp.json()["detail"], resp.text
    deleted = client.delete(f"{B}/path-templates/{used}", headers=admin)
    assert deleted.status_code == 409 and "只能停用不能删除" in deleted.json()["detail"], deleted.text


def test_页面按是否有患者走过给按钮():
    start = PAGE.index("const showNodes = async (templateId) => {")
    nodes = PAGE[start:PAGE.index("const showTask", start)]
    assert 'const editable = tpl.status !== "published" && !tpl.in_use;' in nodes   # 修前只看已发布
    row = PAGE[PAGE.index('data-tpl-nodes="${t.id}"'):PAGE.index('<div id="spd-tpl-detail">')]
    assert '${t.status !== "published" && !t.in_use' in row   # 修前「加节点」无条件画
    assert '${t.in_use ? "" : `<button class="btn danger" data-tpl-del=' in row   # 修前「删除」无条件画
