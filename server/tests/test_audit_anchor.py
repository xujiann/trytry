"""审计链外部锚点（P1-21）。

覆盖点：
- audit_anchor 任务把链尾写进 audit_anchors.jsonl，锚点文件自身成 MAC 链
  （逐行可用 anchor_mac 复算、prev_mac 逐行衔接）；
- webhook 外发：配置时 POST 锚点记录（monkeypatch httpx），失败仅 log 不炸，
  未配置/未过出网校验时零外呼；
- GET /api/audit/verify 锚点对账三态：一致 / 哈希不符 / 行已消失；
- **非空洞**：删掉库内锚点所指行及其后（末尾截断）再带锚点 verify 必须报异常
  ——这正是 P1-21 要抓的洞：不带锚点时截断后的链依旧"自洽"。
"""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app import jobs
from app.audit_chain import anchor_mac, anchor_mac_valid, audit_entry_hash
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import AuditLog


@pytest.fixture()
def archive_dir(tmp_path, monkeypatch):
    """锚点文件落临时目录：settings 是进程单例，monkeypatch 属性即可（自动还原）。"""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    return tmp_path / "archives"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    """模块起点重建库表：任务用例直连 SessionLocal，不能指望别的模块建过表。"""
    reset_database()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _seed_chain(count: int) -> list[tuple[int, str]]:
    """按真实链算法造一段连续审计链，返回 [(id, entry_hash), ...]。"""
    with SessionLocal() as db:
        db.query(AuditLog).delete()
        db.commit()
        prev = ""
        rows: list[tuple[int, str]] = []
        for i in range(count):
            entry_hash = audit_entry_hash(prev, f"u{i}", "POST", f"/api/t/{i}", 200)
            row = AuditLog(
                username=f"u{i}", method="POST", path=f"/api/t/{i}", status_code=200,
                prev_hash=prev, entry_hash=entry_hash,
            )
            db.add(row)
            db.commit()
            rows.append((row.id, entry_hash))
            prev = entry_hash
        return rows


def _anchor_lines(archive_dir) -> list[dict]:
    path = archive_dir / jobs.ANCHOR_FILENAME
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


# ================================================================ 锚点任务


def test_锚点任务写文件且自成MAC链(archive_dir):
    rows = _seed_chain(3)
    with SessionLocal() as db:
        affected, message = jobs.audit_anchor(db)
        assert affected == 1 and f"id={rows[-1][0]}" in message
        # 链尾推进后再锚一次：第二行要衔接第一行的 mac
        extra = audit_entry_hash(rows[-1][1], "u3", "POST", "/api/t/3", 200)
        db.add(AuditLog(username="u3", method="POST", path="/api/t/3", status_code=200,
                        prev_hash=rows[-1][1], entry_hash=extra))
        db.commit()
        jobs.audit_anchor(db)

    lines = _anchor_lines(archive_dir)
    assert len(lines) == 2
    first, second = lines
    # 内容：链尾 id / entry_hash / 全表行数
    assert first["tail_id"] == rows[-1][0]
    assert first["tail_entry_hash"] == rows[-1][1]
    assert first["total_rows"] == 3
    assert second["tail_id"] == rows[-1][0] + 1 and second["total_rows"] == 4
    # 自链：首行 prev_mac 为空串，次行衔接首行 mac；逐行 MAC 可复算
    assert first["prev_mac"] == ""
    assert second["prev_mac"] == first["mac"]
    for line in lines:
        assert line["mac"] == anchor_mac(line["prev_mac"], line)
        assert anchor_mac_valid(line["prev_mac"], line, line["mac"])
    # 篡改锚点内容（如把行数改小掩盖截断）MAC 即对不上
    tampered = {**first, "total_rows": 1}
    assert not anchor_mac_valid(tampered["prev_mac"], tampered, tampered["mac"])


def test_库内无链可锚时跳过不写文件(archive_dir):
    with SessionLocal() as db:
        db.query(AuditLog).delete()
        db.commit()
        affected, message = jobs.audit_anchor(db)
        assert affected == 0 and "无链可锚" in message
    assert not (archive_dir / jobs.ANCHOR_FILENAME).exists()


# ================================================================ webhook 外发


def test_配置webhook时外发锚点记录(archive_dir, monkeypatch):
    _seed_chain(2)
    calls = []
    monkeypatch.setattr(settings, "audit_anchor_webhook_url", "https://anchor.example/hook")
    monkeypatch.setattr(jobs, "egress_url_allowed", lambda url, label: True)
    monkeypatch.setattr(jobs.httpx, "post", lambda url, json, timeout: calls.append((url, json, timeout)))
    with SessionLocal() as db:
        _, message = jobs.audit_anchor(db)
    assert "已外发" in message
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://anchor.example/hook"
    assert timeout == jobs.ANCHOR_WEBHOOK_TIMEOUT_SECONDS
    # 外发的就是落盘的那条锚点（含 mac，可与本地文件对账）
    assert payload == _anchor_lines(archive_dir)[-1]


def test_webhook外发失败仅log_本地锚点已落盘(archive_dir, monkeypatch, caplog):
    _seed_chain(2)

    def boom(url, json, timeout):
        raise ConnectionError("网关不可达")

    monkeypatch.setattr(settings, "audit_anchor_webhook_url", "https://anchor.example/hook")
    monkeypatch.setattr(jobs, "egress_url_allowed", lambda url, label: True)
    monkeypatch.setattr(jobs.httpx, "post", boom)
    with SessionLocal() as db:
        affected, message = jobs.audit_anchor(db)
    assert affected == 1 and "外发失败" in message
    assert len(_anchor_lines(archive_dir)) == 1, "外发失败不影响本地锚点落盘"


def test_未配置webhook零外呼(archive_dir, monkeypatch):
    _seed_chain(2)
    monkeypatch.setattr(settings, "audit_anchor_webhook_url", "")
    monkeypatch.setattr(jobs.httpx, "post", lambda *a, **k: pytest.fail("不该外呼"))
    with SessionLocal() as db:
        affected, message = jobs.audit_anchor(db)
    assert affected == 1 and "外发" not in message


def test_webhook未过出网校验不外发(archive_dir, monkeypatch):
    """SSRF 防线：锚点 webhook 与支付/短信网关同一出网校验口径。"""
    _seed_chain(2)
    monkeypatch.setattr(settings, "audit_anchor_webhook_url", "https://127.0.0.1/hook")
    monkeypatch.setattr(jobs.httpx, "post", lambda *a, **k: pytest.fail("不该外呼"))
    with SessionLocal() as db:
        affected, message = jobs.audit_anchor(db)
    assert affected == 1 and "未过出网校验" in message


def test_锚点任务已注册进调度器():
    from app.scheduler import REGISTRY

    assert "audit_anchor" in REGISTRY


# ================================================================ verify 锚点对账


def test_锚点对账一致(client, admin):
    rows = _seed_chain(5)
    tail_id, tail_hash = rows[-1]
    body = client.get(
        f"/api/audit/verify?anchor_id={tail_id}&anchor_hash={tail_hash}", headers=admin
    ).json()
    assert body["anchor_match"] is True and body["valid"] is True
    assert body["anchor_id"] == tail_id and body["anchor_reason"] == ""


def test_锚点之后链连续才算续接_中途篡改仍报异常(client, admin):
    """锚点行没动、但其后有行被改：anchor_match 真、整体 valid 假。"""
    rows = _seed_chain(5)
    anchor_id, anchor_hash = rows[1]
    with SessionLocal() as db:
        row = db.get(AuditLog, rows[3][0])
        row.path = "/api/被篡改"
        db.commit()
    body = client.get(
        f"/api/audit/verify?anchor_id={anchor_id}&anchor_hash={anchor_hash}", headers=admin
    ).json()
    assert body["anchor_match"] is True
    assert body["valid"] is False and body["broken_at"] == rows[3][0]


def test_锚点哈希不符报改动(client, admin):
    rows = _seed_chain(5)
    tail_id = rows[-1][0]
    body = client.get(
        f"/api/audit/verify?anchor_id={tail_id}&anchor_hash={'0' * 64}", headers=admin
    ).json()
    assert body["anchor_match"] is False and body["valid"] is False
    assert "不符" in body["anchor_reason"]


def test_末尾截断后带锚点对账必报异常(client, admin):
    """非空洞核心：删掉锚点所指行及其后（正是"末尾截断"）——

    不带锚点时剩余链依旧自洽（valid=True），这正是 P1-21 登记的盲区；
    带外部锚点对账则必须报"行不存在=疑似末尾截断"。
    """
    rows = _seed_chain(6)
    tail_id, tail_hash = rows[-1]
    with SessionLocal() as db:
        db.query(AuditLog).filter(AuditLog.id >= rows[4][0]).delete(synchronize_session=False)
        db.commit()
    # 盲区如实存在：不带锚点看不出截断
    assert client.get("/api/audit/verify", headers=admin).json()["valid"] is True
    # 带锚点即暴露
    body = client.get(
        f"/api/audit/verify?anchor_id={tail_id}&anchor_hash={tail_hash}", headers=admin
    ).json()
    assert body["anchor_match"] is False and body["valid"] is False
    assert "截断" in body["anchor_reason"]


def test_不带锚点时响应字节不变(client, admin):
    """向后兼容（CLAUDE.md 第 7 条）：新增字段仅在对账时出现。"""
    _seed_chain(3)
    body = client.get("/api/audit/verify", headers=admin).json()
    assert body["valid"] is True
    assert not any(k.startswith("anchor_") for k in body)


def test_锚点入参必须成对(client, admin):
    _seed_chain(1)
    resp = client.get("/api/audit/verify?anchor_id=1", headers=admin)
    assert resp.status_code == 422
    assert "成对" in resp.json()["detail"]
