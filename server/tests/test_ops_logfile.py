"""日志文件轮转（A10）：MEDPLAT_LOG_FILE 非空时请求日志同时落轮转文件。

settings 是进程内 lru_cache 单例、logger 在 import 时已初始化——测试里
monkeypatch settings 属性 + 清空 handler 后重跑初始化函数来验证。
"""
import json
import logging
from logging.handlers import RotatingFileHandler

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

import app.main as main_mod
from app.config import settings


@pytest.fixture()
def reinit_logger(monkeypatch, tmp_path):
    """清空 medplat.access 的 handler，按临时 log_file 重新初始化；用毕还原。"""
    log_path = tmp_path / "logs" / "access.log"
    monkeypatch.setattr(settings, "log_file", str(log_path))
    logger = logging.getLogger("medplat.access")
    old_handlers = logger.handlers[:]
    logger.handlers = []
    main_mod._configure_access_logger()
    yield logger, log_path
    for h in logger.handlers:
        if h not in old_handlers:
            h.close()
    logger.handlers = old_handlers


def test_log_file_handler_attached_with_settings(reinit_logger):
    logger, _ = reinit_logger
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    fh = file_handlers[0]
    assert fh.maxBytes == settings.log_rotate_max_mb * 1024 * 1024
    assert fh.backupCount == settings.log_rotate_backups
    # stdout 输出保持不变：文件 handler 之外仍有 StreamHandler
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in logger.handlers
    )


def test_request_writes_json_line_to_file(reinit_logger):
    reset_database()
    _, log_path = reinit_logger
    with TestClient(main_mod.app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
    for h in logging.getLogger("medplat.access").handlers:
        h.flush()
    assert log_path.exists(), "目录不存在时应被自动创建并写入"
    lines = [json.loads(x) for x in log_path.read_text("utf-8").splitlines() if x]
    health = [r for r in lines if r.get("path") == "/api/health"]
    assert health, f"日志文件里应有 /api/health 的 JSON 行，实际：{lines}"
    rec = health[-1]
    assert rec["method"] == "GET" and rec["status"] == 200
    assert "request_id" in rec and "duration_ms" in rec


def test_no_file_handler_when_log_file_empty(monkeypatch):
    monkeypatch.setattr(settings, "log_file", "")
    logger = logging.getLogger("medplat.access")
    old_handlers = logger.handlers[:]
    logger.handlers = []
    try:
        main_mod._configure_access_logger()
        assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
        assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    finally:
        for h in logger.handlers:
            if h not in old_handlers:
                h.close()
        logger.handlers = old_handlers
