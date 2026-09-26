"""孕产妇档案一孕一册（P1-140）。

原先 `maternal_records.patient_id` 全量唯一，建册接口「查到这位妇女的任何一本就原样返回」：一位妇女一生只能建一本。
上一胎产后访视结案之后再孕，建册 201 拿回的是那本已结案的旧档案（末次月经、预产期、孕产次都是上一胎的），页面上
已结案的档案没有访视、分娩入口——这一胎的产检、分娩、高危标记无处可记，审方也认不出她在孕期。

修法：唯一性只约束「在册」（未结案）这一态——部分唯一索引 `uq_maternal_patient_open`（`status <> 'closed'`）；
建册只认在册的那本：孕期的原样返回（建册幂等照旧），已分娩未结案的 409（多半是再孕、上一胎漏了结案，把旧档案当
这一胎返回，新一胎的产检就记到上一胎名下），都没有才建新册。

本档钉三面：行为（再孕建新册 / 孕期重复建册幂等 / 已分娩未结案 409）；防拆卸（索引在模型上、在库里、绕开接口直插
拦得住、结案的不占名额）；迁移（存量一行不动；回退遇一人多本拒绝并指名，清掉之后回退复原全量唯一）。
"""
import itertools
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from app.database import engine
from app.models import Base

SERVER_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CARDS = itertools.count(1)


def _woman(client, admin, name):
    resp = client.post("/api/patients", headers=admin, json={
        "name": name, "id_card": f"33010619900303{next(_CARDS):04d}", "gender": "女", "birth_date": "1990-03-03"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _register(client, admin, patient_id, **extra):
    return client.post("/api/maternal/records", headers=admin, json={"patient_id": patient_id, **extra})


def _deliver_and_close(client, admin, record_id):
    visit = client.post(f"/api/maternal/records/{record_id}/visits", headers=admin, json={"visit_type": "postpartum"})
    assert visit.json()["status"] == "delivered", visit.text
    closed = client.post(f"/api/maternal/records/{record_id}/close", headers=admin)
    assert closed.json()["status"] == "closed", closed.text


# ================================================================ 行为


def test_上一胎结案后再孕_建册建出新的一本_不再拿回已结案的旧档案(client, admin):
    patient = _woman(client, admin, "一孕一册再孕")
    first = _register(client, admin, patient, lmp="2024-01-10", edc="2024-10-17")
    assert first.status_code == 201, first.text
    _deliver_and_close(client, admin, first.json()["id"])

    second = _register(client, admin, patient, lmp="2026-06-01", edc="2027-03-08", gravidity=2, parity=1)
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["id"] != first.json()["id"]   # 修前：同一个 id，status closed、预产期是上一胎的
    assert (body["status"], body["edc"], body["gravidity"], body["parity"]) == ("registered", "2027-03-08", 2, 1)

    # 这一胎的产检记得上（修前只能记到已结案的旧档案上，页面上连按钮都没有）
    visit = client.post(f"/api/maternal/records/{body['id']}/visits", headers=admin,
                        json={"visit_type": "prenatal", "gest_week": 12, "bp": "150/95"})
    assert visit.status_code == 201 and visit.json()["high_risk"] is True, visit.text

    rows = {r["id"]: r["status"] for r in client.get("/api/maternal/records", headers=admin).json()}
    assert (rows[first.json()["id"]], rows[body["id"]]) == ("closed", "registered")   # 上一胎的档案原样留着


def test_孕期重复建册仍拿回同一本(client, admin):
    """建册幂等照旧：双击、两台工作站同时建，都是这一本（并发抢输的一路由部分唯一索引兜底，同一条返回路径）。"""
    patient = _woman(client, admin, "一孕一册幂等")
    first = _register(client, admin, patient, edc="2027-01-01")
    again = _register(client, admin, patient, edc="2027-02-02")
    assert first.status_code == again.status_code == 201
    assert again.json()["id"] == first.json()["id"] and again.json()["edc"] == "2027-01-01"


def test_已分娩未结案又来建册_409说清楚先结案_不把旧档案当这一胎(client, admin):
    patient = _woman(client, admin, "一孕一册漏结案")
    record = _register(client, admin, patient).json()
    client.post(f"/api/maternal/records/{record['id']}/visits", headers=admin, json={"visit_type": "postpartum"})

    resp = _register(client, admin, patient, edc="2027-05-05")
    assert resp.status_code == 409   # 修前 201 拿回那本已分娩的旧档案
    assert resp.json()["detail"] == "该孕产妇上一孕次已分娩、档案尚未结案，请先完成产后访视并结案，再为本次妊娠建册"
    mine = [r["id"] for r in client.get("/api/maternal/records", headers=admin).json() if r["patient_id"] == patient]
    assert mine == [record["id"]]   # 没有多建一本
    # 结案之后即可为这一胎建册
    assert client.post(f"/api/maternal/records/{record['id']}/close", headers=admin).status_code == 200
    fresh = _register(client, admin, patient, edc="2027-05-05")
    assert fresh.status_code == 201 and fresh.json()["id"] != record["id"], fresh.text


def test_已结案的档案不再收访视_产检不会挂到上一胎名下(client, admin):
    """P2-124：分娩登记早就拦结案的档案，访视没拦——一孕一册之后，按上一胎的旧档案号记的产检会挂到上一胎名下。"""
    from app.database import SessionLocal
    from app.models import MaternalVisit

    patient = _woman(client, admin, "一孕一册结案不收访视")
    record = _register(client, admin, patient).json()
    _deliver_and_close(client, admin, record["id"])
    with SessionLocal() as db:
        before = db.query(MaternalVisit).filter_by(record_id=record["id"]).count()

    for visit_type in ("prenatal", "postpartum"):
        resp = client.post(f"/api/maternal/records/{record['id']}/visits", headers=admin,
                           json={"visit_type": visit_type, "gest_week": 20 if visit_type == "prenatal" else None,
                                 "bp": "150/95"})
        assert resp.status_code == 409, resp.text   # 修前 201，还把结案的档案标成高危
        assert resp.json()["detail"] == "档案已结案，不可记录访视"
    with SessionLocal() as db:
        assert db.query(MaternalVisit).filter_by(record_id=record["id"]).count() == before
    (row,) = [r for r in client.get("/api/maternal/records", headers=admin).json() if r["id"] == record["id"]]
    assert (row["status"], row["high_risk"]) == ("closed", False)


# ================================================================ 防拆卸


def test_在册部分唯一索引不许消失_也不许退回全量唯一():
    table = Base.metadata.tables["maternal_records"]
    index = next((i for i in table.indexes if i.name == "uq_maternal_patient_open"), None)
    assert index is not None, "maternal_records 的 uq_maternal_patient_open 没了——并发建册会建出两本在册档案"
    assert index.unique and [c.name for c in index.columns] == ["patient_id"]
    for dialect in ("sqlite", "postgresql"):
        where = str(index.dialect_options[dialect].get("where", ""))
        assert "status <> 'closed'" in where, f"{dialect} 侧丢了「只约束在册」的范围：结案后再孕又建不了册"
    full_uniques = [c for c in table.constraints
                    if c.__class__.__name__ == "UniqueConstraint" and [col.name for col in c.columns] == ["patient_id"]]
    assert not full_uniques, "patient_id 又成了全量唯一：一位妇女一生只能建一本（P1-140）"


def test_在册部分唯一索引真的建在库上():
    names = {i["name"] for i in sa_inspect(engine).get_indexes("maternal_records")}
    assert "uq_maternal_patient_open" in names, "maternal_records 上没有这条索引（库与模型对不上）"


def test_绕开接口直插_在册的第二本拦得住_结案的不占名额(client, admin):
    """索引「在不在」与「拦不拦得住」是两回事：直接写库——并发抢输的一路实际到达的位置——看库自己抬不抬手。"""
    from app.database import SessionLocal
    from app.models import MaternalRecord

    patient = _woman(client, admin, "一孕一册直插")
    with SessionLocal() as db:
        db.add_all([MaternalRecord(patient_id=patient, status="closed"),
                    MaternalRecord(patient_id=patient, status="closed"),
                    MaternalRecord(patient_id=patient, status="registered")])
        db.commit()   # 历次结案的多本 + 在册的一本：合法
        db.add(MaternalRecord(patient_id=patient, status="delivered"))
        with pytest.raises(IntegrityError):
            db.commit()   # 在册的第二本（哪怕一本孕期、一本已分娩）：库拦下
        db.rollback()


# ================================================================ 迁移


def _alembic(db_path, *args, expect_ok=True):
    env = {k: v for k, v in os.environ.items() if k != "MEDPLAT_DATABASE_URL"}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=SERVER_ROOT,
        env={**env, "MEDPLAT_DATABASE_URL": f"sqlite:///{db_path}"}, capture_output=True, text=True,
    )
    assert (proc.returncode == 0) == expect_ok, f"alembic {args} 退出码 {proc.returncode}：\n{proc.stdout[-1500:]}{proc.stderr[-3000:]}"
    return proc


def test_迁移存量不动_回退遇一人多本拒绝并指名_清掉后回退复原全量唯一(tmp_path):
    """子进程跑真迁移（理由同 test_migration_model_parity：别和测试会话共用的引擎相互污染）。"""
    db_path = tmp_path / "maternal.db"
    _alembic(db_path, "upgrade", "c3e4f5a6b7d9")   # 本迁移之前那一版：全量唯一

    conn = sqlite3.connect(db_path)   # 裸连接不开外键约束（同迁移本身），患者行不必造
    try:
        conn.execute("INSERT INTO maternal_records (id, patient_id, lmp, edc, gravidity, parity, high_risk, risk_factors, "
                     "status, created_at) VALUES (11, 7, '2024-01-10', '2024-10-17', 1, 0, 0, '', 'closed', '2024-02-01')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):   # 修前的库：结案之后再建一本就撞全量唯一
            conn.execute("INSERT INTO maternal_records (patient_id, lmp, edc, gravidity, parity, high_risk, risk_factors, "
                         "status, created_at) VALUES (7, '', '', 2, 1, 0, '', 'registered', '2026-06-01')")
        conn.rollback()
    finally:
        conn.close()

    _alembic(db_path, "upgrade", "9f6e90d1f6b5")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT id, patient_id, status, edc FROM maternal_records").fetchall() == [
            (11, 7, "closed", "2024-10-17")]   # 存量一行不动
        conn.execute("INSERT INTO maternal_records (id, patient_id, lmp, edc, gravidity, parity, high_risk, risk_factors, "
                     "status, created_at) VALUES (12, 7, '', '2027-03-08', 2, 1, 0, '', 'registered', '2026-06-01')")
        conn.commit()   # 升级之后：结案的上一胎 + 在册的这一胎
    finally:
        conn.close()

    refused = _alembic(db_path, "downgrade", "c3e4f5a6b7d9", expect_ok=False)
    assert "拒绝回退" in refused.stderr and "(7, 11), (7, 12)" in refused.stderr, refused.stderr[-1500:]
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT id FROM maternal_records ORDER BY id").fetchall() == [(11,), (12,)]   # 回退没动数据
        conn.execute("DELETE FROM maternal_records WHERE id = 12")   # 人工处置（本用例的库是一次性的）
        conn.commit()
    finally:
        conn.close()

    _alembic(db_path, "downgrade", "c3e4f5a6b7d9")
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):   # 全量唯一回来了
            conn.execute("INSERT INTO maternal_records (patient_id, lmp, edc, gravidity, parity, high_risk, risk_factors, "
                         "status, created_at) VALUES (7, '', '', 2, 1, 0, '', 'registered', '2026-06-01')")
    finally:
        conn.close()
