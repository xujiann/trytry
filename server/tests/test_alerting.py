"""主动告警通道（工程包 P2）：send_alert 行为与三处接入点。

覆盖：webhook 真的收到 payload、同 kind 冷却、不同 kind 不互相占用冷却、
关闭态零外呼、外呼失败不抛；接入点：任务失败（scheduler.run_job）、
归档任务异常（jobs.access_log_archive / audit_archive）、预警广播无人在线
兜底（jobs._alert）。
"""
import pytest

from conftest import reset_database

import app.alerting as alerting
import app.jobs as jobs_mod
import app.scheduler as scheduler_mod
from app.alerting import send_alert
from app.config import settings
from app.database import SessionLocal


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    alerting.reset_cooldowns()
    yield
    alerting.reset_cooldowns()


@pytest.fixture()
def webhook(monkeypatch):
    """开启告警通道并捕获外呼。"""
    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})

    monkeypatch.setattr(settings, "alert_webhook_url", "http://alert.example/hook")
    monkeypatch.setattr(alerting.httpx, "post", fake_post)
    return calls


def test_send_alert_posts_payload(webhook):
    assert send_alert("job_failed:t1", "任务 t1 挂了") is True
    assert len(webhook) == 1
    call = webhook[0]
    assert call["url"] == "http://alert.example/hook"
    assert call["timeout"] == alerting.WEBHOOK_TIMEOUT_SECONDS
    assert call["json"]["kind"] == "job_failed:t1"
    assert call["json"]["message"] == "任务 t1 挂了"
    assert call["json"]["service"] == "medplat"
    assert call["json"]["at"]


def test_same_kind_cooldown_suppresses(webhook):
    assert send_alert("k1", "第一条") is True
    assert send_alert("k1", "冷却期内的第二条") is False
    assert len(webhook) == 1


def test_different_kind_not_suppressed(webhook):
    assert send_alert("k1", "a") is True
    assert send_alert("k2", "b") is True
    assert len(webhook) == 2


def test_cooldown_expiry_allows_resend(webhook, monkeypatch):
    assert send_alert("k1", "a") is True
    # 把冷却窗调成 0 秒等价于"已过期"
    monkeypatch.setattr(settings, "alert_cooldown_seconds", 0)
    assert send_alert("k1", "b") is True
    assert len(webhook) == 2


def test_disabled_means_zero_outcalls(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    monkeypatch.setattr(alerting.httpx, "post", lambda *a, **k: calls.append(1))
    assert send_alert("k1", "关闭态") is False
    assert calls == []


def test_post_failure_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("webhook down")

    monkeypatch.setattr(settings, "alert_webhook_url", "http://alert.example/hook")
    monkeypatch.setattr(alerting.httpx, "post", boom)
    assert send_alert("k1", "网关挂了") is False  # 只记日志，不抛


# ---------------------------------------------------------------------------
# 接入点
# ---------------------------------------------------------------------------


def test_run_job_failure_triggers_alert(monkeypatch):
    reset_database()
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler_mod, "send_alert", lambda k, m: sent.append((k, m)))

    name = "alerting_probe_failing_job"

    def failing(db):
        raise RuntimeError("探针故障")

    scheduler_mod.REGISTRY[name] = scheduler_mod.JobSpec(name, "告警探针任务", 3600, failing)
    try:
        db = SessionLocal()
        try:
            run = scheduler_mod.run_job(db, name, trigger="manual")
        finally:
            db.close()
    finally:
        scheduler_mod.REGISTRY.pop(name, None)
    assert run.status == "failed"
    assert len(sent) == 1
    kind, message = sent[0]
    assert kind == f"job_failed:{name}"
    assert "探针故障" in message


def test_archive_job_failure_triggers_alert(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(jobs_mod, "send_alert", lambda k, m: sent.append(k))
    monkeypatch.setattr(settings, "access_log_archive_days", 30)
    monkeypatch.setattr(settings, "audit_log_archive_days", 30)

    def boom(*a, **k):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(jobs_mod, "_archive_and_delete", boom)
    with pytest.raises(RuntimeError):  # 告警后照常上抛，run_job 仍记 failed
        jobs_mod.access_log_archive(None)
    with pytest.raises(RuntimeError):
        jobs_mod.audit_archive(None)
    assert sent == ["archive_failed:access_logs", "archive_failed:audit_logs"]


def test_scan_alert_falls_back_to_webhook_when_nobody_online(monkeypatch):
    """预警广播确定无人收到（broadcast 返回 False）时转发 webhook 摘要。"""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(jobs_mod, "send_alert", lambda k, m: sent.append((k, m)))
    monkeypatch.setattr(jobs_mod.manager, "broadcast", lambda *a, **k: False)
    jobs_mod._alert("chronic_overdue", "慢病随访超期", 5)
    assert sent == [("unattended:chronic_overdue", "慢病随访超期：5 条（无在线管理端，广播未送达）")]


def test_scan_alert_no_fallback_when_delivered(monkeypatch):
    sent: list = []
    monkeypatch.setattr(jobs_mod, "send_alert", lambda k, m: sent.append(k))
    monkeypatch.setattr(jobs_mod.manager, "broadcast", lambda *a, **k: True)
    jobs_mod._alert("chronic_overdue", "慢病随访超期", 5)
    assert sent == []


def test_scan_alert_zero_count_is_noop(monkeypatch):
    sent: list = []
    broadcasts: list = []
    monkeypatch.setattr(jobs_mod, "send_alert", lambda k, m: sent.append(k))
    monkeypatch.setattr(jobs_mod.manager, "broadcast", lambda *a, **k: broadcasts.append(1))
    jobs_mod._alert("chronic_overdue", "慢病随访超期", 0)
    assert sent == [] and broadcasts == []
