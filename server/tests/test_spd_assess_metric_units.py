"""考核取数照变量字典说的数：高危只数在管的，评估完成率分子分母都数人（P2-139）。

变量字典（`service` 里每种取数口径承诺的变量及其含义，指标公式只能引用这些名字）写的是：
- 纳管档案 `high_risk` =「在管的高危 / 极高危」——实现数的是这家机构名下**全部**档案里的高危，
  死亡、迁出、排除、召回的都算进去：一位高危患者去世了，这家机构的高危人数一分不少；
- 风险评估 `enrolled` =「在管患者数」、`assessed` =「期内评估过的在管患者数」——实现的分子按人去重、
  分母却数档案：一位同时管着高血压与糖尿病的患者评估过了，完成率是 1/2。

修法：高危只数在管档案；评估的分母与分子同一个单位（在管患者，按人去重）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2139 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    both, other, gone = (client.post("/api/patients", headers=admin, json={
        "name": f"P2139 患者{i}", "id_card": f"33010619680808{i:04d}", "gender": "男",
        "birth_date": "1968-08-08"}).json()["id"] for i in range(3))
    with SessionLocal() as db:
        db.add_all([
            # 一位患者同时在管两个病种（下面给他做评估——评估会按得分改写档案风险，所以高危放在另一位身上）
            SpdEnrollment(patient_id=both, program_code="hypertension", org_id=org, status="active", risk_level="low"),
            SpdEnrollment(patient_id=both, program_code="diabetes", org_id=org, status="active", risk_level="low"),
            SpdEnrollment(patient_id=other, program_code="hypertension", org_id=org, status="active", risk_level="high"),
            # 已去世的高危患者
            SpdEnrollment(patient_id=gone, program_code="hypertension", org_id=org, status="dead",
                          risk_level="very_high"),
        ])
        db.commit()
    assessed = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": both, "scale_code": "assess_risk_common", "program_code": "hypertension",
        "answers": {"control": "达标", "adherence": "良好", "complication": "无", "selfcare": "能自理"}})
    assert assessed.status_code == 201, assessed.text
    return {"org": org}


def _metrics(client, admin, world, source):
    from datetime import date

    from app.database import SessionLocal
    from app.spd.models import SpdIndicator
    from app.spd.routers.assess import collect_metrics_batch

    code = f"p2139_{source}"
    created = client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": f"P2139 {source}", "data_source": source, "object_type": "org"})
    assert created.status_code == 201, created.text
    with SessionLocal() as db:
        indicator = db.query(SpdIndicator).filter(SpdIndicator.code == code).one()
        return collect_metrics_batch(db, indicator, "org", [world["org"]], date.today().strftime("%Y-%m"))[world["org"]]


def test_高危只数在管的(client, admin, world):
    metrics = _metrics(client, admin, world, "enrollment")
    assert metrics["enrolled"] == 3   # 在管档案：两个病种各算一份
    assert metrics["high_risk"] == 1   # 修前 2：已去世的那位极高危照算


def test_评估完成率分子分母都数人(client, admin, world):
    metrics = _metrics(client, admin, world, "assessment")
    assert (metrics["assessed"], metrics["enrolled"]) == (1, 2)   # 修前 (1, 3)：分母数档案，同时管两个病种的算两次
