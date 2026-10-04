"""专病中心的覆盖机构 / 团队编号写重了照存，卫健工作台按清单长度数（P2-1277，第三十七批「一次请求、一次导入里的重复元素」
扫描 AA2-8）。

`spd/routers/config/centers.py` 建 / 改中心查存在时用 `set(ids)`（P2-433 只修了「不存在」）、存的却是原列表；卫健工作台
（`spd/routers/workbench.py::health_commission_workbench`）按 `len(c.org_ids)` 计数。页面是「逗号分隔」自由填写，不去重。
修前实测：覆盖机构填 `[甲, 甲, 乙]`，存下 `[1, 1, 2]`，工作台中心卡片写「覆盖机构 3」。

修法：建 / 改中心时去重后再存（保持首次出现的顺序，响应回去重后的清单）；工作台按不同的编号数计数，修前存下的重复清单也数对。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = []
    for name in ("P21277 甲卫生院", "P21277 乙卫生院"):
        orgs.append(client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"])
    team = client.post(f"{B}/teams", headers=admin, json={"name": "P21277 团队", "org_id": orgs[0], "level": "township"})
    assert team.status_code in (200, 201), team.text
    return {"a": orgs[0], "b": orgs[1], "team": team.json()["id"]}


def _stored(center_id):
    from app.database import SessionLocal
    from app.spd.models import SpdCenter

    with SessionLocal() as db:
        center = db.get(SpdCenter, center_id)
        return center.org_ids, center.team_ids


def _card(client, admin, center_id):
    body = client.get(f"{B}/workbench/health-commission", headers=admin)
    assert body.status_code == 200, body.text
    return next((c["orgs"], c["teams"]) for c in body.json()["centers"] if c["id"] == center_id)


def test_建中心_重复编号去重后再存_工作台按不同机构数(client, admin, world):
    a, b, team = world["a"], world["b"], world["team"]
    created = client.post(f"{B}/centers", headers=admin, json={
        "code": "P21277_NEW", "name": "P21277 高血压中心", "program_code": "hypertension",
        "org_ids": [a, a, b], "team_ids": [team, team]})
    assert created.status_code == 201, created.text
    assert (created.json()["org_ids"], created.json()["team_ids"]) == ([a, b], [team])   # 修前 [甲, 甲, 乙]、[团队, 团队]
    assert _stored(created.json()["id"]) == ([a, b], [team])
    assert _card(client, admin, created.json()["id"]) == (2, 1)   # 修前「覆盖机构 3、团队 2」


def test_改中心_同样去重_保持首次出现的顺序(client, admin, world):
    a, b, team = world["a"], world["b"], world["team"]
    created = client.post(f"{B}/centers", headers=admin, json={
        "code": "P21277_EDIT", "name": "P21277 糖尿病中心", "program_code": "diabetes", "org_ids": [a]})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    changed = client.patch(f"{B}/centers/{cid}", headers=admin, json={"org_ids": [b, a, b, a], "team_ids": [team, team]})
    assert changed.status_code == 200, changed.text
    assert (changed.json()["org_ids"], changed.json()["team_ids"]) == ([b, a], [team])   # 修前 [乙, 甲, 乙, 甲]
    assert _stored(cid) == ([b, a], [team])
    renamed = client.patch(f"{B}/centers/{cid}", headers=admin, json={"name": "P21277 糖尿病中心（改名）"})
    assert renamed.status_code == 200 and renamed.json()["org_ids"] == [b, a]   # 不传清单的不动
    assert _card(client, admin, cid) == (2, 1)


def test_存量重复清单_工作台照样按不同的编号数(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdCenter

    a, b, team = world["a"], world["b"], world["team"]
    with SessionLocal() as db:   # 修前存下的重复清单
        legacy = SpdCenter(code="P21277_OLD", name="P21277 存量中心", program_code="hypertension",
                           org_ids=[a, a, b], team_ids=[team, team, team])
        db.add(legacy)
        db.commit()
        cid = legacy.id
    assert _card(client, admin, cid) == (2, 1)   # 修前 (3, 3)
    assert _stored(cid) == ([a, a, b], [team, team, team])   # 只改计数，库里的存量清单不动
