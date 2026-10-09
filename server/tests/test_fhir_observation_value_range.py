"""FHIR Observation 读数越界时回按字段说明的 422，不再只回「消息解析失败」（P2-1765，第五十二批扫描 AP2-13）。

观测值归档前过随访入参的校验（`FollowUpCreate`：收缩压大于 0、不超过 300，舒张压大于 0、不超过 200，血糖大于 0，P1-101）。
原先 ValidationError 一路抛到 `_run_inbound` 的兜底：修前实测收缩压 350 / 0 两次都回 422「消息解析失败，已记录交换日志」，
真正的错因（pydantic 的「Input should be less than or equal to 300」）只在交换日志里；同一函数别的分支（血糖单位认不出、
收缩压不高于舒张压）都回具体错因，`_do_fhir_observation` 的注释也写着「会在这里报 422」。

修法：捕获 ValidationError，转成中文、带字段与收到的值的 422 detail（不把 pydantic 原文抛出去，界值取请求模型声明的那一份）；
不落随访；正常读数照旧。
"""
import pytest

SBP, DBP, GLUCOSE = "8480-6", "8462-4", "15074-8"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21765 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21765 慢病患者", "id_card": "330102196601011765", "gender": "女", "birth_date": "1966-01-01"}).json()
    chronic = {}
    for disease in ("hypertension", "diabetes"):
        resp = client.post("/api/chronic", headers=admin, json={
            "patient_id": patient["id"], "disease": disease, "managed_by_org_id": org})
        assert resp.status_code in (200, 201), resp.text
        chronic[disease] = resp.json()["id"]
    return {"ehc_no": patient["ehc_no"], "chronic": chronic}


def _observe(client, admin, world, *readings):
    components = [{"code": {"coding": [{"system": "http://loinc.org", "code": code}]},
                   "valueQuantity": {"value": value}} for code, value in readings]
    return client.post("/api/integration/fhir/Observation", headers={**admin, "X-Source-System": "BP-P21765"}, json={
        "resourceType": "Observation", "status": "final", "subject": {"reference": f"Patient/{world['ehc_no']}"},
        "component": components})


def _followups(client, admin, world, disease):
    return client.get(f"/api/chronic/{world['chronic'][disease]}/followups", headers=admin).json()


@pytest.mark.parametrize(("readings", "detail"), [
    (((SBP, 350), (DBP, 95)), "收缩压（sbp）须不超过 300（收到 350）"),
    (((SBP, 0), (DBP, 80)), "收缩压（sbp）须大于 0（收到 0）"),
    (((SBP, -5), (DBP, 250)), "收缩压（sbp）须大于 0（收到 -5）；舒张压（dbp）须不超过 200（收到 250）"),
    (((GLUCOSE, 0),), "血糖（glucose）须大于 0（收到 0）"),
], ids=["收缩压过高", "收缩压为0", "两项都越界", "血糖为0"])
def test_读数越界回具体错因_不落随访(client, admin, world, readings, detail):
    resp = _observe(client, admin, world, *readings)
    assert (resp.status_code, resp.json()) == (422, {"detail": detail})   # 修前「消息解析失败，已记录交换日志」
    assert _followups(client, admin, world, "hypertension") == []
    assert _followups(client, admin, world, "diabetes") == []


def test_交换日志记的是同一句中文错因(client, admin, world):
    assert _observe(client, admin, world, (SBP, 350), (DBP, 95)).status_code == 422
    logs = client.get("/api/integration/exchange-logs", headers=admin,
                      params={"message_type": "fhir_observation", "source_system": "BP-P21765"}).json()["logs"]
    assert logs[0]["error_detail"] == "422: 收缩压（sbp）须不超过 300（收到 350）"   # 修前「解析异常: 1 validation error …」


def test_正常读数照旧归档(client, admin, world):
    resp = _observe(client, admin, world, (SBP, 150), (DBP, 95))
    assert resp.status_code == 201, resp.text
    assert (resp.json()["disease"], resp.json()["values"]) == ("hypertension", {"sbp": 150.0, "dbp": 95.0})
    assert [(f["sbp"], f["dbp"]) for f in _followups(client, admin, world, "hypertension")] == [(150.0, 95.0)]
