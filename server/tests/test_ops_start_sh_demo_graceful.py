"""演示路径的优雅关闭真的等到 uvicorn 收尾完（P2-1281）。

`start.sh` 的演示路径（MEDPLAT_SEED_DEMO=1）把 uvicorn 放后台、`trap` 转发 SIGTERM、再 `wait` 两次——注释写明「第一次
wait 会被信号打断并返回 128+signo，那时子进程还在收尾，直接退出等于没等」。可脚本第 3 行开着 `set -e`：第一次 wait
被打断返回 143，脚本就地退出，第二次 wait 根本走不到。容器里 PID 1 一退，收尾到一半的 uvicorn 被内核 SIGKILL——在途
请求被硬切、lifespan 的收尾不执行，正是那段注释要防的事。`test_ops_container_lifecycle.py` 只按源码字面数 `wait` 的次数，
照不出来。

这里真跑一遍：假 uvicorn 收到 TERM 后花一秒「收尾」再落一个标记文件退出；脚本收到 SIGTERM 后必须等它收尾完、以它的
退出码退出。修前实测：脚本 143 退出时标记文件还没写。
"""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

START_SH = Path(__file__).resolve().parents[1] / "start.sh"

#: 假的 uvicorn：记下自己的 pid；收到 TERM 先「收尾」一秒、落标记再退出；FAKE_UV_RC 非空时起来就按它退出（模拟自己崩了）
FAKE_UVICORN = """#!/bin/sh
echo $$ > "$FAKE_DIR/uvicorn.pid"
if [ -n "$FAKE_UV_RC" ]; then exit "$FAKE_UV_RC"; fi
trap 'sleep 1; echo graceful > "$FAKE_DIR/uvicorn.done"; exit 0' TERM
while :; do sleep 0.1; done
"""
#: 假的 python：健康检查（-c）一律通过；灌数脚本落一个标记就成功
FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-c" ]; then exit 0; fi
if [ "$1" = "scripts/seed_demo.py" ]; then echo seeded > "$FAKE_DIR/seeded"; exit 0; fi
exit 99
"""


def _wait_for(path: Path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"等不到 {path.name}"
        time.sleep(0.05)


@pytest.fixture()
def demo_start(tmp_path):
    """按演示路径拉起 start.sh（假 uvicorn / 假 python），收尾时按记下的 pid 清掉可能残留的假 uvicorn。"""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name, body in (("uvicorn", FAKE_UVICORN), ("python", FAKE_PYTHON)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    procs: list[subprocess.Popen] = []

    def start(**extra_env: str) -> subprocess.Popen:
        env = {k: v for k, v in os.environ.items() if k not in ("MEDPLAT_WORKERS", "WEB_CONCURRENCY")}
        env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", MEDPLAT_MIGRATE_ON_START="0",
                   MEDPLAT_SEED_DEMO="1", FAKE_DIR=str(tmp_path), **extra_env)
        proc = subprocess.Popen(["sh", str(START_SH)], env=env, cwd=tmp_path,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        return proc

    yield start, tmp_path
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    pid_file = tmp_path / "uvicorn.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_收到SIGTERM_等uvicorn收尾完再退_退出码取uvicorn的(demo_start):
    start, fake_dir = demo_start
    proc = start()
    _wait_for(fake_dir / "seeded")   # 灌完数、走到最后的 wait 了
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=15)
    # 修前：(143, False)——set -e 让第一次被打断的 wait 直接带走脚本，uvicorn 还在收尾
    assert (rc, (fake_dir / "uvicorn.done").exists()) == (0, True), proc.stderr.read() if proc.stderr else ""


def test_uvicorn自己崩了_脚本照旧带出它的退出码(demo_start):
    start, fake_dir = demo_start
    proc = start(FAKE_UV_RC="3")
    assert proc.wait(timeout=60) == 3   # 不收信号的路径行为不变：uvicorn 的退出码原样带出去
    assert not (fake_dir / "uvicorn.done").exists()
