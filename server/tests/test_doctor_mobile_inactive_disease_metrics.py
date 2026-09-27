"""医生移动端随访录入只拉启用中的病种：已停用病种的在管档案画不出指标框，只能登一条没有指标的随访（P2-458）。

慢病页写明停用病种是「不再新增」、「已建的档案不受影响、仍按原规则随访」，后端也照原规则分级（停用高血压后录 175/108 仍判
3 级）；可移动端取病种目录带着 `?active=true`，停用病种的档案一选上，指标框一个都画不出来，还提示「该病种未配置分级指标，
可仅登记随访」——把医生推向一条没有指标的随访（照样清超期、计绩效，却没有数）。修后：目录取全部。
"""
import os

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _load_chronic() -> str:
    with open(os.path.join(STATIC, "m", "doctor.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function loadChronic()")
    return source[start:source.index("\nfunction ", start)]


def test_随访录入取全部病种_含已停用的():
    body = _load_chronic()
    assert 'api("/api/chronic/disease-types")' in body
    assert "disease-types?active=true" not in body   # 修前：停用病种的档案画不出指标框


def test_后端对停用病种的档案照原规则分级(client, admin):
    """移动端靠的就是这一点：停用病种的档案随访时照样按目录里的规则分级，所以指标框必须画出来。"""
    created = client.post("/api/chronic/disease-types", headers=admin, json={
        "code": "p2458", "name": "P2458 病种", "followup_interval_days": 30,
        "level_rules": {"metrics": [{"key": "sbp", "name": "收缩压", "unit": "mmHg", "level3": 160, "level2": 140}]}})
    assert created.status_code == 201, created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2458 患者", "id_card": "330106197011112458"}).json()["id"]
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2458 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    archive = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "managed_by_org_id": org, "disease": "p2458"})
    assert archive.status_code == 201, archive.text
    assert client.patch(f"/api/chronic/disease-types/{created.json()['id']}", headers=admin,
                        json={"active": False}).status_code == 200
    followup = client.post(f"/api/chronic/{archive.json()['id']}/followups", headers=admin, json={"sbp": 175})
    assert followup.status_code == 201, followup.text
    assert followup.json()["level"] == 3
