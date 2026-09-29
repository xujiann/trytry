"""集成入站的交换日志写不进去时，不把已提交的业务回成 500（P2-819，第二十二批「失败路径的半截状态」扫描 X1-4）。

`_run_inbound` 在处理函数（业务已经提交）之后才记「成功」交换日志；`_log_exchange` 另开会话当场提交、不吞异常。这一笔要在
请求里拿第二个连接——连接池在突发并发下一时取不到、主备切换、语句超时，都会让它失败：业务已落库，响应却是 500。对接方
按规范重推，FHIR Observation 落两条慢病随访；ORU 重推回 409「已报告」，LIS 那头两次都是失败。「失败」那一笔同样照抛，
把原来的 4xx 换成 500。修后写不进去的留痕回滚、记错误日志后吞掉，与审计落库（`main._write_audit`）同一句。
"""
import pytest
from sqlalchemy.exc import OperationalError

import app.routers.integration as integration
from app.database import SessionLocal
from app.models import ExamReport, ExchangeLog, FollowUp


class _FailNextLog:
    """只让下一次「记交换日志」的提交报库错（按成功 / 失败挑那一笔），其余原样。"""

    armed: dict = {}

    def __init__(self):
        self._session = SessionLocal()
        self._success = None

    def add(self, obj):
        if isinstance(obj, ExchangeLog):
            self._success = obj.success
        return self._session.add(obj)

    def commit(self):
        if self._success is not None and self.armed.pop(self._success, False):
            self._session.rollback()
            raise OperationalError("INSERT INTO exchange_logs ...", {}, Exception("QueuePool limit reached"))
        return self._session.commit()

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.fixture
def flaky_log(monkeypatch):
    _FailNextLog.armed = {}
    monkeypatch.setattr(integration, "SessionLocal", _FailNextLog)
    return _FailNextLog.armed


def _patient(client, admin, name, id_card):
    resp = client.post("/api/patients", headers=admin, json={
        "name": name, "id_card": id_card, "gender": "男", "birth_date": "1965-01-01", "phone": "13812342819"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _logs(message_type):
    with SessionLocal() as db:
        return [(x.success, x.error_detail[:3]) for x in db.query(ExchangeLog).filter(
            ExchangeLog.message_type == message_type).order_by(ExchangeLog.id)]


def test_观测值入站_成功留痕写不进去照回201_只落一条随访(client, admin, flaky_log):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2819 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = _patient(client, admin, "P2819 观测", "110101196501012819")
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient["id"], "disease": "hypertension", "managed_by_org_id": org})
    assert chronic.status_code in (200, 201), chronic.text
    obs = {"resourceType": "Observation", "status": "final", "subject": {"reference": f"Patient/{patient['ehc_no']}"},
           "component": [{"code": {"coding": [{"code": "8480-6"}]}, "valueQuantity": {"value": 168}},
                         {"code": {"coding": [{"code": "8462-4"}]}, "valueQuantity": {"value": 102}}]}
    before = _logs("fhir_observation")
    flaky_log[True] = True
    resp = client.post("/api/integration/fhir/Observation", headers=admin, json=obs)
    assert resp.status_code == 201, resp.text   # 修前 500：业务已提交，对接方按规范重推就落第二条
    assert not flaky_log   # 注入确实打在了成功留痕那一笔
    with SessionLocal() as db:
        rows = [(f.sbp, f.dbp) for f in db.query(FollowUp).filter(FollowUp.chronic_id == chronic.json()["id"])]
    assert rows == [(168, 102)]
    assert _logs("fhir_observation") == before   # 这一条留痕丢了（记进错误日志），不补、不重


def test_检验报告入站_成功留痕写不进去照回201_报告只一份(client, admin, flaky_log):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2819 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    id_card = "110101198807072819"
    patient = _patient(client, admin, "P2819 检验", id_card)
    exam = client.post("/api/exams", headers=admin, json={
        "patient_id": patient["id"], "from_org_id": org, "center_type": "lab", "item_code": "K", "item_name": "血钾"})
    assert exam.status_code == 201, exam.text
    message = "\r".join([
        "MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260929100000||ORU^R01|P2819|P|2.4",
        f"PID|1||{id_card}^^^CN^ID||P2819 检验", f"OBR|1|{exam.json()['id']}||K^血钾",
        "OBX|1|NM|K^血钾|1|6.9|mmol/L|3.5-5.3|HH"])
    flaky_log[True] = True
    resp = client.post("/api/integration/hl7v2/oru", headers={**admin, "X-Source-System": "LIS"},
                       json={"message": message})
    assert resp.status_code == 201, resp.text   # 修前 500；LIS 重推又回 409「已报告」，两次都记失败
    assert not flaky_log
    with SessionLocal() as db:
        assert db.query(ExamReport).filter(ExamReport.request_id == exam.json()["id"]).count() == 1


def test_失败留痕写不进去_原来的4xx不变成500(client, admin, flaky_log):
    flaky_log[False] = True
    resp = client.post("/api/integration/fhir/Observation", headers=admin, json={
        "resourceType": "Observation", "status": "final", "subject": {"reference": "Patient/P2819-NOBODY"},
        "component": []})
    assert 400 <= resp.status_code < 500, resp.text   # 修前 500
    assert not flaky_log
