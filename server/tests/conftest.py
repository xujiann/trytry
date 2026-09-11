from datetime import date, datetime, timezone
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


# ---------- 共享 client / admin / login（P1-20：74 份复制粘贴收敛于此） ----------
#
# 主流形状只有一种：module 级"重建库表 + 进 lifespan 上下文"的 TestClient，
# 加一个拿 admin Bearer 头的 fixture。以下三件套即该形状的唯一权威副本；
# 测试文件不必再各抄一份。
#
# 覆盖语义（pytest 就近原则）：文件里定义**同名** fixture 即自动覆盖共享版，
# 无需任何注册动作。需要不同形状的模块（函数级隔离、raise_server_exceptions=False、
# 额外的进程内状态复位、临时路由、上传目录清理等）就该这么做——本地定义是
# "有意为之的差异"的显式记号，别为绕开共享版另起名字。
# 注意：本地 client 若是 function 级，依赖它的 admin 也必须一并本地定义，
# 否则 module 级的共享 admin 会与之 ScopeMismatch。


def business_today() -> date:
    """当天业务日期，**在用例里现取，不要在模块顶层快照**。

    ## 为什么单列一个函数

    2026-09-11 的 CI run 552 红了 12 条，全是同一种：

        {'at': '2026-09-10'}         != {'at': '2026-09-11'}
        {'today': '2026-09-10'}      != {'today': '2026-09-11'}
        {'period_end': '2026-10-10'} != {'period_end': '2026-10-11'}

    那一轮 runner 偏慢，单元档跑了 **62 分钟**，23:42 开跑、**跨过了午夜**。
    七个测试文件在**模块顶层**写着 `TODAY = date.today().isoformat()`——
    它在 import 那一刻取值（09-10），而服务端是在请求发生时才取（09-11），
    于是整套断言差一天。

    **这不是 flake，是真缺陷**：判据（期望值）与被判对象（服务端算出的日期）
    取自**两个时刻**，中间隔着整轮测试的时长。CI 什么时候跑不由人定，
    深夜那一班必然踩上；而重跑一次就绿，正是让人误判成 flake 的原因。

    改成现取之后，两个时刻之间只剩**毫秒级**的窗口（发请求 → 断言）。
    ⚠️ 窗口没有归零，只是从"一小时"缩到"几毫秒"——真要归零得让
    `app/clock.py` 成为**全部**日期的唯一来源再冻结它，而目前 `app/` 里还有
    一批直接调 `date.today()` 的地方（基线见 `tests/test_clock.py`），那是另一件事。
    """
    return date.today()


def business_today_str() -> str:
    """`business_today()` 的 `YYYY-MM-DD` 形式，对应 `app/clock.today_str()`。"""
    return business_today().isoformat()


def utc_today_str() -> str:
    """当天 **UTC** 日历日，`YYYY-MM-DD`。

    ## 平台有两个"今天"，用例必须挑与被断对象同源的那个

    - `app/clock.today()` —— **本地**业务日期。随访超期、服务周期、效期预警这类
      按当地日历算的东西走它，对应本文件的 `business_today()`。
    - `models.utcnow()` —— **naive UTC** 时间戳。落库的 `paid_at` / `created_at`
      走它；凡是拿时间戳**再切出日期**的口径（典型是对账：
      `billing._orders_of_day` 比的是 `paid_at.strftime("%Y-%m-%d")`），
      切出来的就是 UTC 日，对应本函数。

    挑错了在 CI 上也看不出来——runner 时区是 UTC，两者恒等；
    换到东八区的开发机上，早八点前跑对账用例就会差一天。

    （顺带记一笔：对账日切在 UTC 而非当地日历，是产品侧口径问题，不是测试问题。
    已登记，未改——改它要动线上已出账的批次归属。）
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def login(client, username, password):
    """登录取 Bearer 头（普通函数，非 fixture；`from conftest import login` 使用）。

    断言 200：登录失败当场报出响应体，比调用点 KeyError('access_token') 可定位。
    """
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def client():
    """module 级共享 TestClient：先重建库表，再进 lifespan 上下文（admin 账号在那里种）。

    惰性 import：保持 `app.main` 在首个用到它的测试模块收集时才加载的现状，
    不因 conftest 提前拉起整个应用。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    """平台管理员（admin/admin123）的 Bearer 头，module 级、跟随 client 生命周期。

    文件本地覆盖了 `client` 时，本 fixture 拿到的就是那个本地版本（pytest 按
    用例所在模块就近解析依赖），无需连带覆盖 admin——除非本地 client 是 function 级。
    """
    return login(client, "admin", "admin123")


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
