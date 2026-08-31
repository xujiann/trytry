"""PII 检索索引的主数据完整性：轮换宽限期检索不失明 + 索引丢失可修回。

守的是两条**已复现的静默重复主数据**路径（都不报错、不留日志，只表现为
"这人没建过档"，于是重复建档、重复开户）：

1. **轮换宽限期索引单口径**——`MEDPLAT_SECRET` 换新、`SECRET_PREVIOUS` 设旧、
   回填尚未跑：解密有 previous 回退，检索索引却只按当前钥算，等值检索命中 0。
   两道唯一约束此时同时失效（明文列的因随机 nonce 永不冲突，idx 部分唯一索引
   的因新钥算出新值也不冲突），于是同一身份证号能建出第二条档案。
2. **索引丢失/算错后修不回来**——回填脚本见到 `pii1$` 前缀就跳过、连索引也不
   重算；迁移的索引回填带 `NOT LIKE 'pii1$%'`，密文库重跑迁移会把密文行整片
   跳过。两者叠加的结果是 EMPI 去重**永久**失效。

每条用例都做过"改回旧写法即变红"的非空洞验证（见提交说明）。
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app import jobs
from app.config import DEFAULT_SECRET, settings
from app.database import SessionLocal, engine
from app.models import Patient, ResidentAccount, SmsCode
from app.pii import PII_PREFIX, pii_filter, pii_index
from app.routers.portal import _account_by, _reset_portal_failures

SERVER_DIR = Path(__file__).resolve().parent.parent
#: 测试环境的当前密钥当作"旧钥"——这样 admin 令牌在轮换前后都验得过
#: （令牌签名走 security.verification_keys 的 previous 回退），
#: 用例聚焦的是检索索引口径，不是令牌口径。
OLD_SECRET = settings.secret
NEW_SECRET = "rotation-new-secret-for-pii-index-test"


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "pii_encryption_enabled", True)
    yield
    monkeypatch.setattr(settings, "pii_encryption_enabled", False)


@pytest.fixture(autouse=True)
def clean_portal_state():
    _reset_portal_failures()
    yield
    _reset_portal_failures()


def _raw(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _rotate(monkeypatch, *, old: str = OLD_SECRET, new: str = NEW_SECRET) -> None:
    """进入轮换宽限期：当前钥换新、旧钥留在 secret_previous、回填尚未跑。"""
    monkeypatch.setattr(settings, "secret", new)
    monkeypatch.setattr(settings, "secret_previous", old)


def _send_code(client, phone: str, purpose: str = "login") -> str:
    with SessionLocal() as db:  # 清冷却，允许连续下发
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    resp = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": purpose})
    assert resp.status_code == 200, resp.text
    return resp.json()["debug_code"]


# ------------------------------------------------ 阻断1：轮换宽限期不得重复建档

def test_轮换宽限期_同证件号建档仍幂等命中同一条(client, admin, enabled, monkeypatch):
    id_card = "330782199101010011"
    first = client.post(
        "/api/patients", json={"name": "轮换幂等", "id_card": id_card}, headers=admin
    ).json()
    # 换钥：新 secret + previous=旧，回填脚本尚未跑（索引仍是旧钥算的）
    _rotate(monkeypatch)
    again = client.post(
        "/api/patients", json={"name": "轮换幂等", "id_card": id_card}, headers=admin
    ).json()
    assert again["ehc_no"] == first["ehc_no"], "宽限期内重复建档：主数据被静默复制"
    assert again["id"] == first["id"]
    with SessionLocal() as db:
        assert db.query(Patient).filter(Patient.name == "轮换幂等").count() == 1


def test_轮换宽限期_全值证件号检索仍命中(client, admin, enabled, monkeypatch):
    id_card = "330782199101010022"
    client.post("/api/patients", json={"name": "轮换检索", "id_card": id_card}, headers=admin)
    _rotate(monkeypatch)
    hits = client.get("/api/patients", params={"keyword": id_card}, headers=admin).json()
    assert [p["name"] for p in hits] == ["轮换检索"], "宽限期内按全值证件号检索失明"


def test_轮换宽限期_居民账户不重复开户(client, admin, enabled, monkeypatch):
    phone = "13900110022"
    client.post(
        "/api/patients", json={"name": "轮换居民", "id_card": "330782199101010033",
                               "phone": phone}, headers=admin
    )
    code = _send_code(client, phone)
    first = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    assert first["bound"] is True
    with SessionLocal() as db:
        before = db.query(ResidentAccount).count()
    # 换钥后再来一次：命中的必须还是同一个账户（而不是开第二户、绑不上本人档案）
    _rotate(monkeypatch)
    with SessionLocal() as db:
        account = _account_by(
            db, pii_filter(ResidentAccount.phone_idx, ResidentAccount.phone, phone), phone=phone
        )
        assert db.query(ResidentAccount).count() == before, "宽限期内重复开户"
        assert account.patient_id is not None, "新开的户绑不上本人档案（bound=False）"


def test_轮换宽限期_写入侧只用当前钥_旧钥索引只读不写(client, admin, enabled, monkeypatch):
    """多口径只在**读**侧：写入落的仍是当前钥索引，回填跑完即收敛为单口径。"""
    id_card = "330782199101010044"
    _rotate(monkeypatch)
    created = client.post(
        "/api/patients", json={"name": "轮换新写", "id_card": id_card}, headers=admin
    ).json()
    row = _raw("SELECT id_card_idx FROM patients WHERE id = :i", i=created["id"])[0]
    assert row.id_card_idx == pii_index(id_card)  # 当前（新）钥
    assert row.id_card_idx != pii_index(id_card, OLD_SECRET)
    # 收尾：这条是用轮换后的新钥加密的，留在模块级测试库里会让后续的全表回填/
    # 自检用例遇到一条解不开的密文，删掉它
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM patients WHERE id = :i"), {"i": created["id"]})


def test_未配previous时检索条件与原来逐字节一致(enabled, monkeypatch):
    """没在轮换（绝大多数部署的常态）就不该多出任何 SQL 分支。"""
    monkeypatch.setattr(settings, "secret_previous", "")
    condition = str(pii_filter(Patient.id_card_idx, Patient.id_card, "330782199101010055"))
    assert condition == str(Patient.id_card_idx == pii_index("330782199101010055"))
    assert " IN " not in condition.upper()


# ------------------------------------------ 阻断2：索引丢失后必须能用脚本修回

def _backfill_module():
    sys.path.insert(0, str(SERVER_DIR))
    return importlib.import_module("scripts.pii_encrypt_backfill")


def _run(**kwargs):
    backfill = _backfill_module()
    runner = backfill.rebuild_index_column if kwargs.pop("rebuild", False) else (
        backfill.backfill_column
    )
    return {
        f"{t}.{c}": runner(t, c, i, **kwargs) for t, c, i in backfill.TARGETS
    }


def test_索引全NULL的密文库_加密回填修不回_rebuild_index能修回(client, admin, enabled):
    """复现路径A的收口：迁移重建索引跳过密文行 → 索引一片 NULL。"""
    id_card = "330782199102020011"
    created = client.post(
        "/api/patients", json={"name": "索引丢失", "id_card": id_card}, headers=admin
    ).json()
    _run()  # 存量改写为密文
    with engine.begin() as conn:  # 模拟索引列被重建/回滚掉
        conn.execute(
            text("UPDATE patients SET id_card_idx = NULL WHERE id = :i"), {"i": created["id"]}
        )
    row = _raw("SELECT id_card, id_card_idx FROM patients WHERE id = :i", i=created["id"])[0]
    assert row.id_card.startswith(PII_PREFIX) and row.id_card_idx is None
    # 加密模式修不回来：见到 pii1$ 前缀就跳过，连索引也不重算
    assert _run()["patients.id_card"][0] == 0
    assert _raw(
        "SELECT id_card_idx FROM patients WHERE id = :i", i=created["id"]
    )[0].id_card_idx is None
    # --rebuild-index 能修回：解密后按当前钥重算，密文列不动
    ciphertext_before = row.id_card
    rebuilt, _, failed = _run(rebuild=True)["patients.id_card"]
    assert rebuilt >= 1 and failed == 0
    after = _raw("SELECT id_card, id_card_idx FROM patients WHERE id = :i", i=created["id"])[0]
    assert after.id_card_idx == pii_index(id_card)
    assert after.id_card == ciphertext_before, "--rebuild-index 不得改写密文列"
    # 修回后检索命中、重复建档重新幂等
    hits = client.get("/api/patients", params={"keyword": id_card}, headers=admin).json()
    assert [p["id"] for p in hits] == [created["id"]]
    again = client.post(
        "/api/patients", json={"name": "索引丢失", "id_card": id_card}, headers=admin
    ).json()
    assert again["id"] == created["id"]


def test_rebuild_index_幂等且dry_run不落库(client, admin, enabled):
    id_card = "330782199102020022"
    created = client.post(
        "/api/patients", json={"name": "索引幂等", "id_card": id_card}, headers=admin
    ).json()
    _run()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE patients SET id_card_idx = 'bogus' WHERE id = :i"), {"i": created["id"]}
        )
    _run(rebuild=True, dry_run=True)  # dry-run 只统计
    assert _raw(
        "SELECT id_card_idx FROM patients WHERE id = :i", i=created["id"]
    )[0].id_card_idx == "bogus"
    _run(rebuild=True)
    assert _raw(
        "SELECT id_card_idx FROM patients WHERE id = :i", i=created["id"]
    )[0].id_card_idx == pii_index(id_card)
    for name, (rebuilt, _, failed) in _run(rebuild=True).items():  # 再跑一遍：零改写
        assert rebuilt == 0 and failed == 0, name


def test_rebuild_index_可用old_secret解开旧钥密文(client, admin, enabled, monkeypatch):
    """轮换后只想修索引、暂不重加密：旧钥从命令行传进来即可。"""
    id_card = "330782199102020033"
    created = client.post(
        "/api/patients", json={"name": "旧钥索引", "id_card": id_card}, headers=admin
    ).json()
    _run()  # 用旧钥加密并算索引
    monkeypatch.setattr(settings, "secret", NEW_SECRET)  # 换钥，且没配 previous
    monkeypatch.setattr(settings, "secret_previous", "")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE patients SET id_card_idx = NULL WHERE id = :i"), {"i": created["id"]}
        )
    rebuilt, _, failed = _run(rebuild=True, old_secret=OLD_SECRET)["patients.id_card"]
    assert rebuilt >= 1 and failed == 0
    assert _raw(
        "SELECT id_card_idx FROM patients WHERE id = :i", i=created["id"]
    )[0].id_card_idx == pii_index(id_card)  # 按当前（新）钥重算
    # 收尾：把索引改回当前（测试环境）密钥口径，免得污染后续用例的自检
    monkeypatch.setattr(settings, "secret", OLD_SECRET)
    _run(rebuild=True)


# ------------------------------------------------ 阻断2：默认密钥跑迁移必须被拒

def test_迁移回填_默认密钥且有待回填行时拒跑():
    spec = importlib.util.spec_from_file_location(
        "_e3_migration",
        SERVER_DIR / "alembic" / "versions" / "a4b5c6d7e8f9_PII列加密_检索索引列与回填.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    original = settings.secret
    try:
        settings.secret = DEFAULT_SECRET
        with pytest.raises(RuntimeError) as err:
            migration._assert_real_secret("patients", "id_card", 10)
        assert "MEDPLAT_SECRET" in str(err.value)
        assert "--rebuild-index" in str(err.value)  # 报错里带修法
        # 逃生阀：本地开发库确实就用默认 secret
        os.environ[migration._ALLOW_DEFAULT_SECRET_ENV] = "1"
        try:
            migration._assert_real_secret("patients", "id_card", 10)
        finally:
            os.environ.pop(migration._ALLOW_DEFAULT_SECRET_ENV, None)
        # 真实密钥：照常放行
        settings.secret = "a-real-deployment-secret-value-0001"
        migration._assert_real_secret("patients", "id_card", 10)
    finally:
        settings.secret = original


def test_迁移回填_空库不受守卫影响(tmp_path):
    """守卫只在**真有行要回填**时才拦——全新部署/CI 一行都不写，不该被误伤。"""
    db_path = tmp_path / "fresh.db"
    env = dict(os.environ)
    env["MEDPLAT_DATABASE_URL"] = f"sqlite:///{db_path}"
    env.pop("MEDPLAT_SECRET", None)  # 默认密钥
    env.pop("MEDPLAT_ALLOW_DEFAULT_SECRET_PII_BACKFILL", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]


# ------------------------------------------------------------ 索引覆盖率自检

def test_自检_密文行索引为空时告警(client, admin, enabled):
    id_card = "330782199103030011"
    created = client.post(
        "/api/patients", json={"name": "自检对象", "id_card": id_card}, headers=admin
    ).json()
    _run()
    with SessionLocal() as db:
        assert jobs.check_pii_index_health(db) == []
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE patients SET id_card_idx = NULL WHERE id = :i"), {"i": created["id"]}
        )
    with SessionLocal() as db:
        problems = jobs.check_pii_index_health(db)
    assert any("密文但检索索引为空" in p for p in problems), problems
    _run(rebuild=True)
    with SessionLocal() as db:
        assert jobs.check_pii_index_health(db) == []


def test_自检_索引算错时抽样解密能发现(client, admin, enabled):
    """路径B（默认密钥跑迁移）留下的现场：索引非空但全错，只有重算才看得出来。"""
    id_card = "330782199103030022"
    created = client.post(
        "/api/patients", json={"name": "算错索引", "id_card": id_card}, headers=admin
    ).json()
    _run()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE patients SET id_card_idx = :x WHERE id = :i"),
            {"x": pii_index(id_card, "some-other-secret-entirely"), "i": created["id"]},
        )
    with SessionLocal() as db:
        problems = jobs.check_pii_index_health(db)
    assert any("索引与重算值不符" in p for p in problems), problems
    _run(rebuild=True)
    with SessionLocal() as db:
        assert jobs.check_pii_index_health(db) == []


def test_自检_轮换宽限期内旧钥索引不误报(client, admin, enabled, monkeypatch):
    """宽限期内旧钥索引是**正常状态**（pii_filter 已双口径检索），不该天天告警。"""
    id_card = "330782199103030033"
    client.post("/api/patients", json={"name": "宽限自检", "id_card": id_card}, headers=admin)
    _run()
    _rotate(monkeypatch)
    with SessionLocal() as db:
        assert jobs.check_pii_index_health(db) == []


def test_自检任务已注册且健康时返回0(client, admin):
    from app.scheduler import REGISTRY

    assert "pii_index_health" in REGISTRY
    with SessionLocal() as db:
        count, summary = jobs.pii_index_health_scan(db)
    assert count == 0 and "正常" in summary


# --------------------------------------------------------- seed_bulk 索引不得留空

def test_seed_bulk灌入的患者索引非空(tmp_path):
    """`bulk_insert_mappings` 绕过 mapper 事件——压测库索引全 NULL 会让"检索很快"
    这个容量结论失真（开态等值检索命中恒为 0）。"""
    db_path = tmp_path / "bulk.db"
    env = dict(os.environ)
    env["MEDPLAT_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["MEDPLAT_UPLOAD_DIR"] = str(tmp_path / "uploads")
    boot = subprocess.run(
        [sys.executable, "-c",
         "from fastapi.testclient import TestClient\nfrom app.main import app\n"
         "with TestClient(app):\n    pass"],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True,
    )
    assert boot.returncode == 0, boot.stderr[-2000:]
    seeded = subprocess.run(
        [sys.executable, "scripts/seed_bulk.py", "--patients", "20"],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True,
    )
    assert seeded.returncode == 0, seeded.stderr[-2000:]
    from sqlalchemy import create_engine

    bulk_engine = create_engine(f"sqlite:///{db_path}")
    with bulk_engine.connect() as conn:
        total, indexed = conn.execute(
            text(
                "SELECT count(*), count(id_card_idx) FROM patients WHERE ehc_no LIKE 'SIM-%'"
            )
        ).one()
    bulk_engine.dispose()
    assert total == 20
    assert indexed == total, "seed_bulk 灌入的患者 id_card_idx 留空"
