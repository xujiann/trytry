"""归档任务的数据安全：删行之前，导出必须已经落盘且不会被别人覆盖。

`_archive_and_delete` 的历史写法有两个会**永久丢数据**的洞：

1. **已删未导**：`gzip.open` 写、删行、commit 三步中间不 flush/fsync。进程被
   SIGKILL（OOM kill / 滚动重启）时，已 commit 的删除是持久的，而导出还堵在
   用户态压缩缓冲里——3000 行的复现里删了 2000 行、盘上只救回 1726 行，
   275 行永久消失，manifest 里连条记录都没有。
2. **双跑互相覆盖**：文件名 `表_秒级时间戳_首行id` 只到秒，`gzip.open(path,"wb")`
   又是截断覆盖。调度锁失效窗口里两路归档同秒起跑（scheduler docstring 自认
   该窗口存在），两个写者写同一个文件 → 产出 BadGzipFile，两边的行却都已删。

不变量（本文件的两条网）：**任何情况下"已删行数" ≤ "已落盘且读得出的导出行数"**，
且并发产出的归档文件必须各自完整可 gunzip。

非空洞性（均已实测）：
- 把 `_fsync(raw)` 删掉（或挪到删行之后）→ test_进程被杀时已删行必须已落盘 必红
  （实测"已删 100 行，盘上只救回 0 行"）；
- 把 `_open_new_archive` 换回"秒级同名 + 截断覆盖（`open(path,"wb")`）"→
  test_双跑归档不得互相覆盖 与 test_归档文件独占创建_不覆盖既有同名文件 必红
  （实测前者 BadGzipFile）。
"""
import gzip
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import reset_database

from app import jobs
from app.clock import now_naive
from app.config import settings
from app.database import SessionLocal
from app.models import AuditLog

SERVER_DIR = Path(__file__).resolve().parents[1]

#: 子进程复刻脚本：造行 → 归档到第 2 批 commit 之后 `os._exit(137)`（模拟 SIGKILL：
#: 不跑 atexit、不 flush 任何缓冲）。用真被杀的进程而不是注入异常——异常还会走
#: 到 `with` 的收尾把缓冲冲干净，那就测不出"没 fsync"这个洞了。
_KILL_SCRIPT = """
import os, sys
sys.path.insert(0, {server!r})
os.environ["MEDPLAT_DATABASE_URL"] = "sqlite:///" + {db!r}
from datetime import timedelta
from app.database import Base, engine, SessionLocal
from app.config import settings
settings.upload_dir = {upload!r}
settings.audit_log_archive_days = 90
import app.models  # noqa: F401
from app import jobs
from app.models import AuditLog
from app.clock import now_naive

Base.metadata.create_all(bind=engine)
jobs.ARCHIVE_BATCH_SIZE = {batch}
db = SessionLocal()
old = now_naive() - timedelta(days=200)
for i in range({rows}):
    db.add(AuditLog(username="admin", method="POST", path="/api/x/%d" % i, status_code=200,
                    prev_hash="", entry_hash="h%d" % i, created_at=old))
db.commit()

real_commit = db.commit
state = {{"n": 0}}
def commit_then_die():
    real_commit()
    state["n"] += 1
    if state["n"] == 2:
        os._exit(137)   # 归档跑到第 2 批 commit 之后被"杀"
db.commit = commit_then_die
jobs.audit_archive(db)
"""

TOTAL_ROWS = 300
KILL_BATCH = 50


def _read_ndjson_gz_tolerant(path: Path) -> tuple[list[dict], str | None]:
    """尽力读出归档文件的内容，返回 (记录列表, 读取异常名或 None)。

    "尽力"是刻意的：崩溃后的残留文件正是要勘验的对象——能读出多少行，
    就是这次崩溃真正救回了多少行。多成员 gzip 会被连读成一个流。
    """
    rows: list[dict] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    except Exception as exc:  # noqa: BLE001 - 勘验用：损坏也要报出来
        return rows, type(exc).__name__
    return rows, None


def _archive_files(archive_dir: Path) -> list[Path]:
    return sorted(archive_dir.glob("audit_logs_*.ndjson.gz"))


@pytest.fixture()
def archive_dir(tmp_path, monkeypatch):
    """归档落临时目录：settings 是进程单例，monkeypatch 属性即可（自动还原）。"""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    return tmp_path / "archives"


def test_进程被杀时已删行必须已落盘(tmp_path):
    """子进程跑归档、第 2 批 commit 后被杀：已删行数 ≤ 盘上读得出的导出行数。"""
    db_path = tmp_path / "kill.db"
    upload = tmp_path / "up"
    script = _KILL_SCRIPT.format(
        server=str(SERVER_DIR), db=str(db_path), upload=str(upload),
        batch=KILL_BATCH, rows=TOTAL_ROWS,
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 137, f"子进程没有按预期被杀：rc={proc.returncode} {proc.stderr[-800:]}"

    # 库内剩余 → 反推已删除的行数
    import sqlalchemy

    engine = sqlalchemy.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        left = conn.execute(sqlalchemy.text("SELECT COUNT(*) FROM audit_logs")).scalar()
    engine.dispose()
    deleted = TOTAL_ROWS - left
    assert deleted == 2 * KILL_BATCH, f"复现前提没成立：本应删掉两批，实际删了 {deleted} 行"

    files = _archive_files(upload / "archives")
    assert files, "行已删，盘上却一个归档文件都没有"
    exported: list[dict] = []
    for path in files:
        rows, err = _read_ndjson_gz_tolerant(path)
        assert err is None, f"崩溃残留的归档文件读不出来（{err}）：{path.name}"
        exported += rows

    assert len(exported) >= deleted, (
        f"**已删 {deleted} 行，盘上只救回 {len(exported)} 行 —— "
        f"{deleted - len(exported)} 行永久消失**"
    )
    # 被删掉的行必须都在导出里：id 从 1 连续自增，删的正是最旧的 deleted 条
    assert {r["id"] for r in exported} >= set(range(1, deleted + 1)), (
        "有已删的行不在任何归档文件里"
    )


def test_双跑归档不得互相覆盖(archive_dir, monkeypatch):
    """两线程并发跑 audit_archive：产出文件全部可 gunzip，已删行无一丢失。"""
    reset_database()
    monkeypatch.setattr(settings, "audit_log_archive_days", 1)
    monkeypatch.setattr(jobs, "ARCHIVE_BATCH_SIZE", 3)

    total = 120
    with SessionLocal() as db:
        old = now_naive() - timedelta(days=30)
        for i in range(total):
            db.add(AuditLog(username="u", method="POST", path=f"/api/x/{i}", status_code=200,
                            prev_hash="", entry_hash=f"h{i}", created_at=old))
        db.commit()
        all_ids = {row.id for row in db.query(AuditLog).all()}
    assert len(all_ids) == total

    # 确定性交错：两路都写完第一批、都还没删行时，才放行落盘。裸靠线程调度
    # 只有约四分之一的概率撞上同一秒同一首行 id，这样的锚点抓不住回归。
    # 钩在 _fsync 上（每线程只挡一次），此刻两路都已经把归档文件建出来了。
    barrier = threading.Barrier(2)
    real_fsync = jobs._fsync
    waited: set[int] = set()
    waited_lock = threading.Lock()

    def fsync_once_in_lockstep(fileobj):
        with waited_lock:
            first = threading.get_ident() not in waited
            waited.add(threading.get_ident())
        if first:
            barrier.wait(timeout=30)
        real_fsync(fileobj)

    monkeypatch.setattr(jobs, "_fsync", fsync_once_in_lockstep)

    results: list = []

    def run() -> None:
        db = SessionLocal()
        try:
            results.append(jobs.audit_archive(db))
        except Exception as exc:  # noqa: BLE001 - 撞名被拒也是合法结局，收集下来看
            results.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    with SessionLocal() as db:
        left_ids = {row.id for row in db.query(AuditLog).all()}
    deleted_ids = all_ids - left_ids
    # 防空跑：两路都没归档成任何东西时，下面的断言全是真空的
    assert deleted_ids, f"两路归档都没干活，用例失去意义：{results}"

    exported_ids: set[int] = set()
    gunzip = shutil.which("gunzip")
    for path in _archive_files(archive_dir):
        rows, err = _read_ndjson_gz_tolerant(path)
        assert err is None, (
            f"并发产出的归档文件损坏（{err}）：{path.name} —— 同名截断覆盖又回来了"
        )
        exported_ids |= {r["id"] for r in rows}
        if gunzip:  # 真 gunzip 也要能读（运维手册的恢复步骤靠它，非 CI 必备故容缺）
            assert subprocess.run(
                [gunzip, "-t", str(path)], capture_output=True
            ).returncode == 0, f"gunzip -t 判定文件损坏：{path.name}"

    lost = deleted_ids - exported_ids
    assert not lost, f"**已删 {len(deleted_ids)} 行，其中 {len(lost)} 行任何归档文件里都读不出**：{sorted(lost)[:12]}"

    # manifest 的每条记录各占一个文件名：撞名覆盖会让两条记录指向同一个文件
    manifest = archive_dir / "manifest.jsonl"
    entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]
    names = [e["file"] for e in entries]
    assert len(names) == len(set(names)), f"两条 manifest 指向同一个归档文件（互相覆盖）：{names}"
    for entry in entries:
        assert (archive_dir / entry["file"]).exists(), f"manifest 记了一个不存在的文件：{entry['file']}"


def test_归档文件独占创建_不覆盖既有同名文件(archive_dir):
    """`_open_new_archive` 用 open(path,'xb') 独占创建：绝不截断已有文件。"""
    archive_dir.mkdir(parents=True, exist_ok=True)
    name, raw = jobs._open_new_archive("audit_logs", 1)
    try:
        raw.write(b"x")
    finally:
        raw.close()
    # 同一秒、同一首行 id 再来一次：必须换到另一个文件名，原文件字节不动
    name2, raw2 = jobs._open_new_archive("audit_logs", 1)
    raw2.close()
    assert name != name2
    assert (archive_dir / name).read_bytes() == b"x"
    assert os.path.getsize(archive_dir / name2) == 0
