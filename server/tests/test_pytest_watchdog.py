"""看门狗自证：挂起的用例会被判失败，而不是把套件拖死。

本轮 F2 的教训——`test_p0_fixes.py::test_ws_heartbeat_revalidates_revoked_token`
在有缺陷的旧代码下**挂起而非失败**：WS 服务端不断开，客户端
`ws.receive_text()` 永远等下去。一条在缺陷存在时不返回的"回归测试"什么也挡不住。

`tests/conftest.py::_test_watchdog` 给每条用例装了 SIGALRM 闹钟。守卫本身
必须被守——所以这里两条用例分别证明它的两个承诺：

1. 它能**打断真正的阻塞等待**（不是只在字节码边界生效的软中断）；
2. 挂起的用例在**套件层面报红并指名是哪一条**，而不是拖到 CI 作业超时。

两条都带 `watchdog` 标记，默认跳过（它们自身要卡满阈值）；
用 `pytest tests/test_pytest_watchdog.py --watchdog` 跑。
"""
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent


@pytest.mark.watchdog
@pytest.mark.timeout(1)
def test_看门狗能打断阻塞等待并把本条用例判失败():
    """卡在锁上（不是 sleep 循环）也要能被打断——F2 那条正是卡在锁等待里。

    `Event.wait()` 底层是 `lock.acquire()`：线程方案的异步异常注入打不断它，
    SIGALRM 可以。看门狗把 TimeoutError 抛在**阻塞的那一行**，故此处可捕获，
    捕获得到本身就是"它确实打断了"的证据。
    """
    never_set = threading.Event()
    with pytest.raises(TimeoutError) as exc:
        never_set.wait()  # 无 timeout 的等待：不打断就是永远
    assert "看门狗" in str(exc.value)
    assert "test_看门狗能打断阻塞等待并把本条用例判失败" in str(exc.value)


@pytest.mark.watchdog
def test_挂起用例让套件报红并指名是哪一条():
    """整套跑起来是什么结果——起一个子 pytest，让它跑一条必挂的用例。

    刻意把挂起点放在**模块级 fixture** 里：这正是 autouse fixture 方案覆盖不到的
    位置（函数级 autouse fixture 在 module 级之后才建立），也是真实套件里
    `TestClient(app)` 一类依赖所在的位置。看门狗装在 `pytest_runtest_protocol`
    上就是为了把 setup 也包进窗口。

    断言的是外层看得见的东西：退出码非 0、输出里点名了那条用例。
    这正是 F2 缺的那一半：光"不会永远卡着"不够，还要能一眼看出是谁卡的。
    """
    hanging = SERVER_DIR / "tests" / "_watchdog_probe_hang.py"
    hanging.write_text(
        "import threading\n"
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture(scope=\"module\")\n"
        "def 卡住的模块级依赖():\n"
        "    threading.Event().wait()  # 模块级 fixture 里挂住\n"
        "\n"
        "\n"
        "@pytest.mark.timeout(1)\n"
        "def test_故意挂起(卡住的模块级依赖):\n"
        "    pass\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(hanging), "-q", "-p", "no:cacheprovider"],
            cwd=str(SERVER_DIR),
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        hanging.unlink(missing_ok=True)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"挂起的用例竟然通过了：\n{output}"
    assert "1 error" in output or "1 failed" in output, output
    assert "test_故意挂起" in output, output
    assert "用例超时" in output, output
