"""居民追问与医生「结束咨询」同时到：追问落进已结束的会话，医生的开放清单里没有它（P2-1180，第三十四批扫描 L1-10）。

`portal.start_consult` 锁外查同病种的开放会话、查到就往里追加；`care.close_consult` 无条件把会话置为已结束。读到开放
之后医生刚结束并提交，居民那条「头晕得厉害、胸闷，要不要马上去医院」照旧落进已结束的会话——医生端只看开放会话，没人
看到。顺序发生时（先结束、后追问）会新开一条会话承接（平台在线咨询的同形缺陷已修，P2-404）。

修法：结束与追加用同一把会话行锁（`serialized_on(db, SpdConsult, …)`），追加前在锁里按列复核仍开放，已结束的新开一条
会话承接（与顺序发生时一致）；结束在锁里先重读再改。

时序钉两条：
- 结束落在居民那一路查完开放会话之后、它的下一条语句之前（修后是进锁复核，修前是落消息）——追问进新会话；
- 结束与正在追加的那一路同时到——结束要等追加提交后才进得来（同一把锁），不能先结束、再让消息落进去。
"""
import threading
from datetime import datetime

import pytest
from sqlalchemy import event

from app.config import settings
from app.database import SessionLocal, engine

P = "/api/portal/spd"
B = "/api/spd"
PHONE = "13912201180"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import Patient, ResidentAccount

    with SessionLocal() as db:
        me = Patient(ehc_no="EHC-P21180", name="P21180 居民", id_card="330106198011011180", gender="男",
                     birth_date="1980-11-01", phone=PHONE)
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone=PHONE, patient_id=me.id, nickname="P21180", wechat_openid="", status="active"))
        db.commit()
        patient_id = me.id
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"patient": patient_id, "resident": {"Authorization": f"Bearer {token}"}}


def _open_consult(client, world, program_code):
    """居民先发一条，开出这个病种的开放会话，返回会话号。"""
    first = client.post(f"{P}/consults", headers=world["resident"],
                        json={"program_code": program_code, "content": "最近血压 160/100，要加药吗？"})
    assert first.status_code == 201 and first.json()["status"] == "open", first.text
    return first.json()["consult_id"]


def _messages(consult_id):
    from app.spd.models import SpdConsultMessage

    with SessionLocal() as db:
        return [m.content for m in db.query(SpdConsultMessage).filter_by(consult_id=consult_id)
                .order_by(SpdConsultMessage.id)]


def _status(consult_id):
    from app.spd.models import SpdConsult

    with SessionLocal() as db:
        return db.get(SpdConsult, consult_id).status


def test_查到开放会话之后医生刚结束_追问进新会话_医生开放清单看得到(client, admin, world):
    from app.spd.models import SpdConsult

    consult_id = _open_consult(client, world, "hypertension")
    state: dict = {}

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "fired" in state:
            return
        if "conn" not in state:
            # 居民那一路查同病种开放会话的那一句（只有它带「会话状态 =」条件）
            if statement.lstrip().upper().startswith("SELECT") and "FROM spd_consults" in statement \
                    and "spd_consults.status = " in statement:
                state["conn"] = conn
            return
        if conn is state["conn"]:   # 同一路的下一条语句发出之前：医生结束会话并提交
            state["fired"] = True
            with SessionLocal() as other:
                consult = other.get(SpdConsult, consult_id)
                consult.status, consult.closed_at = "closed", datetime.now()
                other.commit()

    event.listen(engine, "before_cursor_execute", listener)
    try:
        resp = client.post(f"{P}/consults", headers=world["resident"],
                           json={"program_code": "hypertension", "content": "今天早上头晕得厉害，还胸闷，要不要马上去医院？"})
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert "fired" in state
    assert resp.status_code == 201, resp.text
    reopened = resp.json()["consult_id"]
    assert reopened != consult_id and resp.json()["status"] == "open"   # 修前 {consult_id: 原会话, status: closed}
    assert _status(consult_id) == "closed"
    assert "今天早上头晕得厉害，还胸闷，要不要马上去医院？" not in _messages(consult_id)
    assert _messages(reopened) == ["今天早上头晕得厉害，还胸闷，要不要马上去医院？"]
    listed = client.get(f"{B}/consults", headers=admin, params={"status": "open"})
    assert listed.status_code == 200, listed.text
    assert reopened in [row["id"] for row in listed.json()]   # 修前医生端开放清单是空的


def test_结束与正在追加的那一路同时到_等追加提交后才结束(client, admin, world):
    consult_id = _open_consult(client, world, "diabetes")
    closing: dict = {}
    fired: list = []

    def close():
        closing["resp"] = client.post(f"{B}/consults/{consult_id}/close", headers=admin)

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("INSERT INTO SPD_CONSULT_MESSAGES"):
            fired.append(True)
            thread = threading.Thread(target=close)
            thread.start()
            thread.join(timeout=1.5)   # 修前结束一路不拿锁、此刻早已提交；修后它卡在会话行锁外，等追加提交出块
            fired.append(thread.is_alive())
            fired.append(thread)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        resp = client.post(f"{P}/consults", headers=world["resident"],
                           json={"program_code": "diabetes", "content": "空腹血糖 11，要紧吗？"})
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    fired[2].join(timeout=30)
    assert resp.status_code == 201 and resp.json()["consult_id"] == consult_id, resp.text
    assert fired[1] is True   # 修前 False：结束先提交了，追问随后落进已结束的会话
    assert closing["resp"].status_code == 200, closing["resp"].text
    assert _messages(consult_id)[-1] == "空腹血糖 11，要紧吗？"   # 先追加、后结束：医生结束时这条已在会话里
    assert _status(consult_id) == "closed"


def test_没有竞争时照常复用开放会话_结束之后再问开新会话(client, admin, world):
    consult_id = _open_consult(client, world, "copd")
    again = client.post(f"{P}/consults", headers=world["resident"], json={"program_code": "copd", "content": "补充：咳嗽加重"})
    assert again.status_code == 201 and again.json() == {"consult_id": consult_id, "status": "open"}, again.text
    closed = client.post(f"{B}/consults/{consult_id}/close", headers=admin)
    assert closed.status_code == 200 and closed.json() == {"id": consult_id, "status": "closed"}, closed.text
    twice = client.post(f"{B}/consults/{consult_id}/close", headers=admin)
    assert twice.status_code == 409 and twice.json() == {"detail": "该咨询会话已关闭"}, twice.text
    after = client.post(f"{P}/consults", headers=world["resident"], json={"program_code": "copd", "content": "又喘了"})
    assert after.status_code == 201 and after.json()["consult_id"] != consult_id, after.text
