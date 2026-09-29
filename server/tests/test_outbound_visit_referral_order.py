"""县外就诊挂的转诊单不能晚于这次就诊（P2-883，第二十四批「时间窗口的边界」扫描 Z1-2）。

`create_outbound_visit` 挂转诊单只查存在、属于同一患者、不是已退回——同一处注释写明挂单的规矩：「挂到别人的转诊单上会把
有序转诊率算高，必须拦」「被退回的转诊单没有转成，患者是自行外出」（P2-200）。在就诊之后才开的转诊单，同样没有把人转
出去：患者 9-01 自行去了省院、9-20 补开的上转单照样挂上，有序转诊率算成 100%。修后建单日（本地日期）晚于就诊日的 422；
同日照收。转诊单多久内有效、待接诊的算不算依据、一张单能挂几次另行待裁定。
"""
from datetime import datetime

from app.database import SessionLocal
from app.models import Referral


def test_就诊之后才开的转诊单不能挂_同日照收(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2883 {name}", "org_type": otype, "level": level}).json()["id"]
        for name, otype, level in (("卫生院", "township", "township"), ("县医院", "lead_hospital", "county"))]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2883 患者", "id_card": "330106196606062883"}).json()["id"]
    referral = client.post("/api/referrals", headers=admin, json={
        "patient_id": patient, "from_org_id": orgs[0], "to_org_id": orgs[1], "direction": "up"}).json()["id"]
    with SessionLocal() as db:   # 9-20（本地）开的单
        db.get(Referral, referral).created_at = datetime(2026, 9, 20, 2, 0)
        db.commit()

    def visit(date):
        return client.post("/api/analytics/outbound-visits", headers=admin, json={
            "patient_id": patient, "visit_date": date, "external_org_name": "省人民医院", "referral_id": referral})

    got = visit("2026-09-01")
    assert got.status_code == 422, got.text   # 修前 201：就诊之后补开的单算成了有序转诊
    assert "晚于这次县外就诊" in got.json()["detail"]
    assert visit("2026-09-20").status_code == 201   # 同日照收
