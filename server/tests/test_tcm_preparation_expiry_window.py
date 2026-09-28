"""中药制剂效期的每日扫描与页面预警同一个窗口，摘要照实写「到期或已过期」（P2-694，第十七批「阈值边界 vs 文案」扫描 U2-3）。

页面的制剂效期预警按 60 天取（`expiring?days=60`，标题「60天内到期/已过期」），每日扫描 `preparation_expiry_scan`
与告警却用 30 天：45 天后到期的批次页面列着、扫描与告警不算它。扫描摘要写「30 天内到期制剂 N 批」，数的却含
已过期未召回的批次（与接口同一个判据）。聘用合同的提醒早就是页面与扫描同一个 60 天。
"""
from datetime import timedelta

from app import clock
from app.database import SessionLocal
from app.jobs import preparation_expiry_scan
from app.models import TcmFormula, TcmPreparationBatch, User


def _scan():
    with SessionLocal() as db:
        return preparation_expiry_scan(db)


def test_45天后到期的批次_扫描也算_摘要写明含已过期(client, admin):
    org_id = client.post("/api/organizations", headers=admin, json={
        "name": "P2694 中医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    before, _ = _scan()
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        formula = TcmFormula(code="P2694F", name="P2694 健脾丸")
        db.add(formula)
        db.flush()
        db.add(TcmPreparationBatch(formula_id=formula.id, batch_no="P2694-01", org_id=org_id, quantity=10,
                                   produced_date=clock.today().isoformat(), status="released", created_by=admin_id,
                                   expire_date=(clock.today() + timedelta(days=45)).isoformat()))
        db.commit()
    after, summary = _scan()
    assert after == before + 1   # 修前 30 天窗口，不算它
    assert summary == f"60 天内到期或已过期制剂 {after} 批"   # 修前「30 天内到期制剂 N 批」
