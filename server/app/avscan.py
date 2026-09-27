"""附件病毒扫描旁路（ClamAV，P1-22）：clamd 极简客户端 + 后台补扫任务。

协议出处
--------
clamd 的 TCP/unix socket 文本协议，见 ClamAV 官方文档（clamd(8) 与
https://docs.clamav.net/manual/Usage/Scanning.html#clamd ）。本模块只用两条命令：

- ``zINSTREAM\\0``：流式扫描。命令后跟若干数据块，每块为 **4 字节大端块长 +
  块内容**，发送**长度为 0 的块**表示流结束；clamd 回
  ``stream: OK\\0``（干净）/ ``stream: <签名名> FOUND\\0``（检出）/ ``... ERROR\\0``。
  ``z`` 前缀表示命令与响应均以 NUL 结尾（对应的 ``n`` 前缀用换行，这里不用）。
- ``zPING\\0``：健康探测，clamd 回 ``PONG\\0``。

零依赖自实现的理由：协议就上面这几行，为此引入 pyclamd/clamd 库不符合
CLAUDE.md 第 12 条（不无理由引入新依赖）。

定位：旁路异步，不阻塞上传（可用性优先）
--------------------------------------
上传路径**不做**同步扫描——clamd 慢或挂了不能拖垮、更不能拦下业务上传。
上传只把附件标为 ``scan_status=pending``（未配置 clamd 时明示 ``skipped``），
由定时任务 :func:`attachment_av_scan` 分批异步补扫；下载仅对已确证
``infected`` 的附件拦截（410），``pending``/``unavailable``/``skipped`` 一律放行——
接受"已上传、未扫完"的时间窗，换取扫描器故障时业务照常。取舍详见
`routers/attachments.py` 下载拦截处的注释。

三态约定（不误伤业务）：``scan_bytes`` 只回 ``clean`` / ``infected`` /
``unavailable``——连接失败、超时、clamd 报 ERROR 都归 ``unavailable``，
绝不把"扫不了"当成"有毒"。
"""
import socket
import struct

from sqlalchemy.orm import Session

from .alerting import send_alert
from .config import settings
from .models import Attachment
from .scheduler import register
from .storage import get_storage

#: INSTREAM 分块大小。clamd 默认 StreamMaxLength 为 25MB（附件上限 10MB，够用）；
#: 块大小只影响单次 send 的量，64KB 是 clamd 文档示例常用值。
INSTREAM_CHUNK_BYTES = 64 * 1024
#: 连接超时（秒）：旁路调用，连不上就尽快判 unavailable，别拖住扫描任务。
CONNECT_TIMEOUT_SECONDS = 5.0
#: 读响应超时（秒）：覆盖 10MB 附件的扫描耗时；超时同样判 unavailable。
RESPONSE_TIMEOUT_SECONDS = 60.0
#: 补扫任务每轮最多处理的附件数：上传高峰积压时分多轮消化，单轮不长期占库连接。
SCAN_BATCH_SIZE = 50
#: 一条检出告警里逐件列出的上限：再多的只报件数（一轮最多 `SCAN_BATCH_SIZE` 件）
ALERT_LIST_MAX = 20


def _connect() -> socket.socket:
    """按 ``settings.clamd_address`` 建立连接：``host:port`` 走 TCP，``unix:/path`` 走域套接字。

    未配置时抛 OSError（调用方统一归入 unavailable）——但正常路径不该走到这里：
    未配置的部署里附件都是 skipped，补扫任务也直接跳过。
    """
    address = settings.clamd_address.strip()
    if not address:
        raise OSError("clamd_address 未配置")
    if address.startswith("unix:"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(address[len("unix:"):])
        except OSError:
            sock.close()
            raise
    else:
        host, _, port = address.rpartition(":")
        sock = socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT_SECONDS)
    sock.settimeout(RESPONSE_TIMEOUT_SECONDS)
    return sock


def _recv_reply(sock: socket.socket) -> str:
    """读一条以 NUL 结尾的响应（z 前缀命令的约定）；对端提前关闭时返回已读部分。"""
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\0"):
            break
    return buf.rstrip(b"\0").decode("utf-8", errors="replace").strip()


def scan_bytes(data: bytes) -> tuple[str, str]:
    """INSTREAM 扫描一段字节，返回 ``(状态, 详情)``。

    - ``("clean", "")``：clamd 回 ``stream: OK``；
    - ``("infected", 签名名)``：clamd 回 ``stream: <签名名> FOUND``；
    - ``("unavailable", 原因)``：连接失败/超时/ERROR/响应不可识别——
      "扫不了"如实记录，不误伤业务（绝不当 infected 处理）。
    """
    try:
        with _connect() as sock:
            sock.sendall(b"zINSTREAM\0")
            for offset in range(0, len(data), INSTREAM_CHUNK_BYTES):
                chunk = data[offset:offset + INSTREAM_CHUNK_BYTES]
                sock.sendall(struct.pack(">I", len(chunk)) + chunk)
            sock.sendall(struct.pack(">I", 0))  # 零长块 = 流结束
            reply = _recv_reply(sock)
    except OSError as exc:  # socket.timeout 亦是 OSError 子类
        return "unavailable", f"{type(exc).__name__}: {exc}"[:256]
    if reply.endswith("OK"):
        return "clean", ""
    if reply.endswith(" FOUND"):
        # 形如 "stream: Eicar-Test-Signature FOUND"，详情只留签名名
        detail = reply.removesuffix(" FOUND")
        detail = detail.split(":", 1)[1].strip() if ":" in detail else detail.strip()
        return "infected", detail[:256]
    return "unavailable", (reply or "空响应")[:256]


def ping() -> bool:
    """PING 健康探测：clamd 可达且应答 PONG 才算可用。"""
    try:
        with _connect() as sock:
            sock.sendall(b"zPING\0")
            return _recv_reply(sock) == "PONG"
    except OSError:
        return False


@register("attachment_av_scan", "附件病毒扫描（ClamAV 旁路补扫）", 300)
def attachment_av_scan(db: Session) -> tuple[int, str]:
    """分批补扫 ``scan_status=pending`` 的附件（上传旁路的收尾一环）。

    - 未配置 clamd：跳过（新上传本就标 skipped，历史 pending 留待配置后补扫）；
    - clamd 不可用（PING 失败或扫描中途失联）：本轮跳过/中止，**不改附件状态**——
      pending 留着下轮重试，绝不把探测失败写成扫描结论；
    - 检出病毒：置 infected + 记 scan_detail（签名名），外发告警
      （下载已被拦截，但需要有人去处置源头；每轮一条，逐件列出本轮检出的，见 P2-500）；
    - 存储中文件缺失：置 unavailable 并记因，不让它永远堵在 pending 队头；
    - 同一份内容一并隔离（P2-395）：存储按 sha256 只存一份，被隔离的字节挂在别的行上照样下得到——
      每轮扫完把与 infected 行同 sha256 的其余行（早先扫成 clean 的、未配置时标的 skipped、还没轮到的
      pending）一并置 infected，签名名取最早被隔离的那行。下载那头另按内容判（``quarantined_copy``），
      这里是让登记与事实一致、不再为同一份字节反复扫。
    """
    if not settings.clamd_address.strip():
        return 0, "未配置 clamd_address，跳过（新上传附件标 skipped）"
    if not ping():
        return 0, "clamd 不可用（PING 失败），本轮跳过，不改附件状态"
    storage = get_storage()
    pending = (
        db.query(Attachment)
        .filter(Attachment.scan_status == "pending")
        .order_by(Attachment.id)
        .limit(SCAN_BATCH_SIZE)
        .all()
    )
    scanned = infected = 0
    aborted = ""
    found: list[tuple[int, str]] = []   # 本轮检出的 (附件编号, 告警里的一句)
    for attachment in pending:
        if not storage.exists(attachment.sha256):
            attachment.scan_status = "unavailable"
            attachment.scan_detail = "存储中无此文件，无法扫描"
            continue
        with storage.open(attachment.sha256) as f:
            data = f.read()
        status, detail = scan_bytes(data)
        if status == "unavailable":
            # 扫描中途 clamd 失联：中止本轮，剩余 pending 下轮再来
            aborted = f"；clamd 中途不可用（{detail}），剩余待扫下轮重试"
            break
        attachment.scan_status = status
        attachment.scan_detail = detail
        scanned += 1
        if status == "infected":
            infected += 1
            found.append((attachment.id, f"id={attachment.id} 文件={attachment.filename} 签名={detail}"))
    db.flush()  # 会话 autoflush 关着：本轮刚判出的 infected 先落下去，下面按内容扩散才看得见
    spread = _quarantine_same_content(db)
    db.commit()
    if found:
        # 一轮一条、列出本轮检出的每一件（P2-500）：原先逐件发、共用一个告警类别，冷却期（默认 10 分钟）内第二件起一概
        # 被吞——两个感染文件前后脚上传，告警只点得出第一个，第二个的来源没人去处置。类别带上本轮第一件的编号：每件附件
        # 只会被判出一次，不同轮次的检出互不冷却；同一轮检出再多也只发一条，不轰炸
        listed = "；".join(line for _, line in found[:ALERT_LIST_MAX])
        more = f"；另有 {len(found) - ALERT_LIST_MAX} 件，见附件扫描状态" if len(found) > ALERT_LIST_MAX else ""
        send_alert(f"attachment_infected:{found[0][0]}",
                   f"附件检出病毒 {len(found)} 件（下载已拦截，请处置源头）：{listed}{more}")
    tail = f"，同内容另隔离 {spread} 件" if spread else ""
    return scanned, f"补扫 {scanned} 件，检出 {infected} 件{tail}{aborted}"


def _quarantine_same_content(db: Session) -> int:
    """把与 infected 行同 sha256、自己还不是 infected 的行一并置 infected，返回置了几行（P2-395）。**不 commit**。"""
    first_detail: dict[str, str] = {}
    for sha256, detail in (
        db.query(Attachment.sha256, Attachment.scan_detail)
        .filter(Attachment.scan_status == "infected")
        .order_by(Attachment.id)
    ):
        first_detail.setdefault(sha256, detail)
    spread = 0
    for sha256, detail in first_detail.items():
        spread += (
            db.query(Attachment)
            .filter(Attachment.sha256 == sha256, Attachment.scan_status != "infected")
            .update({Attachment.scan_status: "infected", Attachment.scan_detail: detail},
                    synchronize_session=False)
        )
    return spread
