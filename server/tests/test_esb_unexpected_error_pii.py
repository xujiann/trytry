"""ESB 消费撞上库报的错：SQLAlchemy 异常全文连同绑定参数（姓名、证件号、手机号）回给经办，并写进 last_error、交换日志与
应用日志（P2-1144，第三十三批「隐私出口」扫描 A2-2）。

三条消费路径的意外错误都走 `_unexpected`（P1-176），它给的是 `f"未预期错误（{type(exc).__name__}：{exc}）"`：进消费回执的
detail 与编排执行回执的逐步结果、`_record_failure` 的 last_error、`_log_exchange` 的交换日志（任一机构的经办都读得到），
`logger.exception` 的 traceback 进应用日志。而 `str(DBAPIError)` 带 `[SQL: INSERT INTO patients …]` 与全部绑定参数——回执里的
载荷已按 P0-49 清成 `{}`，detail 里却什么都有；开了 PII 加密，证件号、电话是密文，姓名仍是明文。对接方送来的 FHIR Patient
的 birthDate 形状不对（开发库上是绑定参数类型错；真 PG 上 dateTime 撞 String(10) 列宽）就走这里。

修法：应用引擎 `hide_parameters=True`（回执、交换日志、job_runs、告警与 traceback 一处收住）；`_unexpected` 对外只给异常
类名与驱动原错（`exc.orig`）的首行。
"""
import logging

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, build_engine, engine
from app.models import EsbMessage, ExchangeLog
from app.routers import esb as esb_module
from conftest import login
from test_esb import enqueue, register_endpoint

ID_CARD, PHONE, NAME = "330102199001011234", "13812345678", "张三"
BAD_BIRTH_DATE = {"resourceType": "Patient",
                  "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": ID_CARD}],
                  "name": [{"text": NAME}], "gender": "male",
                  "telecom": [{"system": "phone", "value": PHONE}],
                  "birthDate": {"value": "1990-01-01"}}   # 形状不对：落库时撞绑定参数（开发库）/ 列宽（真 PG）


def _pii_in(text: str) -> list[str]:
    return [pii for pii in (ID_CARD, PHONE, NAME) if pii in text]


@pytest.fixture(scope="module")
def operator(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21144 卫生院", "org_type": "township", "level": "township"})
    assert org.status_code == 201, org.text
    user = client.post("/api/users", headers=admin, json={
        "username": "p21144_op", "password": "passw0rd1", "role": "operator", "org_id": org.json()["id"]})
    assert user.status_code == 201, user.text
    return login(client, "p21144_op", "passw0rd1")


def _exchange_details(source_system: str) -> list[str]:
    with SessionLocal() as db:
        return [x.error_detail for x in db.query(ExchangeLog).filter(ExchangeLog.source_system == source_system)]


def test_应用引擎不把绑定参数写进异常文本与日志():
    assert engine.hide_parameters is True
    pg = build_engine("postgresql+psycopg2://u:p@h/db")   # 惰性建连，不实连
    try:
        assert pg.hide_parameters is True
    finally:
        pg.dispose()


def test_手工消费撞库报错_回执_错误说明_交换日志_应用日志都不带患者字段(client, admin, operator, caplog):
    endpoint = register_endpoint(client, admin, "P21144_HIS", system_type="his")
    queued = enqueue(client, endpoint, "fhir_patient", BAD_BIRTH_DATE)
    assert queued.status_code == 201, queued.text
    with caplog.at_level(logging.ERROR, logger="medplat.esb"):
        got = client.post(f"/api/esb/messages/{queued.json()['id']}/process", headers=operator)
    assert got.status_code == 200, got.text
    detail = got.json()["detail"]
    # 走的确实是 `_unexpected`（库报的错），错成哪一类照旧看得出；说明只有驱动原错那一句，不带 SQL
    assert detail.startswith("未预期错误（") and "Error：" in detail, detail
    assert "[SQL" not in detail and "\n" not in detail, detail
    assert _pii_in(detail) == [], detail   # 修前：[parameters: (…, '张三', '330102…', …, '13812345678', …)]
    assert _pii_in(got.json()["last_error"]) == []
    with SessionLocal() as db:
        message = db.get(EsbMessage, queued.json()["id"])
        assert (message.status, message.retry_count) == ("failed", 1)
        assert _pii_in(message.last_error) == []
    logged = _exchange_details("P21144_HIS")
    assert len(logged) == 1 and _pii_in(logged[0]) == [], logged
    assert "消费时出现未预期错误" in caplog.text   # traceback 照旧进应用日志，只是不带参数
    assert _pii_in(caplog.text) == [], caplog.text[-600:]


def test_驱动原错只取首行_PG的DETAIL行不往外给(client, admin, operator, monkeypatch):
    """PG 驱动原错第二行起是 DETAIL，唯一键冲突时带着键值（这里是证件号）；说明只取首行，约束名照旧看得到。"""
    driver_error = Exception('duplicate key value violates unique constraint "uq_p21144"\n'
                             f"DETAIL:  Key (id_card)=({ID_CARD}) already exists.\n")

    def conflict(db, data):
        raise IntegrityError("INSERT INTO patients (name, id_card) VALUES (%(name)s, %(id_card)s)",
                             {"name": NAME, "id_card": ID_CARD}, driver_error)

    monkeypatch.setattr(esb_module, "create_patient_idempotent", conflict)
    endpoint = register_endpoint(client, admin, "P21144_PG", system_type="his")
    queued = enqueue(client, endpoint, "fhir_patient", {**BAD_BIRTH_DATE, "birthDate": "1990-01-01"})
    assert queued.status_code == 201, queued.text
    got = client.post(f"/api/esb/messages/{queued.json()['id']}/process", headers=operator)
    assert got.status_code == 200, got.text
    assert got.json()["detail"] == '未预期错误（IntegrityError：duplicate key value violates unique constraint "uq_p21144"）'
    assert [_pii_in(x) for x in _exchange_details("P21144_PG")] == [[]]


def test_编排执行撞库报错_逐步结果不带患者字段(client, admin, operator):
    endpoint = register_endpoint(client, admin, "P21144_FLOW", system_type="his")
    flow = client.post("/api/esb/flows", headers=admin, json={"code": "P21144_F", "name": "建档编排", "steps": [
        {"type": "transform", "config": {"format": "fhir_patient"}}, {"type": "persist", "config": {"entity": "patient"}}]})
    assert flow.status_code == 201, flow.text
    queued = enqueue(client, endpoint, "fhir_patient", BAD_BIRTH_DATE)
    assert queued.status_code == 201, queued.text
    got = client.post(f"/api/esb/flows/P21144_F/run?message_id={queued.json()['id']}", headers=operator)
    assert got.status_code == 200, got.text
    assert got.json()["status"] == "failed"
    step = got.json()["step_results"][-1]
    assert (step["type"], step["status"]) == ("persist", "failed") and step["detail"].startswith("未预期错误（")
    assert _pii_in(got.text) == [], got.text   # 修前：逐步结果与 error 里整段绑定参数
    assert [_pii_in(x) for x in _exchange_details("P21144_FLOW")] == [[]]
