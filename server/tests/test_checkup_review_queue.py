"""体检的「总检」在页面上够得着、看得出（P2-409）。

体检清单只回最新 200 条、出参里没有总检字段——页面上连「这次总检了没有」都看不出（页面自己的说明就这么写着），
挤出窗口的那次体检更是再没有一行给「总检」。与在线咨询、用血、上门三页（P2-408）同一个毛病，只是后端连筛选都
没有。修后清单接口加 `?reviewed=`（按总检结论判：总检接口要求结论非空，「两列均空 = 尚未总检」），清单行多一个
`reviewed`；登记回执的键集合有特征化用例钉着（`test_checkup_items_review`），不动。页面给能总检的人把没总检的
单独取一遍、排在最前，「总检」列标状态，总检完只改这一行的标记。端到端见 `test_体检没总检的排在最前_…`。
"""
import os

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _render_certs() -> str:
    with open(os.path.join(STATIC, "pages-public.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderCerts()")
    return source[start:source.index("\nasync function ", start + 1)]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2409 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2409 患者", "id_card": "330127197309092409"}).json()["id"]
    ids = []
    for day in ("01", "02"):
        created = client.post("/api/checkups", headers=admin, json={
            "patient_id": patient, "org_id": org, "exam_date": f"2026-09-{day}", "summary": "P2409"})
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    reviewed = client.post(f"/api/checkups/{ids[1]}/review", headers=admin, json={"final_conclusion": "未见异常"})
    assert reviewed.status_code == 200, reviewed.text
    return {"org": org, "patient": patient, "todo": ids[0], "done": ids[1]}


def _ids(client, admin, world, **params):
    rows = client.get("/api/checkups", headers=admin, params={"patient_id": world["patient"], **params}).json()
    return {r["id"]: r["reviewed"] for r in rows}


def test_清单按总检了没有筛_行上带着状态(client, admin, world):
    assert _ids(client, admin, world, reviewed=False) == {world["todo"]: False}   # 修前没有这个筛选，两条都回
    assert _ids(client, admin, world, reviewed=True) == {world["done"]: True}
    assert _ids(client, admin, world) == {world["todo"]: False, world["done"]: True}   # 修前行上没有 reviewed


def test_登记回执的键集合不变(client, admin, world):
    created = client.post("/api/checkups", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "exam_date": "2026-09-03"})
    assert created.status_code == 201, created.text
    assert "reviewed" not in created.json()


def test_页面给能总检的人把没总检的排在最前_总检列标状态():
    body = _render_certs()
    assert 'canReview ? api("/api/checkups?reviewed=false") : []' in body            # 修前只取最新 200 条
    assert "const checkups = [...unreviewed, ...recent.filter((c) => !unreviewedIds.has(c.id))];" in body
    assert "statusTag(CHK_REVIEW, c.reviewed ? \"done\" : \"todo\")" in body            # 修前行上看不出总检了没有
    assert 'data-chkstate="${c.id}"' in body
    review = body[body.index("if (chkreview) {"):]
    assert 'statusTag(CHK_REVIEW, "done")' in review[:review.index("return await showItems(chkreview, r);")]
