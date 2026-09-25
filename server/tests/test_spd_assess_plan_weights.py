"""考核方案条目的权重不经校验：写成文字的建方案照样 201，一跑分整张方案 500（P2-108）。

计分按条目取权重：`float(item.get("weight", indicator.weight) or 0)`。建 / 改方案只查指标编码在不在（P1-94），权重照单
全收——写成文字的（"四十"）`float()` 抛错，整张方案、所有考核对象一起 500；写成负数的倒扣分，总分可以是负的。与 P2-79
（评分规则写坏了计分 500）同一个形状：写库前拦（422），存量里的坏权重计分时逐指标记错、不 500，其余指标照常。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2108 考核院", "org_type": "township", "level": "township"}).json()["id"]


def _plan(client, admin, code, items):
    return client.post(f"{B}/assess-plans", headers=admin, json={
        "code": code, "name": f"{code} 方案", "level": "township", "object_type": "org",
        "period_type": "month", "items": items})


@pytest.mark.parametrize("weight", ["四十", -5, True, [40]], ids=["文字", "负数", "布尔", "列表"])
def test_建方案_权重不是非负数_422(client, admin, weight):
    resp = _plan(client, admin, "P2108_BAD", [{"indicator_code": "followup_rate", "weight": weight}])
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": "考核方案里指标 followup_rate 的权重必须是不小于 0 的数"}


def test_改方案也查(client, admin):
    created = _plan(client, admin, "P2108_EDIT", [{"indicator_code": "followup_rate", "weight": 40}])
    assert created.status_code == 201, created.text
    resp = client.patch(f"{B}/assess-plans/{created.json()['id']}", headers=admin, json={
        "items": [{"indicator_code": "followup_rate", "weight": "四十"}]})
    assert resp.status_code == 422, resp.text


def test_不写权重与数字字符串以外的合法写法照收(client, admin):
    resp = _plan(client, admin, "P2108_OK", [{"indicator_code": "followup_rate"},
                                            {"indicator_code": "path_rate", "weight": 0},
                                            {"indicator_code": "enroll_rate", "weight": 12.5}])
    assert resp.status_code == 201, resp.text


def test_存量坏权重_计分逐指标记错不500_其余照常(client, admin, org):
    from app.database import SessionLocal
    from app.spd.models import SpdAssessPlan

    created = _plan(client, admin, "P2108_LEGACY", [{"indicator_code": "followup_rate", "weight": 50},
                                                   {"indicator_code": "path_rate", "weight": 50}])
    assert created.status_code == 201, created.text
    with SessionLocal() as db:
        plan = db.get(SpdAssessPlan, created.json()["id"])
        plan.items = [{"indicator_code": "followup_rate", "weight": "四十"}, {"indicator_code": "path_rate", "weight": 50}]
        db.commit()
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": created.json()["id"], "period": "2026-09", "object_ids": [org]})
    assert run.status_code == 200, run.text[:300]   # 修前 500
    score_id = client.get(f"{B}/scores?plan_id={created.json()['id']}", headers=admin).json()[0]["id"]
    detail = {d["indicator_code"]: d for d in client.get(f"{B}/scores/{score_id}", headers=admin).json()["detail"]}
    assert detail["followup_rate"] == {"indicator_code": "followup_rate",
                                       "error": "权重非法：考核方案里指标 followup_rate 的权重必须是不小于 0 的数"}
    assert "error" not in detail["path_rate"]
