"""慢病分级规则的阈值先后要与方向一致（P2-886，第二十四批「阈值与边界值」扫描 Z4-3）。

`level_rules_problem` 只查阈值是不是有限的数、direction 取值合不合法，缺省按越高越危放行。3 级是更极端的一档（预置
「≥160→3级，≥140→2级」；越低越危的严重精神障碍 level3=3 < level2=6）。照依从性那样写 level3 < level2 却漏写
direction：新建「认知障碍」MMSE level3=10、level2=20 → 201，之后 MMSE 5 定 1 级、不建议上转，28 反倒定 3 级；方向写
对、阈值写反的，2 级永远到不了。修后两种写反都 422（建 / 改都查）；存量写反的与 P1-125 同一口径，记随访 422 说清楚。
"""
from app.database import SessionLocal
from app.models import ChronicDiseaseType

C = "/api/chronic"


def _create(client, admin, code, metric):
    return client.post(f"{C}/disease-types", headers=admin,
                       json={"code": code, "name": f"{code} 病种", "level_rules": {"metrics": [metric]}})


def test_建病种_阈值先后与方向不符_422(client, admin):
    got = _create(client, admin, "p2886_mmse", {"key": "mmse", "level3": 10, "level2": 20})   # 漏写 direction
    assert got.status_code == 422, got.text   # 修前 201：MMSE 28 定 3 级、5 定 1 级
    assert got.json()["detail"] == ("分级规则非法：指标 mmse 按越高越危（direction: high）算，3 级阈值 10 不能低于 2 级"
                                    "阈值 20；越低越危请写 direction: low")
    got = _create(client, admin, "p2886_low", {"key": "mmse", "direction": "low", "level3": 20, "level2": 10})
    assert got.status_code == 422, got.text   # 方向对、阈值反：2 级永远到不了
    assert "3 级阈值 20 不能高于 2 级阈值 10" in got.json()["detail"]
    ok = _create(client, admin, "p2886_ok", {"key": "mmse", "direction": "low", "level3": 10, "level2": 20})
    assert ok.status_code == 201, ok.text
    assert _create(client, admin, "p2886_eq", {"key": "sbp", "level3": 160, "level2": 160}).status_code == 201   # 相等照收
    got = client.patch(f"{C}/disease-types/{ok.json()['id']}", headers=admin,
                       json={"level_rules": {"metrics": [{"key": "mmse", "level3": 10, "level2": 20}]}})
    assert got.status_code == 422, got.text   # 改也查


def test_存量写反的规则_记随访422说清楚(client, admin):
    with SessionLocal() as db:
        db.add(ChronicDiseaseType(code="p2886_legacy", name="P2886 存量", guidance="", followup_interval_days=90,
                                  level_rules={"metrics": [{"key": "sbp", "level3": 140, "level2": 160}]}))
        db.commit()
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2886 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2886 患者", "id_card": "330106197001012886"}).json()["id"]
    chronic = client.post(C, headers=admin, json={"patient_id": patient, "disease": "p2886_legacy",
                                                  "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    got = client.post(f"{C}/{chronic.json()['id']}/followups", headers=admin, json={"sbp": 150, "dbp": 90})
    assert got.status_code == 422, got.text   # 修前 201、150 定 3 级（2 级永远到不了）
    assert got.json()["detail"].startswith("病种「p2886_legacy」的分级规则配置有误（指标 sbp 按越高越危")
