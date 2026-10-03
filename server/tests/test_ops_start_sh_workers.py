"""start.sh 单 worker 分支不写 `--workers`，uvicorn 按 $WEB_CONCURRENCY 起多个 worker（P2-1217，第三十五批扫描 T1-7）。

`start.sh` 的单 worker 分支（MEDPLAT_WORKERS 未设或 ≤1）起 uvicorn 时不带 `--workers`，而 uvicorn 的 `--workers` 缺省取
环境变量 `$WEB_CONCURRENCY`（`uvicorn --help` 原文）：托管平台常注入这个变量，结果起了多个 worker。`config.py` 的多实例
拒启守卫只看 MEDPLAT_WORKERS / MEDPLAT_MIGRATE_ON_START（注释写这是「部署面上仅有的两个信号」），认不出来——没配 Redis 时
令牌黑名单、登录锁定、限流都只在进程内，登出过的令牌在别的 worker 上照样能用。修前实测（scan35 t1/r8，同一条命令加
WEB_CONCURRENCY=2；本批复核时直接 `sh start.sh` 起服务）：起了两个 worker 进程，登出后同一令牌 30 次新连接里 11～12 次 200。
脚本注释写的是「默认 1 保持现行为」。

修法：单 worker 分支显式写 `--workers 1`，worker 数只认 MEDPLAT_WORKERS（守卫与 Redis 逻辑不动）。本文件两层钉住：照
test_ops_uvicorn_access_log 的写法逐条看 start.sh 的启动命令行；再拿一个只记参数的假 `uvicorn` 真跑一遍 start.sh，
看它在 WEB_CONCURRENCY=4 下实际收到的参数。
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
START_SH = ROOT / "server" / "start.sh"

#: 假的 uvicorn：把收到的参数逐行写进 $FAKE_UVICORN_OUT 就退出（start.sh 非演示档走 exec，跑完即返回）
FAKE_UVICORN = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_UVICORN_OUT"
"""


def _launches() -> list[list[str]]:
    """start.sh 里每一条起服务的 uvicorn 命令行（按空白切开）。"""
    text = START_SH.read_text(encoding="utf-8")
    return [line.split() for line in text.splitlines() if "uvicorn app.main:app" in line]


def test_start_sh每条启动命令都显式写了workers():
    lines = _launches()
    assert len(lines) == 2, lines   # 单 worker、多 worker 各一条——少了说明扫描对象变了，本用例在空转
    missing = [" ".join(argv) for argv in lines if "--workers" not in argv]
    assert missing == [], f"不写 --workers，uvicorn 就按 $WEB_CONCURRENCY 起 worker：{missing}"
    # 单 worker 分支钉死 1（修前这一条没有 --workers）；多 worker 分支照旧取 MEDPLAT_WORKERS
    assert sorted(argv[argv.index("--workers") + 1] for argv in lines) == ['"$WORKERS"', "1"], lines


def _run_start_sh(tmp_path: Path, **env_overrides: str) -> list[str]:
    """PATH 最前面放假的 uvicorn，`sh start.sh` 真跑一遍（跳过启动期迁移、非演示档），返回假 uvicorn 收到的参数。"""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "uvicorn").write_text(FAKE_UVICORN, encoding="utf-8")
    (bin_dir / "uvicorn").chmod(0o755)
    out = tmp_path / "argv.txt"
    env = {k: v for k, v in os.environ.items() if k not in ("MEDPLAT_WORKERS", "WEB_CONCURRENCY", "MEDPLAT_SEED_DEMO")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", MEDPLAT_MIGRATE_ON_START="0",
               FAKE_UVICORN_OUT=str(out), **env_overrides)
    proc = subprocess.run(["sh", str(START_SH)], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)
    assert proc.returncode == 0, proc.stderr
    argv = out.read_text(encoding="utf-8").splitlines()
    assert argv[:1] == ["app.main:app"], argv   # 防空转：确实是 start.sh 起的那条命令
    return argv


def _workers(argv: list[str]) -> str:
    assert argv.count("--workers") == 1, f"uvicorn 没收到 --workers，会按 $WEB_CONCURRENCY 起 worker：{argv}"
    return argv[argv.index("--workers") + 1]


def test_平台注入WEB_CONCURRENCY_单worker分支仍只起一个(tmp_path):
    """修前假 uvicorn 收到的参数里没有 --workers：真 uvicorn 就按 WEB_CONCURRENCY=4 起 4 个 worker。"""
    assert _workers(_run_start_sh(tmp_path, WEB_CONCURRENCY="4")) == "1"


@pytest.mark.parametrize("workers", ["", "0", "1", "abc"])
def test_MEDPLAT_WORKERS不大于1或写错_一律钉一个(tmp_path, workers):
    assert _workers(_run_start_sh(tmp_path, MEDPLAT_WORKERS=workers, WEB_CONCURRENCY="4")) == "1"


def test_多worker分支照旧取MEDPLAT_WORKERS(tmp_path):
    assert _workers(_run_start_sh(tmp_path, MEDPLAT_WORKERS="3", WEB_CONCURRENCY="8")) == "3"


def test_前提_uvicorn不带workers时取WEB_CONCURRENCY_显式给1就不看它(monkeypatch):
    """根因所在：`--workers` 缺省取 $WEB_CONCURRENCY；显式给 1，平台注入的值就不起作用。"""
    from uvicorn.config import Config

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert Config("app.main:app", log_config=None).workers == 4   # 修前单 worker 分支就是这个形状
    assert Config("app.main:app", log_config=None, workers=1).workers == 1
