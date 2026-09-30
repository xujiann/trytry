"""备份 / 恢复脚本照运维手册的写法调用得起来、PG 连接串交得对（P1-234，第三十一批「运维脚本与后台任务」扫描 C2-1）。

① 运维手册、README、compose 一律写 `sh scripts/xxx.sh`（crontab 行也是），`sh` 无视 shebang：Debian 系与 python:3.12-slim
镜像的 /bin/sh 是 dash，脚本里 `set -euo pipefail` 那一行就 "Illegal option -o pipefail"、exit 2——全量备份、PITR、恢复、
演练一样都不执行，备份目录里没有新文件（`test_ops_backup_scripts` 的注释自己写着这个后果，`test_ops_wal_pitr_scripts`
的 `sh -n` 正是它说的假绿：dash 解析得了、执行不了）。
② 即便改用 bash：PG 分支把 `postgresql+psycopg2://…`（运维手册写的、Alembic 与备份脚本共用的串）原样交给 pg_dump /
pg_restore，libpq 不认这个前缀，把整串当库名、去连本机默认 socket——按文档配置的生产 PG，backup.sh 从来备不出库。

修后：六个脚本不是 bash 就换 bash 重新执行自己；交给 libpq 之前去掉驱动后缀。这里用 dash 真跑 SQLite 全链的备份，
再在 PATH 里放假的 pg_dump / pg_restore，记下它们收到的连接串。
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_ops_backup_scripts import SCRIPTS, _build_synthetic_db, _script_env

DASH = shutil.which("dash")
requires_dash = pytest.mark.skipif(DASH is None, reason="没有 dash，测不了「用 sh 调用、sh 是 dash」这一种形状")
SQLA_URL = "postgresql+psycopg2://medplat:pw@127.0.0.1:1/medplat"
LIBPQ_URL = "postgresql://medplat:pw@127.0.0.1:1/medplat"

#: 假的 PG 客户端：把收到的参数逐行记进 $FAKE_PG_LOG，`-f 文件` 就建出这个文件（备份包里要有 database.dump）。
#: alembic 同一个假身（恢复的最后一步 `alembic upgrade heads` 走 SQLAlchemy、认驱动后缀，这里没有真库可连）
FAKE_CLIENT = """#!/bin/sh
printf '%s\\n' "$(basename "$0")" "$@" >> "$FAKE_PG_LOG"
while [ $# -gt 0 ]; do
  if [ "$1" = "-f" ]; then : > "$2"; fi
  shift
done
"""


def _fake_pg_env(tmp_path: Path, env: dict) -> dict:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name in ("pg_dump", "pg_restore", "psql", "alembic"):
        (bin_dir / name).write_text(FAKE_CLIENT, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    return {**env, "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}", "FAKE_PG_LOG": str(tmp_path / "pg.log"),
            "MEDPLAT_DATABASE_URL": SQLA_URL}


def _sqlite_env(tmp_path: Path, marker: str) -> dict:
    db_path, upload_dir = tmp_path / "app.db", tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "report.pdf").write_bytes(b"payload")
    _build_synthetic_db(db_path, heads=["platform_head", "spd_head"], marker=marker)
    return _script_env(db_path=db_path, upload_dir=upload_dir, secret=f"secret-{marker}")


@requires_dash
def test_照手册用sh调用_dash上照样备出包(tmp_path):
    env = _sqlite_env(tmp_path, "dash")
    proc = subprocess.run([DASH, str(SCRIPTS / "backup.sh"), str(tmp_path / "out")],
                          capture_output=True, text=True, env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr   # 修前 exit 2：set: Illegal option -o pipefail
    assert len(list((tmp_path / "out").glob("medplat-backup-*.tar.gz"))) == 1


@requires_dash
def test_照手册用sh调用_PG恢复演练的dry_run跑得完(tmp_path):
    proc = subprocess.run([DASH, str(SCRIPTS / "restore_drill_pg.sh"), "--dry-run"], capture_output=True, text=True,
                          env={**os.environ, "DRILL_DB_URL": SQLA_URL})
    assert proc.returncode == 0, proc.stderr   # 修前 exit 2
    psql = [line for line in proc.stdout.splitlines() if "psql" in line]
    assert psql and all(LIBPQ_URL in line and "+psycopg2" not in line for line in psql), psql


def test_PG连接串带驱动后缀_交给libpq之前去掉(tmp_path):
    env = _fake_pg_env(tmp_path, _sqlite_env(tmp_path, "pg"))
    out = tmp_path / "out"
    backup = subprocess.run(["bash", str(SCRIPTS / "backup.sh"), str(out)], capture_output=True, text=True, env=env,
                            cwd=tmp_path)
    assert backup.returncode == 0, backup.stderr
    (archive,) = out.glob("medplat-backup-*.tar.gz")
    spd = subprocess.run(["bash", str(SCRIPTS / "spd_backup.sh"), str(out)], capture_output=True, text=True, env=env,
                         cwd=tmp_path)
    assert spd.returncode == 0, spd.stderr
    restore = subprocess.run(["bash", str(SCRIPTS / "restore.sh"), str(archive), "--force"], capture_output=True,
                             text=True, env=env, cwd=tmp_path)
    assert restore.returncode == 0, restore.stderr
    calls = (tmp_path / "pg.log").read_text(encoding="utf-8").split("\n")
    assert [c for c in calls if c in ("pg_dump", "pg_restore")] == ["pg_dump", "pg_dump", "pg_restore"], calls
    urls = [c for c in calls if c.startswith("postgres")]
    assert urls == [LIBPQ_URL] * 3, urls   # 修前三处都是原串 postgresql+psycopg2://…


def test_六个脚本都带换bash的一行_且在严格模式之前():
    """静态锚点：新加的 bash 脚本照样要有这一行（dash 走到 `set -o pipefail` 之前就得换掉自己）。"""
    guard = '[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"'
    for script in sorted(SCRIPTS.glob("*.sh")):
        text = script.read_text(encoding="utf-8")
        if "pipefail" not in text:
            continue
        assert guard in text, f"{script.name} 用了 pipefail 却没有换 bash 的那一行"
        assert text.index(guard) < text.index("set -euo pipefail"), script.name
