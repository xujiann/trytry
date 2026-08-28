import faulthandler
import os
import signal
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEDPLAT_DATABASE_URL"] = "sqlite:///./test_run.db"
# 附件测试落独立目录，避免污染开发环境 uploads/（.gitignore 均已排除）
os.environ["MEDPLAT_UPLOAD_DIR"] = "./test_uploads"
# 测试需要 console 通道回显 debug_code 拿验证码；生产默认关（sms_debug_echo=False）。
os.environ.setdefault("MEDPLAT_SMS_DEBUG_ECHO", "true")


def reset_database():
    """各测试模块开始前重建库表，避免跨模块数据串扰。"""
    from app.database import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _reset_monitor_breaker():
    """每个用例都从干净的熔断器出发。

    `monitor` 的熔断状态是**进程级全局**、冷却按真实的 30 秒算。任何一个用例
    （哪怕是无意的：装了个 `pipeline()` 不工作的假 redis 再发几个请求）把它推开，
    后面 30 秒内所有用例的 `_record_cluster` 都直接返回——集群计数是空的，
    而失败信息会指向"Redis 接线不对"，把人带到完全错误的方向。
    放在这里而不是某一个测试文件里：泄漏是全局的，复位也该是全局的。
    """
    from app import monitor

    monitor._breaker_reset()
    yield
    monitor._breaker_reset()


# ---------- 块5：E2E 开关（Playwright 浏览器全链路，默认跳过） ----------


def pytest_addoption(parser):
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="运行 Playwright 端到端测试（需已装 playwright 与 chromium 内核）",
    )
    parser.addoption(
        "--watchdog",
        action="store_true",
        default=False,
        help="运行看门狗自证用例（故意让用例卡住，验证超时夹具会判它失败）",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: Playwright 浏览器端到端测试，默认跳过，需 --e2e 开启"
    )
    config.addinivalue_line(
        "markers", "timeout(seconds): 覆盖本用例的看门狗超时阈值（见 _test_watchdog）"
    )
    config.addinivalue_line(
        "markers", "watchdog: 看门狗自证用例，默认跳过，需 --watchdog 开启"
    )


def pytest_collection_modifyitems(config, items):
    """未传 --e2e / --watchdog 时，跳过对应标记的用例（保持默认套件离线快速跑完）。"""
    if not config.getoption("--watchdog"):
        skip_watchdog = pytest.mark.skip(
            reason="看门狗自证用例默认跳过（自身要卡满阈值），使用 --watchdog 开启"
        )
        for item in items:
            if "watchdog" in item.keywords:
                item.add_marker(skip_watchdog)
    if config.getoption("--e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="端到端测试默认跳过，使用 --e2e 开启")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


# ---------- 用例超时看门狗：会阻塞的回归测试不是回归测试 ----------
#
# 起因（本轮 F2）：`test_p0_fixes.py::test_ws_heartbeat_revalidates_revoked_token`
# 在有缺陷的旧代码下**挂起而不是失败**——WS 服务端不断开，客户端
# `ws.receive_text()` 无限期阻塞。一条"回归测试"若在缺陷存在时永远不返回，
# 它挡不住任何东西：CI 不是变红，而是被拖到作业超时，谁也不知道是哪条卡住。
# 同形状的等待点全仓还有数十处：`ws.receive_*()`、`threading.Barrier.wait()`
# 无 timeout、`Thread.join()` 无 timeout（tests/ 下 grep 可见）。
#
# **为什么不引 pytest-timeout**：CLAUDE.md 第 12 条（不得无理由引入新依赖），
# 而这件事二十行就能做对——本仓库测试跑在 Linux 主线程，`signal.SIGALRM`
# 正是 pytest-timeout 自己的 "signal" 方案，且是唯一能打断**阻塞系统调用与锁等待**
# 的做法（线程看门狗只能靠 `PyThreadState_SetAsyncExc`，它在字节码边界才生效，
# 打不断卡在 `queue.get()` / `lock.acquire()` 里的主线程——F2 那条恰恰卡在这里）。
#
# 兜底与边界如实写：
# - 平台无 SIGALRM（Windows）或用例不在主线程时**自动旁路**，不改变行为；
# - 阈值 0 = 关闭；`MEDPLAT_TEST_TIMEOUT` 环境变量调全局默认；
#   `@pytest.mark.timeout(n)` 覆盖单条；
# - 超时点先 `faulthandler.dump_traceback()` 打全部线程栈——只说"超时了"
#   等于把排查推给下一个人，栈才回答"卡在哪一行"；
# - 装在 `pytest_runtest_protocol` 的 hookwrapper 上而不是 autouse fixture 上：
#   fixture 的建立顺序按作用域排，module/session 级 fixture（`TestClient(app)`
#   那种）在函数级 autouse fixture**之前**建立，卡在那里就落在窗口外了。
#   包住整条 protocol（setup + call + teardown）才是真覆盖，代价是三段共用
#   一个预算——对"判它失败"这个目的够用。

#: 单条用例的默认上限（秒）。取 120 而不是更小：本套件最慢的单条用例约数十秒
#: （并发用例要起真线程），阈值贴太近会把慢用例误判成挂起。
DEFAULT_TEST_TIMEOUT_SECONDS = int(os.environ.get("MEDPLAT_TEST_TIMEOUT", "120"))


#: e2e（Playwright 全链路）的默认上限另给一档：它要起真进程、开浏览器、
#: 走完整页面流程，按单元用例的尺子量必然误判。默认跳过、不进 CI 阻断门，
#: 但真跑起来时同样不该无限期挂着。
E2E_TEST_TIMEOUT_SECONDS = int(os.environ.get("MEDPLAT_E2E_TEST_TIMEOUT", "600"))


def _resolve_timeout(item) -> float:
    marker = item.get_closest_marker("timeout")
    if marker is not None and marker.args:
        return float(marker.args[0])
    if item.get_closest_marker("e2e") is not None:
        return float(E2E_TEST_TIMEOUT_SECONDS)
    return float(DEFAULT_TEST_TIMEOUT_SECONDS)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """给每条用例装一枚闹钟：超时即判**这一条**失败并打出卡住的调用栈。"""
    timeout = _resolve_timeout(item)
    if (
        timeout <= 0
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        return (yield)

    node_id = item.nodeid

    def _fire(signum, frame):  # pragma: no cover - 只在真挂起时执行
        faulthandler.dump_traceback()
        raise TimeoutError(
            f"用例超时：{node_id} 超过 {timeout:g} 秒未结束，已由看门狗判为失败"
            "（上方是各线程调用栈；若为无 timeout 的等待点，请给它补上 timeout）"
        )

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return (yield)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
