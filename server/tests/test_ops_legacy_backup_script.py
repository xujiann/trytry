"""仓库根的旧 `scripts/backup.sh`（留给既有 crontab 用）：导出失败不再打印「备份完成」、exit 0，也不再接着把好备份删掉
（P2-1076，第三十一批「运维脚本与后台任务」扫描 C2-2）。

原先 `docker compose exec … pg_dump | gzip > 包`：`set -eu` 没有 pipefail，管道的退出码取的是 gzip 的。cron 的工作目录是
家目录、docker compose 找不到编排文件（文件头 crontab 示例正是不 cd 的写法），db 容器不在、口令改了也一样——写出一个
20 字节的空包、打印「备份完成」、exit 0，紧接着按 30 天把之前的好备份删掉。连续失败一个月，一份可用备份都没有。

修后先落临时文件，导出成功且非空才压缩、才清理；当前目录没有编排文件时到仓库根去找。PATH 里放假的 docker 驱动。
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

LEGACY = Path(__file__).resolve().parents[2] / "scripts" / "backup.sh"
REPO_ROOT = LEGACY.parents[1]

FAKE_DOCKER = """#!/bin/sh
pwd >> "$FAKE_DOCKER_LOG"
if [ "${FAKE_DOCKER_FAIL:-}" = "1" ]; then
  echo "no configuration file provided: not found" >&2
  exit 1
fi
if [ "${FAKE_DOCKER_EMPTY:-}" = "1" ]; then exit 0; fi
echo "-- PostgreSQL database dump"
echo "CREATE TABLE users (id integer);"
"""


@pytest.fixture
def world(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    backups = tmp_path / "backups"
    backups.mkdir()
    good = backups / "medplat_20260801_020000.sql.gz"   # 40 天前的好备份
    good.write_bytes(b"old-good-backup")
    old = time.time() - 40 * 86400
    os.utime(good, (old, old))
    cwd = tmp_path / "home"   # 模拟 cron：工作目录里没有编排文件
    cwd.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(tmp_path / "docker.log")}
    env.pop("COMPOSE_FILE", None)
    return {"backups": backups, "good": good, "cwd": cwd, "env": env, "log": tmp_path / "docker.log"}


def _run(world, **extra):
    return subprocess.run(["sh", str(LEGACY), str(world["backups"])], capture_output=True, text=True,
                          env={**world["env"], **extra}, cwd=world["cwd"])


@pytest.mark.parametrize("failure", [{"FAKE_DOCKER_FAIL": "1"}, {"FAKE_DOCKER_EMPTY": "1"}])
def test_导出失败或为空_退出非零_不留空包_好备份一个没删(world, failure):
    proc = _run(world, **failure)
    assert proc.returncode != 0, proc.stdout   # 修前 exit 0
    assert "备份完成" not in proc.stdout        # 修前照样打印
    assert world["good"].exists()               # 修前被 -mtime +30 删掉
    assert sorted(p.name for p in world["backups"].iterdir()) == [world["good"].name]   # 修前多一个 20 字节的空包


def test_导出成功_压缩落盘_再清理过期的(world):
    proc = _run(world)
    assert proc.returncode == 0, proc.stderr
    (fresh,) = [p for p in world["backups"].iterdir() if p != world["good"]]
    import gzip

    assert b"CREATE TABLE users" in gzip.decompress(fresh.read_bytes())
    assert not world["good"].exists()   # 保留天数的清理照旧：只在成功之后


def test_工作目录没有编排文件时到仓库根去找_有的照旧用当前目录(world):
    assert _run(world).returncode == 0
    (world["cwd"] / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    assert _run(world).returncode == 0
    assert world["log"].read_text(encoding="utf-8").split() == [str(REPO_ROOT), str(world["cwd"])]
