"""可见性判定缓存（生产整改 A4）。

`patient_basis` 对几十张关系表逐一发 EXISTS 查询，是患者维度接口的公共开销。
缓存口径（与 app/visibility.py 内注释一致，这里逐条钉死）：

1. 命中只省**判定查询**，留痕语义不变：`assert_patient_visible` 每次照常写 AccessLog；
2. 只缓存"允许"结论，拒绝不缓存——授权**放宽立即生效**、收窄最迟 TTL 秒生效；
3. TTL=0 关闭缓存；
4. 有容量上限（LRU），防长期运行内存膨胀。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app import visibility
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import AccessLog, Encounter, Organization, Patient, User
from app.visibility import (
    _BasisCache,
    assert_patient_visible,
    clear_visibility_cache,
    patient_basis,
)
from fastapi import HTTPException


@pytest.fixture(scope="module")
def client():
    reset_database()
    clear_visibility_cache()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    db = SessionLocal()
    try:
        a = Organization(name="缓存甲医院", org_type="lead_hospital", level="county")
        b = Organization(name="缓存乙卫生院", org_type="township", level="township")
        db.add_all([a, b])
        db.flush()
        doc_a = User(username="vc_doc_a", full_name="缓存甲医生", role="doctor",
                     org_id=a.id, password_hash="x")
        doc_b = User(username="vc_doc_b", full_name="缓存乙医生", role="doctor",
                     org_id=b.id, password_hash="x")
        db.add_all([doc_a, doc_b])
        patient = Patient(ehc_no="EHC-VC-0001", name="缓存患者", id_card="330382199202024321")
        db.add(patient)
        db.flush()
        # 患者只与甲医院有就诊关系
        db.add(Encounter(patient_id=patient.id, org_id=a.id, doctor_name="缓存甲医生"))
        db.commit()
        return {"a": a.id, "b": b.id, "doc_a": doc_a.id, "doc_b": doc_b.id,
                "patient": patient.id}
    finally:
        db.close()


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def count_judgments(monkeypatch):
    """给判定本体（_patient_basis_uncached）套计数器：命中缓存则计数不动。"""
    counter = {"calls": 0}
    real = visibility._patient_basis_uncached

    def counting(*args, **kwargs):
        counter["calls"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(visibility, "_patient_basis_uncached", counting)
    return counter


def _access_log_count(db, user_id, patient_id) -> int:
    return (
        db.query(AccessLog)
        .filter(AccessLog.user_id == user_id, AccessLog.patient_id == patient_id)
        .count()
    )


def test_cache_hit_skips_judgment_but_still_logs_every_access(
    client, world, db, count_judgments, monkeypatch
):
    """命中缓存：判定查询只做一次，但每次调阅都照常落一条 AccessLog。"""
    monkeypatch.setattr(settings, "visibility_cache_ttl_seconds", 30)
    clear_visibility_cache()
    doc_a = db.get(User, world["doc_a"])
    before = _access_log_count(db, doc_a.id, world["patient"])

    assert assert_patient_visible(db, doc_a, world["patient"], resource="t1") == "encounter"
    assert count_judgments["calls"] == 1
    assert assert_patient_visible(db, doc_a, world["patient"], resource="t2") == "encounter"
    assert assert_patient_visible(db, doc_a, world["patient"], resource="t3") == "encounter"
    # 第二、三次命中缓存：判定本体没有再被调用
    assert count_judgments["calls"] == 1
    # 留痕语义不变：三次调阅 = 三条留痕，缓存只省判定不省记账
    assert _access_log_count(db, doc_a.id, world["patient"]) == before + 3


def test_ttl_zero_disables_cache(client, world, db, count_judgments, monkeypatch):
    monkeypatch.setattr(settings, "visibility_cache_ttl_seconds", 0)
    clear_visibility_cache()
    doc_a = db.get(User, world["doc_a"])
    assert patient_basis(db, doc_a, world["patient"]) == "encounter"
    assert patient_basis(db, doc_a, world["patient"]) == "encounter"
    assert count_judgments["calls"] == 2, "TTL=0 时必须每次都真判定"


def test_denial_not_cached_grant_takes_effect_immediately(
    client, world, db, monkeypatch
):
    """拒绝不缓存：无关系→403；建立关系后**立即**放行，不等 TTL。"""
    monkeypatch.setattr(settings, "visibility_cache_ttl_seconds", 30)
    clear_visibility_cache()
    doc_b = db.get(User, world["doc_b"])
    for _ in range(2):  # 连拒两次，确认拒绝结论没有被第一次调用缓存住
        with pytest.raises(HTTPException) as exc:
            assert_patient_visible(db, doc_b, world["patient"], resource="deny")
        assert exc.value.status_code == 403
    # 建立乙机构的就诊关系后，下一次调用立刻放行
    db.add(Encounter(patient_id=world["patient"], org_id=world["b"], doctor_name="缓存乙医生"))
    db.commit()
    try:
        assert (
            assert_patient_visible(db, doc_b, world["patient"], resource="grant")
            == "encounter"
        )
    finally:
        db.query(Encounter).filter(
            Encounter.patient_id == world["patient"], Encounter.org_id == world["b"]
        ).delete()
        db.commit()
        clear_visibility_cache()


def test_cached_allow_survives_until_cleared(client, world, db, monkeypatch):
    """收窄最迟 TTL 秒生效：撤掉关系后缓存的允许结论仍活着，清缓存后立即 403。

    这正是"只缓存允许"的代价侧，用测试把口径钉住，防止将来有人顺手把
    拒绝也缓存进去（那会把"放宽立即生效"一起破坏掉）。
    """
    monkeypatch.setattr(settings, "visibility_cache_ttl_seconds", 30)
    clear_visibility_cache()
    doc_b = db.get(User, world["doc_b"])
    enc = Encounter(patient_id=world["patient"], org_id=world["b"], doctor_name="缓存乙医生")
    db.add(enc)
    db.commit()
    assert patient_basis(db, doc_b, world["patient"]) == "encounter"  # 允许进缓存
    db.delete(enc)
    db.commit()
    # 关系已撤，但 TTL 内缓存的允许结论仍生效（最迟 TTL 秒收窄）
    assert patient_basis(db, doc_b, world["patient"]) == "encounter"
    clear_visibility_cache()
    assert patient_basis(db, doc_b, world["patient"]) is None


def test_basis_cache_lru_cap():
    cache = _BasisCache(max_entries=2)
    cache.put((1, 1), "encounter")
    cache.put((2, 2), "contract")
    cache.get((1, 1), ttl=60)  # 触碰 (1,1)，使 (2,2) 成为最久未用
    cache.put((3, 3), "service")
    assert cache.get((1, 1), ttl=60) == "encounter"
    assert cache.get((2, 2), ttl=60) is None, "超上限时应淘汰最久未用条目"
    assert cache.get((3, 3), ttl=60) == "service"


def test_basis_cache_ttl_expiry(monkeypatch):
    cache = _BasisCache()
    now = {"t": 1000.0}
    monkeypatch.setattr(visibility.time, "monotonic", lambda: now["t"])
    cache.put((1, 1), "encounter")
    now["t"] = 1029.0
    assert cache.get((1, 1), ttl=30) == "encounter"
    now["t"] = 1030.0
    assert cache.get((1, 1), ttl=30) is None, "到 TTL 必须过期"
