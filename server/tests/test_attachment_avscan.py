"""附件病毒扫描旁路（工程包 G2 / P1-22）：协议三态、上传置位、补扫任务、下载拦截。

fake clamd 在线程内起真 TCP server，**严格按协议应答**：命令必须以 NUL 结尾且为
zINSTREAM/zPING；数据块必须是"4 字节大端块长 + 内容"并以零长块收尾——客户端
分块或结尾发错时 fake 端回 PROTOCOL ERROR（clean 用例即红），钉住 avscan 的
协议实现不空转。下载拦截同样走真实 API：拿掉 410 拦截，对应用例必红。
"""
import io
import shutil
import socket
import struct
import threading

import pytest
from fastapi.testclient import TestClient

from conftest import login, reset_database

import app.avscan as avscan
from app.avscan import attachment_av_scan, ping, scan_bytes
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Attachment

#: 触发 fake clamd 判毒的标记（真 EICAR 串会惊动宿主机杀软，用自定标记即可）
VIRUS_MARKER = b"FAKE-VIRUS-MARKER-FOR-TEST"
FAKE_SIGNATURE = "Medplat-Test-Signature"


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class FakeClamd:
    """极简 clamd 假实现：只认协议正确的 zPING / zINSTREAM，错协议一律拒绝。"""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.streams: list[bytes] = []  # 每次 INSTREAM 重组出的完整字节流
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    def start(self) -> "FakeClamd":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.sock.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:  # stop() 关掉监听套接字后退出
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            # 命令：NUL 结尾（z 前缀约定）。逐字节读，超长即协议错。
            cmd = bytearray()
            while not cmd.endswith(b"\0"):
                b = conn.recv(1)
                if not b or len(cmd) > 32:
                    conn.sendall(b"PROTOCOL ERROR\0")
                    return
                cmd += b
            command = bytes(cmd[:-1])
            if command == b"zPING":
                conn.sendall(b"PONG\0")
                return
            if command != b"zINSTREAM":
                conn.sendall(b"UNKNOWN COMMAND\0")
                return
            # 数据块：4 字节大端块长 + 内容，零长块收尾。framing 不对即拒绝——
            # 客户端漏发结尾零块/长度写错时，这里回 PROTOCOL ERROR，clean 用例必红。
            data = bytearray()
            while True:
                header = _recv_exact(conn, 4)
                if header is None:
                    conn.sendall(b"PROTOCOL ERROR: missing zero-length end chunk\0")
                    return
                (length,) = struct.unpack(">I", header)
                if length == 0:
                    break
                chunk = _recv_exact(conn, length)
                if chunk is None:
                    conn.sendall(b"PROTOCOL ERROR: chunk shorter than declared length\0")
                    return
                data += chunk
            self.streams.append(bytes(data))
            if VIRUS_MARKER in data:
                conn.sendall(f"stream: {FAKE_SIGNATURE} FOUND\0".encode())
            else:
                conn.sendall(b"stream: OK\0")


@pytest.fixture(scope="module")
def clamd():
    server = FakeClamd().start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def dead_port() -> int:
    """一个刚刚还开着、现已关闭的端口：连接必被拒。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c
    shutil.rmtree("./test_uploads", ignore_errors=True)


@pytest.fixture(scope="module")
def setup(client):
    admin = login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "病毒扫描测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "av_op", "password": "pass123456", "role": "operator", "org_id": org["id"]},
        headers=admin,
    )
    op = login(client, "av_op", "pass123456")
    event = client.post(
        "/api/quality/adverse-events",
        json={"org_id": org["id"], "event_type": "device", "level": "III", "description": "扫描测试事件"},
        headers=op,
    ).json()
    return {"op": op, "event": event}


def _upload(client, headers, event_id, filename, content):
    resp = client.post(
        "/api/attachments",
        data={"owner_type": "adverse_event", "owner_id": str(event_id)},
        files={"file": (filename, io.BytesIO(content), "application/pdf")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _scan_status(attachment_id: int) -> tuple[str, str]:
    with SessionLocal() as db:
        a = db.get(Attachment, attachment_id)
        return a.scan_status, a.scan_detail


# ---------------------------------------------------------------------------
# clamd 协议客户端：clean / infected / unavailable 三态 + PING
# ---------------------------------------------------------------------------


def test_scan_bytes_clean_且分块协议正确(clamd, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", clamd.address)
    payload = b"%PDF-1.4 benign bytes " * 9000  # 跨多个 64KB 块，验证分块 framing
    assert scan_bytes(payload) == ("clean", "")
    # fake 端按"块长+内容+零长块"重组出的字节流必须与发送方逐字节一致
    assert clamd.streams[-1] == payload


def test_scan_bytes_infected_带签名名(clamd, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", clamd.address)
    status, detail = scan_bytes(b"%PDF-1.4 " + VIRUS_MARKER)
    assert status == "infected"
    assert detail == FAKE_SIGNATURE


def test_scan_bytes_连接失败为unavailable_不误伤(dead_port, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", f"127.0.0.1:{dead_port}")
    status, detail = scan_bytes(b"%PDF-1.4 whatever")
    assert status == "unavailable"
    assert detail  # 记下原因，便于排查


def test_ping健康探测(clamd, dead_port, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", clamd.address)
    assert ping() is True
    monkeypatch.setattr(settings, "clamd_address", f"127.0.0.1:{dead_port}")
    assert ping() is False


# ---------------------------------------------------------------------------
# 上传置位：配置了 clamd → pending；未配置 → skipped（明示未扫）
# ---------------------------------------------------------------------------


def test_上传默认pending_不同步扫描(client, setup, dead_port, monkeypatch):
    # clamd 地址指向连不上的端口：上传是旁路语义、不同步扫，照样 201 且置 pending
    monkeypatch.setattr(settings, "clamd_address", f"127.0.0.1:{dead_port}")
    a = _upload(client, setup["op"], setup["event"]["id"], "pending.pdf", b"%PDF-1.4 pending case")
    assert _scan_status(a["id"]) == ("pending", "")
    # 响应契约不变：扫描状态是内部旁路字段，不进上传响应体
    assert "scan_status" not in a


def test_上传未配置clamd为skipped(client, setup, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", "")
    a = _upload(client, setup["op"], setup["event"]["id"], "skipped.pdf", b"%PDF-1.4 skipped case")
    assert _scan_status(a["id"]) == ("skipped", "")


# ---------------------------------------------------------------------------
# 补扫任务：批量置位、检出告警、clamd 不可用不改状态、未配置跳过
# ---------------------------------------------------------------------------


def test_任务批量补扫_置位与告警(client, setup, clamd, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", clamd.address)
    clean = _upload(client, setup["op"], setup["event"]["id"], "clean.pdf", b"%PDF-1.4 clean doc")
    bad = _upload(
        client, setup["op"], setup["event"]["id"], "bad.pdf", b"%PDF-1.4 " + VIRUS_MARKER
    )
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(avscan, "send_alert", lambda kind, msg: alerts.append((kind, msg)))
    with SessionLocal() as db:
        scanned, summary = attachment_av_scan(db)
    assert scanned >= 2 and "检出 1" in summary
    assert _scan_status(clean["id"]) == ("clean", "")
    assert _scan_status(bad["id"]) == ("infected", FAKE_SIGNATURE)
    assert len(alerts) == 1 and alerts[0][0] == "attachment_infected"
    assert FAKE_SIGNATURE in alerts[0][1]


def test_任务clamd不可用_本轮跳过不改状态(client, setup, dead_port, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", f"127.0.0.1:{dead_port}")
    a = _upload(client, setup["op"], setup["event"]["id"], "stay.pdf", b"%PDF-1.4 stays pending")
    with SessionLocal() as db:
        scanned, summary = attachment_av_scan(db)
    assert scanned == 0 and "跳过" in summary
    assert _scan_status(a["id"]) == ("pending", "")  # 探测失败绝不写成扫描结论


def test_任务未配置clamd_跳过(monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", "")
    with SessionLocal() as db:
        scanned, summary = attachment_av_scan(db)
    assert scanned == 0 and "未配置" in summary


# ---------------------------------------------------------------------------
# 下载拦截：infected → 410（隔离）；clean/pending/skipped 放行
# ---------------------------------------------------------------------------


def test_下载infected拦截410(client, setup, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", "127.0.0.1:1")
    a = _upload(client, setup["op"], setup["event"]["id"], "virus.pdf", b"%PDF-1.4 to quarantine")
    with SessionLocal() as db:
        row = db.get(Attachment, a["id"])
        row.scan_status, row.scan_detail = "infected", FAKE_SIGNATURE
        db.commit()
    resp = client.get(f"/api/attachments/{a['id']}", headers=setup["op"])
    assert resp.status_code == 410
    assert "隔离" in resp.json()["detail"]


def test_下载clean与pending照常放行(client, setup, monkeypatch):
    monkeypatch.setattr(settings, "clamd_address", "127.0.0.1:1")
    content = b"%PDF-1.4 downloadable"
    a = _upload(client, setup["op"], setup["event"]["id"], "ok.pdf", content)
    # pending（旁路窗口内）放行——可用性优先，取舍见 avscan.py
    resp = client.get(f"/api/attachments/{a['id']}", headers=setup["op"])
    assert resp.status_code == 200 and resp.content == content
    with SessionLocal() as db:
        db.get(Attachment, a["id"]).scan_status = "clean"
        db.commit()
    resp = client.get(f"/api/attachments/{a['id']}", headers=setup["op"])
    assert resp.status_code == 200 and resp.content == content
