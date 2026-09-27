"""慢病分级规则的阈值收 NaN / Infinity：和谁比都不成立，血压 220/130 定成 1 级、不转诊（P2-465）。

分级规则是宽字典（`level_rules: dict`），P1-92 给数值字段上的 `FiniteFloat` 管不到里面；标准库 `json.loads` 又照收
`NaN` / `Infinity` 记号。原先 `level_rules_problem` 只查「是不是数」，NaN 也是 float——建 / 改病种照样 201 / 200：
SQLite 上存进去，之后这个病种每一次随访都定成 1 级（`value >= NaN` 恒假），正是 P1-125 要挡的「悄悄定错级」；
PG 的 JSON 列存不进 NaN，直接 500。

修法：阈值是非有限的 float 同样 422（记随访时对存量坏规则同一句，说清楚、不定级）。
"""
import json

import pytest

C = "/api/chronic"
HEADERS = {"Content-Type": "application/json"}


def _raw(rules_literal: str, **fields) -> str:
    """请求体原文：NaN / Infinity 只能以 JSON 记号的样子发出去（json.dumps 默认也这么写）。"""
    head = json.dumps(fields, ensure_ascii=False)[:-1]
    return f'{head}, "level_rules": {rules_literal}}}' if fields else f'{{"level_rules": {rules_literal}}}'


@pytest.mark.parametrize(("literal", "level"), [
    ('{"metrics": [{"key": "sbp", "direction": "high", "level3": NaN, "level2": 140}]}', "level3"),
    ('{"metrics": [{"key": "sbp", "direction": "high", "level3": 160, "level2": Infinity}]}', "level2"),
    ('{"metrics": [{"key": "sbp", "direction": "low", "level3": -Infinity}]}', "level3"),
], ids=["NaN", "Infinity", "负Infinity"])
def test_建病种_阈值是非有限数一律422(client, admin, literal, level):
    body = _raw(literal, code="p2465_bad", name="P2465 坏阈值")
    resp = client.post(f"{C}/disease-types", headers={**admin, **HEADERS}, content=body.encode())
    assert resp.status_code == 422, resp.text[:300]   # 修前 201
    assert resp.json() == {"detail": f"分级规则非法：指标 sbp 的 {level} 阈值必须是有限的数（不能是 NaN / Infinity）"}


def test_改病种也查(client, admin):
    rules = {"metrics": [{"key": "sbp", "direction": "high", "level3": 160, "level2": 140}]}
    created = client.post(f"{C}/disease-types", headers=admin,
                          json={"code": "p2465_htn", "name": "P2465 高血压", "level_rules": rules})
    assert created.status_code == 201, created.text
    url = f"{C}/disease-types/{created.json()['id']}"
    resp = client.patch(url, headers={**admin, **HEADERS},
                        content=_raw('{"metrics": [{"key": "sbp", "level3": NaN, "level2": NaN}]}').encode())
    assert resp.status_code == 422, resp.text[:300]   # 修前 200
    assert resp.json() == {"detail": "分级规则非法：指标 sbp 的 level3 阈值必须是有限的数（不能是 NaN / Infinity）"}
    stored = next(t for t in client.get(f"{C}/disease-types", headers=admin).json() if t["code"] == "p2465_htn")
    assert stored["level_rules"] == rules   # 被拒的那次一个字没写进去


def test_存量NaN阈值_记随访422说清楚_不悄悄定成1级(client, admin):
    from app.database import SessionLocal
    from app.models import ChronicDiseaseType, FollowUp

    with SessionLocal() as db:
        db.add(ChronicDiseaseType(code="p2465_legacy", name="P2465 存量 NaN 阈值", guidance="", followup_interval_days=90,
                                  level_rules={"metrics": [{"key": "sbp", "direction": "high", "level3": float("nan"),
                                                            "level2": float("nan")}]}))
        db.commit()
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2465 慢病院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2465 患者", "id_card": "330106197001012465", "gender": "男", "birth_date": "1970-01-01"}).json()["id"]
    chronic = client.post(C, headers=admin, json={"patient_id": patient, "disease": "p2465_legacy",
                                                  "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    chronic_id = chronic.json()["id"]
    resp = client.post(f"{C}/{chronic_id}/followups", headers=admin, json={"sbp": 220, "dbp": 130})
    assert resp.status_code == 422, resp.text[:300]   # 修前 201、定成 1 级（控制良好），不转诊
    assert resp.json() == {"detail": "病种「p2465_legacy」的分级规则配置有误（指标 sbp 的 level3 阈值必须是有限的数"
                                     "（不能是 NaN / Infinity）），请在病种目录里修正后再记随访"}
    with SessionLocal() as db:
        assert db.query(FollowUp).filter(FollowUp.chronic_id == chronic_id).count() == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_校验函数本身(value):
    from app.routers.chronic import level_rules_problem

    assert "有限的数" in level_rules_problem({"metrics": [{"key": "fpg", "level3": value}]})
    assert level_rules_problem({"metrics": [{"key": "fpg", "level3": 13.9, "level2": 7}]}) == ""
