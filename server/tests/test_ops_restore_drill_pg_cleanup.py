"""PG 恢复演练中途失败也收场：停掉临时实例、删掉恢复出的明文整库（P2-1269，第三十七批「数据的保留、清理与无限增长」扫描 AA1-3）。

修前：`restore_drill_pg.sh`（`set -euo pipefail`）把整库恢复进 `mktemp -d /tmp/medplat-restore-drill.XXXXXX`、以临时端口
（默认 54329）起实例之后，第 2～4 步任一步失败（实例起不来、alembic 连不上或版本不对、psql 鉴权不过）就当场退出——停实例与
`rm -rf` 只在第 5 步，全文件没有 trap：恢复出的明文整库留在 /tmp，已起的实例继续占着临时端口，下次演练用同一个默认端口又在
第 2 步失败、再留一份。同目录 restore_drill.sh / backup.sh / restore.sh 都有 `trap 'rm -rf …' EXIT`。扫描复现：alembic
失败后退出码 1，`drill_dir/base/16384`（假数据文件）与 `postmaster.pid`（实例还在跑）都还在。

修后：EXIT trap 收场——起过实例就先 `pg_ctl -m fast stop`（停不下来照样删），再删这次演练恢复出来的目录，按原退出码退出；
调用方给的本来就有东西的目录不碰；第 5 步正常路径与 dry-run 照旧。

照 test_ops_backup_sh_invocation 的假命令法：PATH 最前放假的 pgbackrest / pg_ctl / alembic / psql，每次调用记一行进日志；
pgbackrest restore 往 --pg1-path 写一份假数据文件（目录非空时照真 pgbackrest 的默认拒绝），pg_ctl start 写 postmaster.pid、
stop 删掉它（没在跑就照真 pg_ctl 报错退出 1）；$FAKE_FAIL 点名的那一步以 7 退出。
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_drill_pg.sh"

requires_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="缺少 bash，跑不了演练脚本")

#: 假命令（四个名字共用一份，按 basename 分派）。点名失败的 pgbackrest 先写一半再失败（磁盘满、仓库断连）；
#: 点名失败的 pg_ctl start 先写 postmaster.pid 再失败——即 `-w` 等满超时返回非 0、postmaster 照样在后台跑的那一种
FAKE_CMD = """#!/bin/sh
name="$(basename "$0")"
echo "$name $*" >> "$FAKE_LOG"
D=""; prev=""
for a in "$@"; do
  case "$a" in --pg1-path=*) D="${a#--pg1-path=}";; esac
  [ "$prev" = "-D" ] && D="$a"
  prev="$a"
done
case "$name" in
  pgbackrest)
    if [ -d "$D" ] && [ -n "$(ls -A "$D")" ]; then
      [ -n "$FAKE_DELTA" ] || { echo "ERROR: [040]: unable to restore to path '$D' because it contains files" >&2; exit 40; }
      find "$D" -mindepth 1 -delete   # delta=y：清掉备份清单外的文件再恢复
    fi
    mkdir -p "$D/base" && echo "恢复出的明文整库（含患者证件号、电话）" > "$D/base/16384"
    [ "$FAKE_FAIL" = "pgbackrest" ] && exit 7 ;;
  pg_ctl)
    case "$*" in
      *" start") echo 4242 > "$D/postmaster.pid"; [ "$FAKE_FAIL" = "pg_ctl start" ] && exit 7 ;;
      *" stop")
        [ "$FAKE_STOP_FAIL" = "1" ] && { echo "pg_ctl: server does not shut down" >&2; exit 1; }
        [ -f "$D/postmaster.pid" ] || { echo "pg_ctl: PID file does not exist" >&2; exit 1; }
        rm -f "$D/postmaster.pid" ;;
    esac ;;
  alembic|psql)
    [ "$FAKE_FAIL" = "$name" ] && { echo "FAILED: $name" >&2; exit 7; } ;;
esac
exit 0
"""


def _env(tmp_path: Path, **extra) -> dict:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("pgbackrest", "pg_ctl", "alembic", "psql"):
        (bin_dir / name).write_text(FAKE_CMD, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("PG_BIN", "DRILL_DIR", "DRILL_PORT", "DRILL_DB", "DRILL_DB_URL",
                        "FAKE_FAIL", "FAKE_STOP_FAIL", "FAKE_DELTA")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(tmp_path / "calls.log"))
    env.update(extra)
    return env


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _drill_dir(tmp_path: Path) -> Path:
    """调用方事先建好的空目录（真 pgbackrest 默认要求恢复目标存在且为空）。"""
    drill = tmp_path / "drill"
    drill.mkdir()
    return drill


@requires_bash
@pytest.mark.parametrize("fail, not_reached", [
    ("pg_ctl start", "alembic"),   # 第 2 步：-w 等超时返回非 0，实例却已在跑
    ("alembic", "psql"),           # 第 3 步：连不上 / 版本对不上（扫描复现的那一步）
    ("psql", "-w stop"),           # 第 4 步：鉴权不过
])
def test_第2到4步失败_停掉临时实例并删掉恢复目录_退出码照旧(tmp_path, fail, not_reached):
    drill = _drill_dir(tmp_path)
    proc = _run(_env(tmp_path, DRILL_DIR=str(drill), FAKE_FAIL=fail), "medplat")

    # 失败就是失败，且退出码是失败那一步自己的——没被 trap 里的 stop / rm 盖掉
    assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)
    assert "演练完成" not in proc.stdout
    # 修前：drill/base/16384（恢复出的明文整库）与 postmaster.pid 原样留着
    assert not drill.exists(), f"恢复出的整库留在了磁盘上：{list(drill.rglob('*'))}"
    calls = _calls(tmp_path)
    failed_at = next(i for i, c in enumerate(calls) if c.startswith(fail.split()[0]) and fail.split()[-1] in c)
    stops = [c for c in calls[failed_at + 1:] if c.startswith("pg_ctl") and c.endswith(" stop")]
    assert stops == [f"pg_ctl -D {drill} -w -m fast stop"], calls   # 修前：一次 stop 都没有，实例接着占端口
    assert not any(not_reached in c for c in calls[failed_at + 1:] if not c.endswith("-m fast stop")), calls


@requires_bash
def test_停不下来也照样删目录_退出码不被trap盖掉(tmp_path):
    """trap 里 stop 失败若照 set -e 中止，后面的 rm -rf 就不跑了，退出码也变成 stop 的 1。"""
    drill = _drill_dir(tmp_path)
    proc = _run(_env(tmp_path, DRILL_DIR=str(drill), FAKE_FAIL="alembic", FAKE_STOP_FAIL="1"), "medplat")
    assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)
    assert any(c.endswith("-m fast stop") for c in _calls(tmp_path))
    assert not drill.exists()
    assert "照样删除数据目录" in proc.stderr


@requires_bash
def test_第1步恢复到一半失败_半截明文也删掉_没起过的实例不去停(tmp_path):
    drill = _drill_dir(tmp_path)
    proc = _run(_env(tmp_path, DRILL_DIR=str(drill), FAKE_FAIL="pgbackrest"), "medplat")
    assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)
    assert not drill.exists(), list(drill.rglob("*"))
    assert [c.split()[0] for c in _calls(tmp_path)] == ["pgbackrest"]


@requires_bash
def test_调用方给的目录本来就有东西_失败收场时一个字节都不碰(tmp_path):
    """指错了目录（比如生产 PGDATA）：pgbackrest 默认拒绝往非空目录里恢复，第 1 步就失败。
    修前这里本就原样不动；trap 若不分青红皂白 rm -rf，删掉的就是别人的目录。"""
    drill = tmp_path / "not-a-drill-dir"
    drill.mkdir()
    (drill / "PG_VERSION").write_text("16\n", encoding="utf-8")
    proc = _run(_env(tmp_path, DRILL_DIR=str(drill)), "medplat")
    assert proc.returncode == 40, (proc.returncode, proc.stdout, proc.stderr)
    assert (drill / "PG_VERSION").read_text(encoding="utf-8") == "16\n"
    assert sorted(p.name for p in drill.iterdir()) == ["PG_VERSION"]


@requires_bash
def test_调用方的非空目录经delta恢复成功后_中途失败也删(tmp_path):
    """pgbackrest 配了 delta=y 时非空目录也恢复得进去：第 1 步成功后目录里就是恢复出的整库（第 5 步本来就删）。"""
    drill = tmp_path / "reused"
    drill.mkdir()
    (drill / "stale").write_text("上一轮的残留", encoding="utf-8")
    proc = _run(_env(tmp_path, DRILL_DIR=str(drill), FAKE_DELTA="1", FAKE_FAIL="alembic"), "medplat")
    assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)
    assert not drill.exists(), list(drill.rglob("*"))
    assert any(c.endswith("-m fast stop") for c in _calls(tmp_path))


@requires_bash
def test_全部成功_照旧走完第5步_目录删掉且trap不再多停一次(tmp_path):
    drill = _drill_dir(tmp_path)
    proc = _run(_env(tmp_path, DRILL_DIR=str(drill)), "medplat")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "演练完成" in proc.stdout
    assert not drill.exists()
    calls = _calls(tmp_path)
    assert [c.split()[0] for c in calls] == ["pgbackrest", "pg_ctl", "alembic", "psql", "pg_ctl"], calls
    assert calls[-1] == f"pg_ctl -D {drill} -w stop", calls   # 第 5 步原样；trap 不再补一次 -m fast stop
    assert "演练中途退出" not in proc.stderr


@requires_bash
@pytest.mark.parametrize("args, extra", [
    (("--dry-run", "medplat"), {}),                              # 显式 dry-run（假命令都在 PATH 上）
    (("medplat",), {"PG_BIN": "/nonexistent-pg-bin"}),           # 找不到 pg_ctl，自动转 dry-run
])
def test_dry_run只打印_不动任何东西(tmp_path, args, extra):
    drill = tmp_path / "given"
    drill.mkdir()
    (drill / "keep.txt").write_text("调用方的东西", encoding="utf-8")
    proc = _run(_env(tmp_path, DRILL_DIR=str(drill), **extra), *args)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "dry-run 结束" in proc.stdout
    assert _calls(tmp_path) == []
    assert (drill / "keep.txt").read_text(encoding="utf-8") == "调用方的东西"
    assert "演练中途退出" not in proc.stderr   # dry-run 不装收场的 trap


@requires_bash
def test_默认mktemp目录_中途失败也删掉(tmp_path):
    """运维手册的写法不带 DRILL_DIR：恢复进 mktemp -d /tmp/medplat-restore-drill.XXXXXX——扫描说的正是这一份。"""
    proc = _run(_env(tmp_path, FAKE_FAIL="alembic"), "medplat")
    restore = next(c for c in _calls(tmp_path) if c.startswith("pgbackrest"))
    made = Path(restore.split("--pg1-path=", 1)[1].split()[0])
    try:
        assert made.name.startswith("medplat-restore-drill."), restore
        assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)
        assert not made.exists(), f"恢复出的整库留在了 {made}"
    finally:
        shutil.rmtree(made, ignore_errors=True)   # 修前（变异检查）别把 /tmp 弄脏
