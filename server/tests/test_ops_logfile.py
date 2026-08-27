"""日志文件轮转（A10）：MEDPLAT_LOG_FILE 非空时**全部 medplat.* 日志**落轮转文件。

settings 是进程内 lru_cache 单例、logger 在 import 时已初始化——测试里
monkeypatch settings 属性 + 清空 handler 后重跑初始化函数来验证。

handler 挂在 `medplat` 父 logger 上（不是 `medplat.access`）：原实现只配了 access
一个，另外 17 个 logger 一路 propagate 到没配 handler 的 root，落进
`logging.lastResort`——**INFO 整个丢掉、ERROR 只去 stderr**。运维配了
MEDPLAT_LOG_FILE 以为拿到 6 个月留存，实际只留下访问日志，
而"审计写失败、本条审计丢失"这种必须留存的记录恰好在被丢掉的那一半里。
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
    """清空 medplat 的 handler，按临时 log_file 重新初始化；用毕还原。"""
    log_path = tmp_path / "logs" / "access.log"
    monkeypatch.setattr(settings, "log_file", str(log_path))
    logger = logging.getLogger("medplat")
    old_handlers = logger.handlers[:]
    logger.handlers = []
    main_mod._configure_logging()
    yield logger, log_path
    for h in logger.handlers:
        if h not in old_handlers:
            h.close()
    logger.handlers = old_handlers


def _flush():
    for h in logging.getLogger("medplat").handlers:
        h.flush()


def _lines(log_path) -> list[dict]:
    return [json.loads(x) for x in log_path.read_text("utf-8").splitlines() if x]


def test_log_file_handler_attached_with_settings(reinit_logger):
    logger, _ = reinit_logger
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1, (
        "同一个文件只该有一个轮转 handler——两个各自翻滚会互相改名对方的文件，轮转一次丢一段"
    )
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
    _flush()
    assert log_path.exists(), "目录不存在时应被自动创建并写入"
    health = [r for r in _lines(log_path) if r.get("path") == "/api/health"]
    assert health, f"日志文件里应有 /api/health 的 JSON 行，实际：{_lines(log_path)}"
    rec = health[-1]
    assert rec["method"] == "GET" and rec["status"] == 200
    assert "request_id" in rec and "duration_ms" in rec


def test_访问日志逐字节不变(reinit_logger):
    """access 改成往上 propagate 之后，写出去的仍是那一行原始 JSON，不被再包一层。"""
    _, log_path = reinit_logger
    main_mod._access_logger.info('{"path": "/api/x", "status": 200}')
    _flush()
    assert log_path.read_text("utf-8").strip() == '{"path": "/api/x", "status": 200}'


@pytest.mark.parametrize(
    "name",
    ["medplat.audit", "medplat.jobs", "medplat.alerting", "medplat.payments", "medplat.sms"],
)
def test_非访问日志同样落文件(reinit_logger, name):
    """核心缺口：这些 logger 以前一条都进不了文件。"""
    _, log_path = reinit_logger
    logging.getLogger(name).error("测试记录")
    _flush()
    rows = [r for r in _lines(log_path) if r.get("logger") == name]
    assert rows, f"{name} 的日志没有落文件——配了 MEDPLAT_LOG_FILE 也留不下来"
    assert rows[-1]["level"] == "ERROR"
    assert rows[-1]["message"] == "测试记录"


def test_非访问日志同样进stdout(capsys, reinit_logger):
    """容器部署根本不设 MEDPLAT_LOG_FILE——stdout 由 docker/journald 收集就是全部。
    所以 stream handler 挂错层（挂回 access）在容器里等于这 17 个 logger 全哑，
    而文件那条用例看不出来（文件 handler 还在父层）。

    `capsys` 必须写在 `reinit_logger` **前面**：同作用域的 fixture 按签名顺序初始化，
    而 `StreamHandler()` 在构造时就绑死了当时的 `sys.stderr`——反过来写的话
    handler 绑的是没被接管的那个流，捕获不到。
    """
    capsys.readouterr()  # 清掉此前的输出
    logging.getLogger("medplat.jobs").error("要出现在 stdout")
    captured = capsys.readouterr()
    assert "要出现在 stdout" in captured.out + captured.err


def test_INFO级别不被丢掉(reinit_logger):
    """lastResort 的级别是 WARNING：原实现下所有 logger.info 静默消失。"""
    _, log_path = reinit_logger
    logging.getLogger("medplat.jobs").info("任务开始")
    _flush()
    assert any(r.get("message") == "任务开始" for r in _lines(log_path))


def test_异常堆栈落文件(reinit_logger):
    """`_write_audit` 那句"审计写失败、本条审计丢失"是靠 traceback 才查得出原因的。"""
    _, log_path = reinit_logger
    try:
        raise ValueError("库抖了")
    except ValueError:
        logging.getLogger("medplat.audit").error("审计写失败", exc_info=True)
    _flush()
    rows = [r for r in _lines(log_path) if r.get("logger") == "medplat.audit"]
    assert rows and "ValueError: 库抖了" in rows[-1].get("traceback", ""), (
        f"没有 traceback，丢审计时查不出是库抖动还是锁超时：{rows}"
    )


def test_文件里每条只出现一次(reinit_logger):
    """一条记录一行。access 现在往上传、其余 logger 本来就往上传，
    共用同一份 handler；只要 handler 不重复挂，就不会写两遍。"""
    _, log_path = reinit_logger
    logging.getLogger("medplat.jobs").error("只该出现一次")
    main_mod._access_logger.info('{"path": "/api/once"}')
    _flush()
    rows = _lines(log_path)
    assert sum(1 for r in rows if r.get("message") == "只该出现一次") == 1
    assert sum(1 for r in rows if r.get("path") == "/api/once") == 1


def test_保留往root传的行为(reinit_logger):
    """`medplat` 不设 propagate=False：这 17 个 logger 本来就往 root 传，
    改掉属于本次修复不需要的行为变更，还会让 8 个测试文件里的 caplog 收不到记录。
    挂上 handler 之后 `logging.lastResort` 不再触发（它只在整条链一个 handler
    都没有时才兜底），所以双打的问题本来就不存在。"""
    logger, _ = reinit_logger
    assert logger.propagate is True
    assert main_mod._access_logger.propagate is True, "access 不往上传就拿不到 handler"


def test_初始化幂等(reinit_logger):
    """reload / 多次 import 不该把 handler 挂两遍——那才会真的写两遍。"""
    logger, _ = reinit_logger
    before = len(logger.handlers)
    main_mod._configure_logging()
    assert len(logger.handlers) == before


def test_no_file_handler_when_log_file_empty(monkeypatch):
    monkeypatch.setattr(settings, "log_file", "")
    logger = logging.getLogger("medplat")
    old_handlers = logger.handlers[:]
    logger.handlers = []
    try:
        main_mod._configure_logging()
        assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
        assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    finally:
        for h in logger.handlers:
            if h not in old_handlers:
                h.close()
        logger.handlers = old_handlers
