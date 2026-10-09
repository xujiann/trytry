"""块5：Playwright 端到端测试——管理端 SPA 全链路。

覆盖链路：登录 → 决策驾驶舱 → 共享诊断中心开单 → 领取并出报告（危急值）
          → 危急值操作台确认接收 → 处置反馈闭环。

默认跳过（避免离线/无浏览器环境阻断 CI），开启方式见 README「端到端测试」：

    cd server
    pip install playwright && python -m playwright install chromium
    python -m pytest tests/e2e -q --e2e

实现要点：
- 真实拉起 uvicorn 子进程 + 独立 SQLite 库（e2e_run.db），跑完即删，
  不污染开发库与单元测试库；
- 端口由内核分配（避免与本机占用端口冲突）；
- SPA 里**还在用** prompt/confirm 的交互（排班、术中记录、处置反馈、随访办结），
  统一注册 dialog 处理器应答，见 `_answers`；
- 已经换成页内模态框（`spdModal`）的交互走 `_spd_modal`——两者驱动方式完全不同，
  改一处 UI 会让另一种驱动**静默卡住**：模态框不触发 dialog 事件，它的全屏遮罩还会
  拦住后续的一切点击，于是失败现场是"点不到左侧导航"而不是"这个按钮没反应"
  （2026-09-16 实测：出报告改模态框后，红的是下一步的 `_open_page`）。
"""
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import pytest

pytest.importorskip("playwright", reason="端到端测试需要 playwright")
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

# expect() 断言的重试窗口默认只有 5 秒，而本文件把动作超时设为 15 秒（page 夹具）。
# CI 跑在共享的慢机器上，写操作后整页重画（route()）常常不止 5 秒——把两个口径
# 对齐，断言和动作用同一把尺子等页面。
expect.set_options(timeout=15_000)

SERVER_DIR = Path(__file__).resolve().parents[2]
E2E_DB = SERVER_DIR / "e2e_run.db"
# CI 的共享 runner 冷启动 uvicorn（258 张表 create_all + 种子化）比本地慢得多，
# 40 秒在慢盘上偶发不够；90 秒只是上限，就绪即返回，不拖慢正常路径。
STARTUP_TIMEOUT_S = 90


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    """拉起独立 uvicorn 实例（独立库），会话结束后回收进程与库文件。"""
    port = _free_port()
    E2E_DB.unlink(missing_ok=True)
    env = {
        **os.environ,
        "MEDPLAT_DATABASE_URL": f"sqlite:///./{E2E_DB.name}",
        "MEDPLAT_UPLOAD_DIR": "./e2e_uploads",
        "MEDPLAT_LOG_JSON": "0",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=SERVER_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + STARTUP_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("uvicorn 启动失败")
        try:
            with urlopen(f"{url}/api/health", timeout=1) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.3)
    else:
        proc.terminate()
        raise RuntimeError("服务在超时时间内未就绪")
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底强杀
            proc.kill()
        E2E_DB.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def seed(base_url):
    """经 API 预置机构与患者（UI 只验证关键链路，基础数据走接口更稳）。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    org = post(
        "/api/organizations",
        {"name": "E2E县人民医院", "org_type": "lead_hospital", "level": "county"},
        token,
    )
    patient = post(
        "/api/patients",
        {"name": "E2E患者", "id_card": "320981199001019999", "gender": "男"},
        token,
    )
    # T6.8：住院 + 手术链路的前置数据（病区/床位/入院），UI 只驱动关键步骤
    ward = post("/api/inpatient/wards", {"org_id": org["id"], "name": "E2E外科病区"}, token)
    bed = post("/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "E2E-01"}, token)
    admission = post(
        "/api/inpatient/admissions",
        {"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
         "doctor_name": "E2E外科医生", "diagnosis_name": "急性阑尾炎"},
        token,
    )
    room = post("/api/surgery/rooms", {"org_id": org["id"], "name": "E2E一号手术间"}, token)
    # 手术链路需要两个人：申请人与审批人不能是同一个（职责分离，
    # `approve_request` 里明确拒绝"审批本人提出的手术申请"）。
    # 用例此前用 admin 一个人从头做到尾，那条规则加进来之后就一直红着。
    post(
        "/api/users",
        {"username": "e2e_doctor", "password": "passw0rd1", "role": "doctor",
         "full_name": "E2E外科医生", "org_id": org["id"]},
        token,
    )
    doctor_token = post(
        "/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"}
    )["access_token"]
    # 手术申请由**医师**提出：审批环节明确拒绝"审批本人提出的手术申请"（职责分离），
    # 而 UI 用例是以 admin 登录的。走接口预置申请，UI 只驱动审批→排班→术中记录
    # 这三步——与本文件既有做法一致（基础数据走接口更稳）。
    #
    # 曾试过在 UI 里退出再以管理员登录来切换身份，实测是不稳定的：慢机器上
    # 退出与页面重画会打架，时好时坏。换人这件事不该由 UI 用例承担。
    surgery_request = post(
        "/api/surgery/requests",
        {"admission_id": admission["id"], "surgery_name": "腹腔镜阑尾切除术"},
        doctor_token,
    )
    return {"org": org, "patient": patient, "ward": ward, "bed": bed,
            "admission": admission, "room": room, "surgery_request": surgery_request}


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        # 允许用 PLAYWRIGHT_CHROMIUM_PATH 指定已装好的内核：容器镜像里常预装了
        # chromium 但 playwright 的版本目录对不上，为此再下一份浏览器没有必要。
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
        kwargs = {"args": ["--no-sandbox"]}
        if exe:
            kwargs["executable_path"] = exe
        browser = p.chromium.launch(**kwargs)
        yield browser
        browser.close()


@pytest.fixture()
def page(browser, base_url):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(15000)
    yield page
    context.close()


def _login(page, base_url, username="admin", password="admin123"):
    page.goto(base_url)
    page.fill("#login-username", username)
    page.fill("#login-password", password)
    page.click("#login-form button[type=submit]")
    expect(page.locator("#app-view")).to_be_visible()


@contextmanager
def _answers(page, values):
    """按顺序应答连续多个 prompt；退出时摘掉处理器，避免影响后续用例。

    Playwright 的 page.once 只应答一次，而排班/术中记录要连续弹 4 个 prompt，
    用一个持久处理器按序喂值更直观，也不会因为顺序注册出错而卡死。
    """
    pending = iter(values)
    handler = lambda dialog: dialog.accept(next(pending, ""))  # noqa: E731
    page.on("dialog", handler)
    try:
        yield
    finally:
        page.remove_listener("dialog", handler)


def _spd_modal(page, values):
    """应答 `spdModal()` 弹出的页内模态框：按字段名填值 → 确定 → 等遮罩消失。

    与 `_answers`（应答浏览器原生 prompt/confirm）互斥：同一个交互只会是其中一种。
    结尾必须等表单消失——遮罩是 `position:fixed;inset:0`，留在页面上会让后续
    任何点击都超时，而报错指向的是被拦住的那个元素，不是这里。
    """
    form = _fill_spd_modal(page, values)
    form.locator("button[type=submit]").click()
    expect(form).to_have_count(0)


def _fill_spd_modal(page, values):
    form = page.locator("form.panel:has(button[data-cancel])")
    expect(form).to_be_visible()
    for name, value in values.items():
        field = form.locator(f'[name="{name}"]')
        if field.evaluate("el => el.tagName") == "SELECT":
            field.select_option(value)
        else:
            field.fill(value)
    return form


def _spd_modal_rejected(page, values):
    """应答一张由框自己提交（`spdModal` 的 `submit`，P2-607）、这次会被后端拒收的模态框：填值 → 确定 → 等框内出现报错。

    框不关、填的值都在——返回表单，调用方断言报错与保留下来的值，再用 `_spd_modal` 改几项交一次（它会等框关掉）。
    """
    form = _fill_spd_modal(page, values)
    form.locator("button[type=submit]").click()
    expect(form.locator("[data-modal-msg]")).not_to_have_text("")
    expect(form.locator("button[type=submit]")).to_be_enabled()
    return form


def _open_page(page, page_id, title):
    """点击左侧导航进入指定页面（nav 链接带 data-page 标识）。"""
    page.click(f'#nav a[data-page="{page_id}"]')
    expect(page.locator("#main h2")).to_have_text(title)
    expect(page.locator("#page-body")).not_to_contain_text("加载中…")


def _submit(page, selector):
    """提交表单并**等这一页重画完**再返回。

    管理端每次写操作成功后都会 `route()` 重画整页，`#page-body` 的 innerHTML
    被整个替换。测试如果紧接着 `fill` 下一个表单，填进去的值会被这次重画抹掉，
    下一次 `click` 提交的就是一张空表——表现是"某几步没生效"，
    而不是任何一步报错，极难从断言信息看出来。

    这正是本文件此前 4 条用例一直红着的原因：应用没坏（同样的操作手工做、
    或步与步之间加等待，都能走通），是用例没等重画。所以补这个助手，
    而不是去改应用。

    等待条件**不用超时也不用 networkidle**，而是给当前 `#page-body` 打一个标记，
    等到页面上出现一个没有该标记的 `#page-body` 为止——那才是"整页确实重画过了"
    的确定信号。networkidle 会在 route() 还没开始时就判定静默（POST 的响应一到、
    后续 GET 还没发出，网络就空了 500ms），于是 fill 填进了马上要被丢弃的那份
    DOM；这个坑在改用标记之前实测踩到过：填完读回来是空串，而表单类型明明是
    text、单独填又没问题。
    """
    _redrawn(page, lambda: page.click(selector))


def _redrawn(page, action):
    """执行 `action`，并等整页重画完再返回（判据见 `_submit`）。

    模态框同样需要：`_spd_modal` 在表单关掉时就返回了，写请求还在路上——紧接着按接口
    读回会读到旧值、紧接着点下一个按钮会被随后的重画抹掉（2026-09-24 住院页用例实测，
    第一次跑碰巧绿、第二次读回 404「病案首页未填写」）。
    """
    marker = "e2e-stale"
    page.eval_on_selector("#page-body", f"el => el.dataset.stamp = '{marker}'")
    action()
    page.wait_for_function(
        f"() => {{ const el = document.querySelector('#page-body');"
        f" return el && el.dataset.stamp !== '{marker}'; }}"
    )
    expect(page.locator("#page-body")).not_to_contain_text("加载中…")


def test_login_then_dashboard(page, base_url, seed):
    """登录 → 决策驾驶舱：登录成功进入应用壳，驾驶舱指标卡渲染。"""
    _login(page, base_url)
    _open_page(page, "dashboard", "决策驾驶舱")
    expect(page.locator("#page-body .card").first).to_be_visible()


def test_login_rejects_bad_password(page, base_url):
    page.goto(base_url)
    page.fill("#login-username", "admin")
    page.fill("#login-password", "wrong-password")
    page.click("#login-form button[type=submit]")
    # 报的是后端的原话（P2-1225）：原先登录请求的 401 也走「会话过期」分支，口令敲错写成「登录已过期」
    expect(page.locator("#login-error")).to_have_text("用户名或密码错误")
    expect(page.locator("#login-view")).to_be_visible()


def test_医生移动端口令错_报用户名或密码错误_不说登录已失效(page, base_url):
    """P2-1225：医生移动端的 api() 把登录请求的 401 也当会话失效，口令敲错时登录框写「登录已失效，请重新登录」。"""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_no_such_doctor")   # 不存在的账号：不给 admin 记失败次数
    page.fill("#lg-pass", "wrong-password")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#login-error")).to_have_text("用户名或密码错误")
    expect(page.locator("#workbench")).to_be_hidden()


def _todos_fail(route):
    """取待办恒失败：口令超 90 天的账号除改密 / 登出外一律 428（端到端建不出这种账号，拦截接口模拟）。"""
    route.fulfill(status=428, content_type="application/json", body='{"detail": "口令已超过 90 天未修改"}')


@pytest.fixture()
def bell_patient(admin_call):
    """P2-1220 用例的前置：甲院医师 e2e_p21220_a 的铃铛里一条待确认的危急值（本院申请单），乙镇医师 e2e_p21220_b 什么都没有。
    用完把这条危急值闭环：后面的用例点的是危急值操作台上「第一个」确认接收按钮，留一条待确认的就点到它头上。"""
    org = admin_call("POST", "/api/organizations", {"name": "E2E铃铛甲县医院", "org_type": "lead_hospital", "level": "county"})
    other = admin_call("POST", "/api/organizations", {"name": "E2E铃铛乙镇卫生院", "org_type": "township", "level": "township"})
    for username, org_id in (("e2e_p21220_a", org["id"]), ("e2e_p21220_b", other["id"])):
        admin_call("POST", "/api/users", {"username": username, "password": "passw0rd1", "role": "doctor", "org_id": org_id})
    patient = admin_call("POST", "/api/patients", {"name": "E2E铃铛患者", "id_card": "320981197104055262", "gender": "女"})
    req = admin_call("POST", "/api/exams", {"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "lab",
                                            "item_code": "E2E-P21220", "item_name": "E2E铃铛血钾"})
    report = admin_call("POST", f"/api/exams/{req['id']}/report", {
        "finding": "", "conclusion": "E2E铃铛 血钾 6.9（危急值）", "critical": True, "reported_by": "检验科"})
    yield patient
    admin_call("POST", f"/api/exams/reports/{report['id']}/acknowledge")
    admin_call("POST", f"/api/exams/reports/{report['id']}/resolve", {"note": "E2E 用例收尾"})


def test_管理端换人登录_铃铛不留上一位的待办_选过的患者退出即清(page, base_url, bell_patient):
    """P2-1220（第三十五批扫描 T1-8）：管理端退出原先只删令牌、角色、CSRF 三个键——`stopTodoPolling` 只把铃铛藏起来，
    角标和面板原样留着，取待办失败又是静默的：换人登录后，取待办恒 428 的后一位一直挂着前一位的条数，点开是前一位机构的
    危急值结论（todos.py 的 P0-40：别家的危急值不进别人的铃铛）；前一位在「统一申请单中心」筛过的患者成了后一位的默认筛选。
    修后取待办失败、退出都把角标与面板复原；退出清掉指向具体记录的业务选择。"""
    patient = bell_patient
    count, panel = page.locator("#todo-count"), page.locator("#todo-panel")
    sr_patient = page.locator("#sr-form input[name=patient_id]")

    _login(page, base_url, "e2e_p21220_a", "passw0rd1")
    expect(count).to_be_visible()
    expect(panel).to_contain_text("E2E铃铛 血钾 6.9（危急值）")
    page.route("**/api/todos", _todos_fail)   # 取待办失败：清空，不再留着上一次取到的
    page.evaluate("pollTodos()")
    expect(count).to_be_hidden()
    expect(panel).to_be_empty()
    page.unroute("**/api/todos")
    page.evaluate("pollTodos()")
    expect(panel).to_contain_text("E2E铃铛 血钾 6.9（危急值）")
    _open_page(page, "servicerequests", "统一申请单中心")
    sr_patient.fill(str(patient["id"]))
    _submit(page, "#sr-form button")
    expect(sr_patient).to_have_value(str(patient["id"]))

    page.click("#logout")
    expect(page.locator("#login-view")).to_be_visible()
    expect(panel).to_be_empty()   # 修前退出只藏铃铛，面板里还是前一位的危急值
    assert "medplat_sr_patient" not in page.evaluate("Object.keys(localStorage)")
    page.route("**/api/todos", _todos_fail)
    page.fill("#login-username", "e2e_p21220_b")
    page.fill("#login-password", "passw0rd1")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#app-view")).to_be_visible()
    page.click("#todo-bell")
    expect(count).to_be_hidden()   # 修前一直挂着前一位的条数
    expect(panel).to_be_empty()
    _open_page(page, "servicerequests", "统一申请单中心")
    expect(sr_patient).to_have_value("")   # 修前是前一位筛的那位患者


@pytest.fixture()
def bell_refresh_report(admin_call):
    """P2-1312 用例的前置：医师 e2e_p21312 的铃铛里一条待确认的危急值（本院申请单），站内消息里一条未读（这条危急值的通知）。
    用完把这条危急值闭环：后面的用例点的是危急值操作台上「第一个」确认接收 / 处置反馈按钮。"""
    from urllib.error import HTTPError

    org = admin_call("POST", "/api/organizations", {"name": "E2E铃铛刷新县医院", "org_type": "lead_hospital", "level": "county"})
    admin_call("POST", "/api/users", {"username": "e2e_p21312", "password": "passw0rd1", "role": "doctor", "org_id": org["id"]})
    patient = admin_call("POST", "/api/patients", {"name": "E2E铃铛刷新患者", "id_card": "320981197305055318", "gender": "男"})
    req = admin_call("POST", "/api/exams", {"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "lab",
                                            "item_code": "E2E-P21312", "item_name": "E2E铃铛刷新血钾"})
    report = admin_call("POST", f"/api/exams/{req['id']}/report", {
        "finding": "", "conclusion": "E2E铃铛刷新 血钾 7.1（危急值）", "critical": True, "reported_by": "检验科"})
    yield report
    try:
        admin_call("POST", f"/api/exams/reports/{report['id']}/acknowledge")
    except HTTPError:
        pass   # 用例里已在页面上确认接收
    admin_call("POST", f"/api/exams/reports/{report['id']}/resolve", {"note": "E2E 用例收尾"})


def test_确认接收危急值后铃铛立即刷新_医生移动端标已读角标跟着减(page, base_url, bell_refresh_report):
    """P2-1312（第三十八批扫描 AB1-8）：铃铛原先只靠 30 秒轮询，危急值确认接收等改变待办的动作办完只重画本页——铃铛照挂
    「待确认危急值（1）」、下拉里还列着这一条，最长 30 秒；医生移动端标已读后只移除卡片，「未读消息」角标照挂原来的数。"""
    report = bell_refresh_report
    _login(page, base_url, "e2e_p21312", "passw0rd1")
    panel = page.locator("#todo-panel")
    expect(panel).to_contain_text("待确认危急值（1）")
    _open_page(page, "critical", "危急值操作台")
    _redrawn(page, lambda: page.click(f'button[data-ack="{report["id"]}"]'))
    expect(panel).to_contain_text("待确认危急值（0）")   # 修前要等下一拍轮询（登录起 30 秒），这里 15 秒内等不到

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_p21312")
    page.fill("#lg-pass", "passw0rd1")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    notices = page.locator("#todo-list .todo-group", has_text="未读消息")
    badge = notices.locator(".head .badge")
    expect(badge).to_have_text("1")
    notices.locator("button[data-ntread]").first.click()
    expect(notices.locator(".notice")).to_have_count(0)
    expect(badge).to_have_text("0")   # 修前卡片没了、角标还是 1
    expect(badge).to_have_attribute("class", "badge zero")


def test_exam_order_report_and_critical_closed_loop(page, base_url, seed):
    """开单 → 领取 → 出报告（危急值）→ 确认接收 → 处置反馈全链路。"""
    _login(page, base_url)

    # 1) 共享诊断中心开单
    _open_page(page, "exams", "共享诊断中心")
    page.fill("#exam-form input[name=patient_id]", str(seed["patient"]["id"]))
    page.fill("#exam-form input[name=from_org_id]", str(seed["org"]["id"]))
    page.select_option("#exam-form select[name=center_type]", "lab")
    page.fill("#exam-form input[name=item_code]", "E2E-LAB-K")
    page.fill("#exam-form input[name=item_name]", "血钾测定")
    _submit(page, "#exam-form button")
    expect(page.locator("#page-body")).to_contain_text("血钾测定")

    # 2) 领取申请单
    page.click("button[data-claim]")
    expect(page.locator("#page-body")).to_contain_text("诊断中")

    # 3) 出报告并标记为危急值（页内模态框：结论 + 所见 + 危急值下拉）
    #
    # 2026-09-16 前这里是 prompt + confirm 两连问，用 `_answers` 按序喂值
    # （当时的坑记在 `_answers` 的 docstring 里：两个 `page.once` 会被同一个
    # dialog 同时消耗掉）。出报告改成 `spdModal` 之后原生对话框不再出现，
    # `_answers` 喂不出去、遮罩也不会关——真正红的是下一步点不到导航。
    page.click("button[data-report]")
    _spd_modal(page, {
        "conclusion": "血钾 7.2mmol/L，危急",
        "finding": "电解质紊乱",       # 这个字段以前没有入口，报告的所见恒为空
        "critical": "1",               # 下拉：1=是（进危急值闭环）
    })
    expect(page.locator("#page-body")).to_contain_text("危急值")

    # 4) 危急值操作台：确认接收
    _open_page(page, "critical", "危急值操作台")
    expect(page.locator("#page-body")).to_contain_text("血钾 7.2mmol/L")
    page.click("button[data-ack]")
    expect(page.locator("#page-body")).to_contain_text("已确认")

    # 5) 处置反馈闭环（页内表单）。先点取消：原先弹窗输入框点取消照样提交，危急值就此"闭环"、
    #    处置说明一个字没有。重进一次页面按后端真值核对——取消之后仍是"已确认"。
    page.click("button[data-resolve]")
    page.locator("form.panel button[data-cancel]").click()
    expect(page.locator("form.panel:has(button[data-cancel])")).to_have_count(0)
    _open_page(page, "critical", "危急值操作台")
    expect(page.locator("tr", has_text="血钾 7.2mmol/L")).to_contain_text("已确认")
    page.click("button[data-resolve]")
    # P2-607 第十一批：框自己提交——反馈写超了（后端 512 字）报错写在框里、框不关、写的还在，危急值仍是已确认
    form = _spd_modal_rejected(page, {"note": "处" * 513})
    expect(form.locator('[name="note"]')).to_have_value("处" * 513)
    _spd_modal(page, {"note": "已联系患者急诊复查并降钾治疗"})
    expect(page.locator("#page-body")).to_contain_text("已处置")

    # 6) 处置留痕轨迹可查（确认接收 + 处置反馈两条）
    page.click("button[data-trail]")
    expect(page.locator("#crit-trail")).to_contain_text("确认接收")
    expect(page.locator("#crit-trail")).to_contain_text("处置反馈")


def test_统一申请单中心按状态和类型筛_标题照实写(page, base_url, admin_call):
    """P2-1311（第三十八批扫描 AB1-6）：筛选栏原先只有患者号——卡片数得出「待处理」，最新 200 条里却可能一条都没有、又无处按状态筛；
    标题「在办事项（N）」把已完成、已取消也数进去。修后加状态与类型下拉、选了带参数重取，标题不筛写「全部事项」、筛了写「筛选结果」。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E统一申请单卫生院", "org_type": "township", "level": "township"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E统一申请单患者", "id_card": "320981197202025218", "gender": "男"})
    def exam(name):
        return admin_call("POST", "/api/exams", {"patient_id": patient["id"], "from_org_id": org["id"],
                                                 "center_type": "lab", "item_code": "E2E-P21311", "item_name": name})

    pending, done = exam("E2E统一申请单待处理"), exam("E2E统一申请单已完成")
    admin_call("POST", f"/api/exams/{done['id']}/claim")
    admin_call("POST", f"/api/exams/{done['id']}/report", {"finding": "", "conclusion": "E2E 未见异常", "critical": False})

    _login(page, base_url)
    _open_page(page, "servicerequests", "统一申请单中心")
    titles = page.locator("#page-body .panel h3")
    expect(titles.nth(1)).to_contain_text("全部事项（")   # 修前「在办事项（」
    page.fill("#sr-form input[name=patient_id]", str(patient["id"]))
    page.select_option("#sr-form select[name=status]", "pending")   # 修前没有这个下拉
    page.select_option("#sr-form select[name=request_type]", "exam")
    _submit(page, "#sr-form button")

    expect(titles.nth(1)).to_have_text("筛选结果（1）")
    rows = page.locator("#page-body table tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.first.locator("td").nth(1)).to_have_text(str(pending["id"]))
    expect(rows.first).to_contain_text("E2E统一申请单待处理")
    expect(rows.first).to_contain_text("待处理")
    expect(page.locator("#sr-form select[name=status]")).to_have_value("pending")   # 重画后选中的还在
    expect(page.locator("#sr-form select[name=request_type]")).to_have_value("exam")


@pytest.fixture(scope="session")
def recognition_seed(base_url, seed):
    """开单前互认的前置：同一患者同一项目 30 天内已有一份报告（互认目录未配置 = 不管控）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    source = call("/api/exams", {"patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"],
                                 "center_type": "lab", "item_code": "E2E-RC-1",
                                 "item_name": "E2E互认血常规"}, doctor)
    call(f"/api/exams/{source['id']}/report", {"conclusion": "E2E互认源报告：血常规未见异常"}, doctor)
    return {"source": source, "read": lambda path: call(path, None, admin)}


def test_开单前互认在页内表单里选_取消即不开单(page, base_url, seed, recognition_seed):
    """P2-38：开单前命中可互认，原先是 confirm「确定=互认」——取消就是"不互认"，接着弹理由框，
    理由框再点取消照样开单（理由记"未填写"）：想放弃开单的人连点两次取消，反而开出一张重复检查。
    换成页内表单：互认与否显式选，取消就是不开单（按接口核对单数没变）；不互认的理由落库；
    选互认则新单直接是"已互认"、指向原报告。"""
    source_id = recognition_seed["source"]["id"]

    def orders():
        rows = recognition_seed["read"]("/api/exams?limit=500")
        return sorted((r for r in rows if r["item_code"] == "E2E-RC-1"), key=lambda r: r["id"])

    def fill_and_submit():
        page.fill("#exam-form input[name=patient_id]", str(seed["patient"]["id"]))
        page.fill("#exam-form input[name=from_org_id]", str(seed["org"]["id"]))
        page.select_option("#exam-form select[name=center_type]", "lab")
        page.fill("#exam-form input[name=item_code]", "E2E-RC-1")
        page.fill("#exam-form input[name=item_name]", "E2E互认血常规")
        page.click("#exam-form button")

    assert [r["id"] for r in orders()] == [source_id]
    _login(page, base_url)
    _open_page(page, "exams", "共享诊断中心")
    fill_and_submit()
    modal = page.locator("form.panel:has(button[data-cancel])")
    expect(modal).to_contain_text("E2E互认源报告：血常规未见异常")
    modal.locator("select[name=decision]").select_option("decline")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert [r["id"] for r in orders()] == [source_id], "点了取消却照样开了单"

    page.click("#exam-form button")  # 取消不清表：原样再交一次
    # 不互认理由写超了（后端 256 字）：框自己提交，报错写在框里、框不关、没开单（P2-607 第七批）
    form = _spd_modal_rejected(page, {"decision": "decline", "reason": "理" * 257})
    expect(form.locator('[name="reason"]')).to_have_value("理" * 257)
    assert [r["id"] for r in orders()] == [source_id]
    _redrawn(page, lambda: _spd_modal(page, {"decision": "decline", "reason": "患者要求本院复查"}))
    declined = orders()[-1]
    assert declined["id"] != source_id and declined["status"] == "pending", declined
    assert declined["recognition_declined_reason"] == "患者要求本院复查", declined

    fill_and_submit()
    _redrawn(page, lambda: _spd_modal(page, {"decision": "accept"}))
    accepted = orders()[-1]
    assert accepted["status"] == "recognized" and accepted["recognized_from_id"] == source_id, accepted
    # 互认单那一行打得开依据的报告（P2-1200）：互认不另出报告，修前这一行一个报告按钮都没有
    with page.expect_popup() as popup:
        page.locator("tr", has_text="已互认").filter(has_text="E2E互认血常规").locator(
            "button", has_text="查看依据报告").click()
    expect(popup.value.locator("body")).to_contain_text("E2E互认源报告：血常规未见异常")


@pytest.fixture(scope="session")
def correction_seed(base_url, seed):
    """更正 / 注销申请审核的前置：一名患者，窗口代提一条改姓名的更正申请、一条注销申请。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    patient = call("/api/patients", {"name": "E2E更正患者", "id_card": "320000198808086673"}, admin)
    fix = call("/api/consents/corrections", {
        "patient_id": patient["id"], "request_type": "correction",
        "changes": {"name": "E2E更正后姓名"}, "reason": "登记时姓名录错"}, doctor)
    deact = call("/api/consents/corrections", {
        "patient_id": patient["id"], "request_type": "deactivate", "reason": "疑似重复建档"}, doctor)
    return {"patient": patient, "fix": fix, "deactivate": deact,
            "read": lambda path: call(path, None, admin)}


def test_更正注销申请的审核在页内表单里填_取消即不审(page, base_url, correction_seed):
    """P2-38：「通过」原先弹审核意见框，点取消照样通过——**通过即改写患者主索引，注销申请则直接注销
    档案**。换成页内表单：取消就是不审（按接口核对申请仍待审、档案没动），通过前表单上方写明这一下
    会改什么；拒绝不填意见由后端报人话，填了才拒。"""
    read = correction_seed["read"]
    fix_id, deact_id = correction_seed["fix"]["id"], correction_seed["deactivate"]["id"]
    ehc_no = correction_seed["patient"]["ehc_no"]

    def status(rid):
        (row,) = [r for r in read("/api/consents/corrections?limit=500") if r["id"] == rid]
        return row["status"]

    def button(rid, verdict):
        return f'#cr-table button[data-review="{rid}"][data-verdict="{verdict}"]'

    _login(page, base_url)
    _open_page(page, "consents", "知情同意与行权")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(button(fix_id, "approved"))
    expect(modal).to_contain_text("E2E更正后姓名")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert status(fix_id) == "pending", "点了取消却照样通过了"
    assert read(f"/api/patients/{ehc_no}")["name"] == "E2E更正患者"
    page.click(button(fix_id, "approved"))
    _spd_modal(page, {"comment": "已核对身份证原件"})
    expect(page.locator("#cr-msg")).to_contain_text("已处理")
    assert status(fix_id) == "approved"
    assert read(f"/api/patients/{ehc_no}")["name"] == "E2E更正后姓名"

    page.click(button(deact_id, "approved"))
    expect(modal).to_contain_text("注销")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert status(deact_id) == "pending", "点了取消却照样注销了"
    page.click(button(deact_id, "rejected"))
    # P2-607 第十批：框自己提交——没写意见的报错写在框里、框不关（原先报在页面消息行、框已关）
    form = _spd_modal_rejected(page, {"comment": ""})
    expect(form.locator("[data-modal-msg]")).to_contain_text("拒绝申请必须填写审核意见")
    assert status(deact_id) == "pending"
    _spd_modal(page, {"comment": "核实非重复建档，不予注销"})
    expect(page.locator("#cr-msg")).to_contain_text("已处理")
    assert status(deact_id) == "rejected"
    assert read(f"/api/patients/{ehc_no}")["name"] == "E2E更正后姓名"


@pytest.fixture(scope="session")
def dual_channel_seed(base_url, seed):
    """双通道审核的前置：同一患者两条待审核的双通道申报（一条用来批准、一条用来驳回）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    apps = [call("/api/insurance/dual-channel", {"patient_id": seed["patient"]["id"], "drug_name": name,
                                                 "reason": "院内无药"}, doctor)
            for name in ("E2E双通道药甲", "E2E双通道药乙")]
    return {"apps": apps, "read": lambda path: call(path, None, admin)}


def test_双通道申报审核在页内表单里填_取消即不审(page, base_url, dual_channel_seed):
    """P2-38：双通道申报的「批准」「驳回」原先弹意见框，点取消照样批准 / 驳回（意见记空）。
    换成页内表单：取消就是不审（按接口核对仍待审核），再走完并读回审核意见。"""
    read = dual_channel_seed["read"]
    ok_id, no_id = (a["id"] for a in dual_channel_seed["apps"])

    def app(aid):
        (row,) = [a for a in read("/api/insurance/dual-channel") if a["id"] == aid]
        return row

    _login(page, base_url)
    _open_page(page, "insurance", "医保协同")
    modal = page.locator("form.panel:has(button[data-cancel])")
    for aid, attr, comment, status in ((ok_id, "dualok", "符合双通道药品目录", "approved"),
                                       (no_id, "dualno", "院内已有同通用名药品", "rejected")):
        page.click(f'button[data-{attr}="{aid}"]')
        modal.locator("button[data-cancel]").click()
        expect(modal).to_have_count(0)
        assert app(aid)["status"] == "pending", f"{attr}：点了取消却照样审了"
        page.click(f'button[data-{attr}="{aid}"]')
        # P2-607 第十批：框自己提交——意见写超了（后端 512 字）报错写在框里、框不关、写的还在，申报不动
        form = _spd_modal_rejected(page, {"comment": "审" * 513})
        expect(form.locator('[name="comment"]')).to_have_value("审" * 513)
        assert app(aid)["status"] == "pending"
        _redrawn(page, lambda: _spd_modal(page, {"comment": comment}))
        row = app(aid)
        assert (row["status"], row["review_comment"]) == (status, comment), row


def test_医保协同页经办医师也打得开_表单按角色给(page, base_url, seed):
    """P1-175：页面一个 Promise.all 连管理层才看得到的基金监测一起取，医师、经办一进来整页只剩「需要以下角色之一：
    管理层」——特病 / 双通道申报这些给他们用的表单一张也看不到。改后基金监测只给管理层取，表单与审核按钮按接口的
    角色守卫给。"""
    _login(page, base_url, "e2e_doctor", "passw0rd1")
    _open_page(page, "insurance", "医保协同")
    expect(page.locator("#page-body")).not_to_contain_text("需要以下角色之一")   # 修前：整页就这一句
    expect(page.locator("#spec-form")).to_be_visible()
    expect(page.locator("#dual-form")).to_be_visible()
    expect(page.locator("#ins-form")).to_have_count(0)   # 结算登记只给经办
    expect(page.locator("#page-body .cards")).to_have_count(0)   # 基金监测只给管理层


@pytest.fixture(scope="session")
def ph_event_seed(base_url, seed):
    """公卫事件处置的前置：一起进行中的突发公卫事件。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    event = call("/api/publichealth/events", {"title": "E2E聚集性发热事件", "level": "IV",
                                              "disease_name": "流感样病例"}, doctor)
    return {"event": event, "read": lambda path: call(path, None, admin)}


def test_公卫事件处置记录在页内表单里填_取消即不记(page, base_url, ph_event_seed):
    """P2-38：「处置记录」原先两连问，第二问「执行人」点取消照样提交（执行人记空）。
    换成一个表单：取消就是不记（按接口核对一条都没落），填了才记，并读回动作与执行人。"""
    eid = ph_event_seed["event"]["id"]

    def actions():
        return ph_event_seed["read"](f"/api/publichealth/events/{eid}/actions")

    _login(page, base_url)
    _open_page(page, "publichealth", "公卫协同")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-act="{eid}"]')
    modal.locator('[name="action"]').fill("现场流调")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert actions() == [], "点了取消却照样记了一条"
    page.click(f'button[data-act="{eid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"action": "现场流调", "actor": "E2E疾控张三"}))
    assert [(a["action"], a["actor"]) for a in actions()] == [("现场流调", "E2E疾控张三")]


def test_公卫事件的处置记录看得见_结案之后照样能查(page, base_url, admin_call):
    """P2-477：处置记录原先只记得进——事件列表只有登记与结案两个按钮，记了什么、谁做的页面上看不到，
    结案之后连按钮都没有。"""
    ev = admin_call("POST", "/api/publichealth/events",
                    {"title": "E2E处置可查事件", "level": "III", "disease_name": "诺如病毒"})
    admin_call("POST", f"/api/publichealth/events/{ev['id']}/actions", {"action": "E2E 采样送检", "actor": "E2E疾控李四"})

    _login(page, base_url)
    _open_page(page, "publichealth", "公卫协同")
    _redrawn(page, lambda: page.click(f'button[data-view="{ev["id"]}"]'))
    expect(page.locator("tr", has_text="E2E 采样送检")).to_contain_text("E2E疾控李四")   # 修前页面上没有处置记录

    # 页内登记一条：记完这一起的处置记录就展开着，刚记的那条在眼前
    page.click(f'button[data-act="{ev["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {"action": "E2E 环境消杀", "actor": "E2E疾控王五"}))
    expect(page.locator("tr", has_text="E2E 环境消杀")).to_contain_text("E2E疾控王五")

    # 结案之后照样能查（修前已结案的事件操作栏只有一个「—」）
    admin_call("POST", f"/api/publichealth/events/{ev['id']}/close")
    _redrawn(page, lambda: page.click(f'button[data-view="{ev["id"]}"]'))   # 重画一遍，读到已结案
    expect(page.locator(f'button[data-act="{ev["id"]}"]')).to_have_count(0)
    expect(page.locator(f'button[data-view="{ev["id"]}"]')).to_have_count(1)
    expect(page.locator("tr", has_text="E2E 采样送检")).to_have_count(1)
    expect(page.locator("tr", has_text="E2E 环境消杀")).to_have_count(1)


def test_症候群日报同日再报_先重画再提示已覆盖原上报(page, base_url, admin_call):
    """P2-1432（第四十二批扫描 AF2-1）：同机构同症候群同日按覆盖，后端回 `overwritten: true`，页面原先走 postAction、
    回执整个丢掉——发热门诊报的 8 例被儿科报的 5 例盖掉、预警跟着消失，页面上只是那一行悄悄变成 5。
    修后先重画、再提示盖掉的是哪天哪家的哪个症候群、原来几例。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E症候群覆盖卫生院", "org_type": "township", "level": "township"})

    def report(case_count, threshold=""):
        form = page.locator("#syn-form")
        form.locator('[name="org_id"]').fill(str(org["id"]))
        form.locator('[name="syndrome"]').select_option("fever")
        form.locator('[name="case_count"]').fill(str(case_count))
        form.locator('[name="threshold"]').fill(threshold)
        form.locator('[name="record_date"]').fill("2026-09-15")
        _submit(page, "#syn-form button")

    _login(page, base_url)
    _open_page(page, "surveillance", "多点触发监测")
    report(8, "6")
    expect(page.locator("#syn-msg")).to_have_text("")   # 首报：照旧重画，不提示
    report(5)
    # 修前什么都不说
    expect(page.locator("#syn-msg")).to_have_text("已覆盖 2026-09-15 E2E症候群覆盖卫生院 的「发热」原上报（原 8 例）")
    rows = admin_call("GET", f"/api/surveillance/syndromes?org_id={org['id']}")
    assert [(r["record_date"], r["case_count"], r["threshold"]) for r in rows] == [("2026-09-15", 5, 6)]


def test_门诊接诊登记成功_就地列出这位患者的诊间公卫提醒(page, base_url, admin_call):
    """P2-1434（第四十二批扫描 AF2-8）：诊间提醒原先只能在「公卫协同」页手输患者 ID 查，门诊接诊登记成功只是整页重画——
    医生接诊时看不到这位患者的疫苗禁忌、随访超期、处置中的公卫事件。修后先写「登记成功」，再就地列出这位患者的提醒。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E接诊提醒卫生院", "org_type": "township", "level": "township"})
    patient = admin_call("POST", "/api/patients",
                         {"name": "E2E接诊提醒患者", "id_card": "320981198804041434", "gender": "女"})
    admin_call("POST", "/api/vaccination/contraindications", {
        "patient_id": patient["id"], "vaccine_code": "E2E-HPV9", "reason": "E2E 青霉素过敏", "contra_type": "permanent"})

    _login(page, base_url)
    _open_page(page, "archive", "患者360视图")
    form = page.locator("#enc-form")
    form.locator('[name="patient_id"]').fill(str(patient["id"]))
    form.locator('[name="org_id"]').fill(str(org["id"]))
    form.locator('[name="diagnosis_name"]').fill("E2E上呼吸道感染")
    _submit(page, "#enc-form button")
    expect(page.locator("#enc-msg")).to_contain_text("登记成功（就诊ID ")
    # 修前登记完一个提醒都不取
    expect(page.locator("#enc-reminders")).to_contain_text("疫苗 E2E-HPV9 禁忌：E2E 青霉素过敏")
    rows = admin_call("GET", f"/api/encounters?patient_id={patient['id']}")
    assert [(r["org_id"], r["diagnosis_name"]) for r in rows] == [(org["id"], "E2E上呼吸道感染")]


@pytest.fixture(scope="session")
def labqc_seed(base_url, seed):
    """室内质控失控处理的前置：一个质控批号，录一个 z=+5 的点（1-3s 失控）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    lot = call("/api/labqc/lots", {"org_id": seed["org"]["id"], "item_code": "E2E-QC-K",
                                   "item_name": "E2E质控血钾", "lot_no": "E2E-LOT-1",
                                   "target_value": 4.0, "sd": 0.1}, doctor)
    point = call(f"/api/labqc/lots/{lot['id']}/measurements", {"value": 4.5}, doctor)
    assert point["out_of_control"], point
    return {"lot": lot, "point": point, "read": lambda path: call(path, None, admin)}


def test_室内质控失控处理在页内表单里填_取消即放弃(page, base_url, labqc_seed):
    """P2-38：失控处理原先两连问，第二问点取消就交上一个空措施、被后端 422 拒回。
    换成一个表单，两项必填，取消就是放弃（按接口核对仍未处理），再登记并读回。"""
    lot_id, mid = labqc_seed["lot"]["id"], labqc_seed["point"]["id"]

    def point():
        (row,) = [m for m in labqc_seed["read"](f"/api/labqc/lots/{lot_id}/measurements") if m["id"] == mid]
        return row

    _login(page, base_url)
    _open_page(page, "labqc", "检验室内质控")
    page.click(f'button[data-lot="{lot_id}"]')
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-handle="{mid}"]')
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert point()["handled"] is False
    page.click(f'button[data-handle="{mid}"]')
    _spd_modal(page, {"reason": "质控品复溶后放置过久", "corrective_action": "更换质控品复测在控"})
    expect(page.locator("#lot-detail")).to_contain_text("已处理")
    # P2-492：原因、纠正措施、处理人原先只进了库，L-J 表上只剩一个「已处理」
    handled = page.locator("#lot-detail tr", has_text="已处理")
    expect(handled).to_contain_text("原因：质控品复溶后放置过久；纠正措施：更换质控品复测在控")
    row = point()
    assert (row["handled"], row["handle_reason"], row["corrective_action"]) == (
        True, "质控品复溶后放置过久", "更换质控品复测在控"), row


def test_室内质控批号还没有测定点时_页内改靶值SD_录了点之后不再摆(page, base_url, seed, admin_call):
    """P2-1368：批号的靶值 / SD 建好后改不了，SD 多敲一位（0.1 录成 1.0）之后永远判不出失控。还没有测定点的批号在 L-J 面板上
    摆「改靶值 / SD」，框里预填现值、由框自己提交，改完重画、台账里换成新的；录了第一个测定值之后不再摆（后端 409，既往判定
    怎么处理待裁定）。"""
    lot = admin_call("POST", "/api/labqc/lots", {"org_id": seed["org"]["id"], "item_code": "E2E-1368", "item_name": "E2E改靶值批号",
                                                 "lot_no": "E2E-1368-LOT", "target_value": 4.0, "sd": 1.0})
    _login(page, base_url)
    _open_page(page, "labqc", "检验室内质控")
    page.click(f'button[data-lot="{lot["id"]}"]')
    page.click(f'button[data-baseline="{lot["id"]}"]')                  # 修前面板上无处可改
    modal = _modal(page)
    expect(modal.locator('[name="target_value"]')).to_have_value("4")   # 预填现值
    expect(modal.locator('[name="sd"]')).to_have_value("1")
    _redrawn(page, lambda: _spd_modal(page, {"sd": "0.1"}))
    (saved,) = [row for row in admin_call("GET", "/api/labqc/lots") if row["id"] == lot["id"]]
    assert (saved["target_value"], saved["sd"]) == (4.0, 0.1), saved
    expect(page.locator("#page-body tr", has_text="E2E-1368-LOT")).to_contain_text("0.1")
    admin_call("POST", f"/api/labqc/lots/{lot['id']}/measurements", {"value": 4.45})   # z=+4.5，按新 SD 判失控
    page.click(f'button[data-lot="{lot["id"]}"]')
    expect(page.locator("#lot-detail")).to_contain_text("失控 1-3s")
    expect(page.locator(f'button[data-baseline="{lot["id"]}"]')).to_have_count(0)   # 有测定点了，入口不再摆


@pytest.fixture(scope="session")
def contract_seed(base_url, seed):
    """家医签约页的前置：一份履约中的签约。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    contract = call("/api/contracts", {"patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"],
                                       "doctor_name": "E2E家庭医生", "package": "standard"}, doctor)
    return {"contract": contract, "read": lambda path: call(path, None, admin)}


def test_停用的目录与批号不再摆出填了也交不上的表单(page, base_url, seed, admin_call):
    """第十五批 S1-7：停用的对象，后端都拒（404 / 409），页面却照摆提交入口——填完才被拒。
    ①专病目录停用后「打开」仍摆入组表单；②检验质控停用的批号仍摆「录入测定值」；③「生成随访计划」的方案下拉含停用方案。
    修后前两处换成一句说明（历史照看），第三处只列启用的。"""
    program = admin_call("POST", "/api/disease-programs", {"code": "E2E_S17_DP", "name": "E2E 停用专病"})
    admin_call("PATCH", f"/api/disease-programs/{program['id']}", {"active": False})
    lot = admin_call("POST", "/api/labqc/lots", {"org_id": seed["org"]["id"], "item_code": "E2E-S17", "item_name": "E2E停用批号",
                                                 "lot_no": "E2E-S17-LOT", "target_value": 5.0, "sd": 0.2})
    admin_call("PATCH", f"/api/labqc/lots/{lot['id']}", {"active": False})
    rule = admin_call("POST", "/api/spd/followup-rules", {"code": "E2E_S17_RULE", "name": "E2E 停用随访方案",
                                                          "scene": "outpatient", "points": [7]})
    admin_call("PATCH", f"/api/spd/followup-rules/{rule['id']}", {"active": False})
    _login(page, base_url)

    _open_page(page, "diseaseprograms", "专病管理")
    _redrawn(page, lambda: page.click(f'button[data-dppick="{program["id"]}"]'))
    expect(page.locator("#page-body")).to_contain_text("该专病目录已停用，不再入组")
    expect(page.locator("#dp-enroll")).to_have_count(0)                  # 修前照摆，入组 409「该专病目录已停用」

    _open_page(page, "labqc", "检验室内质控")
    page.click(f'button[data-lot="{lot["id"]}"]')
    expect(page.locator("#lot-detail")).to_contain_text("该批号已停用，不再录入测定值")
    expect(page.locator("#meas-form")).to_have_count(0)                  # 修前照摆，录入 409「批号已停用」

    _open_page(page, "spdfollowup", "智能随访服务端")
    options = page.locator('#spd-fuplan-form select[name="rule_id"] option')
    expect(options.first).to_be_attached()
    assert "E2E 停用随访方案" not in options.all_inner_texts()          # 修前列着，生成 404「随访方案不存在或已停用」


def test_家医签约的履约与解约都在页内表单里_取消即不动(page, base_url, contract_seed):
    """P2-38：「记录履约」原先两连问，类型要手打英文代码、备注框点取消照样记；「解约」点一下就生效，
    没有任何确认。换成页内表单：类型从下拉里选，取消就是不记 / 不解（按接口核对），再走完并读回。"""
    read = contract_seed["read"]
    cid = contract_seed["contract"]["id"]

    def status():
        (row,) = [c for c in read("/api/contracts?limit=500") if c["id"] == cid]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "contracts", "家医签约")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-svc="{cid}"]')
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert read(f"/api/contracts/{cid}/services") == [], "点了取消却照样记了履约"
    page.click(f'button[data-svc="{cid}"]')
    form = _spd_modal_rejected(page, {"service_type": "visit", "note": "履" * 513})   # 备注写超了：框不关（P2-607 第七批）
    expect(form.locator('[name="note"]')).to_have_value("履" * 513)
    assert read(f"/api/contracts/{cid}/services") == []
    _spd_modal(page, {"service_type": "visit", "note": "上门测血压，嘱低盐饮食"})
    expect(page.locator("#ct-msg")).to_contain_text("履约已记录")
    assert [(x["service_type"], x["note"]) for x in read(f"/api/contracts/{cid}/services")] \
        == [("visit", "上门测血压，嘱低盐饮食")]

    page.click(f'button[data-term="{cid}"]')
    expect(modal).to_contain_text("须重新签约")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert status() == "active", "点了取消却照样解约了"
    page.click(f'button[data-term="{cid}"]')
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert status() == "terminated"

    # P2-494：履约记录原先只能记、不能看；解约之后照样查得到
    expect(page.locator(f'button[data-svc="{cid}"]')).to_have_count(0)
    page.click(f'button[data-svclist="{cid}"]')
    services = page.locator("#ct-services")
    expect(services).to_contain_text("履约记录（1 次）")
    expect(services.locator("tr", has_text="上门测血压，嘱低盐饮食")).to_contain_text("上门服务")


@pytest.fixture(scope="session")
def green_channel_seed(base_url, seed):
    """急救绿道的前置：一起胸痛通道的急救事件。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    case = call("/api/emergency/cases", {"location": "E2E绿道事发地", "symptom": "持续胸痛",
                                         "channel_type": "chest_pain", "dest_org_id": seed["org"]["id"]}, doctor)
    return {"case": case, "read": lambda path: call(path, None, admin)}


def test_绿道节点在页内表单里录_节点下拉_取消即不录(page, base_url, green_channel_seed):
    """P2-38：「录节点」原先要按"1=发病，2=呼救…"输序号、再手打时刻。换成页内表单：节点下拉，
    取消就是不录（按接口核对该节点仍缺失）；时刻写错由后端报人话；写对了读回时间轴。"""
    cid = green_channel_seed["case"]["id"]

    def recorded():
        tl = green_channel_seed["read"](f"/api/emergency/cases/{cid}/timeline")
        return {m["milestone"]: m["occurred_at"] for m in tl["timeline"] if m["recorded"]}

    _login(page, base_url)
    _open_page(page, "emtimeline", "急救绿道时间轴")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-mile="{cid}"]')
    modal.locator('[name="milestone"]').select_option("call")
    modal.locator('[name="occurred_at"]').fill("2026-09-20 08:05")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert recorded() == {}, "点了取消却照样录了节点"
    page.click(f'button[data-mile="{cid}"]')
    _spd_modal(page, {"milestone": "call", "occurred_at": "昨天早上八点"})
    expect(page.locator("#gc-msg")).to_contain_text("occurred_at")
    assert recorded() == {}
    page.click(f'button[data-mile="{cid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"milestone": "call", "occurred_at": "2026-09-20 08:05"}))
    assert list(recorded()) == ["call"] and recorded()["call"].startswith("2026-09-20"), recorded()


def test_急救回传体征_心跳骤停记0_留空才是未测(page, base_url, seed, green_channel_seed):
    """P2-249：「回传体征」的心率原先是数字框再 `|| null`——spdModal 的数字框把空值读成 0，页面再把 0 改成 null，
    心跳骤停记的 0 与「未测」混成同一个 null（后端写着 0 照收，抢救现场心跳骤停记 0 是真实的）。改成文本框自己解析：
    0 记 0、留空记未测、填错了页内提示且不落库。按接口读回核对。"""
    import json
    from urllib.request import Request

    doctor = json.loads(urlopen(Request(f"{base_url}/api/auth/login", data=json.dumps(
        {"username": "e2e_doctor", "password": "passw0rd1"}).encode(),
        headers={"Content-Type": "application/json"}), timeout=10).read())["access_token"]
    cid = json.loads(urlopen(Request(f"{base_url}/api/emergency/cases", data=json.dumps(
        {"location": "E2E体征事发地", "symptom": "意识丧失", "dest_org_id": seed["org"]["id"]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {doctor}"}), timeout=10).read())["id"]

    def heart_rates():
        return [v["heart_rate"] for v in green_channel_seed["read"](f"/api/emergency/cases/{cid}/vitals")]

    _login(page, base_url)
    _open_page(page, "emergency", "智慧急救")
    page.click(f'button[data-vital="{cid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"heart_rate": "0", "note": "心跳骤停"}))
    assert heart_rates() == [0], heart_rates()   # 修前 [None]：心跳骤停记成未测
    page.click(f'button[data-vital="{cid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"heart_rate": "", "note": "未测"}))
    assert heart_rates() == [0, None], heart_rates()
    page.click(f'button[data-vital="{cid}"]')
    _spd_modal(page, {"heart_rate": "八十", "note": "填错"})
    expect(page.locator("#em-msg")).to_contain_text("心率须填数字")
    assert heart_rates() == [0, None], heart_rates()

    # P2-491：血压、血氧原先录不进（弹窗只有心率），途中体征也没有一个页面看得见（接口写着「院内可实时调阅」）
    page.click(f'button[data-vital="{cid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"heart_rate": "112", "sbp": "86", "dbp": "52", "spo2": "90",
                                             "note": "E2E 转运中血压下降"}))
    vitals = page.locator("#em-vitals")
    expect(vitals).to_contain_text("途中体征（3 次）")   # 记完就展开这一起
    expect(vitals.locator("tr", has_text="E2E 转运中血压下降")).to_contain_text("86/52")
    expect(vitals.locator("tr", has_text="心跳骤停").locator("td").nth(1)).to_have_text("0")   # 0 照原样，不是「—」
    page.locator("#em-vitals").evaluate("el => el.innerHTML = ''")
    page.click(f'button[data-vitals="{cid}"]')
    expect(vitals.locator("tr", has_text="未测").locator("td").nth(1)).to_have_text("—")


@pytest.fixture(scope="session")
def home_visit_seed(base_url, seed):
    """上门服务的前置：两张待派单的上门工单（一张走派单→完成、一张用来取消）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    orders = [call("/api/homevisits", {"patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"],
                                       "service_type": "nursing", "demand": demand}, doctor)
              for demand in ("E2E上门换药", "E2E上门采血")]
    return {"orders": orders, "read": lambda path: call(path, None, admin)}


def test_上门服务派单完成与取消都在页内表单里_取消即不动(page, base_url, home_visit_seed):
    """P2-38：派单 / 完成原先各弹一个单行输入框（服务记录是整段文字，粘不了）；「取消」工单
    点一下就作废、没有任何确认。换成页内表单：取消按钮就是不动（按接口核对状态），服务记录
    留空由后端报人话；取消工单先确认。"""
    read = home_visit_seed["read"]
    done_id, cancel_id = (o["id"] for o in home_visit_seed["orders"])

    def order(oid):
        (row,) = [o for o in read("/api/homevisits?limit=500") if o["id"] == oid]
        return row

    _login(page, base_url)
    _open_page(page, "contracts", "家医签约")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-hvdis="{done_id}"]')
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert order(done_id)["status"] == "applied", "点了取消却照样派了单"
    page.click(f'button[data-hvdis="{done_id}"]')
    _redrawn(page, lambda: _spd_modal(page, {"assignee_name": "E2E护士李"}))
    assert (order(done_id)["status"], order(done_id)["assignee_name"]) == ("dispatched", "E2E护士李")

    page.click(f'button[data-hvdone="{done_id}"]')
    # 留空：框自己提交，后端的话写在框里、框不关（P2-607 第九批；修前框关、报错落到页面消息行）
    form = _spd_modal_rejected(page, {"service_note": ""})
    expect(form.locator("[data-modal-msg]")).to_contain_text("service_note")
    assert order(done_id)["status"] == "dispatched"
    note = "伤口换药，愈合良好\n嘱三日后复诊"
    _redrawn(page, lambda: _spd_modal(page, {"service_note": note}))
    assert (order(done_id)["status"], order(done_id)["service_note"]) == ("completed", note)

    page.click(f'button[data-hvcancel="{cancel_id}"]')
    expect(modal).to_contain_text("不能恢复")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert order(cancel_id)["status"] == "applied", "点了保留却照样作废了"
    page.click(f'button[data-hvcancel="{cancel_id}"]')
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert order(cancel_id)["status"] == "cancelled"


@pytest.fixture(scope="session")
def training_seed(base_url, seed):
    """适宜技术实训考核的前置：一个实训计划，e2e_doctor 已报名。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None, method=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
            method=method,
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    plan = call("/api/education/training-plans", {"title": "E2E适宜技术实训：针刺", "org_id": seed["org"]["id"],
                                                  "plan_date": "2026-09-30", "capacity": 10}, admin)
    call(f"/api/education/training-plans/{plan['id']}/enroll", None, doctor, method="POST")
    return {"plan": plan, "read": lambda path: call(path, None, admin)}


def test_实训考核在页内表单里录_学员从报名名单里选_取消即不录(page, base_url, training_seed):
    """P2-38：「录考核」原先三连问——学员要手打用户 ID（只收本计划已报名的，打错就是 409），
    评语框点取消照样提交。换成页内表单：学员从报名名单里选，取消就是不录（按接口核对），再录并读回。"""
    pid = training_seed["plan"]["id"]

    def board():
        return training_seed["read"](f"/api/education/training-plans/{pid}/assessments")

    _login(page, base_url)
    _open_page(page, "education", "远程医学教育")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-assess="{pid}"]')
    expect(modal.locator('[name="user_id"] option')).to_have_count(1)
    expect(modal.locator('[name="user_id"]')).to_contain_text("e2e_doctor")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert board()["total"] == 0, "点了取消却照样录了考核"
    page.click(f'button[data-assess="{pid}"]')
    form = _spd_modal_rejected(page, {"score": "101", "comment": "进针角度规范"})   # 分数越界：框不关、评语还在（P2-607 第九批）
    expect(form.locator('[name="comment"]')).to_have_value("进针角度规范")
    assert board()["total"] == 0
    _redrawn(page, lambda: _spd_modal(page, {"score": "86.5"}))
    (item,) = board()["items"]
    assert (item["score"], item["passed"]) == (86.5, True), item


def test_实训计划的适宜技术从下拉里选_计划表写技术名称(page, base_url, seed, admin_call, admin_read):
    """P2-1430：发布实训计划原先要手填「适宜技术ID」，而技术库页面不显示编号——填一个别的、但确实存在的编号照样 201，计划挂到
    另一项技术上；计划表也没有技术这一列。修后从技术库下拉里选（首项「不挂适宜技术」），计划表写技术名称。"""
    tech = admin_call("POST", "/api/tcm/techniques", {"name": "E2E实训用刮痧", "category": "外治"})
    _login(page, base_url)
    _open_page(page, "education", "远程医学教育")
    form = page.locator("#tp-plan-form")
    expect(form.locator('[name="technique_id"] option').first).to_have_text("不挂适宜技术")
    form.locator('[name="title"]').fill("E2E挂技术的实训")
    form.locator('[name="org_id"]').fill(str(seed["org"]["id"]))
    form.locator('[name="technique_id"]').select_option(label="E2E实训用刮痧")   # 修前是手填编号的数字框
    form.locator('[name="plan_date"]').fill("2026-10-30")
    _submit(page, "#tp-plan-form button")
    expect(page.locator("tr", has_text="E2E挂技术的实训")).to_contain_text("E2E实训用刮痧")   # 修前计划表没有这一列
    (plan,) = [p for p in admin_read("/api/education/training-plans") if p["title"] == "E2E挂技术的实训"]
    assert (plan["technique_id"], plan["technique_name"]) == (tech["id"], "E2E实训用刮痧"), plan


@pytest.fixture(scope="session")
def staffing_seed(base_url, seed):
    """人员下沉调度的前置：一名员工、一条派驻记录（台账里才有「维护」职称等级的按钮）。"""
    import json
    from urllib.request import Request

    def call(path, payload, token):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"}, "")["access_token"]
    emp = call("/api/mgmt/employees", {"org_id": seed["org"]["id"], "name": "E2E下沉医师",
                                       "title": "主治医师"}, admin)
    other = call("/api/organizations", {"name": "E2E下沉接收卫生院", "org_type": "township",
                                        "level": "township"}, admin)
    call("/api/staffing/secondments", {"employee_id": emp["id"], "from_org_id": seed["org"]["id"],
                                       "to_org_id": other["id"], "start_date": "2026-01-05",
                                       "assignment_type": "long_term"}, admin)
    return {"employee": emp}


def test_职称等级在页内下拉里选_不再手打英文代码(page, base_url, staffing_seed, admin_read):
    """P2-38：「维护」职称等级原先要手打 junior/intermediate/…，打错被后端 422 拒回；
    换成下拉，取消就是不改（按接口核对仍"未填"），选了读回等级名。"""
    eid = staffing_seed["employee"]["id"]

    def level():
        (row,) = [r for r in admin_read("/api/staffing/secondments?limit=500") if r["employee_id"] == eid]
        return row["title_level"]

    _login(page, base_url)
    _open_page(page, "staffing", "人员下沉调度")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-stlevel="{eid}"]')
    expect(modal.locator('[name="title_level"] option')).to_have_count(5)
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert level() == "none", "点了取消却照样改了等级"
    page.click(f'button[data-stlevel="{eid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"title_level": "deputy_senior"}))
    assert level() == "deputy_senior"
    expect(page.locator("tr", has_text="E2E下沉医师")).to_contain_text("副高")


@pytest.fixture(scope="session")
def contraindication_seed(base_url, seed):
    """接种禁忌解除的前置：给 seed 患者登记一条长期禁忌（拦截中）。"""
    import json
    from urllib.request import Request

    def call(path, payload, token):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"}, "")["access_token"]
    contra = call("/api/vaccination/contraindications", {
        "patient_id": seed["patient"]["id"], "vaccine_code": "E2E-FLU", "reason": "E2E发热待查"}, doctor)
    return {"contra": contra}


def test_接种禁忌解除在页内表单里填_取消即不解(page, base_url, seed, contraindication_seed, admin_read):
    """P2-38：「解除」禁忌原先弹单行输入框；换成页内表单，上方写明解除的后果，取消就是不解
    （按接口核对仍在拦截），填了原因才解并读回。"""
    pid, cid = seed["patient"]["id"], contraindication_seed["contra"]["id"]

    def contra():
        (row,) = [c for c in admin_read(f"/api/vaccination/contraindications?patient_id={pid}") if c["id"] == cid]
        return row

    _login(page, base_url)
    _open_page(page, "vaccination", "疫苗接种")
    page.fill("#contra-list input[name=patient_id]", str(pid))
    page.click("#contra-list button")
    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-lift="{cid}"]')
    expect(modal).to_contain_text("不再因这一条拦截")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert contra()["status"] != "lifted", "点了取消却照样解除了"
    page.click(f'button[data-lift="{cid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"lift_reason": "复测体温已正常"}))
    row = contra()
    assert (row["status"], row["lift_reason"]) == ("lifted", "复测体温已正常"), row


@pytest.fixture(scope="session")
def admin_call(base_url):
    """以 admin 身份调写接口（造前置数据 / 用例结束时复原共享配置）。"""
    import json
    from urllib.request import Request

    def call(method, path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
            method=method,
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    return lambda method, path, payload=None: call(method, path, payload, admin)


def _modal(page):
    return page.locator("form.panel:has(button[data-cancel])")


def _cancel_modal(page):
    _modal(page).locator("button[data-cancel]").click()
    expect(_modal(page)).to_have_count(0)


def test_流程画布加节点在页内表单里填_角色从字典里选(page, base_url):
    """P2-38：画布「加节点」原先三连问，角色要手打英文码——后端不校验角色名，打错就得到一个
    除管理员外谁也推不动的节点。换成一个表单，角色从字典里选；取消不加。"""
    _login(page, base_url)
    _open_page(page, "workflows", "流程引擎")
    page.click("#wfc-add")
    expect(_modal(page).locator('[name="role"]')).to_contain_text("医师（doctor）")
    _modal(page).locator('[name="key"]').fill("e2e_canvas_apply")
    _cancel_modal(page)
    expect(page.locator("#wfc-json")).not_to_contain_text("e2e_canvas_apply")
    page.click("#wfc-add")
    _spd_modal(page, {"key": "e2e_canvas_apply", "name": "E2E画布申请", "role": "doctor"})
    expect(page.locator("#wfc-json")).to_contain_text('"key": "e2e_canvas_apply"')
    expect(page.locator("#wfc-json")).to_contain_text('"role": "doctor"')


def test_账号管理的角色下拉不默认落在平台管理员(page, base_url, admin_call):
    """P1-174：「开通账号」的角色下拉原先默认第一项「平台管理员」，不碰它直接开通，建出来的就是不挂机构的管理员；
    「调角色」弹窗不预选现有角色，点开看一眼再点确定，经办就成了管理员（变更记录里一条「经办→平台管理员」）。
    改后开通要先选角色，弹窗预选现有角色——原样确定是空操作。"""
    admin_call("POST", "/api/users", {"username": "e2e_role_op", "password": "passw0rd1", "role": "operator",
                                      "full_name": "E2E经办"})
    _login(page, base_url)
    _open_page(page, "users", "用户管理")
    form = page.locator("#user-form")
    expect(form.locator('[name="role"]')).to_have_value("")
    form.locator('[name="username"]').fill("e2e_role_new")
    form.locator('[name="password"]').fill("passw0rd1")
    assert form.evaluate("f => f.checkValidity()") is False   # 修前 True：一点「开通」就建出一个管理员
    page.locator("tr", has_text="e2e_role_op").locator("[data-chrole]").click()
    expect(_modal(page).locator('[name="role"]')).to_have_value("operator")   # 修前 admin
    _redrawn(page, lambda: _spd_modal(page, {}))
    users = {u["username"]: u["role"] for u in admin_call("GET", "/api/users")}
    assert users["e2e_role_op"] == "operator" and "e2e_role_new" not in users


def test_新增机构的层级与上级不给缺省_上级按层级筛(page, base_url, admin_call):
    """P1-247：「新增机构」的类型 / 层级 / 上级三个下拉原先缺省「牵头医院 / 县级 / 无上级机构」，只改了类型的村卫生室
    落成一家县级树根；上级下拉列出全部机构，村挂县、村挂村都点得出来，而机构建好之后上级改不了（P2-441）。
    改后层级必选、上级随层级重列：村级只列乡级、乡级只列县 / 市级，两级都必选；县、市级照旧可选「无上级机构」。"""
    county = admin_call("POST", "/api/organizations",
                        {"name": "E2E P1247 县医院", "org_type": "lead_hospital", "level": "county"})
    town = admin_call("POST", "/api/organizations", {"name": "E2E P1247 卫生院", "org_type": "township",
                                                     "level": "township", "parent_id": county["id"]})
    _login(page, base_url)
    _open_page(page, "orgs", "机构管理")
    form = page.locator("#org-form")
    level, parent = form.locator('[name="level"]'), form.locator('[name="parent_id"]')
    expect(level).to_have_value("")
    expect(parent).to_be_disabled()
    form.locator('[name="name"]').fill("E2E P1247 西村卫生室")
    form.locator('[name="org_type"]').select_option("village")
    assert form.evaluate("f => f.checkValidity()") is False   # 修前 True：一点「新增」就落成一家县级、无上级的树根
    level.select_option("village")
    options = parent.locator("option").all_inner_texts()
    assert "E2E P1247 卫生院" in options and "E2E P1247 县医院" not in options, options   # 村级只列乡级
    assert form.evaluate("f => f.checkValidity()") is False   # 村级的上级必选
    level.select_option("county")
    expect(parent.locator("option").first).to_have_text("无上级机构")
    assert form.evaluate("f => f.checkValidity()") is True    # 县、市级照旧可以不挂上级
    level.select_option("village")
    parent.select_option(str(town["id"]))
    _submit(page, "#org-form button")
    created = {o["name"]: o for o in admin_call("GET", "/api/organizations")}
    assert (created["E2E P1247 西村卫生室"]["level"], created["E2E P1247 西村卫生室"]["parent_id"]) == ("village", town["id"])


def test_定时任务改间隔在页内表单里填_取消即不改(page, base_url, admin_read, admin_call):
    """P2-38：「改间隔」原先弹窗输分钟；换成数字框（可带小数，折成整秒），取消即不改。"""
    job = admin_read("/api/jobs")[0]
    name, before = job["name"], job["interval_seconds"]

    def interval():
        (row,) = [j for j in admin_read("/api/jobs") if j["name"] == name]
        return row["interval_seconds"]

    _login(page, base_url)
    _open_page(page, "jobs", "定时任务")
    page.click(f'button[data-interval="{name}"]')
    _cancel_modal(page)
    assert interval() == before, "点了取消却照样改了间隔"
    page.click(f'button[data-interval="{name}"]')
    _redrawn(page, lambda: _spd_modal(page, {"minutes": "1.5"}))
    try:
        assert interval() == 90
    finally:
        admin_call("PATCH", f"/api/jobs/{name}", {"interval_seconds": before})  # 共享调度配置复原


def test_定时任务执行历史按任务与结果筛_重画后选中项还在(page, base_url, admin_read):
    """P2-467：执行历史只看最新 50 条、不能筛——日跑任务的失败记录半小时就滚出这一页。补「任务」「结果」两个筛选，
    选中项只留在内存里（不进存储），整页重画之后下拉框仍是选中的那一项。"""
    name = admin_read("/api/jobs")[0]["name"]
    _login(page, base_url)
    _open_page(page, "jobs", "定时任务")
    status = page.locator('#job-run-filter select[name="status"]')
    _redrawn(page, lambda: status.select_option("failed"))
    expect(page.locator('#job-run-filter select[name="status"]')).to_have_value("failed")
    history = page.locator(".panel", has_text="执行历史")
    expect(history.locator("td .tag.green")).to_have_count(0)   # 只看失败：一行「成功」都不该有
    job = page.locator('#job-run-filter select[name="job_name"]')
    _redrawn(page, lambda: job.select_option(name))
    expect(page.locator('#job-run-filter select[name="job_name"]')).to_have_value(name)
    expect(page.locator('#job-run-filter select[name="status"]')).to_have_value("failed")   # 两个筛选叠加
    shown = {c.strip() for c in history.locator("td code").all_inner_texts()}
    assert shown <= {name}, shown
    _redrawn(page, lambda: page.locator('#job-run-filter select[name="status"]').select_option(""))
    _redrawn(page, lambda: page.locator('#job-run-filter select[name="job_name"]').select_option(""))


def test_就诊凭据作废在页内表单里填_写明后果(page, base_url, seed, admin_read):
    """P2-38：「作废」原先弹窗要原因；换成表单，上方写明作废不能恢复，取消即不作废。"""
    import json
    from urllib.request import Request

    def call(path, payload, token):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"}, "")["access_token"]
    cred = call("/api/credentials", {"patient_id": seed["patient"]["id"], "credential_type": "temp"}, doctor)

    def status():
        (row,) = [c for c in admin_read("/api/credentials?limit=500") if c["id"] == cred["id"]]
        return row["status"], row["close_reason"]

    _login(page, base_url)
    _open_page(page, "credentials", "就诊凭据")
    page.click(f'button[data-cvoid="{cred["id"]}"]')
    expect(_modal(page)).to_contain_text("不能恢复")
    _cancel_modal(page)
    assert status()[0] == "active", "点了取消却照样作废了"
    page.click(f'button[data-cvoid="{cred["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {"reason": "患者挂失"}))
    assert status() == ("void", "患者挂失"), status()


def test_按月导出运营月报在页内表单里填_月份写错报人话(page, base_url):
    """P2-38：按月导出原先弹窗输月份；换成表单。月份写错时导出失败原先只报状态码，
    现在带后端给的原因；写对了照常下载。"""
    _login(page, base_url)
    _open_page(page, "performance", "绩效考核")
    page.click("#exp-ops-period")
    _cancel_modal(page)
    page.click("#exp-ops-period")
    _spd_modal(page, {"period": "2026-13"})
    expect(page.locator("#rpt-msg")).to_contain_text("2026-13 不存在")  # 原先只有"导出失败(422)"
    page.click("#exp-ops-period")
    with page.expect_download() as download:
        _spd_modal(page, {"period": "2026-07"})
    assert download.value.suggested_filename == "operations_report_2026-07.csv"


def test_知识库续期与停用在页内表单里_停用先确认(page, base_url, admin_read, admin_call):
    """P2-38：「续期」原先弹窗输日期；「停用」点一下就生效、没有任何确认，停用后条目从检索里
    消失、页面上恢复不了。换成表单：日期写错由后端报人话；停用先确认，取消即不停。

    P2-1668：有效期不得早于今天——日子按今天起算，写死的日子过了那天就发布不了、续不上期。"""
    from datetime import date, timedelta

    first = (date.today() + timedelta(days=83)).isoformat()
    renewed = (date.today() + timedelta(days=448)).isoformat()
    entry = admin_call("POST", "/api/knowledge", {"category": "regulation", "title": "E2E知识条目续期",
                                                  "expire_date": first})

    def row():
        rows = [k for k in admin_read("/api/knowledge?include_expired=true") if k["id"] == entry["id"]]
        return rows[0] if rows else None

    _login(page, base_url)
    _open_page(page, "knowledge", "知识库")
    page.click(f'button[data-renew="{entry["id"]}"]')
    _cancel_modal(page)
    page.click(f'button[data-renew="{entry["id"]}"]')
    _spd_modal(page, {"expire_date": "2027-02-30"})
    expect(page.locator("#kb-msg")).to_contain_text("expire_date")
    assert row()["expire_date"] == first
    page.click(f'button[data-renew="{entry["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {"expire_date": renewed}))
    assert row()["expire_date"] == renewed

    page.click(f'button[data-deact="{entry["id"]}"]')
    expect(_modal(page)).to_contain_text("不能恢复")
    _cancel_modal(page)
    assert row() is not None, "点了取消却照样停用了"
    page.click(f'button[data-deact="{entry["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert row() is None


def test_绩效指标调权重在页内表单里填_取消即不改(page, base_url, admin_read, admin_call):
    """P2-38：「调权重」原先弹窗输数字；换成数字框（可带小数），取消即不改。"""
    ind = admin_read("/api/performance/indicators")[0]
    key, before = ind["key"], ind["weight"]

    def weight():
        (row,) = [i for i in admin_read("/api/performance/indicators") if i["key"] == key]
        return row["weight"]

    _login(page, base_url)
    _open_page(page, "perfind", "绩效指标调权")
    page.click(f'button[data-weight="{key}"]')
    _cancel_modal(page)
    assert weight() == before
    page.click(f'button[data-weight="{key}"]')
    _redrawn(page, lambda: _spd_modal(page, {"weight": "1.5"}))
    try:
        assert weight() == 1.5
    finally:
        admin_call("PATCH", f"/api/performance/indicators/{key}", {"weight": before})  # 复原共享配置


def test_绩效指标改名在页内表单里填_带出原名_取消即不改(page, base_url, admin_read, admin_call):
    """P2-472：接口一直收 name，页面只给调权重与启停；迁移 b5d9f3a71c2e 让现场「在指标目录里改名」却无处可改。"""
    ind = admin_read("/api/performance/indicators")[0]
    key, before = ind["key"], ind["name"]

    def name():
        (row,) = [i for i in admin_read("/api/performance/indicators") if i["key"] == key]
        return row["name"]

    _login(page, base_url)
    _open_page(page, "perfind", "绩效指标调权")
    page.click(f'button[data-rename-ind="{key}"]')
    expect(_modal(page).locator('[name="name"]')).to_have_value(before)   # 带出原名，改一两个字不用重敲
    _cancel_modal(page)
    assert name() == before
    page.click(f'button[data-rename-ind="{key}"]')
    _redrawn(page, lambda: _spd_modal(page, {"name": "E2E协同量"}))
    try:
        assert name() == "E2E协同量"
        expect(page.locator("#page-body")).to_contain_text("E2E协同量")
    finally:
        admin_call("PATCH", f"/api/performance/indicators/{key}", {"name": before})  # 复原共享配置


def test_绩效考核页按周期与口径计分_写错回到缺省(page, base_url, admin_read):
    """P2-474：接口收 period / group_id / volume_cap / include_auto_passed，页面原先一个都不给——永远是当年、全县、
    默认口径。补四个参数；写错（如 13 月）说清楚、回到缺省口径，不把整页掀掉。"""
    year = admin_read("/api/performance/orgs")["period"]
    _login(page, base_url)
    _open_page(page, "performance", "绩效考核")
    form = page.locator("#perf-filter")
    form.locator('[name="period"]').fill(f"{year}-01")
    form.locator('[name="volume_cap"]').fill("1")
    _redrawn(page, lambda: form.locator("button").click())
    expect(page.locator("#page-desc")).to_contain_text(f"当前评分周期：{year}-01")
    expect(page.locator('#perf-filter [name="volume_cap"]')).to_have_value("1")   # 重画之后参数还在
    # 13 月：浏览器的 pattern 放行（形状对），后端 422——页面说清楚并回到缺省口径
    page.locator('#perf-filter [name="period"]').fill(f"{year}-13")
    _redrawn(page, lambda: page.locator("#perf-filter button").click())
    expect(page.locator("#perf-filter-msg")).to_contain_text("已回到缺省口径")
    expect(page.locator("#page-desc")).to_contain_text(f"当前评分周期：{year}")
    expect(page.locator('#perf-filter [name="volume_cap"]')).to_have_value("")


def test_集成平台对消息执行编排在页内表单里填消息号(page, base_url, admin_call):
    """P2-38：「对消息执行」原先弹窗输消息 ID；换成数字框，取消即不执行；消息号不存在由后端报人话。"""
    # 校验步不写 required 的 P2-1730 起 422（跑起来什么也不校验）
    admin_call("POST", "/api/esb/flows", {"code": "e2e_flow_run", "name": "E2E编排",
                                          "steps": [{"type": "validate", "config": {"required": ["id"]}}]})
    _login(page, base_url)
    _open_page(page, "esb", "集成平台")
    page.click('button[data-esbrun="e2e_flow_run"]')
    _cancel_modal(page)
    expect(page.locator("#esb-flow-msg")).to_have_text("")
    page.click('button[data-esbrun="e2e_flow_run"]')
    _spd_modal(page, {"message_id": "987654"})
    expect(page.locator("#esb-msg")).to_contain_text("消息不存在")


def test_编辑编排流程由框自己提交_步骤写错框不关(page, base_url, admin_call, admin_read):
    """P2-607 第九批：集成平台「编辑流程」原先点确定就关框——步骤 JSON 少个括号、步骤类型写错（后端只认四种），报错落在
    页面消息行，改了一半的步骤全丢。现在框自己提交：写错都在框里说、框不关、流程不动；改好再交才落库。"""
    flow = admin_call("POST", "/api/esb/flows", {"code": "e2e_p2607_flow", "name": "E2E框内提交编排",
                                                 "steps": [{"type": "validate", "config": {"required": ["id"]}}]})

    def saved():
        return next(f for f in admin_read("/api/esb/flows") if f["id"] == flow["id"])

    _login(page, base_url)
    _open_page(page, "esb", "集成平台")
    page.click(f'button[data-esbflowedit="{flow["id"]}"]')
    form = _spd_modal_rejected(page, {"name": "E2E框内提交编排（改）", "steps": '[{"type":"validate"},{坏的'})
    expect(form.locator("[data-modal-msg]")).to_contain_text("步骤 JSON 解析失败")
    expect(form.locator('[name="name"]')).to_have_value("E2E框内提交编排（改）")   # 修前框关，改的名字也没了
    form.locator('[name="steps"]').fill('[{"type":"validate"},{"type":"shred"}]')
    form.locator("button[type=submit]").click()
    expect(form.locator("[data-modal-msg]")).to_contain_text("第 2 步")
    assert (saved()["name"], [s["type"] for s in saved()["steps"]]) == ("E2E框内提交编排", ["validate"])
    # 落库交换日志：缺省实体是患者档案，前面没有 transform 的 P2-1122 起 422（跑起来必失败）；校验步不写 required 的
    # P2-1730 起 422（跑起来什么也不校验）
    _redrawn(page, lambda: _spd_modal(page, {"steps": '[{"type":"validate","config":{"required":["id"]}},'
                                                      '{"type":"persist","config":{"entity":"exchange_log"}}]'}))
    assert (saved()["name"], [s["type"] for s in saved()["steps"]]) == ("E2E框内提交编排（改）", ["validate", "persist"])


def _confirm_then(page, click, intro, before, after):
    """P2-43 的共同步骤：点按钮 → 确认表单上写着后果 → 先取消（before() 仍成立）→ 再点并确定（after() 成立）。"""
    click()
    expect(_modal(page)).to_contain_text(intro)
    _cancel_modal(page)
    assert before(), "点了取消却照样生效了"
    click()
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert after()


def test_互联网诊疗结束先确认(page, base_url, seed, admin_read, admin_call):
    """P2-43：「结束」原先点一下就结束（已回复 → 已结束），页面上没有撤回入口。"""
    consult = admin_call("POST", "/api/telemedicine/consults", {
        "patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "question": "E2E结束前确认"})
    admin_call("POST", f"/api/telemedicine/consults/{consult['id']}/reply",
               {"reply": "按时服药", "doctor_name": "E2E医师"})

    def status():
        (row,) = [c for c in admin_read("/api/telemedicine/consults") if c["id"] == consult["id"]]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "telemedicine", "互联网+诊疗")
    _confirm_then(page, lambda: page.click(f'button[data-close="{consult["id"]}"]'), "不能撤回",
                  lambda: status() == "replied", lambda: status() == "closed")


def test_公卫事件结案先确认(page, base_url, admin_read, admin_call):
    """P2-43：「结案」原先点一下就结案，结案后不能再登记处置记录。"""
    event = admin_call("POST", "/api/publichealth/events", {"title": "E2E结案前确认事件", "level": "IV"})

    def status():
        (row,) = [e for e in admin_read("/api/publichealth/events") if e["id"] == event["id"]]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "publichealth", "公卫协同")
    _confirm_then(page, lambda: page.click(f'button[data-close="{event["id"]}"]'), "不能重开",
                  lambda: status() == "active", lambda: status() == "closed")


def test_结束派驻先确认_可填实际结束日期(page, base_url, seed, admin_read, admin_call):
    """P2-43：「结束派驻」原先点一下就结束、结束日一律记今天；现在先确认，并能填实际结束日期补录。"""
    emp = admin_call("POST", "/api/mgmt/employees", {"org_id": seed["org"]["id"], "name": "E2E结束派驻医师"})
    other = admin_call("POST", "/api/organizations", {"name": "E2E派驻接收站", "org_type": "township",
                                                      "level": "township"})
    row = admin_call("POST", "/api/staffing/secondments", {
        "employee_id": emp["id"], "from_org_id": seed["org"]["id"], "to_org_id": other["id"],
        "start_date": "2026-01-05", "assignment_type": "long_term"})

    def end_date():
        (r,) = [x for x in admin_read("/api/staffing/secondments?limit=500") if x["id"] == row["id"]]
        return r["end_date"]

    _login(page, base_url)
    _open_page(page, "staffing", "人员下沉调度")
    page.click(f'button[data-stend="{row["id"]}"]')
    expect(_modal(page)).to_contain_text("不能撤回")
    _cancel_modal(page)
    assert not end_date(), "点了取消却照样结束了"
    page.click(f'button[data-stend="{row["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {"end_date": "2026-08-31"}))
    assert end_date() == "2026-08-31"


def test_管理端取消预约先确认(page, base_url, seed, admin_read, admin_call):
    """P2-43：替居民「取消」预约原先点一下就生效——号源随即释放给他人，居民那边的预约就没了。"""
    patient = admin_call("POST", "/api/patients",
                         {"name": "E2E取消预约患者", "id_card": "320981198811112221", "gender": "男"})
    slot = admin_call("POST", "/api/appointments/slots", {
        "org_id": seed["org"]["id"], "resource_type": "outpatient", "resource_name": "E2E取消预约门诊",
        "slot_date": "2099-12-01", "slot_time": "09:00", "capacity": 1})
    apt = admin_call("POST", "/api/appointments", {"slot_id": slot["id"], "patient_id": patient["id"]})

    def status():
        (row,) = [a for a in admin_read(f"/api/appointments?patient_id={patient['id']}") if a["id"] == apt["id"]]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "appointments", "预约诊疗")
    # 行内按钮与模态框的取消键同名 data-cancel：限定在页面里点，别点到模态框上
    _confirm_then(page, lambda: page.click(f'#page-body button[data-cancel="{apt["id"]}"]'), "号源立即释放",
                  lambda: status() == "booked", lambda: status() == "cancelled")


def test_管理端核销预约先确认(page, base_url, seed, admin_read, admin_call):
    """P2-1701：「核销」原先点一下就生效——核销没有回退的路由，点错一行，别人的预约永久成了「已就诊」、号也一直占着。
    确认框写明是谁、哪个号（P2-1700 的认人键）与「核销后不可撤回」；点取消不核销。"""
    patient = admin_call("POST", "/api/patients",
                         {"name": "E2E核销预约患者", "id_card": "320981198811112238", "gender": "男"})
    slot = admin_call("POST", "/api/appointments/slots", {
        "org_id": seed["org"]["id"], "resource_type": "outpatient", "resource_name": "E2E核销预约门诊",
        "slot_date": "2099-12-02", "slot_time": "09:00", "capacity": 1})
    apt = admin_call("POST", "/api/appointments", {"slot_id": slot["id"], "patient_id": patient["id"]})

    def status():
        (row,) = [a for a in admin_read(f"/api/appointments?patient_id={patient['id']}") if a["id"] == apt["id"]]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "appointments", "预约诊疗")
    # 行内按钮限定在页面里点，别点到模态框上（同上一条）
    _confirm_then(page, lambda: page.click(f'#page-body button[data-fulfill="{apt["id"]}"]'),
                  "E2E核销预约患者 · 2099-12-02 09:00 · E2E核销预约门诊",
                  lambda: status() == "booked", lambda: status() == "fulfilled")
    expect(page.locator(f'#page-body button[data-fulfill="{apt["id"]}"]')).to_have_count(0)


def test_撤销调阅授权先确认(page, base_url, seed, admin_read, admin_call):
    """P2-43：「撤销」调阅授权原先点一下就生效；要恢复，得患者本人再来办一次授权。"""
    pid = seed["patient"]["id"]
    grantee = admin_call("POST", "/api/organizations",
                         {"name": "E2E被授权卫生院", "org_type": "township", "level": "township"})
    auth = admin_call("POST", f"/api/patients/{pid}/authorizations",   # 有效期不得早于今天（P2-1726）：取远期
                      {"grantee_org_id": grantee["id"], "scope": "all", "expire_date": "2099-12-31"})

    def status():
        (row,) = [a for a in admin_read(f"/api/patients/{pid}/authorizations") if a["id"] == auth["id"]]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "patients", "患者主索引")
    page.fill('#auth-list-form [name="patient_id"]', str(pid))
    page.click("#auth-list-form button")
    revoke = page.locator(f'#auth-table button[data-revoke="{auth["id"]}"]')
    revoke.click()
    expect(_modal(page)).to_contain_text("须患者本人重新办理授权")
    _cancel_modal(page)
    assert status() == "active", "点了取消却照样撤销了"
    # 这一页撤销后只重画授权表（不整页 route）：等这条的撤销按钮消失
    revoke.click()
    _spd_modal(page, {})
    expect(revoke).to_have_count(0)
    assert status() == "revoked"


def test_建档撞上已有证件号_提示不一致的项_不报建档成功(page, base_url, admin_read, admin_call):
    """P2-1244：证件号已建过档，后端幂等返回既有档案、本次所填一概不写，页面原先照样报「建档成功」——补填的出生日期被吞掉、
    证件号录错撞上别人的档案（姓名性别都不同）也看不出来。现在据回执的 created 提示与档案不一致的项、指向档案更正。"""
    old = admin_call("POST", "/api/patients", {"name": "E2E早年建档", "id_card": "320981194603011244", "gender": "男"})
    _login(page, base_url)
    _open_page(page, "patients", "患者主索引")
    form = page.locator("#patient-form")
    form.locator('[name="name"]').fill("E2E录错证件号")
    form.locator('[name="id_card"]').fill("320981194603011244")
    form.locator('[name="gender"]').select_option("女")
    form.locator('[name="birth_date"]').fill("1950-05-05")
    form.locator("button").click()
    msg = page.locator("#patient-msg")
    expect(msg).to_contain_text(f"该证件号已建档（电子健康卡号：{old['ehc_no']}），未按本次所填改动档案")
    expect(msg).to_contain_text("姓名（档案：E2E早年建档，本次：E2E录错证件号）")
    expect(msg).to_contain_text("出生日期（档案：未填，本次：1950-05-05）")
    expect(msg).to_contain_text("如需更正请走档案更正")
    expect(msg).not_to_contain_text("建档成功")                    # 修前就是这一句
    stored = admin_read(f"/api/patients/{old['ehc_no']}")
    assert (stored["name"], stored["gender"], stored["birth_date"]) == ("E2E早年建档", "男", "")
    # 新证件号照旧「建档成功」
    form.locator('[name="id_card"]').fill("320981195005051252")
    form.locator("button").click()
    expect(msg).to_contain_text("建档成功，电子健康卡号：")


def test_孕产妇保健结案先确认(page, base_url, admin_read, admin_call):
    """P2-43：「结案」原先点一下就结案，页面上没有重开入口。"""
    patient = admin_call("POST", "/api/patients",
                         {"name": "E2E结案孕妇", "id_card": "320981199203032224", "gender": "女"})
    record = admin_call("POST", "/api/maternal/records", {"patient_id": patient["id"]})
    admin_call("POST", f"/api/maternal/records/{record['id']}/visits", {"visit_type": "postpartum"})

    def status():
        (row,) = [r for r in admin_read("/api/maternal/records") if r["id"] == record["id"]]
        return row["status"]

    assert status() == "delivered"  # 产后访视把档案推到「已分娩」，结案按钮才出现
    _login(page, base_url)
    _open_page(page, "maternal", "妇幼保健")
    _confirm_then(page, lambda: page.click(f'button[data-close="{record["id"]}"]'), "页面上不能重开",
                  lambda: status() == "delivered", lambda: status() == "closed")


def test_产后访视先录的分娩记录照样能从页面上补登_没访视的不给结案(page, base_url, admin_read, admin_call):
    """P2-594：产后访视先录（分娩在别处、记录后补）把档案推到「已分娩」，「分娩登记」按钮原先随之消失，分娩记录从此
    录不进来；分娩登记了、还没做产后访视的，「结案」按钮原先已经亮着，点下去 409。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E补登分娩医院", "org_type": "lead_hospital", "level": "county"})
    late = admin_call("POST", "/api/maternal/records", {"patient_id": admin_call("POST", "/api/patients", {
        "name": "E2E补登孕妇", "id_card": "320981199203032259", "gender": "女"})["id"]})
    admin_call("POST", f"/api/maternal/records/{late['id']}/visits", {"visit_type": "postpartum"})
    fresh = admin_call("POST", "/api/maternal/records", {"patient_id": admin_call("POST", "/api/patients", {
        "name": "E2E待访孕妇", "id_card": "320981199203032267", "gender": "女"})["id"]})
    admin_call("POST", f"/api/maternal/records/{fresh['id']}/delivery",
               {"org_id": org["id"], "delivery_date": "2026-09-20"})

    _login(page, base_url)
    _open_page(page, "maternal", "妇幼保健")
    expect(page.locator(f'button[data-visit="{fresh["id"]}"]')).to_have_count(1)
    expect(page.locator(f'button[data-close="{fresh["id"]}"]')).to_have_count(0)   # 修前亮着、点下去 409
    page.click(f'button[data-delivery="{late["id"]}"]')   # 修前没有这个按钮
    _spd_modal(page, {"org_id": str(org["id"]), "delivery_date": "2026-09-18"})
    expect(page.locator(f'button[data-delivery="{late["id"]}"]')).to_have_count(0)
    detail = admin_read(f"/api/maternal/records/{late['id']}/delivery")
    assert (detail["org_id"], detail["delivery_date"]) == (org["id"], "2026-09-18")
    expect(page.locator(f'button[data-close="{late["id"]}"]')).to_have_count(1)   # 有产后访视，结案照给


def test_上一胎结案后再孕_从页面上建出新册且孕产次录得进去(page, base_url, admin_read, admin_call):
    """P1-140：原先一位妇女一生只能建一本，结案后再孕建册拿回的是那本已结案的旧档案；建册表单也没有孕次 / 产次两格，
    每本都是 G1P0。"""
    patient = admin_call("POST", "/api/patients",
                         {"name": "E2E再孕孕妇", "id_card": "320981199203032240", "gender": "女"})
    first = admin_call("POST", "/api/maternal/records", {"patient_id": patient["id"]})
    admin_call("POST", f"/api/maternal/records/{first['id']}/visits", {"visit_type": "postpartum"})
    admin_call("POST", f"/api/maternal/records/{first['id']}/close")

    _login(page, base_url)
    _open_page(page, "maternal", "妇幼保健")
    page.fill("#mat-form input[name=patient_id]", str(patient["id"]))
    page.fill("#mat-form input[name=edc]", "2027-03-08")
    page.fill("#mat-form input[name=gravidity]", "2")
    page.fill("#mat-form input[name=parity]", "1")
    _submit(page, "#mat-form button")

    mine = [r for r in admin_read("/api/maternal/records") if r["patient_id"] == patient["id"]]
    assert sorted((r["status"], r["gravidity"], r["parity"]) for r in mine) == [("closed", 1, 0), ("registered", 2, 1)]
    (fresh,) = [r for r in mine if r["status"] == "registered"]
    row = page.locator("#page-body tr", has=page.locator(f'button[data-visit="{fresh["id"]}"]'))
    expect(row).to_contain_text("G2P1")
    expect(row).to_contain_text("孕期管理")


def test_医废收集从页面上登记带上产生点(page, base_url, admin_read, admin_call):
    """P2-143：模型注释「产生点必填」，收集登记表单原先只有机构 ID、类别、重量、日期四格，从不送产生点——
    页面上收的每一袋医废，扫码追溯到的「收集」一环来源都是空的。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E医废卫生院", "org_type": "township", "level": "township"})
    source = admin_call("POST", "/api/medwaste/locations",
                        {"org_id": org["id"], "name": "E2E外科病区", "location_type": "source"})

    _login(page, base_url)
    _open_page(page, "medwaste", "医废追溯")
    page.select_option('#waste-form select[name="source_location_id"]', str(source["id"]))
    page.select_option('#waste-form select[name="waste_type"]', "sharp")
    page.fill('#waste-form input[name="weight_kg"]', "1.5")
    page.fill('#waste-form input[name="collected_date"]', "2026-09-20")
    _submit(page, "#waste-form button")

    (waste,) = [w for w in admin_read(f"/api/medwaste?org_id={org['id']}") if w["org_id"] == org["id"]]
    assert (waste["source_location_id"], waste["waste_type"], waste["weight_kg"]) == (source["id"], "sharp", 1.5)
    timeline = admin_read(f"/api/medwaste/trace/{waste['trace_code']}")["timeline"]
    assert timeline[0] == {"step": "收集", "at": "2026-09-20", "location": "E2E外科病区"}


def test_医废滞留预警逐包列出_就地交接(page, base_url, admin_read, admin_call):
    """P2-1309（第三十八批扫描 AB1-1）：滞留预警面板原先只印条数和一句说明——预警接口逐包给的追溯码、暂存点、超期天数全丢了，
    「交接」按钮只摆在主清单（最新 500 包）里，而滞留的恰是最早收的那批、最先被挤出窗口。修后面板逐包列出、每包带交接。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E医废滞留卫生院", "org_type": "township", "level": "township"})
    storage = admin_call("POST", "/api/medwaste/locations",
                         {"org_id": org["id"], "name": "E2E滞留东楼暂存间", "location_type": "storage"})
    waste = admin_call("POST", "/api/medwaste", {"org_id": org["id"], "waste_type": "infectious",
                                                  "weight_kg": 2.5, "collected_date": "2026-01-05"})
    admin_call("POST", f"/api/medwaste/{waste['id']}/store", {"storage_location_id": storage["id"]})
    (alert,) = [a for a in admin_read("/api/medwaste/alerts") if a["id"] == waste["id"]]

    _login(page, base_url)
    _open_page(page, "medwaste", "医废追溯")
    overdue = page.locator("#page-body .panel", has=page.locator("h3", has_text="滞留预警"))
    row = overdue.locator("tr", has_text=waste["trace_code"])
    expect(row).to_contain_text("E2E滞留东楼暂存间")          # 修前面板里没有任何一包
    expect(row).to_contain_text(f"{alert['overdue_days']} 天")
    row.locator(f'button[data-hand="{waste["id"]}"]').click()
    _redrawn(page, lambda: _spd_modal(page, {"handler_name": "E2E滞留转运员"}))

    traced = admin_read(f"/api/medwaste/trace/{waste['trace_code']}")
    assert (traced["status"], traced["handler_name"]) == ("handed_over", "E2E滞留转运员")
    expect(overdue.locator("tr", has_text=waste["trace_code"])).to_have_count(0)


def test_停用考核公式先确认(page, base_url, admin_read, admin_call):
    """P2-43：「停用」考核公式原先点一下就停用，停用的公式页面上没有启用入口。"""
    key = "e2e_p243_formula"
    admin_call("POST", "/api/analytics/formulas", {"key": key, "name": "E2E停用前确认", "expression": "encounters"})

    def active():
        (row,) = [f for f in admin_read("/api/analytics/formulas") if f["key"] == key]
        return row["active"]

    _login(page, base_url)
    _open_page(page, "analytics", "决策指标扩展")
    _confirm_then(page, lambda: page.click(f'button[data-off="{key}"]'), "不能重新启用",
                  lambda: active() is True, lambda: active() is False)


def test_停用规则先确认(page, base_url, admin_read, admin_call):
    """P2-43：「停用」规则原先点一下就停用，停用的规则页面上没有启用入口。"""
    key = "e2e_p243_rule"
    admin_call("POST", "/api/rules", {"key": key, "name": "E2E停用前确认", "domain": "prescription",
                                      "condition": "age >= 65"})

    def active():
        (row,) = [r for r in admin_read("/api/rules") if r["key"] == key]
        return row["active"]

    _login(page, base_url)
    _open_page(page, "rules", "统一规则引擎")
    _confirm_then(page, lambda: page.click(f'button[data-off="{key}"]'), "不能重新启用",
                  lambda: active() is True, lambda: active() is False)


def test_撤销完成失败要说出来_不再一声不吭(page, base_url, seed, admin_call):
    """P2-423：「撤销完成」原先 `await api(...)` 不接错误——没有权限（接口只收管理层 / 经办）点下去 403，页面一声不吭。
    同一处理函数里「完成」那个分支早就接住了（P2-378），逐处判的闸门之前只看第一处 await，这一处就漏在后面。"""
    project = admin_call("POST", "/api/projects", {"org_id": seed["org"]["id"], "name": "E2E撤销完成失败"})
    milestone = admin_call("POST", f"/api/projects/{project['id']}/milestones", {"name": "E2E已完成的里程碑"})
    admin_call("POST", f"/api/projects/milestones/{milestone['id']}/done")
    _login(page, base_url, "e2e_doctor", "passw0rd1")   # 医师看得到项目页，撤销不了
    _open_page(page, "projects", "项目管理")
    with _answers(page, [""]):   # 原生 confirm：点确定
        page.click(f'button[data-msreopen="{milestone["id"]}"]')
    expect(page.locator("#pj-msg")).to_contain_text("需要以下角色之一")   # 修前什么也不说


def test_集成平台按接入方筛出的老消息_查看载荷就在当前页里找(page, base_url, admin_call):
    """P2-424：「查看载荷」原先另取「最新 50 条」、不带筛选条件——按接入方筛出来的老消息，点下去反倒说
    「载荷不在当前页，请先按条件筛选」。修后就在消息表当前这一页里找。"""
    import json
    from urllib.request import Request

    def endpoint(code):
        return admin_call("POST", "/api/esb/endpoints", {"code": code, "name": code, "system_type": "his",
                                                          "rate_limit_per_min": 1000})

    def enqueue(ep, payload):
        req = Request(f"{base_url}/api/esb/messages", method="POST",
                      data=json.dumps({"msg_type": "E2E_P2424", "payload": payload}).encode(),
                      headers={"Content-Type": "application/json", "X-Esb-Endpoint": ep["code"],
                               "X-Esb-Token": ep["auth_token"]})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    old_ep, busy_ep = endpoint("e2e_p2424_old"), endpoint("e2e_p2424_busy")
    old = enqueue(old_ep, {"marker": "E2E老消息载荷"})
    for i in range(50):   # 后来的 50 条把它挤出「最新 50 条」
        enqueue(busy_ep, {"seq": i})

    _login(page, base_url)
    _open_page(page, "esb", "集成平台")
    page.select_option('#esb-msg-filter select[name="endpoint_id"]', str(old_ep["id"]))
    page.click("#esb-msg-filter button")
    shown = page.locator(f'button[data-esbpayload="{old["id"]}"]')
    expect(shown).to_be_visible()
    seen = []

    def on_dialog(dialog):
        seen.append(dialog.message)
        dialog.accept()

    page.once("dialog", on_dialog)
    shown.click()
    for _ in range(50):   # 弹窗在点击的同步处理里就弹出；稳妥起见最多再等 5 秒
        if seen:
            break
        page.wait_for_timeout(100)
    assert seen and "E2E老消息载荷" in seen[0], seen   # 修前：「载荷不在当前页，请先按条件筛选」


def _p2429_user(admin_call, seed, username, role):
    try:
        admin_call("POST", "/api/users", {"username": username, "password": "passw0rd1", "role": role,
                                          "full_name": username, "org_id": seed["org"]["id"]})
    except Exception:  # noqa: BLE001 - 同一会话里第二次建同名账号是 409，账号已在
        pass


def test_问诊的回复按钮只给医师(page, base_url, seed, admin_call):
    """P2-429：互联网诊疗的「回复」接口只收医师，经办打开页面原先照样摆着「回复」，点下去一次 403。"""
    _p2429_user(admin_call, seed, "e2e_p2429_op", "operator")
    consult = admin_call("POST", "/api/telemedicine/consults", {
        "patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "question": "E2E续方咨询P2429"})
    _login(page, base_url, "e2e_p2429_op", "passw0rd1")
    _open_page(page, "telemedicine", "互联网+诊疗")
    expect(page.locator("tr", has_text="E2E续方咨询P2429")).to_contain_text("待医师回复")
    expect(page.locator(f'button[data-reply="{consult["id"]}"]')).to_have_count(0)


def test_上门服务的派单与取消不给公卫人员(page, base_url, seed, admin_call):
    """P2-429：上门服务的「派单」「取消」只收经办 / 医师，公卫人员原先也看得到，点下去一次 403。"""
    _p2429_user(admin_call, seed, "e2e_p2429_ph", "public_health")
    visit = admin_call("POST", "/api/homevisits", {
        "patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "service_type": "nursing",
        "demand": "E2E待派单的上门"})
    _login(page, base_url, "e2e_p2429_ph", "passw0rd1")
    _open_page(page, "contracts", "家医签约")   # 上门服务调度挂在家医签约页下方
    expect(page.locator("tr", has_text="E2E待派单的上门")).to_be_visible()
    expect(page.locator(f'button[data-hvdis="{visit["id"]}"], button[data-hvcancel="{visit["id"]}"]')).to_have_count(0)


def _dispatched_visit(admin_call, seed, demand):
    visit = admin_call("POST", "/api/homevisits", {
        "patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "service_type": "nursing", "demand": demand})
    admin_call("POST", f"/api/homevisits/{visit['id']}/dispatch", {"assignee_name": "E2E护士王"})
    return visit


def test_已派单的上门工单能从页面上取消(page, base_url, seed, admin_call, admin_read):
    """P2-595：取消接口只挡已完成的工单，页面原先只给待派单的画「取消」——派出去才知道去不成的工单一直挂在待完成里。"""
    visit = _dispatched_visit(admin_call, seed, "E2E派出后去不成的上门")
    _login(page, base_url)
    _open_page(page, "contracts", "家医签约")
    expect(page.locator(f'button[data-hvdone="{visit["id"]}"]')).to_have_count(1)
    page.click(f'button[data-hvcancel="{visit["id"]}"]')   # 修前已派单的行上没有这个按钮
    _redrawn(page, lambda: _spd_modal(page, {}))
    (row,) = [o for o in admin_read("/api/homevisits?limit=500") if o["id"] == visit["id"]]
    assert row["status"] == "cancelled"


def test_已派单的上门工单公卫人员只见完成(page, base_url, seed, admin_call):
    """P2-595 / P2-429：已派单的行加了「取消」，取消仍只给经办 / 医师——公卫人员只见「完成」。"""
    _p2429_user(admin_call, seed, "e2e_p2429_ph", "public_health")
    visit = _dispatched_visit(admin_call, seed, "E2E公卫只见完成的上门")
    _login(page, base_url, "e2e_p2429_ph", "passw0rd1")
    _open_page(page, "contracts", "家医签约")
    expect(page.locator(f'button[data-hvdone="{visit["id"]}"]')).to_have_count(1)
    expect(page.locator(f'button[data-hvcancel="{visit["id"]}"]')).to_have_count(0)


def test_课件附件上传后看得到也下得了(page, base_url, admin_call, tmp_path):
    """P2-431：课件附件传得上去，页面上却只给个数——看不到是哪些文件，也下不回来。修后上传完就列出来，课件清单里
    「N 个 · 查看」点开同一份清单，每个文件能下载（鉴权下载，同检查报告附件）。"""
    course = admin_call("POST", "/api/education/courses", {"title": "E2E课件附件课程"})
    material = admin_call("POST", f"/api/education/courses/{course['id']}/materials",
                          {"title": "E2E带附件的课件", "material_type": "doc"})
    pdf = tmp_path / "e2e_p2431.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    _login(page, base_url)
    _open_page(page, "education", "远程医学教育")
    form = page.locator("#cm-att")
    form.locator('input[name="material_id"]').fill(str(material["id"]))
    form.locator('input[type="file"]').set_input_files(str(pdf))
    form.locator("button").click()
    listing = page.locator("#cm-att-list")
    expect(listing).to_contain_text("e2e_p2431.pdf")   # 修前：上传成功之后什么也不列
    with page.expect_download() as info:
        listing.locator("button[data-attdl]").first.click()
    assert info.value.suggested_filename == "e2e_p2431.pdf"
    query = page.locator("#cm-query")
    query.locator('input[name="course_id"]').fill(str(course["id"]))
    query.locator("button").click()
    page.click(f'button[data-cmatt="{material["id"]}"]')   # 修前这一格只是个数字
    expect(listing).to_contain_text("e2e_p2431.pdf")


def test_课件外链看得到点得开_点播打开并计一次_清单不被重画(page, base_url, admin_call, admin_read, tmp_path):
    """P2-1428：课件清单原先不读外链——视频、PPT 只能填外链，填进去页面上哪儿都看不到；「点播」只发计数再整页重画，什么也
    不打开，刚查出来的课件清单也没了。修后「外链」列给 http(s) 地址画链接；「点播」先开外链再计一次、只改这一行的点播数；
    没有外链但有附件的计一次并展开附件清单；两样都没有的不摆「点播」。"""
    course = admin_call("POST", "/api/education/courses", {"title": "E2E课件外链课程"})
    url = f"{base_url}/api/health"   # 用本服务自己的地址：端到端环境不出网
    mats = {title: admin_call("POST", f"/api/education/courses/{course['id']}/materials", body)["id"]
            for title, body in (("E2E外链课件", {"title": "E2E外链课件", "material_type": "video", "url": url}),
                                ("E2E只有附件的课件", {"title": "E2E只有附件的课件", "material_type": "doc"}),
                                ("E2E什么都没有的课件", {"title": "E2E什么都没有的课件", "material_type": "doc"}))}
    pdf = tmp_path / "e2e_p21428.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")

    def plays():
        return {m["title"]: m["play_count"] for m in admin_read(f"/api/education/courses/{course['id']}/materials")}

    _login(page, base_url)
    _open_page(page, "education", "远程医学教育")
    form = page.locator("#cm-att")
    form.locator('input[name="material_id"]').fill(str(mats["E2E只有附件的课件"]))
    form.locator('input[type="file"]').set_input_files(str(pdf))
    form.locator("button").click()
    expect(page.locator("#cm-att-list")).to_contain_text("e2e_p21428.pdf")
    query = page.locator("#cm-query")
    query.locator('input[name="course_id"]').fill(str(course["id"]))
    query.locator("button").click()
    listing = page.locator("#cm-list")
    linked = listing.locator("tr", has_text="E2E外链课件")
    expect(linked.locator("a")).to_have_attribute("href", url)   # 修前清单没有外链这一列
    expect(listing.locator("tr", has_text="E2E什么都没有的课件").locator("button[data-play]")).to_have_count(0)
    page.eval_on_selector("#page-body", "el => el.dataset.stamp = 'e2e-keep'")
    with page.expect_popup() as popup:
        linked.locator("button[data-play]").click()   # 修前什么也不打开
    expect(popup.value).to_have_url(url)
    popup.value.close()
    expect(linked.locator(f'[data-plays="{mats["E2E外链课件"]}"]')).to_have_text("1")
    assert page.eval_on_selector("#page-body", "el => el.dataset.stamp") == "e2e-keep", "点播之后整页重画了，查出来的清单被冲掉"
    page.eval_on_selector("#cm-att-list", "el => el.innerHTML = ''")
    attached = listing.locator("tr", has_text="E2E只有附件的课件")
    attached.locator("button[data-play]").click()
    expect(page.locator("#cm-att-list")).to_contain_text("e2e_p21428.pdf")   # 没有外链的展开附件清单
    expect(attached.locator(f'[data-plays="{mats["E2E只有附件的课件"]}"]')).to_have_text("1")
    assert plays() == {"E2E外链课件": 1, "E2E只有附件的课件": 1, "E2E什么都没有的课件": 0}


def test_会诊与转诊的佐证材料在页面上传得上看得到(page, base_url, seed, admin_call, tmp_path):
    """P2-432：会诊、转诊两类附件（病历影像截图、检查单 PDF）后端早就支持，页面上连上传入口都没有——
    只能靠接口调用方。修后两页各有一块佐证材料面板：按单号上传、查附件、下载。"""
    org = seed["org"]["id"]
    other = admin_call("POST", "/api/organizations", {"name": "E2E佐证受邀医院", "org_type": "township",
                                                      "level": "township"})["id"]
    consult = admin_call("POST", "/api/consultations", {
        "patient_id": seed["patient"]["id"], "from_org_id": org, "to_org_id": other, "question": "E2E佐证会诊"})
    referral = admin_call("POST", "/api/referrals", {
        "patient_id": seed["patient"]["id"], "from_org_id": other, "to_org_id": org, "direction": "up",
        "reason": "E2E佐证转诊"})
    pdf = tmp_path / "e2e_p2432.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    _login(page, base_url)
    for page_id, title, prefix, owner_id in (("consultations", "远程会诊", "cons", consult["id"]),
                                             ("referrals", "双向转诊", "ref", referral["id"])):
        _open_page(page, page_id, title)
        form = page.locator(f"#{prefix}-att-form")   # 修前：页面上没有这张表
        form.locator('input[name="owner_id"]').fill(str(owner_id))
        form.locator('input[type="file"]').set_input_files(str(pdf))
        form.locator("button").click()
        listing = page.locator(f"#{prefix}-att-list")
        expect(listing).to_contain_text("e2e_p2432.pdf")
        with page.expect_download() as info:
            listing.locator("button[data-attdl]").first.click()
        assert info.value.suggested_filename == "e2e_p2432.pdf"


def test_专病中心的牵头机构覆盖机构团队在页面上录得进改得了(page, base_url, seed, admin_call, admin_read):
    """P2-433：中心的牵头机构 / 覆盖机构 / 团队三项页面上原先没有录入框——卫健委工作台按两张清单数覆盖机构与团队，
    页面上建的中心恒为 0。修后建中心的表单与编辑框都能录，编辑框按库里的现值预填。"""
    org = seed["org"]["id"]
    team = admin_call("POST", "/api/spd/teams", {"name": "E2E中心团队", "org_id": org, "level": "county"})
    _login(page, base_url)
    _open_page(page, "spdexpert", "专病专家端·临床指导")
    form = page.locator("#spd-center-form")
    form.locator('input[name="code"]').fill("E2E_C433")
    form.locator('input[name="name"]').fill("E2E覆盖中心")
    form.locator('input[name="program_code"]').fill("hypertension")
    form.locator('input[name="lead_org_id"]').fill(str(org))   # 修前：没有这三个框
    form.locator('input[name="org_ids"]').fill(str(org))
    form.locator('input[name="team_ids"]').fill(str(team["id"]))
    _submit(page, "#spd-center-form button")
    (center,) = [c for c in admin_read("/api/spd/centers") if c["code"] == "E2E_C433"]
    assert (center["lead_org_id"], center["org_ids"], center["team_ids"]) == (org, [org], [team["id"]]), center

    page.click(f'button[data-center-edit="{center["id"]}"]')
    expect(_modal(page).locator('[name="team_ids"]')).to_have_value(str(team["id"]))   # 按现值预填
    _redrawn(page, lambda: _spd_modal(page, {"team_ids": ""}))
    (after,) = [c for c in admin_read("/api/spd/centers") if c["id"] == center["id"]]
    assert (after["lead_org_id"], after["org_ids"], after["team_ids"]) == (org, [org], []), after


def test_删除路径节点先确认(page, base_url, admin_read, admin_call):
    """P2-43：「删除」路径节点原先点一下就删，节点的时限、角色与表单配置一并没了。"""
    hyp = next(p for p in admin_read("/api/spd/programs") if p["code"] == "hypertension")
    tpl = admin_call("POST", "/api/spd/path-templates",
                     {"program_id": hyp["id"], "code": "e2e_p243_path", "name": "E2E删节点前确认"})
    node = admin_call("POST", f"/api/spd/path-templates/{tpl['id']}/nodes",
                      {"key": "p243", "name": "E2E待删节点", "seq": 1, "due_days": 7})

    def node_ids():
        return [n["id"] for n in admin_read(f"/api/spd/path-templates/{tpl['id']}")["nodes"]]

    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    page.click(f'button[data-tpl-nodes="{tpl["id"]}"]')
    delete = page.locator(f'button[data-node-del="{node["id"]}"]')
    delete.click()
    expect(_modal(page)).to_contain_text("不能恢复")
    _cancel_modal(page)
    assert node_ids() == [node["id"]], "点了取消却照样删了"
    # 删除后只重画节点表并给提示（不整页 route）
    delete.click()
    _spd_modal(page, {})
    expect(page.locator("#spd-tpl-msg")).to_contain_text("节点已删除")
    assert node_ids() == []


def test_路径节点时限能填0天_编辑能把顺序与时限改成0(page, base_url, admin_read, admin_call):
    """P2-588：spdModal 的 number 字段把空串折成 0，添加节点又 `|| 7`、编辑节点 `if (form.due_days)`——「当天完成」
    的节点建不出来、改不过去（改成 0 不送，照样提示「节点已更新」），顺序也回不到 0。"""
    hyp = next(p for p in admin_read("/api/spd/programs") if p["code"] == "hypertension")
    tpl = admin_call("POST", "/api/spd/path-templates",
                     {"program_id": hyp["id"], "code": "e2e_p2588_path", "name": "E2E当天完成"})
    node = admin_call("POST", f"/api/spd/path-templates/{tpl['id']}/nodes",
                      {"key": "p2588a", "name": "E2E节点A", "seq": 3, "due_days": 7})

    def nodes():
        return {n["key"]: n for n in admin_read(f"/api/spd/path-templates/{tpl['id']}")["nodes"]}

    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    page.click(f'button[data-tpl-node="{tpl["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {"key": "p2588b", "due_days": "0"}))
    assert nodes()["p2588b"]["due_days"] == 0   # 修前 7

    page.click(f'button[data-tpl-nodes="{tpl["id"]}"]')
    page.click(f'button[data-node-edit="{node["id"]}"]')
    _spd_modal(page, {"seq": "0", "due_days": "0"})
    expect(page.locator("#spd-tpl-msg")).to_contain_text("节点已更新")
    assert (nodes()["p2588a"]["seq"], nodes()["p2588a"]["due_days"]) == (0, 0)   # 修前 (3, 7)


def test_管理目标的上限能改成0(page, base_url, admin_read, admin_call):
    """P2-589：编辑管理目标原先 `if (form.target_high)`——吸烟目标从「每日 ≤5 支」收紧到 0（戒烟）不送、照样提示
    「管理目标已更新」，之后每天 3 支仍判「正常」。"""
    program = admin_call("POST", "/api/spd/programs", {"code": "e2e_p2589", "name": "E2E控烟", "category": "chronic"})
    target = admin_call("POST", f"/api/spd/programs/{program['id']}/targets",
                        {"metric": "smoke", "metric_name": "吸烟", "target_high": 5, "unit": "支/日"})
    _login(page, base_url)
    _open_page(page, "spdadmin", "平台管理端·运行中枢")
    page.click(f'button[data-prog-targets="{program["id"]}"]')
    page.click(f'button[data-target-edit="{target["id"]}"]')
    _spd_modal(page, {"target_high": "0"})
    expect(page.locator("#spd-program-msg")).to_contain_text("管理目标已更新")
    (row,) = [t for t in admin_read(f"/api/spd/programs/{program['id']}/targets") if t["id"] == target["id"]]
    assert (row["target_high"], row["metric_name"]) == (0, "吸烟"), row   # 修前 5.0


def test_基金池的预付比例能改成0(page, base_url, admin_read, admin_call):
    """P2-590：编辑基金池原先 `if (picked2.prepay_ratio_pct)`——预付比例改成 0（不预付）不送，照样保存成功、仍按原比例
    算计划预付额。"""
    pool = admin_call("POST", "/api/fund/pools", {
        "year": 2098, "insurance_type": "employee", "total_amount": 1000000, "prepay_ratio_pct": 30, "note": "E2E P2590"})
    _login(page, base_url)
    _open_page(page, "fund", "医保基金总额付费")
    page.click(f'button[data-fdedit="{pool["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {"prepay_ratio_pct": "0"}))
    (row,) = [p for p in admin_read("/api/fund/pools") if p["id"] == pool["id"]]
    assert (row["prepay_ratio_pct"], row["total_amount"]) == (0, 1000000), row   # 修前 30.0


def test_已归档的基金池写明状态_不给预付预结清算表单(page, base_url, admin_call):
    """P2-598：清单原先把已归档的池子写成「已关闭」（后端说「已归档」）；打开它，预付、预结、清算三张表单照样摆着，
    填完点下去才 409。"""
    pool = admin_call("POST", "/api/fund/pools", {
        "year": 2096, "insurance_type": "employee", "total_amount": 800000, "prepay_ratio_pct": 50, "note": "E2E P2598"})
    admin_call("PATCH", f"/api/fund/pools/{pool['id']}", {"status": "closed"})
    _login(page, base_url)
    _open_page(page, "fund", "医保基金总额付费")
    expect(page.locator("tr", has=page.locator(f'button[data-fdpick="{pool["id"]}"]'))).to_contain_text("已归档")
    page.click(f'button[data-fdpick="{pool["id"]}"]')
    expect(page.locator("#page-body")).to_contain_text("基金池已归档，不能再预付")
    for form in ("#fd-prepay", "#fd-period", "#fd-settle"):
        expect(page.locator(form)).to_have_count(0)


def test_机构协作分组改档改类型_表里类型列跟着变(page, base_url, admin_call):
    """P2-1533：分组表每行原先只有「管理成员」「停用 / 启用」——名称、类型、牵头机构、备注建错了只能停用。现在点「改档」弹页内
    表单、预填原值，改了类型点确定即重画，表里类型列换成新类型，没改的名称、备注照旧。"""
    group = admin_call("POST", "/api/org-groups", {"name": "E2E P21533 胸痛专科联盟", "note": "E2E 原备注"})   # 类型缺省片区
    _login(page, base_url)
    _open_page(page, "orggroups", "机构协作分组")
    row = page.locator(f'tr:has(button[data-ogedit="{group["id"]}"])')
    expect(row.locator("td").nth(1)).to_have_text("片区/分片")
    page.click(f'button[data-ogedit="{group["id"]}"]')
    expect(_modal(page).locator('[name="name"]')).to_have_value("E2E P21533 胸痛专科联盟")   # 预填原值
    expect(_modal(page).locator('[name="group_type"]')).to_have_value("zone")
    _redrawn(page, lambda: _spd_modal(page, {"group_type": "alliance"}))
    expect(row.locator("td").nth(1)).to_have_text("专科联盟")
    (saved,) = [g for g in admin_call("GET", "/api/org-groups") if g["id"] == group["id"]]
    assert (saved["group_type"], saved["name"], saved["note"]) == ("alliance", "E2E P21533 胸痛专科联盟", "E2E 原备注"), saved


def test_统一支付的金额占位按渠道写明留空时收哪一份(page, base_url):
    """P2-605：金额留空时医保渠道记本单的统筹支付额、其余渠道记冲抵后的自付额，占位原先一律写后者。"""
    _login(page, base_url)
    _open_page(page, "billing", "费用结算")
    amount = page.locator('#pay-form input[name="amount"]')
    expect(amount).to_have_attribute("placeholder", "金额(元，空=押金冲抵后应补缴的自付额)")
    page.locator('#pay-form select[name="channel"]').select_option("insurance")
    expect(amount).to_have_attribute("placeholder", "金额(元，空=本单医保统筹支付额)")   # 修前不变
    page.locator('#pay-form select[name="channel"]').select_option("cash")
    expect(amount).to_have_attribute("placeholder", "金额(元，空=押金冲抵后应补缴的自付额)")


def test_调阅留痕按依据筛_下拉写中文名查的是编码(page, base_url, seed, admin_read):
    """P2-1023：「依据」原先是自由文本框，照表格填「全域角色」查回空表，只有填 global 才查得到。"""
    admin_read(f"/api/access-logs?patient_id={seed['patient']['id']}")   # 按患者查调阅记录本身留痕，依据记「全域角色」
    _login(page, base_url)
    _open_page(page, "access-logs", "调阅留痕")
    basis = page.locator('#al-search select[name="basis"]')
    expect(basis.locator('option[value="global"]')).to_have_text("全域角色")   # 修前是文本框，没有这一项
    basis.select_option("global")
    with page.expect_response(lambda r: "/api/access-logs?" in r.url and "basis=global" in r.url) as got:
        page.click("#al-search button")
    assert got.value.ok
    page.wait_for_function("""() => {
        const tags = [...document.querySelectorAll('#al-table td .tag')].map((t) => t.textContent);
        return tags.length > 0 && tags.every((t) => t === '全域角色');
    }""")
    expect(basis).to_have_value("global")   # 重填选项不丢已选的依据


def test_慢专病转诊全轨迹_县级退回写退回(page, base_url, seed, admin_call):
    """P2-1022：县级医院那一格通过与退回共用环节名「县级医院接收」，全轨迹的「动作」列原先原样印 pass / reject。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E转诊退回", "id_card": "320981199405051022", "gender": "男"})
    admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    case = admin_call("POST", "/api/spd/referrals", {
        "patient_id": patient["id"], "program_code": "hypertension", "reason": "E2E血压不达标"})
    admin_call("POST", f"/api/spd/referrals/{case['id']}/review", {"action": "pass", "opinion": "同意上转"})
    admin_call("POST", f"/api/spd/referrals/{case['id']}/review", {"action": "reject", "opinion": "E2E床位紧张"})
    _login(page, base_url)
    _open_page(page, "spdreferral", "逐级转诊闭环")
    page.click(f'[data-ref-detail="{case["id"]}"]')
    last = page.locator("#spd-ref-detail tr", has_text="E2E床位紧张")
    expect(last).to_contain_text("县级医院接收")
    expect(last).to_contain_text("退回")   # 修前印 reject
    expect(page.locator("#spd-ref-detail tr", has_text="同意上转")).to_contain_text("通过")


def test_统一支付选得到网关支付_受理回执写待回调_不报支付失败(page, base_url):
    """P2-1021：渠道下拉原先没有「网关支付」；网关受理（pending）的回执原先写「支付失败：」加空原因，付款链接 / 二维码串
    一并丢掉。e2e 服务没有真网关，下单应答由 Playwright 截下来按网关受理的形状回（后端这一形状见
    test_payment_gateway_channel.py），这里只看页面怎么写。"""
    _login(page, base_url)
    _open_page(page, "billing", "费用结算")
    replies = [{"pay_url": "https://pay.example.com/h5/E2E1021", "qr_code": "weixin://wxpay/E2E1021"},
               {"pay_url": "javascript:alert(1021)", "qr_code": ""}]

    def accepted(route):
        if route.request.method != "POST":
            return route.continue_()
        route.fulfill(status=201, json={
            "id": 991021, "settlement_id": 1, "channel": "gateway", "channel_name": "网关支付", "amount": 12.5,
            "refunded_amount": 0, "status": "pending", "status_name": "待支付", "trade_no": "GW-E2E-1021",
            "fail_reason": "", "paid_at": None, "refunded_at": None, "callback_at": None,
            "created_at": "2026-09-30T08:00:00", **replies.pop(0)})

    page.route("**/api/billing/payments", accepted)
    msg = page.locator("#pay-msg")
    for _ in range(2):
        page.fill('#pay-form input[name="settlement_id"]', "1")
        page.locator('#pay-form select[name="channel"]').select_option("gateway")   # 修前没有这一项
        _submit(page, "#pay-form button")
        expect(msg).to_contain_text("已受理，待网关回调确认到账（流水号 GW-E2E-1021）")
        expect(msg).not_to_contain_text("支付失败")   # 修前「支付失败：」
        if replies:
            expect(msg).to_contain_text("二维码串 weixin://wxpay/E2E1021")
            expect(msg.locator("a")).to_have_attribute("href", "https://pay.example.com/h5/E2E1021")
        else:
            expect(msg.locator("a")).to_have_count(0)   # javascript: 链接不进页面
    page.unroute("**/api/billing/payments")


def test_退款金额填了读不成数_框里报错不发请求_不当成全额退款(page, base_url):
    """P1-233：退款金额是文本框，原先「50元」「５０」「1,000」经 Number() 得 NaN、JSON 里成了 null，后端按「没填」
    整单全额退款。e2e 服务里造一张已支付的网关单要真网关回调，支付单清单与退款应答由 Playwright 截下来回（后端
    「显式 null 按缺省全额」见 billing.RefundIn），这里看页面发不发、发什么。"""
    paid = {"id": 991233, "settlement_id": 1, "channel": "cash", "channel_name": "现金", "amount": 300,
            "refunded_amount": 0, "status": "paid", "status_name": "已支付", "trade_no": "E2E-1233", "fail_reason": "",
            "paid_at": "2026-09-30T08:00:00", "refunded_at": None, "callback_at": None,
            "created_at": "2026-09-30T08:00:00", "pay_url": "", "qr_code": ""}
    sent = []

    def payments(route):
        if route.request.method != "GET":
            return route.continue_()
        route.fulfill(status=200, json=[paid])

    def refund(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=200, json={"payment_id": paid["id"], "refund_amount": 50, "refund_no": "RF-E2E-1233",
                                        "status": "paid", "refunded_amount": 50})

    page.route("**/api/billing/payments", payments)
    page.route("**/api/billing/payments/991233/refund", refund)
    _login(page, base_url)
    _open_page(page, "billing", "费用结算")
    for typed in ("50元", "５０", "1,000"):
        page.click('[data-refund="991233"]')
        form = _spd_modal_rejected(page, {"amount": typed})
        expect(form.locator("[data-modal-msg]")).to_contain_text("退款金额要填大于 0 的数字")
        expect(form.locator('[name="amount"]')).to_have_value(typed)   # 框不关、填的还在
        form.locator("button[data-cancel]").click()
        expect(form).to_have_count(0)
    assert sent == []   # 修前三次都发出 {"amount": null}，整单全额退
    page.click('[data-refund="991233"]')
    _spd_modal(page, {"amount": "50", "reason": "E2E部分退款"})
    expect(page.locator("#pay-msg")).to_contain_text("退款成功 50 元，退款单号 RF-E2E-1233")
    assert sent == [{"amount": 50, "reason": "E2E部分退款"}]
    page.unroute("**/api/billing/payments/991233/refund")
    page.unroute("**/api/billing/payments")


def test_编辑专病中心只改名_已停用的状态不被悄悄改成筹建(page, base_url, admin_read, admin_call):
    """P2-421：编辑框的状态下拉原先写死筹建 / 运行中 / 暂停三项，已停用的中心一打开就落在第一项「筹建」，
    只改个名字保存，状态被悄悄改掉。修后选项取自后端的状态文案表（专家工作台下发的 `center_status_names`）。"""
    center = admin_call("POST", "/api/spd/centers",
                        {"code": "E2E_C421", "name": "E2E停用中心", "program_code": "hypertension"})
    admin_call("PATCH", f"/api/spd/centers/{center['id']}", {"status": "disabled"})

    _login(page, base_url)
    _open_page(page, "spdexpert", "专病专家端·临床指导")
    page.click(f'button[data-center-edit="{center["id"]}"]')
    expect(_modal(page).locator('[name="status"]')).to_have_value("disabled")   # 修前 draft
    _redrawn(page, lambda: _spd_modal(page, {"name": "E2E停用中心改名"}))
    (row,) = [c for c in admin_read("/api/spd/centers") if c["id"] == center["id"]]
    assert (row["name"], row["status"]) == ("E2E停用中心改名", "disabled"), row


@pytest.fixture(scope="session")
def spd_open_consult(base_url, admin_call):
    """一条还开着的慢专病在线咨询。会话只能由居民端发起，造数也走居民端：开户 → 实名 → 发消息。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    name, id_card = "E2E咨询结束患者", "320981198507072227"
    admin_call("POST", "/api/patients", {"name": name, "id_card": id_card, "gender": "男"})
    resident = post("/api/portal/auth/wechat/login", {"code": "mock-e2e-p243", "state": ""})["access_token"]
    post("/api/portal/auth/realname", {"name": name, "id_card": id_card}, resident)
    return post("/api/portal/spd/consults", {"content": "E2E结束前确认", "program_code": "hypertension"},
                resident)["consult_id"]


def test_结束慢专病咨询先确认(page, base_url, admin_read, spd_open_consult):
    """P2-43：慢专病咨询「结束」原先点一下就结束，结束后医生端不能再回复这次会话。"""
    cid = spd_open_consult

    def status():
        (row,) = [c for c in admin_read("/api/spd/consults?limit=500") if c["id"] == cid]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "spdmanager", "个案管理师端·专属衔接")
    _confirm_then(page, lambda: page.click(f'button[data-consult-close="{cid}"]'), "不能再回复",
                  lambda: status() == "open", lambda: status() == "closed")


def test_已结束的慢专病咨询打开会话不给回复框(page, base_url, admin_call):
    """P2-596：回复接口对已结束的会话 409「该会话已结束」，打开会话原先照样摆着回复框，写完一段点下去才报错。"""
    import json
    from urllib.request import Request

    def post(path, payload, token):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    name, id_card = "E2E已结束咨询患者", "320981198507072235"
    admin_call("POST", "/api/patients", {"name": name, "id_card": id_card, "gender": "男"})
    login = Request(f"{base_url}/api/portal/auth/wechat/login", headers={"Content-Type": "application/json"},
                    data=json.dumps({"code": "mock-e2e-p2596", "state": ""}).encode())
    with urlopen(login, timeout=10) as resp:
        resident = json.loads(resp.read())["access_token"]
    post("/api/portal/auth/realname", {"name": name, "id_card": id_card}, resident)
    cid = post("/api/portal/spd/consults", {"content": "E2E结束后再看", "program_code": "diabetes"},
               resident)["consult_id"]
    admin_call("POST", f"/api/spd/consults/{cid}/close")

    _login(page, base_url)
    _open_page(page, "spdmanager", "个案管理师端·专属衔接")
    page.click(f'button[data-consult="{cid}"]')
    thread = page.locator("#spd-consult-thread")
    expect(thread).to_contain_text("E2E结束后再看")
    expect(thread).to_contain_text("会话已结束，不能再回复")
    expect(page.locator("#spd-consult-reply")).to_have_count(0)   # 修前照样摆着回复框


def test_慢专病咨询按患者查_找得回已结束的会话(page, base_url, admin_call):
    """P2-1604：咨询清单原先不收 patient_id，页面只取最新 50 条加进行中的，已结束的会话一被挤出窗口就找不回。"""
    import json
    from urllib.request import Request

    def post(path, payload, token):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    name, id_card = "E2E按患者查咨询", "320981198507071604"
    pid = admin_call("POST", "/api/patients", {"name": name, "id_card": id_card, "gender": "男"})["id"]
    login = Request(f"{base_url}/api/portal/auth/wechat/login", headers={"Content-Type": "application/json"},
                    data=json.dumps({"code": "mock-e2e-p21604", "state": ""}).encode())
    with urlopen(login, timeout=10) as resp:
        resident = json.loads(resp.read())["access_token"]
    post("/api/portal/auth/realname", {"name": name, "id_card": id_card}, resident)
    cid = post("/api/portal/spd/consults", {"content": "E2E按患者查", "program_code": "hypertension"},
               resident)["consult_id"]
    admin_call("POST", f"/api/spd/consults/{cid}/close")

    _login(page, base_url)
    _open_page(page, "spdmanager", "个案管理师端·专属衔接")
    form = page.locator("#spd-consult-filter")
    form.locator("input[name=patient_id]").fill(str(pid))
    form.locator("button").click()
    rows = page.locator("#spd-consult-list tbody tr")
    expect(rows).to_have_count(1)   # 只剩这位患者的会话
    expect(rows.first.locator(f'button[data-consult="{cid}"]')).to_have_count(1)
    expect(rows.first).to_contain_text(name)
    expect(rows.first).to_contain_text("已结束")


def test_随访问卷在界面上录题目与异常规则_执行随访逐题作答判出异常(page, base_url, seed, admin_read, admin_call):
    """P1-122：建问卷的表单原先没有题目框，异常规则编辑器列的是 /api/spd/meta 的事实字段、交上去的是平铺条件
    （后端读 when，一条都存不进）；执行随访只填渠道与结果、answers 恒为空——问卷的异常分级从界面上一次都触发
    不了。现在题目在表单里录，规则字段取自题目，执行随访逐题作答。

    规则区要等 `/api/spd/meta` 回来才建，这里扣住它、先填题目：加载慢时先填了题目，规则区建好得认上（第四十轮全量端到端
    在慢机器上撞到——题目的 input 事件早于监听挂上，「添加异常规则」一直藏着）。"""
    held = []
    page.route("**/api/spd/meta", lambda route: held.append(route))
    _login(page, base_url)
    _open_page(page, "spdfollowup", "智能随访服务端")
    form = page.locator("#spd-quest-form")
    form.locator('[name="code"]').fill("E2E_Q122")
    form.locator('[name="name"]').fill("E2E 术后问卷")
    form.locator('[name="items"]').fill("pain:疼痛评分:number；mood:情绪:single:好/差")
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    assert held, "随访页没去取 /api/spd/meta"
    for route in held:
        route.continue_()
    page.unroute("**/api/spd/meta")
    rules = page.locator("#spd-quest-rules")
    # 题目框此刻还有焦点：点「添加」先让它失焦、触发 change——规则区不能在这一刻重画，否则这次点击被吞
    rules.locator("button.abn-add").click()
    expect(rules.locator(".abn-field option")).to_have_count(2)
    row = rules.locator(".spd-abn-row")
    row.locator(".abn-field").select_option("pain")
    row.locator(".abn-op").select_option(">=")
    row.locator(".abn-value").fill("7")
    row.locator(".abn-level").select_option("high")
    row.locator(".abn-action").fill("通知主管医师")
    _submit(page, "#spd-quest-form button")
    saved = next(q for q in admin_read("/api/spd/questionnaires") if q["code"] == "E2E_Q122")
    assert [it["key"] for it in saved["items"]] == ["pain", "mood"]
    assert saved["abnormal_rules"] == [
        {"when": {"field": "pain", "op": ">=", "value": 7}, "level": "high", "action": "通知主管医师"}]

    rule = admin_call("POST", "/api/spd/followup-rules", {
        "code": "E2E_R122", "name": "E2E 术后随访", "points": [0], "questionnaire_code": "E2E_Q122"})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": seed["patient"]["id"], "rule_id": rule["id"], "base_date": "2001-01-01",
        "org_id": seed["org"]["id"]})
    record_id = plan["items"][0]["id"]
    page.click("#spd-fu-filter button")   # 看板重新查一次，带上刚生成的随访（计划日最早，排在第一页）
    page.click(f'button[data-fu-exec="{record_id}"]')
    expect(_modal(page)).to_contain_text("疼痛评分")
    _redrawn(page, lambda: _spd_modal(page, {"q_pain": "9", "result": "诉疼痛加重"}))
    record = admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]
    assert (record["status"], record["abnormal_level"], record["answers"]) == ("done", "high", {"pain": 9})


def test_执行随访的多选题是复选框_勾中的选项照原样交上去(page, base_url, seed, admin_read, admin_call):
    """P2-1636：管理端执行随访时多选题原先是「（多选，逗号分隔）」自由文本，录成「胸 痛」不命中「胸痛」的规则、选项外的
    作答照存。现在有选项的多选题是复选框（与居民端自助作答同一个形态），交上去的就是勾中的选项；后端对选项外的作答 422。"""
    admin_call("POST", "/api/spd/questionnaires", {
        "code": "E2E_Q1636", "name": "E2E 多选问卷",
        "items": [{"key": "sym", "title": "近期症状", "type": "multi",
                   "options": [{"label": "无"}, {"label": "胸痛"}, {"label": "气短"}]}],
        "abnormal_rules": [{"when": {"field": "sym", "op": "in", "value": ["胸痛"]}, "level": "high",
                            "action": "胸痛立即上转"}]})
    rule = admin_call("POST", "/api/spd/followup-rules", {
        "code": "E2E_R1636", "name": "E2E 多选随访", "points": [0], "questionnaire_code": "E2E_Q1636"})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": seed["patient"]["id"], "rule_id": rule["id"], "base_date": "2001-01-01",
        "org_id": seed["org"]["id"]})
    record_id = plan["items"][0]["id"]
    _login(page, base_url)
    _open_page(page, "spdfollowup", "智能随访服务端")
    page.click("#spd-fu-filter button")
    page.click(f'button[data-fu-exec="{record_id}"]')
    form = _modal(page)
    boxes = form.locator('input[type="checkbox"][name="q_sym"]')
    expect(boxes).to_have_count(3)   # 修前是一个文本框
    expect(form).not_to_contain_text("逗号分隔")
    form.get_by_text("气短", exact=True).click()   # 点选项文字也勾得上，且只勾这一个
    form.locator('input[name="q_sym"][value="胸痛"]').check()
    expect(form.locator('input[name="q_sym"][value="无"]')).not_to_be_checked()
    _redrawn(page, lambda: _spd_modal(page, {"result": "诉胸痛气短"}))
    record = admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]
    assert (record["status"], record["abnormal_level"], record["answers"]) == ("done", "high", {"sym": ["胸痛", "气短"]})


def test_超期的随访在看板上照样能执行与转呼叫(page, base_url, seed, admin_read, admin_call):
    """P1-132：随访看板的「执行」「转呼叫」原先只对待随访的给。超期扫描（定时任务、工作台、任务汇总进来都扫一遍）一过，
    过了日期没做的随访都成了「已超期」——最需要补做的那些在页面上再也执行不了（接口本就收已超期的）。"""
    rule = admin_call("POST", "/api/spd/followup-rules", {"code": "E2E_R132", "name": "E2E 超期补做", "points": [0]})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": seed["patient"]["id"], "rule_id": rule["id"], "base_date": "2001-02-02",
        "org_id": seed["org"]["id"]})
    record_id = plan["items"][0]["id"]
    admin_read("/api/spd/tasks/summary")   # 进任务汇总顺手扫一次超期（与定时任务同一个函数）
    assert admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]["status"] == "overdue"

    _login(page, base_url)
    _open_page(page, "spdfollowup", "智能随访服务端")
    page.click("#spd-fu-filter button")
    expect(page.locator(f'button[data-fu-call="{record_id}"]')).to_have_count(1)   # 修前 0
    page.click(f'button[data-fu-exec="{record_id}"]')   # 修前超期的这一行没有「执行」
    # 随访结果写超了（后端 512 字）：框自己提交，报错写在框里、框不关、写的还在（P2-607 第四批）
    form = _spd_modal_rejected(page, {"result": "补" * 513})
    expect(form.locator('[name="result"]')).to_have_value("补" * 513)
    assert admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]["status"] == "overdue"
    _redrawn(page, lambda: _spd_modal(page, {"result": "补做电话随访，恢复良好"}))
    record = admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]
    assert (record["status"], record["result"]) == ("done", "补做电话随访，恢复良好"), record


def test_失访的随访在看板上能补录(page, base_url, seed, admin_read, admin_call):
    """P2-1637：后端执行收失访（`allowed_from` 含 unreachable，注释写「失访的还能补录」），看板却只对待随访、已超期的给
    「执行」——失访的要先「调整 → 恢复为待随访」才能补录。现在失访行给「补录」；「转呼叫」不动（接通回写 P2-1256 待裁定）。"""
    rule = admin_call("POST", "/api/spd/followup-rules", {"code": "E2E_R1637", "name": "E2E 失访补录", "points": [0]})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": seed["patient"]["id"], "rule_id": rule["id"], "base_date": "2001-01-02",
        "org_id": seed["org"]["id"]})
    record_id = plan["items"][0]["id"]
    admin_call("POST", f"/api/spd/followup-records/{record_id}/execute",
               {"channel": "phone", "result": "三次未接通", "unreachable": True})

    _login(page, base_url)
    _open_page(page, "spdfollowup", "智能随访服务端")
    page.locator('#spd-fu-filter [name="status"]').select_option("unreachable")
    page.click("#spd-fu-filter button")
    button = page.locator(f'button[data-fu-exec="{record_id}"]')
    expect(button).to_have_text("补录")   # 修前失访行没有这个按钮
    expect(page.locator(f'button[data-fu-call="{record_id}"]')).to_have_count(0)
    button.click()
    _redrawn(page, lambda: _spd_modal(page, {"result": "患者回电，恢复良好"}))
    record = admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]
    assert (record["status"], record["result"]) == ("done", "三次未接通 患者回电，恢复良好"), record


def test_回写通话结果由框自己提交_录音地址写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第四批：「回写通话结果」原先点确定就关框、再发请求——录音地址写超了（后端 256 字）报错落在页面消息行，
    写好的沟通结果全丢。现在框自己提交：失败留框、报错写在框里、填的都在；成功才关框、整页重画。"""
    rule = admin_call("POST", "/api/spd/followup-rules", {"code": "E2E_P2607_CALL", "name": "E2E 通话回写", "points": [0]})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": seed["patient"]["id"], "rule_id": rule["id"], "org_id": seed["org"]["id"]})
    call = admin_call("POST", "/api/spd/call-tasks", {   # 种子患者没留电话：转呼叫时带上号码（P2-881 起没号码 422）
        "patient_id": seed["patient"]["id"], "ref_type": "followup", "ref_id": plan["items"][0]["id"],
        "phone": "13800002607"})

    def status():
        return next(c for c in admin_read("/api/spd/call-tasks?limit=50") if c["id"] == call["id"])["status"]

    assert status() == "pending"
    _login(page, base_url)
    _open_page(page, "spdfollowup", "智能随访服务端")
    page.click(f'button[data-call-result="{call["id"]}"]')
    form = _spd_modal_rejected(page, {"status": "failed", "record_url": "u" * 257, "result": "E2E 占线，明日再拨"})
    expect(form.locator('[name="result"]')).to_have_value("E2E 占线，明日再拨")   # 修前框关，沟通结果全丢
    assert status() == "pending"
    _redrawn(page, lambda: _spd_modal(page, {"record_url": ""}))
    assert status() == "failed"


def test_呼叫台账印中文来源与计划日_抽查按数量_基本合格(page, base_url, seed, admin_read, admin_call):
    """P2-1638：呼叫台账「来源」列原先原样印 followup、看不出是哪条随访；抽查结论 warn 页面写「提醒」（列注释是「基本合格」）；
    抽查表单只有比例，接口早有 count。现在台账印「随访 #号」与计划日，warn 显示「基本合格」，表单能按数量抽。"""
    dept = "E2E质控科1638"
    rule = admin_call("POST", "/api/spd/followup-rules", {"code": "E2E_R1638", "name": "E2E 质控随访", "points": [0, 7]})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": seed["patient"]["id"], "rule_id": rule["id"], "base_date": "2001-01-05",
        "org_id": seed["org"]["id"], "dept": dept})
    first, second = (item["id"] for item in plan["items"])
    call = admin_call("POST", "/api/spd/call-tasks", {
        "patient_id": seed["patient"]["id"], "ref_type": "followup", "ref_id": first, "phone": "13800001638"})
    for record_id in (first, second):
        admin_call("POST", f"/api/spd/followup-records/{record_id}/execute", {"channel": "phone", "result": "已随访"})

    _login(page, base_url)
    _open_page(page, "spdfollowup", "智能随访服务端")
    row = page.locator("tr", has=page.locator(f'button[data-call-result="{call["id"]}"]'))
    expect(row).to_contain_text(f"随访 #{first}")   # 修前印 followup
    expect(row).to_contain_text("计划 2001-01-05")
    expect(row).not_to_contain_text("followup")

    form = page.locator("#spd-qc-form")
    form.locator('[name="dept"]').fill(dept)
    form.locator('[name="count"]').fill("2")   # 修前没有这一栏
    _submit(page, "#spd-qc-form button")
    samples = [s for s in admin_read("/api/spd/qc-samples?limit=200") if s["dept"] == dept]
    assert sorted(s["record_id"] for s in samples) == sorted([first, second]), samples
    page.click(f'button[data-qc-judge="{samples[0]["id"]}"]')
    expect(_modal(page).locator('[name="result"] option[value="warn"]')).to_have_text("基本合格")   # 修前「提醒」
    _redrawn(page, lambda: _spd_modal(page, {"result": "warn"}))
    qc_rows = page.locator("tr", has=page.locator(f'td:text-is("{dept}")'))
    expect(qc_rows.filter(has_text="基本合格")).to_have_count(1)
    expect(qc_rows.filter(has_text="提醒")).to_have_count(0)


def test_统筹调度的拒绝申请_改目标池状态_召回进度由框自己提交(page, base_url, seed, admin_read, admin_call):
    """P2-607 第五批：统筹调度中枢三张框（受理 / 拒绝服务申请、调整目标池状态、登记召回进度）原先点确定就关框、再发请求——
    拒绝原因、依据、联系情况写超了（后端 256 字）报错落在页面消息行，写好的一段全丢；拒绝没写原因也是框关了才说。
    现在框自己提交：失败留框、报错写在框里、填的都在；成功才关框、整页重画。"""
    import json
    from urllib.parse import quote
    from urllib.request import Request

    def post(path, payload, token):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    name, id_card = "E2E统筹调度申请人", "320981198909092241"
    admin_call("POST", "/api/patients", {"name": name, "id_card": id_card, "gender": "女"})
    login = Request(f"{base_url}/api/portal/auth/wechat/login", headers={"Content-Type": "application/json"},
                    data=json.dumps({"code": "mock-e2e-p2607-center", "state": ""}).encode())
    with urlopen(login, timeout=10) as resp:
        resident = json.loads(resp.read())["access_token"]
    post("/api/portal/auth/realname", {"name": name, "id_card": id_card}, resident)
    accept_id = post("/api/portal/spd/service-applies", {"program_code": "hypertension", "note": "E2E 想加入高血压管理"},
                     resident)["id"]
    reject_id = post("/api/portal/spd/service-applies", {"program_code": "diabetes", "note": "E2E 想加入糖尿病管理"},
                     resident)["id"]
    lost = admin_call("POST", "/api/patients", {"name": "E2E统筹调度失访患者", "id_card": "320981199001012250"})
    enrollment = admin_call("POST", "/api/spd/enrollments", {
        "patient_id": lost["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    admin_call("POST", f"/api/spd/enrollments/{enrollment['id']}/lifecycle", {"event": "recall", "reason": "E2E 失访三个月"})
    (recall,) = [r for r in admin_read("/api/spd/recalls?limit=50") if r["enrollment_id"] == enrollment["id"]]

    def apply_status(apply_id):
        return next(a for a in admin_read("/api/spd/service-applies?status=&limit=50") if a["id"] == apply_id)["status"]

    _login(page, base_url)
    _open_page(page, "spdcenter", "全程管理中心端·统筹调度")
    page.click(f'button[data-apply="{reject_id}"][data-decision="rejected"]')
    form = _spd_modal_rejected(page, {"handle_note": ""})
    expect(form.locator("[data-modal-msg]")).to_have_text("拒绝须写明原因")   # 修前框先关、报错落到页面消息行
    form.locator('[name="handle_note"]').fill("拒" * 257)
    form.locator("button[type=submit]").click()
    expect(form.locator("[data-modal-msg]")).to_contain_text("最多 256 个字")
    expect(form.locator('[name="handle_note"]')).to_have_value("拒" * 257)
    assert apply_status(reject_id) == "pending"
    _redrawn(page, lambda: _spd_modal(page, {"handle_note": "E2E 不在本辖区，请到户籍地申请"}))
    assert apply_status(reject_id) == "rejected"

    page.click(f'button[data-apply="{accept_id}"][data-decision="accepted"]')
    _redrawn(page, lambda: _spd_modal(page, {"handle_note": ""}))   # 受理说明可留空
    assert apply_status(accept_id) == "accepted"
    (cand,) = [c for c in admin_read(f"/api/spd/candidates?keyword={quote(name)}&limit=50") if c["program_code"] == "hypertension"]
    assert cand["status"] == "target", cand
    page.click(f'button[data-cand-status="{cand["id"]}"]')
    form = _spd_modal_rejected(page, {"status": "excluded", "reason": "依" * 257})
    expect(form.locator('[name="reason"]')).to_have_value("依" * 257)
    assert admin_read(f"/api/spd/candidates?keyword={quote(name)}&limit=50")[0]["status"] == "target"
    _redrawn(page, lambda: _spd_modal(page, {"reason": "E2E 已在外院规范管理"}))
    assert admin_read(f"/api/spd/candidates?keyword={quote(name)}&limit=50")[0]["status"] == "excluded"

    page.click(f'button[data-recall="{recall["id"]}"]')
    form = _spd_modal_rejected(page, {"status": "returned", "contact_note": "联" * 257, "result": "E2E 已回社区复诊"})
    expect(form.locator('[name="result"]')).to_have_value("E2E 已回社区复诊")
    assert next(r for r in admin_read("/api/spd/recalls?limit=50") if r["id"] == recall["id"])["status"] == "pending"
    _redrawn(page, lambda: _spd_modal(page, {"contact_note": "E2E 电话联系上，本周回社区复诊"}))
    done = next(r for r in admin_read("/api/spd/recalls?limit=50") if r["id"] == recall["id"])
    assert (done["status"], done["result"]) == ("returned", "E2E 已回社区复诊"), done
    assert admin_read(f"/api/spd/enrollments/{enrollment['id']}")["status"] == "active"   # 召回成功恢复在管


def test_服务项目扣减登记由框自己提交_备注写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第六批：「记用量」原先点确定就关框、再发请求——备注写超了（后端 256 字）、次数超了余量，报错落在页面消息行，
    选好的项目与写的备注全丢。现在框自己提交：失败留框、报错写在框里；成功才关框、刷新档案明细并报「扣减已登记」。"""
    pkg = admin_call("POST", "/api/spd/service-packages", {
        "code": "E2E_P2607_PKG", "name": "E2E扣减服务包", "program_code": "hypertension",
        "items": [{"code": "visit", "name": "上门随访", "times": 2}]})
    patient = admin_call("POST", "/api/patients", {"name": "E2E扣减登记患者", "id_card": "320981199103032260"})
    enrollment = admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    binding = admin_call("POST", f"/api/spd/enrollments/{enrollment['id']}/packages", {"package_id": pkg["id"]})

    def remaining():
        return admin_read(f"/api/spd/enrollments/{enrollment['id']}")["packages"][0]["remaining"]

    _login(page, base_url)
    _open_page(page, "spdpatients", "筛查建档与纳管")
    search = page.locator("#spd-enroll-filter")
    search.locator('[name="keyword"]').fill("E2E扣减登记患者")
    search.locator("button").click()
    page.click(f'button[data-enr-detail="{enrollment["id"]}"]')
    page.click(f'button[data-bind-use="{binding["id"]}"]')
    form = _spd_modal_rejected(page, {"note": "注" * 257})
    expect(form.locator('[name="note"]')).to_have_value("注" * 257)   # 修前框关，备注全丢
    assert remaining() == 2
    _spd_modal(page, {"note": "E2E 上门测血压"})
    expect(page.locator("#spd-enroll-msg")).to_have_text("扣减已登记")
    assert remaining() == 1


def test_成员端办结干预与处置上报由框自己提交_写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第六批：成员端「办结干预」「处置异常上报」原先点确定就关框、再发请求——患者反馈、处置意见写超了（后端 512 字）
    报错落在页面消息行，写好的一段全丢。现在框自己提交：失败留框、报错写在框里、填的都在；成功才关框、整页重画。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E成员端办结患者", "id_card": "320981199204042273"})
    admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    (intv_id,) = admin_call("POST", "/api/spd/interventions", {
        "patient_ids": [patient["id"]], "program_code": "hypertension", "goal": "E2E 控盐",
        "content": "E2E 每日食盐 5 克以下", "create_task": False})["ids"]
    report = admin_call("POST", "/api/spd/case-reports", {
        "patient_id": patient["id"], "program_code": "hypertension", "content": "E2E 血压 190/115"})

    def intervention():
        return next(i for i in admin_read(f"/api/spd/interventions?patient_id={patient['id']}") if i["id"] == intv_id)

    def case_report():
        return next(r for r in admin_read("/api/spd/case-reports?limit=50") if r["id"] == report["id"])

    _login(page, base_url)
    _open_page(page, "spdmember", "服务团队成员端·日常服务")
    page.click(f'button[data-intv="{intv_id}"][data-s="done"]')
    form = _spd_modal_rejected(page, {"feedback": "反" * 513})
    expect(form.locator('[name="feedback"]')).to_have_value("反" * 513)   # 修前框关，反馈全丢
    assert intervention()["status"] == "planned"
    _redrawn(page, lambda: _spd_modal(page, {"feedback": "E2E 已按要求控盐"}))
    assert (intervention()["status"], intervention()["feedback"]) == ("done", "E2E 已按要求控盐")

    page.click(f'button[data-crpt="{report["id"]}"]')
    form = _spd_modal_rejected(page, {"status": "done", "handle_note": "处" * 513})
    expect(form.locator('[name="handle_note"]')).to_have_value("处" * 513)
    assert case_report()["status"] == "pending"
    _redrawn(page, lambda: _spd_modal(page, {"handle_note": "E2E 已电话嘱其急诊"}))
    assert (case_report()["status"], case_report()["handle_note"]) == ("done", "E2E 已电话嘱其急诊")


def test_出具会诊意见由框自己提交_写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第七批：「出意见」原先点确定就关框、再发请求——意见写超了（后端 2048 字）报错落在页面消息行，写好的会诊意见
    全丢；留空则框关了什么也不发生。现在框自己提交：失败留框、报错写在框里、写的还在；成功才关框、整页重画。"""
    other = admin_call("POST", "/api/organizations", {"name": "E2E会诊意见受邀院", "org_type": "township", "level": "township"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E会诊意见患者", "id_card": "320981199305052286"})
    consult = admin_call("POST", "/api/consultations", {
        "patient_id": patient["id"], "from_org_id": seed["org"]["id"], "to_org_id": other["id"], "question": "E2E 出意见"})
    admin_call("POST", f"/api/consultations/{consult['id']}/accept", {"expert_name": "E2E专家"})

    def status():
        return next(c for c in admin_read("/api/consultations") if c["id"] == consult["id"])["status"]

    _login(page, base_url)
    _open_page(page, "consultations", "远程会诊")
    page.click(f'button[data-act="complete"][data-id="{consult["id"]}"]')
    form = _spd_modal_rejected(page, {"opinion": "会" * 2049})
    expect(form.locator('[name="opinion"]')).to_have_value("会" * 2049)   # 修前框关，意见全丢
    assert status() == "accepted"
    _redrawn(page, lambda: _spd_modal(page, {"opinion": "E2E 建议上级医院进一步检查"}))
    assert status() == "completed"


def test_修订危急值报告由框自己提交_理由写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第七批：「修订」原先点确定就关框、再发请求——修订理由写超了（后端 512 字）报错落在页面消息行，改好的结论
    与理由全丢。现在框自己提交：失败留框、报错写在框里、填的都在、报告不动；成功才关框、留一条修订史。"""
    req = admin_call("POST", "/api/exams", {"patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"],
                                            "center_type": "lab", "item_code": "E2E-AMD", "item_name": "E2E修订血钾"})
    admin_call("POST", f"/api/exams/{req['id']}/claim")
    report = admin_call("POST", f"/api/exams/{req['id']}/report", {
        "finding": "E2E 血钾 6.8mmol/L", "conclusion": "E2E 高钾血症（危急）", "critical": True, "reported_by": "检验科"})

    _login(page, base_url)
    _open_page(page, "exams", "共享诊断中心")
    page.click(f'button[data-amend="{report["id"]}"]')
    form = _spd_modal_rejected(page, {"conclusion": "E2E 复核：高钾血症（危急）", "reason": "修" * 513})
    expect(form.locator('[name="reason"]')).to_have_value("修" * 513)
    expect(form.locator('[name="conclusion"]')).to_have_value("E2E 复核：高钾血症（危急）")
    assert admin_read(f"/api/exams/reports/{report['id']}/revisions") == []
    _redrawn(page, lambda: _spd_modal(page, {"reason": "E2E 复核后改结论"}))
    (rev,) = admin_read(f"/api/exams/reports/{report['id']}/revisions")
    assert (rev["prev_conclusion"], rev["reason"]) == ("E2E 高钾血症（危急）", "E2E 复核后改结论"), rev


def test_互认弹窗印出原报告的项目名_与本次开单并排(page, base_url, seed, admin_call):
    """P2-1086（第三十一批 C4-3）：互认只比编码、名称随手填——开「头颅CT平扫」误填成胸片的编码，弹窗原先只印一句
    结论，照着点确定就拿胸片报告互认掉了。修后印出原报告的项目名、本次开单的名称，两个名对不上一眼看得出。"""
    admin_call("POST", "/api/exams/recognition-items",
               {"item_code": "E2E-P21086", "item_name": "E2E胸部DR", "center_type": "imaging"})
    req = admin_call("POST", "/api/exams", {"patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"],
                                            "center_type": "imaging", "item_code": "E2E-P21086", "item_name": "E2E胸部DR"})
    admin_call("POST", f"/api/exams/{req['id']}/claim")
    admin_call("POST", f"/api/exams/{req['id']}/report", {
        "finding": "", "conclusion": "E2E 双肺未见异常", "critical": False, "reported_by": "影像科"})

    _login(page, base_url)
    _open_page(page, "exams", "共享诊断中心")
    page.fill("#exam-form input[name=patient_id]", str(seed["patient"]["id"]))
    page.fill("#exam-form input[name=from_org_id]", str(seed["org"]["id"]))
    page.select_option("#exam-form select[name=center_type]", "imaging")
    page.fill("#exam-form input[name=item_code]", "E2E-P21086")
    page.fill("#exam-form input[name=item_name]", "E2E头颅CT平扫")
    page.click("#exam-form button")
    intro = _modal(page).locator(".desc")
    expect(intro).to_contain_text("已有报告：E2E胸部DR（E2E-P21086）")   # 修前只有「已有报告结论：…」
    expect(intro).to_contain_text("报告结论：E2E 双肺未见异常")
    expect(intro).to_contain_text("本次开单：E2E头颅CT平扫（E2E-P21086）")
    _cancel_modal(page)


def test_申请单行上直接打印本单的报告_不用照申请单号去填报告ID(page, base_url, seed, admin_call):
    """P2-678（第十六批 T1-6）：打印 / 修订史按报告号取，申请单清单原先只给申请单号——两套编号各自递增，后开的单先出报告，
    照申请单号填「报告ID」打出来的是另一张单的报告。修后已出报告的申请单行直接给「打印报告」，打的是这一行的报告。"""
    reqs = {}
    for tag in ("甲", "乙"):
        reqs[tag] = admin_call("POST", "/api/exams", {
            "patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"], "center_type": "imaging",
            "item_code": "E2E-T16", "item_name": f"E2E报告号{tag}"})["id"]
    for tag in ("乙", "甲"):   # 后开的单先出报告
        admin_call("POST", f"/api/exams/{reqs[tag]}/claim")
        admin_call("POST", f"/api/exams/{reqs[tag]}/report", {
            "finding": "", "conclusion": f"E2E 报告号{tag}单的结论", "critical": False, "reported_by": "影像科"})

    _login(page, base_url)
    _open_page(page, "exams", "共享诊断中心")
    with page.expect_popup() as popup:
        page.locator("tr", has_text="E2E报告号甲").locator("button[data-printreport]").click()   # 修前这一行没有这个按钮
    expect(popup.value.locator("body")).to_contain_text("E2E 报告号甲单的结论")
    expect(popup.value.locator("body")).not_to_contain_text("E2E 报告号乙单的结论")


def test_打印先开窗再取数_取数失败先开的空窗关掉_报错照旧(page, base_url):
    """P2-682：打印原先取回打印页之后才 window.open——await 之后已不在点击手势里，Safari 一律拦、Chrome 在打印页生成
    慢时拦。改成点击时先开窗、取回再写；取数失败要把先开的空窗关掉，报错照旧写在页面上（端到端档的 Chromium 不拦弹窗，
    「先开窗」本身由静态闸门 `test_window_open_before_await.py` 钉，这里钉失败分支）。"""
    popups = []
    page.on("popup", lambda p: popups.append(p))
    _login(page, base_url)
    _open_page(page, "exams", "共享诊断中心")
    page.fill("#exam-print-form input[name=report_id]", "987654")
    page.click("#exam-print-form button")
    expect(page.locator("#exam-print-msg")).not_to_have_text("")   # 后端 404 的原话
    for _ in range(50):
        if popups and popups[0].is_closed():
            break
        page.wait_for_timeout(100)
    assert len(popups) == 1 and popups[0].is_closed(), popups   # 修前根本不开窗；修后开了就得关


def test_直播排期审核与直播评价由框自己提交_写超了框不关(page, base_url, admin_read, admin_call):
    """P2-607 第十批：直播「排期」与「评价」原先点确定就关框、再发请求——审核意见写超了（后端 256 字）、评价写超了
    （后端 512 字）报错落在页面消息行，写好的一段随框一起没了。现在框自己提交：失败留框、报错写在框里、填的都在、
    库里不动；改好再交才落库。"""
    pending = admin_call("POST", "/api/education/live-sessions", {"title": "E2E框内提交直播排期", "speaker": "E2E讲者"})
    done = admin_call("POST", "/api/education/live-sessions", {"title": "E2E框内提交直播评价", "speaker": "E2E讲者"})
    admin_call("POST", f"/api/education/live-sessions/{done['id']}/review?approve=true&comment=E2E")
    admin_call("POST", f"/api/education/live-sessions/{done['id']}/finish")

    def session(sid):
        return next(x for x in admin_read("/api/education/live-sessions") if x["id"] == sid)

    _login(page, base_url)
    _open_page(page, "education", "远程医学教育")
    page.click(f'button[data-liveok="{pending["id"]}"]')
    form = _spd_modal_rejected(page, {"comment": "排" * 257})
    expect(form.locator('[name="comment"]')).to_have_value("排" * 257)   # 修前框关，写的意见没了
    assert session(pending["id"])["status"] == "pending"
    _redrawn(page, lambda: _spd_modal(page, {"comment": "E2E 同意排期，周五下午"}))
    assert (session(pending["id"])["status"], session(pending["id"])["review_comment"]) == (
        "approved", "E2E 同意排期，周五下午")

    page.click(f'button[data-livefb="{done["id"]}"]')
    form = _spd_modal_rejected(page, {"rating": "4", "comment": "评" * 513})
    expect(form.locator('[name="comment"]')).to_have_value("评" * 513)
    assert admin_read(f"/api/education/live-sessions/{done['id']}/feedback")["count"] == 0
    _spd_modal(page, {"comment": "E2E 讲得清楚"})
    expect(page.locator("#edu-msg")).to_contain_text("评价已提交")
    (fb,) = admin_read(f"/api/education/live-sessions/{done['id']}/feedback")["feedbacks"]
    assert (fb["rating"], fb["comment"]) == (4, "E2E 讲得清楚"), fb


def test_冷链超温处置由框自己提交_写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第十批：「超温处置」原先点确定就关框、再发请求——处置说明写超了（后端 512 字）报错落在页面消息行，写好的
    处置经过随框一起没了；留空则关框、什么也不发生。现在框自己提交：失败留框、报错写在框里、写的都在、记录不动。"""
    rec = admin_call("POST", "/api/vaccine-supply/cold-chain", {
        "org_id": seed["org"]["id"], "device_name": "E2E框内提交冰箱", "temperature": 12.5,
        "recorded_at": "2026-09-01 10:00:00"})
    assert rec["exceeded"] is True, rec

    def record():
        return next(x for x in admin_read("/api/vaccine-supply/cold-chain?exceeded_only=true") if x["id"] == rec["id"])

    _login(page, base_url)
    _open_page(page, "vaccinesupply", "疫苗批次与冷链")
    page.click(f'button[data-handle="{rec["id"]}"]')
    form = _spd_modal_rejected(page, {"handle_note": "处" * 513})
    expect(form.locator('[name="handle_note"]')).to_have_value("处" * 513)   # 修前框关，写的处置经过没了
    assert record()["handled"] is False
    _redrawn(page, lambda: _spd_modal(page, {"handle_note": "E2E 疫苗已转移至备用冰箱，报修压缩机"}))
    got = record()
    assert (got["handled"], got["handle_note"]) == (True, "E2E 疫苗已转移至备用冰箱，报修压缩机"), got


def test_编辑资源由框自己提交_备注写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第十一批：「编辑资源」原先点确定就关框、再发请求——备注写超了（后端 512 字）报错落在页面消息行，改了的
    位置、备注随框一起没了；五项都留空也是框关了才说。现在框自己提交：两种都在框里说、框不关、填的都在、资源不动。"""
    rs = admin_call("POST", "/api/resources", {"org_id": seed["org"]["id"], "resource_type": "meeting_room",
                                               "code": "E2E-RS-P2607", "name": "E2E框内提交会议室"})

    def saved():
        return next(x for x in admin_read("/api/resources") if x["id"] == rs["id"])

    _login(page, base_url)
    _open_page(page, "resources", "统一资源与排程")
    page.click(f'button[data-rsedit="{rs["id"]}"]')
    # 框里多了「单位」一项（P2-1508），全留空的提示跟着是六项
    form = _spd_modal_rejected(page, {"name": "", "capacity": "", "unit": "", "location": "", "contact": "", "note": ""})
    expect(form.locator("[data-modal-msg]")).to_contain_text("六项都留空了")
    form = _spd_modal_rejected(page, {"name": "E2E框内提交会议室", "location": "E2E三楼东", "note": "备" * 513})
    expect(form.locator('[name="location"]')).to_have_value("E2E三楼东")   # 修前框关，改的位置没了
    assert (saved()["location"], saved()["note"]) == ("", "")
    _redrawn(page, lambda: _spd_modal(page, {"note": "E2E 可容纳 20 人"}))
    assert (saved()["location"], saved()["note"]) == ("E2E三楼东", "E2E 可容纳 20 人"), saved()


def test_编辑资源改得动单位_别的项不动(page, base_url, seed, admin_read, admin_call):
    """P2-1508：后端改档入参原先没有 unit、编辑框里也没有这一项——建档时单位误填成「人」，`PATCH {"unit": "台"}` 照回 200、
    单位还是「人」。现在框里有「单位」，只改单位那一次，别的几项照旧。"""
    rs = admin_call("POST", "/api/resources", {"org_id": seed["org"]["id"], "resource_type": "equipment",
                                               "code": "E2E-RS-P21508", "name": "E2E便携投影仪", "unit": "人",
                                               "location": "E2E设备科"})

    def saved():
        return next(x for x in admin_read("/api/resources") if x["id"] == rs["id"])

    _login(page, base_url)
    _open_page(page, "resources", "统一资源与排程")
    page.click(f'button[data-rsedit="{rs["id"]}"]')
    expect(_modal(page).locator('[name="unit"]')).to_have_value("人")   # 修前框里没有这一项
    _redrawn(page, lambda: _spd_modal(page, {"unit": "台"}))
    assert (saved()["unit"], saved()["name"], saved()["location"]) == ("台", "E2E便携投影仪", "E2E设备科"), saved()
    expect(page.locator(f'tr:has(button[data-rsedit="{rs["id"]}"])')).to_contain_text("1台")   # 登记表的容量列


def test_不良事件审核与整改由框自己提交_写超了框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第十一批：「不良事件审核」「登记整改措施」原先点确定就关框、再发请求——写超了（后端 1024 字）报错落在
    页面消息行，写好的一段随框一起没了；留空则关框、什么也不发生。现在框自己提交：失败留框、报错写在框里、库里不动。"""
    ev = admin_call("POST", "/api/quality/adverse-events", {
        "org_id": seed["org"]["id"], "event_type": "fall", "level": "III", "description": "E2E框内提交：患者如厕跌倒"})

    def event():
        return next(x for x in admin_read("/api/quality/adverse-events") if x["id"] == ev["id"])

    _login(page, base_url)
    _open_page(page, "quality", "质量安全")
    page.click(f'button[data-review="{ev["id"]}"]')
    form = _spd_modal_rejected(page, {"note": "审" * 1025})
    expect(form.locator('[name="note"]')).to_have_value("审" * 1025)   # 修前框关，写的意见没了
    assert event()["status"] == "reported"
    _redrawn(page, lambda: _spd_modal(page, {"note": "E2E 属实，护理组牵头整改"}))
    assert (event()["status"], event()["review_note"]) == ("reviewed", "E2E 属实，护理组牵头整改")

    page.click(f'button[data-rectify="{ev["id"]}"]')
    form = _spd_modal_rejected(page, {"note": "整" * 1025})
    expect(form.locator('[name="note"]')).to_have_value("整" * 1025)
    assert event()["status"] == "reviewed"
    _redrawn(page, lambda: _spd_modal(page, {"note": "E2E 卫生间加装扶手与防滑垫"}))
    assert (event()["status"], event()["rectify_note"]) == ("rectified", "E2E 卫生间加装扶手与防滑垫")


def test_编辑慢病病种由框自己提交_分级规则JSON写错框不关(page, base_url, admin_read, admin_call):
    """P2-607 第七批：编辑病种原先点确定就关框，分级规则 JSON 写错一个括号，报错落在页面消息行，改了一半的规则与指导要点
    全丢。现在框自己提交：JSON 解析不了、写超了都在框里说、框不关；改好再交才落库。"""
    admin_call("POST", "/api/chronic/disease-types", {"code": "e2e_p2607", "name": "E2E 框内提交病种",
                                                      "followup_interval_days": 90})

    def saved():
        return next(t for t in admin_read("/api/chronic/disease-types") if t["code"] == "e2e_p2607")

    _login(page, base_url)
    _open_page(page, "chronic", "慢病管理")
    page.click(f'button[data-typeedit="{saved()["id"]}"]')
    form = _spd_modal_rejected(page, {"guidance": "E2E 每日监测血压", "level_rules": "{坏的 JSON"})
    expect(form.locator("[data-modal-msg]")).to_contain_text("分级规则 JSON 解析失败")
    expect(form.locator('[name="guidance"]')).to_have_value("E2E 每日监测血压")   # 修前框关，写的指导要点也没了
    assert saved()["guidance"] == ""
    _redrawn(page, lambda: _spd_modal(page, {"level_rules": "{}"}))
    assert saved()["guidance"] == "E2E 每日监测血压"


def test_同意书模板编辑由框自己提交_版本号写超了框不关(page, base_url, admin_read, admin_call):
    """P2-607 第八批：门急诊文书的「编辑模板」原先点确定就关框、再发请求——版本号写超了（后端 16 字）报错落在页面消息行，
    改了一半的同意书正文全丢。现在框自己提交：失败留框、报错写在框里、正文还在、模板不动；改好再交才落库。"""
    tpl = admin_call("POST", "/api/outpatient/consent-templates", {
        "consent_type": "treatment", "title": "E2E 框内提交同意书", "version": "e2e-p2607-1", "body": "E2E 原正文"})

    def saved():
        return next(t for t in admin_read("/api/outpatient/consent-templates") if t["id"] == tpl["id"])

    _login(page, base_url)
    _open_page(page, "outpatientdocs", "门急诊文书")
    page.click(f'button[data-tpledit="{tpl["id"]}"]')
    form = _spd_modal_rejected(page, {"version": "v" * 17, "body": "E2E 改过的正文：治疗期间可能出现不适"})
    expect(form.locator('[name="body"]')).to_have_value("E2E 改过的正文：治疗期间可能出现不适")   # 修前框关，正文全丢
    assert (saved()["version"], saved()["body"]) == ("e2e-p2607-1", "E2E 原正文")
    _redrawn(page, lambda: _spd_modal(page, {"version": "e2e-p2607-2"}))
    assert (saved()["version"], saved()["body"]) == ("e2e-p2607-2", "E2E 改过的正文：治疗期间可能出现不适")


def test_专病目录编辑与记录路径节点由框自己提交_写错框不关(page, base_url, seed, admin_read, admin_call):
    """P2-607 第八批：专病管理的「编辑」与「记录节点」原先点确定就关框——路径节点 JSON 少个括号、节点键重复、完成日期写错，
    报错落在页面消息行，改了一半的节点 JSON 与备注全丢。现在框自己提交：写错写超都在框里说、框不关；改好再交才落库。"""
    program = admin_call("POST", "/api/disease-programs", {
        "code": "E2E_P2607_DP", "name": "E2E 框内提交专病",
        "path_nodes": [{"key": "apply", "name": "申请"}, {"key": "review", "name": "评估"}]})
    patient = admin_call("POST", "/api/patients", {"name": "E2E专病节点患者", "id_card": "320981199406062299"})
    enrollment = admin_call("POST", f"/api/disease-programs/{program['id']}/enrollments", {
        "patient_id": patient["id"], "org_id": seed["org"]["id"]})

    def saved():
        return next(p for p in admin_read("/api/disease-programs") if p["id"] == program["id"])

    _login(page, base_url)
    _open_page(page, "diseaseprograms", "专病管理")
    page.click(f'button[data-dpedit="{program["id"]}"]')
    form = _spd_modal_rejected(page, {"description": "E2E 新说明", "path_nodes": "[{坏的"})
    expect(form.locator("[data-modal-msg]")).to_contain_text("路径节点 JSON 解析失败")
    expect(form.locator('[name="description"]')).to_have_value("E2E 新说明")   # 修前框关，说明也没了
    form.locator('[name="path_nodes"]').fill('[{"key":"apply","name":"申请"},{"key":"apply","name":"重复"}]')
    form.locator("button[type=submit]").click()
    expect(form.locator("[data-modal-msg]")).to_contain_text("路径节点 key 不得重复")
    assert saved()["description"] == ""
    _redrawn(page, lambda: _spd_modal(page, {
        "path_nodes": '[{"key":"apply","name":"申请"},{"key":"review","name":"评估"},{"key":"close","name":"结案"}]'}))
    assert (saved()["description"], [n["key"] for n in saved()["path_nodes"]]) == ("E2E 新说明", ["apply", "review", "close"])

    _redrawn(page, lambda: page.click(f'button[data-dppick="{program["id"]}"]'))
    page.click(f'button[data-dpnode="{enrollment["id"]}"]')
    form = _spd_modal_rejected(page, {"node_key": "review", "performed_at": "2026-13-45", "note": "E2E 评估完成"})
    expect(form.locator('[name="note"]')).to_have_value("E2E 评估完成")
    assert admin_read(f"/api/disease-programs/enrollments/{enrollment['id']}")["records"] == []
    _redrawn(page, lambda: _spd_modal(page, {"performed_at": ""}))
    (rec,) = admin_read(f"/api/disease-programs/enrollments/{enrollment['id']}")["records"]
    assert (rec["node_key"], rec["note"]) == ("review", "E2E 评估完成"), rec


def test_随访方案在界面上新建与改诊断关键词_自动匹配据此排随访(page, base_url, seed, admin_read, admin_call):
    """P2-92：随访方案面板原先没有新建入口、编辑不给诊断关键词，三套预置方案又都没配关键词——出院即派生随访与「按患者
    特征自动匹配」从界面上一个人都匹配不到；没有可用方案时回执照样弹「扫描 undefined 人」，原因被吞掉。"""
    from datetime import date, datetime, timedelta, timezone

    _login(page, base_url)
    _open_page(page, "spdfollowup", "智能随访服务端")
    match = page.locator("#spd-fumatch-form")
    match.locator('[name="scene"]').select_option("checkup")   # 体检场景没有配了关键词的方案
    match.locator('[name="org_id"]').fill(str(seed["org"]["id"]))
    match.locator("button").click()
    expect(page.locator("#spd-fu-msg")).to_contain_text("没有配置了诊断关键词的可用方案")   # 修前弹「扫描 undefined 人」

    form = page.locator("#spd-furule-form")
    form.locator('[name="code"]').fill("E2E_R092")
    form.locator('[name="name"]').fill("E2E 门诊关键词随访")
    form.locator('[name="scene"]').select_option("outpatient")
    form.locator('[name="diagnosis_keywords"]').fill("E2E随访病甲，E2E随访病乙")
    form.locator('[name="points"]').fill("7,30")
    _submit(page, "#spd-furule-form button")
    rule = next(r for r in admin_read("/api/spd/followup-rules") if r["code"] == "E2E_R092")
    assert (rule["scene"], rule["diagnosis_keywords"], rule["points"]) == (
        "outpatient", ["E2E随访病甲", "E2E随访病乙"], [7, 30]), rule
    expect(page.locator(f'button[data-rule-edit="{rule["id"]}"]').locator("xpath=ancestor::tr")).to_contain_text(
        "E2E随访病甲、E2E随访病乙")

    admin_call("POST", "/api/encounters", {"patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"],
                                           "encounter_type": "outpatient", "diagnosis_name": "E2E随访病乙（复诊）"})
    match = page.locator("#spd-fumatch-form")
    match.locator('[name="scene"]').select_option("outpatient")
    match.locator('[name="org_id"]').fill(str(seed["org"]["id"]))
    seen = []

    def on_dialog(dialog):
        seen.append(dialog.message)
        dialog.accept()

    page.once("dialog", on_dialog)
    _submit(page, "#spd-fumatch-form button")
    assert seen and "生成随访任务 2 条" in seen[0], seen
    planned = sorted(r["planned_at"] for r in admin_read(f"/api/spd/followup-records?patient_id={seed['patient']['id']}")
                     if r["rule_id"] == rule["id"])
    base = date.fromisoformat(planned[0]) - timedelta(days=7)
    today = datetime.now(timezone.utc).date()
    assert base in (today, today - timedelta(days=1)) and planned == [
        (base + timedelta(days=7)).isoformat(), (base + timedelta(days=30)).isoformat()], planned

    page.click(f'button[data-rule-edit="{rule["id"]}"]')
    expect(_modal(page).locator('[name="diagnosis_keywords"]')).to_have_value("E2E随访病甲，E2E随访病乙")
    _redrawn(page, lambda: _spd_modal(page, {"diagnosis_keywords": "E2E随访病丙"}))
    rule = next(r for r in admin_read("/api/spd/followup-rules") if r["code"] == "E2E_R092")
    assert (rule["diagnosis_keywords"], rule["points"]) == (["E2E随访病丙"], [7, 30]), rule


def test_新建路径模板按病种号挂病种_病种号不连续也不挂错(page, base_url, admin_read, admin_call):
    """P2-96：目录接口的病种原先不带病种号，路径模板表单的病种下拉拿「第几个 + 1」冒充。生产库（PostgreSQL）的序列被一次
    失败的插入吃掉一个号（新建病种编码重复 409），此后新建的病种号就比「第几个 + 1」大：选空档后的第二个病种，模板挂到
    空档后的第一个病种上。这里把甲的号往后挪一位，造出这个空档。"""
    import sqlite3

    first = admin_call("POST", "/api/spd/programs", {"code": "e2e_p96a", "name": "E2E病种甲", "category": "specialty"})
    with sqlite3.connect(E2E_DB) as conn:
        conn.execute("UPDATE spd_programs SET id = id + 1 WHERE id = ?", (first["id"],))
    second = admin_call("POST", "/api/spd/programs", {"code": "e2e_p96b", "name": "E2E病种乙", "category": "specialty"})
    assert second["id"] == first["id"] + 2, (first, second)

    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    form = page.locator("#spd-tpl-form")
    form.locator('[name="program_id"]').select_option(label="E2E病种乙")
    form.locator('[name="code"]').fill("E2E_TPL96")
    form.locator('[name="name"]').fill("E2E病种乙路径")
    _submit(page, "#spd-tpl-form button")
    template = next(t for t in admin_read("/api/spd/path-templates?limit=100") if t["code"] == "E2E_TPL96")
    assert template["program_id"] == second["id"], (template, first, second)   # 修前挂到甲（first + 1）


def test_筛查登记的量表随病种联动_不列别的病种的量表(page, base_url):
    """P2-98：量表下拉原先列全部病种的筛查量表、与病种下拉不联动——病种选高血压、量表点到糖尿病问卷，按糖尿病问卷的分数
    判高血压疑似。现在只列这个病种的与通用的（后端对不上的 422）。"""
    _login(page, base_url)
    _open_page(page, "spdpatients", "筛查建档与纳管")
    form = page.locator("#spd-screen-form")
    scales = form.locator('[name="scale_code"] option')
    form.locator('[name="program_code"]').select_option("hypertension")
    expect(scales.filter(has_text="高血压高危筛查问卷")).to_have_count(1)
    expect(scales.filter(has_text="糖尿病高危筛查问卷")).to_have_count(0)
    form.locator('[name="program_code"]').select_option("diabetes")
    expect(scales.filter(has_text="糖尿病高危筛查问卷")).to_have_count(1)
    expect(scales.filter(has_text="高血压高危筛查问卷")).to_have_count(0)


@pytest.fixture(scope="session")
def scale136(admin_call):
    """同一编码两版都发布：v2 比 v1 多两道题——筛查与评估都按编码取**最新发布**的那版评分。"""
    yes_no = lambda yes: [{"label": "是", "score": yes}, {"label": "否", "score": 0}]   # noqa: E731
    salt = {"key": "salt", "title": "口味偏咸", "type": "single", "options": yes_no(2)}
    ranges = {"ranges": [{"min": 0, "max": 2, "risk": "low"}, {"min": 3, "max": None, "risk": "high"}]}
    versions = {}
    for version, items in (("v1", [salt]), ("v2", [
            salt, {"key": "family", "title": "直系亲属有高血压（v2 新增）", "type": "single", "options": yes_no(3)},
            {"key": "smoke", "title": "吸烟", "type": "single", "options": yes_no(1)}])):
        scale = admin_call("POST", "/api/spd/scales", {
            "code": "E2E_SCR136", "version": version, "name": f"E2E高血压自评{version}", "category": "screen",
            "program_code": "hypertension", "items": items, "scoring": ranges})
        admin_call("POST", f"/api/spd/scales/{scale['id']}/publish")
        versions[version] = scale["id"]
    return versions


def test_筛查登记选了量表要逐题作答_同一量表只列最新发布的一版(page, base_url, admin_call, admin_read, scale136):
    """P1-136：筛查登记的表单原先只送量表编码、不送作答——量表评分恒 0、风险恒「低危」，「量表评分与病种规则双通道判定」
    里量表这一条从界面上判不出任何人。现在选了量表先逐题作答（默认「（未答）」，没答的题不交）。下拉原先把同一编码
    已发布的两版都列出来，而后端按编码取最新发布的那版评分——选了旧版就是按旧版的题作答、按新版评分。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E筛查作答", "id_card": "320981199306060136"})
    _login(page, base_url)
    _open_page(page, "spdpatients", "筛查建档与纳管")
    form = page.locator("#spd-screen-form")
    form.locator('[name="patient_id"]').fill(str(patient["id"]))
    form.locator('[name="program_code"]').select_option("hypertension")
    expect(form.locator('[name="scale_code"] option[value="E2E_SCR136"]')).to_have_count(1)   # 修前两版各一项
    form.locator('[name="scale_code"]').select_option("E2E_SCR136")
    form.locator("button").click()
    expect(_modal(page)).to_contain_text("直系亲属有高血压（v2 新增）")   # 最新发布的那版的题目
    expect(_modal(page).locator('[name="q_smoke"]')).to_have_value("")   # 默认「（未答）」
    _redrawn(page, lambda: _spd_modal(page, {"q_salt": "是", "q_family": "是"}))
    screening = admin_read(f"/api/spd/screenings?patient_id={patient['id']}")[0]
    assert screening["answers"] == {"salt": "是", "family": "是"}, screening   # 修前 {}：没答的「吸烟」不交
    assert (screening["score"], screening["risk_level"], screening["result"]) == (5, "high", "suspect"), screening


def test_量表评估的单选默认未答_没答的题不交(page, base_url, seed, admin_call, admin_read, scale136):
    """P1-136：评估的逐题作答原先单选默认选中第一个选项、数值题空着读成 0——没答的题被当成答了「是」（种子量表里分值高的
    那个），评估结论回写档案风险等级，高危还自动派干预与复诊。现在与执行随访同一套写法。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E评估作答", "id_card": "320981199307070136"})
    admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    _login(page, base_url)
    _open_page(page, "spdmember", "服务团队成员端·日常服务")
    form = page.locator("#spd-assess-form")
    form.locator('[name="patient_id"]').fill(str(patient["id"]))
    expect(form.locator(f'[name="scale_id"] option[value="{scale136["v1"]}"]')).to_have_count(0)   # 旧版不列
    form.locator('[name="scale_id"]').select_option(str(scale136["v2"]))
    form.locator("button").click()
    expect(_modal(page).locator('[name="q_family"]')).to_have_value("")   # 修前默认「是」
    _spd_modal(page, {"q_salt": "否", "q_smoke": "是"})
    expect(page.locator("#spd-assess-msg")).to_contain_text("评估完成：1 分")   # 修前没答的「家族史」按「是」计，4 分高危
    assessment = admin_read(f"/api/spd/assessments?patient_id={patient['id']}")[0]
    assert assessment["answers"] == {"salt": "否", "smoke": "是"}, assessment


def test_筛查与出报告由框自己提交_被拒时作答与所见都在(page, base_url, seed, admin_call, admin_read, scale136):
    """P2-1091（第三十一批 C3-3）：筛查逐题作答、出报告原先点确定就关框、再发请求——患者号敲错被拒、别人已出过报告
    409，一整套作答 / 一整段所见随框一起没了（执行随访的逐题作答早按 P2-607 改成框内提交）。现在框自己提交：失败留框、
    报错写在框里、填的都在。"""
    _login(page, base_url)
    _open_page(page, "spdpatients", "筛查建档与纳管")
    form = page.locator("#spd-screen-form")
    form.locator('[name="patient_id"]').fill("987654321")   # 敲错的患者号
    form.locator('[name="program_code"]').select_option("hypertension")
    form.locator('[name="scale_code"]').select_option("E2E_SCR136")
    form.locator("button").click()
    modal = _spd_modal_rejected(page, {"q_salt": "是", "q_family": "是"})   # 修前框先关、作答丢了
    expect(modal.locator('[name="q_family"]')).to_have_value("是")
    _cancel_modal(page)

    req = admin_call("POST", "/api/exams", {"patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"],
                                            "center_type": "imaging", "item_code": "E2E-P21091", "item_name": "E2E框内出报告"})
    admin_call("POST", f"/api/exams/{req['id']}/claim")
    _open_page(page, "exams", "共享诊断中心")
    page.click(f'button[data-report="{req["id"]}"]')
    admin_call("POST", f"/api/exams/{req['id']}/report", {   # 框开着的时候别人先出了报告
        "finding": "", "conclusion": "E2E 别人先出的报告", "critical": False, "reported_by": "影像科"})
    finding = "双肺纹理清晰\n心影不大"
    modal = _spd_modal_rejected(page, {"conclusion": "E2E 我写的结论", "finding": finding})   # 修前框先关、所见丢了
    expect(modal.locator('[name="finding"]')).to_have_value(finding)
    _cancel_modal(page)
    (row,) = [r for r in admin_read(f"/api/exams?patient_id={seed['patient']['id']}") if r["id"] == req["id"]]
    assert row["status"] == "reported"


def test_干预模板与服务包的下拉按病种联动(page, base_url, seed, admin_call):
    """P2-99：干预下发的模板下拉、绑服务包的弹窗原先列全部病种的——高血压患者下发到糖尿病的干预、绑上糖尿病的服务包。
    现在只列这个病种的与通用的（后端对不上的 422）。"""
    for program in ("hypertension", "diabetes"):
        admin_call("POST", "/api/spd/intervention-templates", {
            "code": f"E2E_T99_{program}", "name": f"E2E干预模板·{program}", "program_code": program})
        admin_call("POST", "/api/spd/service-packages", {
            "code": f"E2E_K99_{program}", "name": f"E2E服务包·{program}", "program_code": program,
            "items": [{"code": "visit", "name": "上门", "times": 1}]})
    patient = admin_call("POST", "/api/patients", {"name": "E2E联动患者", "id_card": "320981199202020299"})
    enrollment = admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})

    _login(page, base_url)
    _open_page(page, "spdmember", "服务团队成员端·日常服务")
    form = page.locator("#spd-intv-form")
    templates = form.locator('[name="template_id"] option')
    form.locator('[name="program_code"]').select_option("hypertension")
    expect(templates.filter(has_text="E2E干预模板·hypertension")).to_have_count(1)
    expect(templates.filter(has_text="E2E干预模板·diabetes")).to_have_count(0)   # 修前列着

    _open_page(page, "spdpatients", "筛查建档与纳管")
    search = page.locator("#spd-enroll-filter")
    search.locator('[name="keyword"]').fill("E2E联动患者")
    search.locator("button").click()
    page.click(f'button[data-enr-detail="{enrollment["id"]}"]')
    page.click(f'button[data-enr-bind="{enrollment["id"]}"]')
    packages = _modal(page).locator('[name="package_id"] option')
    expect(packages.filter(has_text="E2E服务包·hypertension")).to_have_count(1)
    expect(packages.filter(has_text="E2E服务包·diabetes")).to_have_count(0)   # 修前列着
    _cancel_modal(page)


def test_界面上报异常挂上纳管档案_病种取上报任务的(page, base_url, seed, admin_call, admin_read):
    """P2-100：上报表单原先不带病种、接口也不看上报任务配的病种——从界面上报的每一条都取不到纳管档案，村医的「异常上报」
    积分一分不入账、派生的处置任务不挂档案。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E上报患者", "id_card": "320981199303030399"})
    enrollment = admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    admin_call("POST", "/api/spd/case-report-tasks", {"code": "E2E_CRT100", "name": "E2E高血压异常上报",
                                                       "program_code": "hypertension"})
    _login(page, base_url)
    _open_page(page, "spdmember", "服务团队成员端·日常服务")
    form = page.locator("#spd-report-form")
    form.locator('[name="patient_id"]').fill(str(patient["id"]))
    form.locator('[name="task_id"]').select_option(label="E2E高血压异常上报")
    form.locator('[name="content"]').fill("E2E 血压 188/112")
    _submit(page, "#spd-report-form button")   # 病种留空：取上报任务的
    tasks = admin_read(f"/api/spd/tasks?patient_id={patient['id']}&limit=50")
    spawned = next(t for t in tasks if t["title"].startswith("异常上报处置：E2E 血压"))
    assert (spawned["program_code"], spawned["enrollment_id"]) == ("hypertension", enrollment["id"]), spawned


def test_运行中枢能新建宣教素材与接入数据源(page, base_url, admin_read):
    """P2-93（动词级孤儿）：宣教素材、数据源两块原先只有「编辑」——建的接口一直在，界面上没有入口，新的素材与新接入的
    院内系统只能靠接口调用方登记。"""
    _login(page, base_url)
    _open_page(page, "spdadmin", "平台管理端·运行中枢")
    edu = page.locator("#spd-edumat-form")
    edu.locator('[name="code"]').fill("E2E_EDU93")
    edu.locator('[name="title"]').fill("E2E 限盐小讲堂")
    edu.locator('[name="program_code"]').select_option("hypertension")
    edu.locator('[name="content"]').fill("每日食盐不超过 5 克")
    _submit(page, "#spd-edumat-form button")
    material = next(m for m in admin_read("/api/spd/edu-materials?limit=100") if m["code"] == "E2E_EDU93")
    assert (material["title"], material["program_code"], material["media_type"]) == (
        "E2E 限盐小讲堂", "hypertension", "text"), material

    ds = page.locator("#spd-ds-form")
    ds.locator('[name="code"]').fill("E2E_DS93")
    ds.locator('[name="name"]').fill("E2E 检验系统")
    ds.locator('[name="source_type"]').select_option("LIS")
    ds.locator('[name="freq_minutes"]').fill("30")
    _submit(page, "#spd-ds-form button")
    source = next(s for s in admin_read("/api/spd/data-sources") if s["code"] == "E2E_DS93")
    assert (source["name"], source["source_type"], source["freq_minutes"]) == ("E2E 检验系统", "LIS", 30), source


def test_运行中枢建服务包_次数写错当场报出_不再静默改成1次(page, base_url, admin_read):
    """P2-587：项目「编码:名称:次数」原先 `Number(times) || 1`——「12次」、0、用逗号连着写的下一项都静默变成 1 次，
    建出来的包第二次扣减就「剩余次数不足」，编辑框又没有项目栏改不回来。"""
    _login(page, base_url)
    _open_page(page, "spdadmin", "平台管理端·运行中枢")
    form = page.locator("#spd-package-form")
    form.locator('[name="code"]').fill("E2E_PKG587")
    form.locator('[name="name"]').fill("E2E 高血压包")
    form.locator('[name="items"]').fill("BP:血压测量:12次")
    form.locator("button").click()
    expect(page.locator("#spd-package-msg")).to_contain_text("次数须为正整数")
    form.locator('[name="items"]').fill("BP:血压测量:12，GLU:血糖检测:4")
    form.locator("button").click()
    expect(page.locator("#spd-package-msg")).to_contain_text("多于三段")
    assert not any(k["code"] == "E2E_PKG587" for k in admin_read("/api/spd/service-packages")), "报错了却照样建了"

    form.locator('[name="items"]').fill("BP:血压测量:12；GLU:血糖检测")
    _submit(page, "#spd-package-form button")
    package = next(k for k in admin_read("/api/spd/service-packages") if k["code"] == "E2E_PKG587")
    assert [(i["code"], i["name"], i["times"]) for i in package["items"]] == [("BP", "血压测量", 12), ("GLU", "血糖检测", 1)]


def test_运行中枢改已有病种的纳入规则_保存即升一版(page, base_url, admin_call, admin_read):
    """P2-173：接口「改规则即升版本并留快照」，页面原先没有入口——编辑对话框叫人「用下方编辑器新建版本」，
    下方那两个编辑器挂在「新建病种」表单上：同编码提交 409，换个编码就多出一个病种。"""
    prog = admin_call("POST", "/api/spd/programs", {"code": "E2E_P2173", "name": "E2E 规则升版病种", "category": "chronic"})
    _login(page, base_url)
    _open_page(page, "spdadmin", "平台管理端·运行中枢")
    page.click(f'button[data-prog-rules="{prog["id"]}"]')
    include = page.locator("#spd-edit-include")
    include.locator("button.rule-add").click()
    row = include.locator(".spd-rule-row").first
    row.locator("select.rule-field").select_option("age")
    row.locator("select.rule-op").select_option(">=")
    row.locator("input.rule-value").fill("35")
    page.locator('#spd-rules-form input[name="note"]').fill("纳入年龄下限 35 岁")
    _submit(page, "#spd-rules-form button")

    saved = admin_read(f"/api/spd/programs/{prog['id']}")
    assert (saved["version"], saved["include_rules"]) == (
        "v2", [{"field": "age", "op": ">=", "value": 35, "label": ""}]), saved
    history = admin_read(f"/api/spd/programs/{prog['id']}/versions")
    assert [(v["version"], v["note"]) for v in history] == [("v1", "纳入年龄下限 35 岁")]   # 修前页面无处可改
    assert not [p for p in admin_read("/api/spd/programs?limit=100") if p["name"] == "E2E 规则升版病种"
                and p["id"] != prog["id"]]   # 没有多出第二个病种


def test_运行中枢编辑与改规则_按点击这一刻的病种预填(page, base_url, admin_call, admin_read):
    """P2-618：「编辑」的说明框原先恒空（看不到原来写的什么、空着不送也清不掉），名称 / 科室与「改规则」的编辑器
    用的是进页面时那份列表——其间别人改过的，这里一保存就改回去。现在点的时候先按接口取这个病种。"""
    prog = admin_call("POST", "/api/spd/programs", {"code": "E2E_P2618", "name": "E2E 预填病种", "category": "chronic",
                                                   "description": "E2E 原说明"})
    _login(page, base_url)
    _open_page(page, "spdadmin", "平台管理端·运行中枢")
    # 进页面之后，别人改了名、改了纳入规则（升到 v2）
    admin_call("PATCH", f"/api/spd/programs/{prog['id']}", {
        "name": "E2E 预填病种（改名）", "include_rules": [{"field": "age", "op": ">=", "value": 60}], "note": "别人改的"})

    page.click(f'button[data-prog-rules="{prog["id"]}"]')
    expect(page.locator("#spd-edit-include .spd-rule-row")).to_have_count(1)   # 修前 0：摆的是进页面时的空规则
    expect(page.locator("#spd-edit-include input.rule-value")).to_have_value("60")
    expect(page.locator("#spd-cfg-detail")).to_contain_text("当前 v2")

    page.click(f'button[data-prog-edit="{prog["id"]}"]')
    modal = _modal(page)
    expect(modal.locator('[name="name"]')).to_have_value("E2E 预填病种（改名）")   # 修前：进页面时的旧名
    expect(modal.locator('[name="description"]')).to_have_value("E2E 原说明")   # 修前：恒空
    # 说明写超了（后端 512 字）：框自己提交，报错写在框里、框不关、写的还在（P2-607 第六批）
    form = _spd_modal_rejected(page, {"description": "说" * 513})
    expect(form.locator('[name="description"]')).to_have_value("说" * 513)
    assert admin_read(f"/api/spd/programs/{prog['id']}")["description"] == "E2E 原说明"
    _redrawn(page, lambda: _spd_modal(page, {"description": ""}))
    saved = admin_read(f"/api/spd/programs/{prog['id']}")
    assert (saved["name"], saved["description"], saved["include_rules"][0]["value"]) == (
        "E2E 预填病种（改名）", "", 60), saved   # 修前：名字被改回旧名、说明清不掉


def test_路径页刚发布的模板_同一页启动路径的下拉里就有(page, base_url, admin_call, admin_read):
    """P2-107：目录（病种 / 团队 / 已发布的量表 / 中心 / 已发布的路径模板）原先首次访问慢专病页面时拉一次、存进全局变量，
    只有运行中枢强制重取——页面之间跳转不重载浏览器，路径页上发布了模板，同一页「启动患者路径」的下拉里没有，
    要刷新整个浏览器才看得到。"""
    hyp = next(p for p in admin_read("/api/spd/programs") if p["code"] == "hypertension")
    tpl = admin_call("POST", "/api/spd/path-templates", {"program_id": hyp["id"], "code": "E2E_FRESH_TPL",
                                                        "name": "E2E刚发布路径"})
    admin_call("POST", f"/api/spd/path-templates/{tpl['id']}/nodes", {"key": "n1", "name": "首诊", "seq": 1})
    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    option = page.locator(f'#spd-inst-form select[name="template_id"] option[value="{tpl["id"]}"]')
    expect(option).to_have_count(0)   # 草稿不列
    _redrawn(page, lambda: page.click(f'button[data-tpl-pub="{tpl["id"]}"]'))
    expect(option).to_have_count(1)   # 修前 0：目录还是进页面时拉的那份


def test_路径模板与推送任务能在界面上删除_用过的删不掉改停用(page, base_url, seed, admin_call, admin_read):
    """P2-93（动词级孤儿）：删路径模板、删报告推送任务原先只有接口——建错了的草稿在界面上删不掉。后端早就挡着用过的
    （有患者走过的路径、生成过报告的任务 409）；路径模板的 409 让人「停用」，页面上原先也没有停用按钮。"""
    hyp = next(p for p in admin_read("/api/spd/programs") if p["code"] == "hypertension")
    draft = admin_call("POST", "/api/spd/path-templates", {"program_id": hyp["id"], "code": "E2E_DEL_TPL",
                                                          "name": "E2E待删路径"})
    used = admin_call("POST", "/api/spd/path-templates", {"program_id": hyp["id"], "code": "E2E_USED_TPL",
                                                         "name": "E2E在用路径"})
    admin_call("POST", f"/api/spd/path-templates/{used['id']}/nodes", {"key": "n1", "name": "首诊", "seq": 1})
    admin_call("POST", f"/api/spd/path-templates/{used['id']}/status", {"status": "published"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E在用路径患者", "id_card": "320981199404040499"})
    enrollment = admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    admin_call("POST", "/api/spd/path-instances", {"enrollment_id": enrollment["id"], "template_id": used["id"]})

    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    page.click(f'button[data-tpl-del="{draft["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert all(t["id"] != draft["id"] for t in admin_read("/api/spd/path-templates?limit=100"))
    # 有患者走过的不给「删除」「加节点」（P2-599：原先照样给，点下去 409「只能停用不能删除」——接口一侧见
    # tests/test_spd_path_template_in_use.py），「停用」照给
    expect(page.locator(f'button[data-tpl-del="{used["id"]}"], button[data-tpl-node="{used["id"]}"]')).to_have_count(0)
    _redrawn(page, lambda: page.click(f'button[data-tpl-off="{used["id"]}"]'))
    assert admin_read(f"/api/spd/path-templates/{used['id']}")["status"] == "disabled"

    template = admin_read("/api/spd/report-templates")[0]
    task = admin_call("POST", "/api/spd/report-tasks", {"code": "E2E_DEL_RPT", "name": "E2E待删推送",
                                                        "template_id": template["id"]})
    _open_page(page, "spdreport", "智能辅助报告端")
    page.click(f'button[data-rpt-del="{task["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert all(t["id"] != task["id"] for t in admin_read("/api/spd/report-tasks"))


def test_报告模板能在界面上新建_段落取自注册表(page, base_url, admin_read):
    """P2-93（动词级孤儿）：报告模板原先只能改名称 / 周期 / 层级 / 启停，新的报告版式只能靠接口调用方。段落勾选项取自段落
    注册表（`/api/spd/meta` 的 `report_sections`），按注册顺序排、标题取段落名；考核指标段落按编码每个一段。"""
    _login(page, base_url)
    _open_page(page, "spdreport", "智能辅助报告端")
    form = page.locator("#spd-rpttpl-form")
    form.locator('[name="code"]').fill("E2E_RPT_TPL")
    form.locator('[name="name"]').fill("E2E 基层月报")
    form.locator('[name="period"]').select_option("monthly")
    form.locator('[name="scope_level"]').select_option("grassroots")
    form.locator('.rpt-sec[value="referral"]').check()   # 先勾后面的：段落顺序按注册顺序，不按勾选先后
    form.locator('.rpt-sec[value="summary"]').check()
    form.locator('[name="indicator_codes"]').fill("followup_rate， enroll_rate")   # 全角逗号、多余空格照认
    _submit(page, "#spd-rpttpl-form button")
    created = next(t for t in admin_read("/api/spd/report-templates") if t["code"] == "E2E_RPT_TPL")
    assert (created["period"], created["scope_level"]) == ("monthly", "grassroots"), created
    assert created["sections"] == [
        {"key": "summary", "title": "总体概览"}, {"key": "referral", "title": "转诊闭环"},
        {"key": "indicator", "indicator_code": "followup_rate", "title": "考核指标 followup_rate"},
        {"key": "indicator", "indicator_code": "enroll_rate", "title": "考核指标 enroll_rate"}], created
    expect(page.locator("tr", has_text="E2E_RPT_TPL")).to_contain_text("总体概览、转诊闭环")


def _scale(admin_read, code, version):
    return next(x for x in admin_read("/api/spd/scales?limit=200") if (x["code"], x["version"]) == (code, version))


def test_评估量表能在界面上新建_编辑草稿_发布后复制为新版本(page, base_url, admin_read):
    """P2-93（动词级孤儿）：评估量表原先只有「发布 / 停用」，建量表、改量表的接口都没有入口——新的筛查 / 评估问卷只能靠
    接口调用方。构建器：题目逐行（选项写成「文字=分值」）、评分分段逐行；「编辑草稿」走 PATCH（编码、版本不可改，题目
    key 原样保留——历史作答按 key 记）；已发布的不能改题目（后端 409），「复制为新版本」以它为底稿建 v2。"""
    _login(page, base_url)
    _open_page(page, "spdadmin", "平台管理端·运行中枢")
    page.click("#spd-scale-new summary")
    form = page.locator("#spd-scale-form")
    form.locator('[name="code"]').fill("E2E_FALL")
    form.locator('[name="name"]').fill("E2E 跌倒风险量表")
    form.locator('[name="category"]').select_option("risk")
    items = form.locator(".scale-item")
    items.nth(0).locator(".i-title").fill("近一年跌倒过")
    items.nth(0).locator(".i-options").fill("是=3 / 否=0")
    items.nth(1).locator(".i-title").fill("行走需要辅助")
    items.nth(1).locator(".i-options").fill("是=2/否=0")
    form.locator(".scale-add-item").click()
    items.nth(2).locator(".i-title").fill("服用镇静安眠药")
    items.nth(2).locator(".i-options").fill("是=两分 / 否=0")
    ranges = form.locator(".scale-range")
    ranges.nth(0).locator(".r-min").fill("0")
    ranges.nth(0).locator(".r-max").fill("2")
    ranges.nth(0).locator(".r-risk").select_option("low")
    ranges.nth(0).locator(".r-advice").fill("常规防跌倒宣教")
    form.locator(".scale-add-range").click()
    ranges.nth(1).locator(".r-min").fill("3")
    ranges.nth(1).locator(".r-risk").select_option("high")
    ranges.nth(1).locator(".r-advice").fill("转康复评估")
    form.locator("button.scale-save").click()
    # 分值写成文字：前端先拦（原样送出去是 NaN → null，后端按 0 分收下）
    expect(page.locator("#spd-scale-msg")).to_contain_text("分值要写成数字")
    items.nth(2).locator(".i-options").fill("是=1 / 否=0")
    _submit(page, "#spd-scale-form button.scale-save")
    created = _scale(admin_read, "E2E_FALL", "v1")
    assert created["status"] == "draft" and created["program_code"] == "", created
    assert [(i["key"], i["title"], i["options"]) for i in created["items"]] == [
        ("q1", "近一年跌倒过", [{"label": "是", "score": 3}, {"label": "否", "score": 0}]),
        ("q2", "行走需要辅助", [{"label": "是", "score": 2}, {"label": "否", "score": 0}]),
        ("q3", "服用镇静安眠药", [{"label": "是", "score": 1}, {"label": "否", "score": 0}])], created["items"]
    assert created["scoring"] == {"ranges": [
        {"min": 0, "max": 2, "risk": "low", "advice": "常规防跌倒宣教"},
        {"min": 3, "max": None, "risk": "high", "advice": "转康复评估"}]}, created["scoring"]

    # 编辑草稿：PATCH，编码与版本锁着，题目 key 原样
    page.click(f'button[data-scale-edit="{created["id"]}"]')   # 重画后折叠着：点「编辑草稿」自己展开
    expect(form.locator('[name="code"]')).to_be_disabled()
    form.locator('[name="name"]').fill("E2E 跌倒风险量表（修订）")
    form.locator(".scale-item").nth(0).locator(".i-options").fill("是=4 / 否=0")
    _submit(page, "#spd-scale-form button.scale-save")
    edited = _scale(admin_read, "E2E_FALL", "v1")
    assert (edited["id"], edited["name"], edited["status"]) == (created["id"], "E2E 跌倒风险量表（修订）", "draft")
    assert [i["key"] for i in edited["items"]] == ["q1", "q2", "q3"]
    assert edited["items"][0]["options"][0] == {"label": "是", "score": 4}

    # 发布后复制为新版本：v2 草稿，题目与分段照抄
    _redrawn(page, lambda: page.click(f'button[data-scale-pub="{created["id"]}"]'))
    assert _scale(admin_read, "E2E_FALL", "v1")["status"] == "published"
    page.click(f'button[data-scale-copy="{created["id"]}"]')
    expect(form.locator('[name="version"]')).to_have_value("v2")
    _submit(page, "#spd-scale-form button.scale-save")
    copied = _scale(admin_read, "E2E_FALL", "v2")
    assert copied["status"] == "draft" and copied["items"] == edited["items"] and copied["scoring"] == edited["scoring"]


def test_数据质控规则能在界面上新增_配置写坏由后端说清楚(page, base_url, admin_read):
    """P2-93（动词级孤儿）：规则库原先只能启停、切严重度，新增规则的接口（管理员）没有入口——种子 docstring 写着「落地时应由
    医共体质控办……经 /api/dataquality/rules 增删调整」。配置按类型给示例；写坏的由后端逐项说清楚（P2-81），不落库。"""
    _login(page, base_url)
    _open_page(page, "dataquality", "数据质控")
    form = page.locator("#qc-rule-form")
    form.locator('[name="code"]').fill("E2EQC1")
    form.locator('[name="name"]').fill("E2E 患者联系电话宜填写")
    form.locator('[name="target_table"]').fill("patients")
    form.locator('[name="rule_type"]').select_option("range")
    assert '"min"' in form.locator('[name="config"]').get_attribute("placeholder")   # 示例随类型换
    form.locator('[name="rule_type"]').select_option("required")
    form.locator('[name="config"]').fill('{"field": "mobile"}')   # 字段名写错
    form.locator("button").click()
    expect(page.locator("#qc-rule-msg")).to_contain_text("不是 patients 的字段")
    assert all(r["code"] != "E2EQC1" for r in admin_read("/api/dataquality/rules"))
    form.locator('[name="config"]').fill('{"field": "phone"}')
    form.locator('[name="severity"]').select_option("warn")
    _submit(page, "#qc-rule-form button")
    created = next(r for r in admin_read("/api/dataquality/rules") if r["code"] == "E2EQC1")
    assert (created["target_table"], created["rule_type"], created["config"], created["severity"]) == (
        "patients", "required", {"field": "phone"}, "warn"), created
    # 启用即参与扫描：规则汇总与规则库各一行，看规则库那行（带启停按钮）
    expect(page.locator("tr:has(button[data-qctoggle])", has_text="E2EQC1")).to_contain_text("必填项")
    # P2-564：自建的能删（先确认），内置的只能停用、没有删除按钮
    expect(page.locator("tr:has(button[data-qctoggle])", has_text="QC001").locator("button[data-qcdel]")).to_have_count(0)
    with _answers(page, [""]):   # 原生 confirm：点确定
        page.locator("tr:has(button[data-qctoggle])", has_text="E2EQC1").locator("button[data-qcdel]").click()
        expect(page.locator("tr:has(button[data-qctoggle])", has_text="E2EQC1")).to_have_count(0)
    assert all(r["code"] != "E2EQC1" for r in admin_read("/api/dataquality/rules"))


def test_消毒供应成本项按批次看得见(page, base_url, admin_call):
    """P2-495：成本核算原先只看得到按批次的合计与构成，逐条登记的成本项录进去就没处核对。"""
    org = admin_call("POST", "/api/organizations", {"name": "E2E供应中心", "org_type": "lead_hospital", "level": "county"})
    batch = admin_call("POST", "/api/cssd/batches", {"batch_no": "E2E-CSSD-1", "center_org_id": org["id"],
                                                      "item_name": "E2E换药包", "quantity": 10})
    admin_call("POST", "/api/cssd/cost-items", {"batch_id": batch["id"], "cost_type": "labor", "amount": 40,
                                                 "note": "E2E 两人打包"})

    _login(page, base_url)
    _open_page(page, "cssd", "消毒供应")
    page.click(f'button[data-costitems="{batch["id"]}"]')   # 修前批次表没有这一格
    items = page.locator("#cost-items")
    expect(items).to_contain_text("成本项（1 项）")
    expect(items.locator("tr", has_text="E2E 两人打包")).to_contain_text("人工")


def test_消毒供应发放不选接收机构不发(page, base_url, admin_call, admin_read):
    """P2-1443（第四十二批扫描 AF4-2）：「发放」弹窗的接收机构原先没有空项，不动下拉点确定就发给机构表第一家（多半是中心
    自己），发放撤不回。修后首项是「请选择接收机构」：不选就点确定，提示写在框里、框不关、批次不动；选了才发。"""
    center = admin_call("POST", "/api/organizations", {"name": "E2E供应中心P21443", "org_type": "lead_hospital",
                                                        "level": "county"})
    town = admin_call("POST", "/api/organizations", {"name": "E2E接收卫生院P21443", "org_type": "township",
                                                      "level": "township"})
    batch = admin_call("POST", "/api/cssd/batches", {"batch_no": "E2E-P21443", "center_org_id": center["id"],
                                                      "item_name": "E2E缝合包", "quantity": 5})
    admin_call("POST", f"/api/cssd/batches/{batch['id']}/advance")   # 灭菌中 → 已灭菌，待发放

    _login(page, base_url)
    _open_page(page, "cssd", "消毒供应")
    page.click(f'button[data-adv="{batch["id"]}"]')
    form = page.locator("form.panel:has(button[data-cancel])")
    picker = form.locator('select[name="dispatched_to_org_id"]')
    expect(picker).to_have_value("")   # 修前落在机构表第一家
    expect(picker.locator("option").first).to_have_text("请选择接收机构")
    form.locator("button[type=submit]").click()
    expect(form.locator("[data-modal-msg]")).to_have_text("请选择接收机构")
    expect(form).to_be_visible()
    (row,) = admin_read("/api/cssd/batches?batch_no=E2E-P21443")
    assert row["status"] == "sterile"   # 没发出去
    _redrawn(page, lambda: _spd_modal(page, {"dispatched_to_org_id": str(town["id"])}))
    (row,) = admin_read("/api/cssd/batches?batch_no=E2E-P21443")
    assert (row["status"], row["dispatched_to_org_id"]) == ("dispatched", town["id"])


def test_计费明细能按住院单查_未结清的看得见(page, base_url, admin_call):
    """P2-493：「计费与结算」原先只能计、不能查——结算前看不到这次住院挂着哪些未结清的明细，计错了也无从发现。"""
    org = admin_call("POST", "/api/organizations", {"name": "E2E计费县医院", "org_type": "lead_hospital", "level": "county"})
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": "E2E计费病区"})
    bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "E2E-B1"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E计费患者", "id_card": "33010619650505247X", "gender": "男"})
    adm = admin_call("POST", "/api/inpatient/admissions",
                     {"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "肺炎"})
    admin_call("POST", "/api/billing/charge-items", {"code": "E2E-BD-1", "name": "E2E雾化吸入", "category": "treatment",
                                                     "price": 12.5})
    for qty in (2, 1):
        admin_call("POST", "/api/billing/details", {"patient_id": patient["id"], "admission_id": adm["id"],
                                                    "item_code": "E2E-BD-1", "quantity": qty})

    _login(page, base_url)
    _open_page(page, "billing", "费用结算")
    form = page.locator("#bd-query")
    form.locator("button").click()
    # 提示写在明细面板里（P2-1010）：原先写进上方「收费项目目录」面板的消息行
    expect(page.locator("#bd-list")).to_contain_text("查明细请填患者ID、住院单ID 或就诊ID 之一")
    form.locator('[name="admission_id"]').fill(str(adm["id"]))
    form.locator("button").click()   # 缺省只看未结清
    listing = page.locator("#bd-list")
    expect(listing).to_contain_text("共 2 条，合计 37.50 元")   # 修前页面上没有明细
    expect(listing.locator("tr", has_text="E2E雾化吸入").first).to_contain_text("未结清")


def test_会诊计费不填金额提交不了_明确填0照常计费(page, base_url, seed, admin_call, admin_read):
    """会诊计费的费用框原先能留空：spdModal 的数字框把空值读成 0，这单照样标成「已计费」、计 0 元——弹窗标签自己写着
    「0 与未计费是两回事」（本院内部会诊常计 0 元，由 `fee_settled` 区分，不拿 0 当哨兵）。现在必填，要计 0 元就明确填 0。"""
    other = admin_call("POST", "/api/organizations", {"name": "E2E会诊受邀院", "org_type": "township", "level": "township"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E会诊计费", "id_card": "320981199308080137"})
    consult = admin_call("POST", "/api/consultations", {
        "patient_id": patient["id"], "from_org_id": seed["org"]["id"], "to_org_id": other["id"], "question": "E2E 计费"})
    admin_call("POST", f"/api/consultations/{consult['id']}/accept", {"expert_name": "E2E专家"})
    admin_call("POST", f"/api/consultations/{consult['id']}/complete", {"opinion": "E2E 会诊意见"})
    settled = lambda: admin_read("/api/consultations/stats")["fee"]["settled_count"]   # noqa: E731
    before = settled()

    _login(page, base_url)
    _open_page(page, "consultations", "远程会诊")
    page.click(f'button[data-act="fee"][data-id="{consult["id"]}"]')
    _modal(page).locator("button[type=submit]").click()   # 空着点确定：浏览器按必填拦下，弹窗不走
    expect(_modal(page)).to_be_visible()
    assert settled() == before   # 修前：空值读成 0，这单已计费
    _redrawn(page, lambda: _spd_modal(page, {"fee": "0"}))
    assert settled() == before + 1


def test_会诊专家暂停排班先确认_恢复排班一点即回(page, base_url, seed, admin_call, admin_read):
    """P2-1302（第三十八批扫描 AB2-4）：专家库的「排班状态」原先建档后改不了——专家表只有这一列、没有按钮，后端也只有建档与
    清单两个接口；专家请假暂停不了、受理照样选他，建档误选暂停排班的永远受理不了。现在每行有「暂停排班 / 恢复排班」：暂停先在
    页内框里确认（点取消不动），恢复一点即回。

    专家名取「E2E专家」：本文件别处的会诊用例按这个名字受理（库为空时手填）；库里有可排班的专家之后受理只认库里的名字
    （P2-764）——同名，用例先后怎么排都对得上。"""
    expert = admin_call("POST", "/api/consultations/experts", {
        "name": "E2E专家", "org_id": seed["org"]["id"], "specialty": "心内科"})

    def available():
        return next(x for x in admin_read("/api/consultations/experts") if x["id"] == expert["id"])["available"]

    _login(page, base_url)
    _open_page(page, "consultations", "远程会诊")
    _confirm_then(page, lambda: page.click(f'button[data-act="expert-off"][data-id="{expert["id"]}"]'),
                  "受理会诊时不能再选「E2E专家」", lambda: available() is True, lambda: available() is False)
    _redrawn(page, lambda: page.click(f'button[data-act="expert-on"][data-id="{expert["id"]}"]'))   # 修前没有这个按钮
    assert available() is True
    expect(page.locator(f'button[data-act="expert-off"][data-id="{expert["id"]}"]')).to_be_visible()


def test_专病目录的路径节点认全角冒号与逗号_拆不出的点名(page, base_url, admin_read):
    """P1-137 前端同一族：「键:名称,键:名称」原先只按半角逗号、冒号拆——中文输入法填的「apply：申请，review：评估」一个节点都拆
    不出，建出来是个没有节点的专病，不报错。现在全角逗号、顿号、全角冒号都认；拆不出「键:名称」的那一段点名报出来。"""
    _login(page, base_url)
    _open_page(page, "diseaseprograms", "专病管理")
    form = page.locator("#dp-form")
    form.locator('[name="code"]').fill("E2E_DP137")
    form.locator('[name="name"]').fill("E2E 全角节点专病")
    form.locator('[name="nodes"]').fill("apply：申请，review：评估、少了冒号")
    form.locator("button").click()
    expect(page.locator("#dp-msg")).to_contain_text("路径节点要写成「键:名称」：少了冒号")
    form.locator('[name="nodes"]').fill("apply：申请，review：评估、discharge:出院")
    _submit(page, "#dp-form button")
    created = next(p for p in admin_read("/api/disease-programs") if p["code"] == "E2E_DP137")
    assert [(n["key"], n["name"]) for n in created["path_nodes"]] == [
        ("apply", "申请"), ("review", "评估"), ("discharge", "出院")], created   # 修前 []


def test_发起转诊的病种改下拉_留空时只在管一个病种的挂上档案(page, base_url, seed, admin_call, admin_read):
    """P1-139：发起转诊的病种原先是个「病种编码」文本框，不填就不挂纳管档案——有效上转的积分记给录单的人、下转的承接随访
    任务不挂档案。现在是病种下拉；留空时患者只在管一个病种的，挂这份档案。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E转诊挂档", "id_card": "320981199509090139"})
    enrollment = admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    _login(page, base_url)
    _open_page(page, "spdreferral", "逐级转诊闭环")
    form = page.locator("#spd-ref-form")
    expect(form.locator('select[name="program_code"] option[value="hypertension"]')).to_have_count(1)   # 下拉，不是编码框
    form.locator('[name="patient_id"]').fill(str(patient["id"]))
    form.locator('[name="target_org_id"]').fill(str(seed["org"]["id"]))
    form.locator('[name="reason"]').fill("E2E 血压控制不佳")
    _submit(page, "#spd-ref-form button")
    case = next(c for c in admin_read(f"/api/spd/referrals?patient_id={patient['id']}") if c["reason"] == "E2E 血压控制不佳")
    assert (case["program_code"], case["enrollment_id"]) == ("hypertension", enrollment["id"]), case


def test_转诊审核与随访接收由框自己提交_意见写超了框不关(page, base_url, seed, admin_call, admin_read):
    """P2-607 第三批：逐级转诊页的「审核通过」「退回转诊」「下转随访接收」原先点确定就关框、再发请求——意见写超了
    （后端 512 字）或单子状态已变，报错落在页面消息行，写好的意见全丢。现在由框自己提交：失败留框、报错写在框里、
    意见还在；成功才关框、整页重画。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E转诊框内提交", "id_card": "320981199509090607"})
    # 挂上在管档案：管理员代录时发起机构从档案推（否则 422「推不出发起机构」）
    admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    reviewed = admin_call("POST", "/api/spd/referrals", {
        "patient_id": patient["id"], "program_code": "hypertension", "target_org_id": seed["org"]["id"],
        "reason": "E2E 框内提交·审核"})
    down = admin_call("POST", "/api/spd/referrals", {
        "patient_id": patient["id"], "program_code": "hypertension", "target_org_id": seed["org"]["id"],
        "reason": "E2E 框内提交·随访接收"})
    for _ in range(2):   # submitted → township_reviewed → accepted
        admin_call("POST", f"/api/spd/referrals/{down['id']}/review", {"action": "pass"})
    # 下转给下级卫生院（P2-1608）：单子此刻在县医院手上，下转给县医院自己 422
    town = admin_call("POST", "/api/organizations", {"name": "E2E转诊框内提交卫生院", "org_type": "township",
                                                     "level": "township", "parent_id": seed["org"]["id"]})
    admin_call("POST", f"/api/spd/referrals/{down['id']}/down", {"target_org_id": town["id"]})

    def status(case_id):
        return next(c for c in admin_read(f"/api/spd/referrals?patient_id={patient['id']}&open_only=false")
                    if c["id"] == case_id)["status"]

    assert status(down["id"]) == "down_referred"
    _login(page, base_url)
    _open_page(page, "spdreferral", "逐级转诊闭环")
    page.click(f'button[data-ref-pass="{reviewed["id"]}"]')
    form = _spd_modal_rejected(page, {"opinion": "长" * 513})        # 修前框关、报错落到页面消息行，意见全丢
    expect(form.locator('[name="opinion"]')).to_have_value("长" * 513)
    assert status(reviewed["id"]) == "submitted"
    _redrawn(page, lambda: _spd_modal(page, {"opinion": "E2E 同意上转"}))
    assert status(reviewed["id"]) == "township_reviewed"
    _redrawn(page, lambda: page.click(f'button[data-ref-reject="{reviewed["id"]}"]') or
             _spd_modal(page, {"opinion": "E2E 资料不全退回"}))
    assert status(reviewed["id"]) == "rejected"
    _redrawn(page, lambda: page.click(f'button[data-ref-recv="{down["id"]}"]') or
             _spd_modal(page, {"opinion": "E2E 已接收随访"}))
    assert status(down["id"]) == "closed"


def test_规则试算的病种改下拉_留空时命中即开的上转单也挂上档案(page, base_url, seed, admin_call, admin_read):
    """P1-139 同一族：规则试算的病种原先是个「病种编码（可选）」文本框，留空时勾「命中即开上转单」开出的单子不挂纳管档案
    （管理员代录连发起机构都推不出、422）。现在是病种下拉；留空时患者只在管一个病种的按这份档案。"""
    patient = admin_call("POST", "/api/patients", {
        "name": "E2E试算挂档", "id_card": "320981199509090147", "gender": "男", "birth_date": "1995-09-09"})
    enrollment = admin_call("POST", "/api/spd/enrollments", {
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": seed["org"]["id"]})
    rule = admin_call("POST", "/api/spd/referral-rules", {
        "code": "E2E_P139_CHECK", "name": "E2E 试算挂档规则", "program_code": "hypertension",
        "conditions": [{"field": "age", "op": ">=", "value": 0}]})
    try:
        _login(page, base_url)
        _open_page(page, "spdreferral", "逐级转诊闭环")
        form = page.locator("#spd-refcheck-form")
        expect(form.locator('select[name="program_code"] option[value="hypertension"]')).to_have_count(1)   # 下拉，不是编码框
        form.locator('[name="patient_id"]').fill(str(patient["id"]))
        form.locator('[name="auto_create"]').check()
        _submit(page, "#spd-refcheck-form button")   # 开出单子后整页重画
        cases = admin_read(f"/api/spd/referrals?patient_id={patient['id']}")
        assert [(c["trigger_rule_code"], c["program_code"], c["enrollment_id"]) for c in cases] == [
            ("E2E_P139_CHECK", "hypertension", enrollment["id"])], cases   # 修前：管理员代录 422、一张都开不出
    finally:
        admin_call("PATCH", f"/api/spd/referral-rules/{rule['id']}", {"active": False})   # 共享库：停用，免得别的试算命中


def test_慢病病种目录能在界面上新增(page, base_url, admin_read):
    """P2-93（动词级孤儿）：病种目录是分级规则与随访周期的唯一数据源，页面原先只有「编辑」——新增一个慢病病种只能靠接口
    调用方。"""
    _login(page, base_url)
    _open_page(page, "chronic", "慢病管理")
    form = page.locator("#chronic-type-form")
    form.locator('[name="code"]').fill("e2e_ckd93")
    form.locator('[name="name"]').fill("E2E慢性肾病")
    form.locator('[name="followup_interval_days"]').fill("60")
    form.locator('[name="guidance"]').fill("低盐优质低蛋白饮食")
    _submit(page, "#chronic-type-form button")
    created = next(t for t in admin_read("/api/chronic/disease-types") if t["code"] == "e2e_ckd93")
    assert (created["name"], created["followup_interval_days"], created["active"]) == ("E2E慢性肾病", 60, True), created
    expect(page.locator("#chronic-form select[name=disease] option[value=e2e_ckd93]")).to_have_count(1)   # 建档下拉里有了


def test_告知书模板能在界面上新增(page, base_url, admin_read):
    """P2-93（动词级孤儿）：门急诊文书页的告知书模板原先只有「编辑」——新的告知书类型、新版本只能靠接口调用方登记。"""
    _login(page, base_url)
    _open_page(page, "outpatientdocs", "门急诊文书")
    form = page.locator("#od-tpl-form")
    form.locator('[name="consent_type"]').select_option("transfusion")
    form.locator('[name="title"]').fill("E2E 输血治疗知情同意书")
    form.locator('[name="version"]').fill("v2")
    form.locator('[name="body"]').fill("输血可能出现过敏反应、发热反应等，已向患者说明。")
    _submit(page, "#od-tpl-form button")
    created = next(t for t in admin_read("/api/outpatient/consent-templates") if t["title"] == "E2E 输血治疗知情同意书")
    assert (created["consent_type"], created["version"], created["active"]) == ("transfusion", "v2", True), created


def test_中医适宜技术能在界面上入库(page, base_url, admin_read):
    """P2-93（动词级孤儿）：适宜技术库原先只能看——入库接口（仅管理员）一直在，界面上没有入口。"""
    _login(page, base_url)
    _open_page(page, "tcm", "中医药服务")
    form = page.locator("#tcm-tech-form")
    form.locator('[name="name"]').fill("E2E 耳穴压豆")
    form.locator('[name="category"]').fill("外治类")
    form.locator('[name="indication"]').fill("失眠、便秘")
    _submit(page, "#tcm-tech-form button")
    created = next(t for t in admin_read("/api/tcm/techniques") if t["name"] == "E2E 耳穴压豆")
    assert (created["category"], created["indication"]) == ("外治类", "失眠、便秘"), created
    expect(page.locator("#page-body")).to_contain_text("E2E 耳穴压豆")


def test_体质辨识的兼夹体质在界面上标为是(page, base_url):
    """P2-117：气虚 55、阳虚 50——阳虚质原先在结果表里只显示「—」（后端只取最高的一个，其余 ≥ 40 的哪儿都没有）。"""
    _login(page, base_url)
    _open_page(page, "tcm", "中医药服务")
    form = page.locator("#tcm-const")
    form.locator('[name="qi_deficiency"]').fill("55")
    form.locator('[name="yang_deficiency"]').fill("50")
    form.locator("button").click()
    result = page.locator("#tcm-const-result")
    expect(result).to_contain_text("兼夹体质")
    expect(result.locator("tr", has_text="阳虚质").locator(".tag")).to_have_text("是")


def test_模拟诊疗病例能在界面上新建_作答按新建的答案评分(page, base_url, admin_read):
    """P2-93（动词级孤儿）：模拟病例原先只能作答、不能新建——建病例的接口（医生 / 管理层）一直在，新装的平台上这张表
    一条都没有，「模拟诊疗」从界面上无从用起。正确答案从选项里挑（手填差一个字就是后端 422「正确答案不在选项里」）。"""
    _login(page, base_url)
    _open_page(page, "tcmheritage", "名老中医传承与模拟诊疗")
    page.click("#sim-new summary")
    form = page.locator("#sim-new-form")
    form.locator('[name="title"]').fill("E2E 胸痛接诊模拟")
    form.locator('[name="category"]').select_option("emergency")
    first, second = form.locator(".sim-point").nth(0), form.locator(".sim-point").nth(1)
    first.locator(".p-question").fill("首选检查？")
    first.locator(".p-options").fill("心电图 / 腹部B超")
    first.locator(".p-answer").select_option("心电图")
    first.locator(".p-explain").fill("胸痛首要排除急性冠脉综合征")
    second.locator(".p-question").fill("下一步？")
    second.locator(".p-options").fill("启动胸痛中心流程/门诊随访")
    second.locator(".p-answer").select_option("启动胸痛中心流程")
    _submit(page, "#sim-new-form button:has-text('保存病例')")
    created = next(c for c in admin_read("/api/tcm-heritage/simulations") if c["title"] == "E2E 胸痛接诊模拟")
    assert created["category"] == "emergency" and created["total_score"] == 20, created
    assert [p["options"] for p in created["decision_points"]] == [["心电图", "腹部B超"], ["启动胸痛中心流程", "门诊随访"]]

    # 按新建的答案评分：第一题答错（给解析）、第二题答对，10 / 20 → 50 分，未过 60
    page.locator("tr", has_text="E2E 胸痛接诊模拟").locator("button[data-simdo]").click()
    page.check('#sim-form input[name="p1"][value="腹部B超"]')
    page.check('#sim-form input[name="p2"][value="启动胸痛中心流程"]')
    page.click("#sim-form button:has-text('交卷')")
    result = page.locator("#sim-result")
    expect(result).to_contain_text("未通过")
    expect(result).to_contain_text("胸痛首要排除急性冠脉综合征")
    expect(result.locator(".card .value").first).to_have_text("50")

    # 本人的历次作答（P2-479）：交卷后写着「练几次、进步多少都查得到」，原先页面上哪儿都查不到
    history = page.locator("#sim-history")
    expect(history).to_contain_text("我的作答记录（1 次，最高 50 分）")
    page.check('#sim-form input[name="p1"][value="心电图"]')
    page.click("#sim-form button:has-text('交卷')")
    expect(history).to_contain_text("我的作答记录（2 次，最高 100 分）")
    expect(history.locator("tbody tr").first.locator(".tag")).to_have_text("通过")   # 新的在前：第 2 次满分
    expect(history.locator("tbody tr").nth(1).locator(".tag")).to_have_text("未通过")


def test_名老中医医案按病名筛出来_展开读得到按语(page, base_url, admin_read, admin_call):
    """P2-1408（第四十一批扫描 AE4-4）：四诊、按语录得进去，页面上看不见——医案表只列名老中医 / 标题 / 病证 / 治法 / 处方；
    检索只送 keyword（只搜处方、按语、标题），按病名、老师检索页面上没有入口。录一条四诊、按语都分了行的医案，按病名筛出来，
    点「展开」读得到那段按语，再点收起。"""
    admin_call("POST", "/api/tcm-heritage/master-cases", {
        "master_name": "E2E李老", "title": "E2E 胃脘痛案", "disease": "E2E胃痛"})   # 另一个病名：筛掉的那条
    _login(page, base_url)
    _open_page(page, "tcmheritage", "名老中医传承与模拟诊疗")
    form = page.locator("#mc-form")
    form.locator('[name="master_name"]').fill("E2E陈老")
    form.locator('[name="successor_name"]').fill("E2E王医生")
    form.locator('[name="title"]').fill("E2E 膝关节冷痛案")
    form.locator('[name="disease"]').fill("E2E痹证")
    form.locator('[name="four_exams"]').fill("膝冷痛遇寒加重\n舌淡苔白腻，脉沉紧")
    form.locator('[name="commentary"]').fill("附子先煎一小时\n量自10g渐加，口麻即止")
    _submit(page, "#mc-form button")
    created = next(c for c in admin_read("/api/tcm-heritage/master-cases?include_draft=true")
                   if c["title"] == "E2E 膝关节冷痛案")
    assert created["commentary"] == "附子先煎一小时\n量自10g渐加，口麻即止", created   # 多行文本框：换行照录
    expect(page.locator("#mc-list")).to_contain_text("E2E 胃脘痛案")
    search = page.locator("#mc-search")
    search.locator('[name="disease"]').fill("E2E痹证")
    search.locator("button").click()
    shown = page.locator("#mc-list tbody tr:visible")
    expect(shown).to_have_count(1)   # 按病名筛：胃痛那条不在了，展开行默认收着
    expect(shown.first).to_contain_text("E2E 膝关节冷痛案")
    detail = page.locator(f'#mc-list tr[data-mcdetail="{created["id"]}"]')
    toggle = page.locator(f'#mc-list button[data-mcopen="{created["id"]}"]')
    expect(detail).to_be_hidden()
    toggle.click()
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("附子先煎一小时")
    expect(detail).to_contain_text("E2E王医生")
    expect(toggle).to_have_text("收起")
    toggle.click()
    expect(detail).to_be_hidden()


def test_考核指标能在界面上新建_口径与变量提示取自后端(page, base_url, admin_read):
    """P2-93（动词级孤儿）：考核指标库原先只能改、不能建——各县自己的考核口径只能靠接口调用方。口径下拉与「可用变量」
    提示取自 `/api/spd/meta`（后端 `INDICATOR_SOURCES` 一份），换口径提示跟着换；按比例计分的目标取指标的目标值（P2-104）。"""
    _login(page, base_url)
    _open_page(page, "spdassess", "专病考核与积分")
    form = page.locator("#spd-ind-form")
    form.locator('[name="data_source"]').select_option("referral")
    expect(page.locator("#spd-ind-vars")).to_contain_text("effective（其中有效就诊）")
    form.locator('[name="code"]').fill("E2E_REF_EFF")
    form.locator('[name="name"]').fill("E2E 有效上转率")
    form.locator('[name="object_type"]').select_option("village_doctor")
    form.locator('[name="formula"]').fill("effective / total * 100")
    form.locator('[name="weight"]').fill("12.5")
    form.locator('[name="target_value"]').fill("60")
    _submit(page, "#spd-ind-form button")
    created = next(i for i in admin_read("/api/spd/indicators?limit=200") if i["code"] == "E2E_REF_EFF")
    assert (created["data_source"], created["object_type"], created["formula"], created["weight"],
            created["target_value"], created["score_rule"]) == (
        "referral", "village_doctor", "effective / total * 100", 12.5, 60, {"type": "ratio", "full": 100}), created
    expect(page.locator("tr", has_text="E2E_REF_EFF")).to_contain_text("转诊")   # 口径显示名称，不是 referral


def test_考核方案不写权重的指标按默认权重计_编辑回显不补0(page, base_url, admin_read):
    """P2-105：方案表单「指标:权重」串里没写权重的指标，原先送 `weight: 0`——计分只在条目**没有** weight 键时才取指标库的
    默认权重（`item.get("weight", indicator.weight)`），这个指标在方案里就一分不计；编辑弹窗又把没有权重的条目回显成「:0」，
    原样存一次就归零。权重写成文字的原先成了 null，同样按 0 计。"""
    _login(page, base_url)
    _open_page(page, "spdassess", "专病考核与积分")
    form = page.locator("#spd-plan-form")
    form.locator('[name="code"]').fill("E2E_PLAN_W")
    form.locator('[name="name"]').fill("E2E 默认权重方案")
    form.locator('[name="items"]').fill("followup_rate， path_rate:四十")
    form.locator("button").click()
    expect(page.locator("#spd-plan-msg")).to_contain_text("权重要写成数字")
    form.locator('[name="items"]').fill("followup_rate, path_rate:30")
    _submit(page, "#spd-plan-form button")
    expected = [{"indicator_code": "followup_rate"}, {"indicator_code": "path_rate", "weight": 30}]
    plan = next(p for p in admin_read("/api/spd/assess-plans") if p["code"] == "E2E_PLAN_W")
    assert plan["items"] == expected, plan["items"]   # 修前 followup_rate 带着 weight 0

    page.click(f'button[data-plan-edit="{plan["id"]}"]')
    expect(page.locator('form.panel [name="items"]')).to_have_value("followup_rate,path_rate:30")   # 修前回显 followup_rate:0
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert next(p for p in admin_read("/api/spd/assess-plans") if p["code"] == "E2E_PLAN_W")["items"] == expected


def test_任务中心能手工派发慢专病任务(page, base_url, seed, admin_read):
    """P2-93（动词级孤儿）：建任务的接口 `POST /api/spd/tasks` 一直在，任务中心却只有查、办、批量操作——临时要给某位患者派一件事
    （补测一次血压、电话确认用药），界面上无从下手；孤儿端点棘轮按路径算，清单有页面调就算接上了。"""
    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    form = page.locator("#spd-task-form")
    form.locator('[name="patient_id"]').fill(str(seed["patient"]["id"]))
    form.locator('[name="title"]').fill("E2E 手工派发·补测血压")
    form.locator('[name="task_type"]').select_option("followup")
    form.locator('[name="org_id"]').fill(str(seed["org"]["id"]))
    form.locator('[name="due_days"]').fill("3")
    form.locator('[name="priority"]').select_option("2")
    form.locator('[name="require_evidence"]').check()
    expect(form.locator('[name="task_type"] option[value="path"]')).to_have_count(0)   # 路径节点任务由路径派生
    _submit(page, "#spd-task-form button")
    task = next(t for t in admin_read(f"/api/spd/tasks?patient_id={seed['patient']['id']}&limit=100")
                if t["title"] == "E2E 手工派发·补测血压")
    assert (task["task_type"], task["priority"], task["require_evidence"], task["org_id"], task["status"]) == (
        "followup", 2, True, seed["org"]["id"], "pending"), task


def test_clinical_documents_flow(page, base_url, seed):
    """住院临床文书（T2.1/T2.2）：写首次病程 → 记护理 → 录体征 → 完整性自查转为完整。"""
    _login(page, base_url)
    _open_page(page, "clinicaldocs", "住院临床文书")
    expect(page.locator("#page-body")).to_contain_text("缺首次病程记录")

    page.select_option("#note-form select[name=note_type]", "first")
    page.fill("#note-form input[name=content]", "患者因转移性右下腹痛入院，拟行阑尾切除术")
    _submit(page, "#note-form button")
    expect(page.locator("#page-body")).to_contain_text("首次病程")

    page.select_option("#note-form select[name=note_type]", "daily")
    page.fill("#note-form input[name=content]", "术后第一天，体温37.4℃，切口无渗出")
    _submit(page, "#note-form button")

    page.fill("#nursing-form input[name=content]", "一级护理，持续心电监护")
    _submit(page, "#nursing-form button")

    # 日期时间控件（P1-100）：填的是 `T` 写法，界面换成空格再送（formJson）
    page.fill("#vital-form input[name=measured_at]", "2026-08-12T08:00")
    page.fill("#vital-form input[name=temperature]", "37.4")
    page.fill("#vital-form input[name=pulse]", "86")
    page.fill("#vital-form input[name=intake_ml]", "1800")   # P2-473：出入量、体重原先录不进也看不见
    page.fill("#vital-form input[name=weight_kg]", "61.5")
    _submit(page, "#vital-form button")

    expect(page.locator("#page-body")).to_contain_text("文书完整")
    row = page.locator("tr", has_text="2026-08-12")
    expect(row.first).to_contain_text("1800")
    expect(row.first).to_contain_text("61.5")


def test_交接班交得进也看得见_按病区筛(page, base_url, admin_call):
    """P2-476：交接班原先只记得进——清单接口一直在，页面一个调用都没有，接班的人无从读起；病区还要手填编号。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E交接班县医院", "org_type": "lead_hospital", "level": "county"})
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": "E2E交接班病区"})
    other = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": "E2E交接班另一病区"})
    admin_call("POST", "/api/inpatient/handovers", {"ward_id": other["id"], "shift": "day",
                                                    "handover_date": "2026-09-26", "content": "E2E 另一病区的交班"})

    _login(page, base_url)
    _open_page(page, "clinicaldocs", "住院临床文书")
    form = page.locator("#handover-form")
    form.locator('[name="ward_id"]').select_option(str(ward["id"]))   # 修前手填病区编号
    form.locator('[name="shift"]').select_option("night")
    form.locator('[name="handover_date"]').fill("2026-09-27")
    form.locator('[name="from_staff"]').fill("E2E白班护士")
    form.locator('[name="to_staff"]').fill("E2E夜班护士")
    form.locator('[name="content"]').fill("E2E 3床术后第一天，注意引流")
    _submit(page, "#handover-form button")
    row = page.locator("tr", has_text="E2E 3床术后第一天")
    expect(row).to_contain_text("大夜")            # 修前页面上没有交接班清单
    expect(row).to_contain_text("E2E交接班病区")
    expect(row).to_contain_text("E2E白班护士 → E2E夜班护士")

    filt = page.locator("#handover-filter")
    filt.locator('[name="ward_id"]').select_option(str(ward["id"]))
    _submit(page, "#handover-filter button")
    expect(page.locator("tr", has_text="E2E 3床术后第一天")).to_have_count(1)
    expect(page.locator("tr", has_text="E2E 另一病区的交班")).to_have_count(0)
    expect(page.locator('#handover-filter [name="ward_id"]')).to_have_value(str(ward["id"]))


def test_体温单缺测的不画成0_在那儿断开(page, base_url, admin_read, admin_call):
    """P2-158：体温单曲线原先用 `v.temperature || 0`——一次只测了血压的记录把体温、脉搏两条曲线都拽到 0，
    与接口注释、用户手册「未测项留空不要填 0（填 0 会污染体温单趋势曲线）」相反。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E体温单县医院", "org_type": "lead_hospital", "level": "county"})
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": "E2E体温单病区"})
    bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "E2E-T1"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E发热患者", "id_card": "320981198505055158", "gender": "男"})
    adm = admin_call("POST", "/api/inpatient/admissions",
                     {"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "肺炎"})
    for body in ({"measured_at": "2026-09-20 08:00", "temperature": 38.6, "pulse": 96},
                 {"measured_at": "2026-09-20 10:00", "sbp": 150, "dbp": 90},   # 只测了血压
                 {"measured_at": "2026-09-20 14:00", "temperature": 39.1, "pulse": 102}):
        admin_call("POST", f"/api/inpatient/admissions/{adm['id']}/vitals", body)

    _login(page, base_url)
    _open_page(page, "clinicaldocs", "住院临床文书")
    page.select_option("#doc-pick select[name=admission_id]", str(adm["id"]))
    _submit(page, "#doc-pick button")
    chart = page.locator("#page-body svg").first
    # 体温曲线（#c0392b）：两个测量点、在缺测处断成两段；修前是一条三个点的折线，中间那个点画在 0 上
    expect(chart.locator('circle[fill="#c0392b"]')).to_have_count(2)
    expect(chart.locator('polyline[stroke="#c0392b"]')).to_have_count(2)


def _two_admissions(admin_call, tag, cards):
    """同一病区两位在院患者（甲、乙），给「下拉换了人」的用例用。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": f"E2E{tag}县医院", "org_type": "lead_hospital", "level": "county"})
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": f"E2E{tag}病区"})
    adms = []
    for i, (name, id_card, dx) in enumerate(zip(("甲", "乙"), cards, ("肺炎", "心衰"))):
        bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": f"E2E-{tag}{i}"})
        patient = admin_call("POST", "/api/patients", {"name": f"E2E{tag}{name}", "id_card": id_card, "gender": "男"})
        adms.append(admin_call("POST", "/api/inpatient/admissions", {
            "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": dx}))
    return adms


def _note_contents(admin_read, admission_id):
    return [n["content"] for n in admin_read(f"/api/inpatient/admissions/{admission_id}/progress-notes")]


def test_住院文书下拉改成乙_不点切换_病程写进乙(page, base_url, admin_read, admin_call):
    """P1-231：下拉显示乙、没点「切换」，病程原先照旧写进甲，回执照样「已记录」。修后下拉一改就切换。"""
    a, b = _two_admissions(admin_call, "切换", ("320981198606061210", "320981198707071311"))
    _login(page, base_url)
    page.evaluate(f"localStorage.setItem('medplat_doc_adm', '{a['id']}')")
    _open_page(page, "clinicaldocs", "住院临床文书")
    expect(page.locator("#doc-pick select[name=admission_id]")).to_have_value(str(a["id"]))
    # 只在下拉里选乙、不点切换：修前页面不重画（这一步就等不到），病程写进甲
    _redrawn(page, lambda: page.select_option("#doc-pick select[name=admission_id]", str(b["id"])))
    expect(page.locator("#doc-pick select[name=admission_id]")).to_have_value(str(b["id"]))
    page.locator("#note-form input[name=content]").fill("E2E 乙的日常病程")
    _submit(page, "#note-form button")
    assert _note_contents(admin_read, b["id"]) == ["E2E 乙的日常病程"]
    assert _note_contents(admin_read, a["id"]) == []


def test_病程不动类型下拉_存日常病程不落成首次病程(page, base_url, admin_read, admin_call):
    """P2-1306（第三十八批扫描 AB3-8）：桌面病程表单的类型下拉原先没有预选、首项是「首次病程」——新入院先写的一条不动
    下拉就落成首次病程，真正的首次病程随后 409、且改不回来。修后预选日常病程，与医生移动端一致。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E病程类型县医院", "org_type": "lead_hospital", "level": "county"})
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": "E2E病程类型病区"})
    bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "E2E-NT1"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E病程类型患者", "id_card": "320981199203061413", "gender": "男"})
    adm = admin_call("POST", "/api/inpatient/admissions", {
        "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "肺炎"})
    _login(page, base_url)
    page.evaluate(f"localStorage.setItem('medplat_doc_adm', '{adm['id']}')")
    _open_page(page, "clinicaldocs", "住院临床文书")
    expect(page.locator("#doc-pick select[name=admission_id]")).to_have_value(str(adm["id"]))
    expect(page.locator("#note-form select[name=note_type]")).to_have_value("daily")   # 修前 first
    page.locator("#note-form input[name=content]").fill("E2E 入院当日病情平稳")
    _submit(page, "#note-form button")
    notes = admin_read(f"/api/inpatient/admissions/{adm['id']}/progress-notes")
    assert [(n["note_type"], n["content"]) for n in notes] == [("daily", "E2E 入院当日病情平稳")]


def test_查房下拉改成乙_不点切换患者_病程写进乙(page, base_url, admin_read, admin_call):
    """P1-231：医生移动端查房同形——下拉显示乙、没点「切换患者」，病程原先写进甲。"""
    a, b = _two_admissions(admin_call, "查房", ("320981198808081412", "320981198909091513"))
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    page.click('a.tab-btn[data-tab="round"]')
    page.locator("#round-adm").select_option(str(a["id"]))
    page.click("#round-pick button[type=submit]")
    expect(page.locator("#round-status")).to_contain_text("病程记录")
    page.locator("#round-adm").select_option(str(b["id"]))   # 不点切换患者
    page.locator("#round-content").fill("E2E 乙查房：体温降至37.2℃")
    page.click("#round-note button[type=submit]")
    expect(page.locator("#round-msg")).to_contain_text("病程已记录")
    assert _note_contents(admin_read, b["id"]) == ["E2E 乙查房：体温降至37.2℃"]
    assert _note_contents(admin_read, a["id"]) == []


def test_医生端慢病随访录完_下拉仍是刚才那一份(page, base_url, seed, admin_call, admin_read):
    """P2-1011：录完一条随访，档案下拉原先悄悄跳回分级最高的第一条（重画不带 selected），补一条就记进了别人的档案。"""
    high, low = (admin_call("POST", "/api/patients", {"name": f"E2E随访{tag}", "id_card": card, "gender": "男"})
                 for tag, card in (("三级", "320981199001011610"), ("一级", "320981199102021711")))
    high_rec = admin_call("POST", "/api/chronic", {"patient_id": high["id"], "disease": "hypertension",
                                                   "managed_by_org_id": seed["org"]["id"]})
    admin_call("POST", f"/api/chronic/{high_rec['id']}/followups", {"sbp": 182, "dbp": 112})   # 定 3 级，排在前面
    low_rec = admin_call("POST", "/api/chronic", {"patient_id": low["id"], "disease": "hypertension",
                                                  "managed_by_org_id": seed["org"]["id"]})
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    page.click('a.tab-btn[data-tab="chronic"]')
    picker = page.locator("#fu-chronic")
    picker.select_option(str(low_rec["id"]))
    page.locator('#fu-metrics input[data-key="sbp"]').fill("128")
    page.locator('#fu-metrics input[data-key="dbp"]').fill("80")
    page.click("#fu-form button[type=submit]")
    expect(page.locator("#fu-msg")).to_contain_text("已录入")
    expect(picker.locator(f'option[value="{high_rec["id"]}"]')).to_have_count(1)   # 重画完了
    expect(picker).to_have_value(str(low_rec["id"]))   # 修前跳回第一条
    assert len(admin_read(f"/api/chronic/{low_rec['id']}/followups")) == 1


def _user_call(base_url, username, password="passw0rd1"):
    """以指定账号调接口（造医师才能造的前置数据，如医嘱）。"""
    import json
    from urllib.request import Request

    def call(method, path, payload=None, token=None):
        req = Request(f"{base_url}{path}", method=method,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json",
                               **({"Authorization": f"Bearer {token}"} if token else {})})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = call("POST", "/api/auth/login", {"username": username, "password": password})["access_token"]
    return lambda method, path, payload=None: call(method, path, payload, token)


def test_医嘱单面板只画最后点的那一次住院_标题写住院号(page, base_url, seed, admin_call):
    """P2-1012：先点甲的「医嘱单」、立刻改点乙，甲那次响应晚到原先把甲的医嘱画在面板里，标题只写「医嘱单」——
    「登记执行」记到了甲的医嘱上。用 Playwright 扣住甲的请求、等乙画完再放行，先后是确定的。"""
    doctor = _user_call(base_url, "e2e_doctor")   # 种子机构的医师：医嘱开立 = 医师
    adms = []
    for i, (card, dx) in enumerate((("320981199203031812", "肺炎"), ("320981199304041913", "心衰"))):
        bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": seed["ward"]["id"], "bed_no": f"E2E-O{i}"})
        patient = admin_call("POST", "/api/patients", {"name": f"E2E医嘱{'甲乙'[i]}", "id_card": card, "gender": "男"})
        adms.append(admin_call("POST", "/api/inpatient/admissions", {
            "patient_id": patient["id"], "ward_id": seed["ward"]["id"], "bed_id": bed["id"], "diagnosis_name": dx}))
    a, b = adms
    doctor("POST", "/api/inpatient/orders", {"admission_id": a["id"], "order_type": "long", "content": "E2E甲 头孢曲松 2g"})
    doctor("POST", "/api/inpatient/orders", {"admission_id": b["id"], "order_type": "long", "content": "E2E乙 呋塞米 20mg"})
    _login(page, base_url)
    _open_page(page, "inpatient", "住院管理")
    held = []
    # 医嘱单续页取全（P2-1693）：地址后面跟着 `&limit=500&offset=0`
    pattern = f"**/api/inpatient/orders?admission_id={a['id']}&*"
    page.route(pattern, lambda route: held.append(route))
    page.click(f'[data-orders="{a["id"]}"]')   # 甲那次被扣住
    page.click(f'[data-orders="{b["id"]}"]')
    panel = page.locator("#inp-orders")
    expect(panel).to_contain_text("E2E乙 呋塞米")
    page.wait_for_function("() => true")   # 让扣住的那次路由回调落地
    assert len(held) == 1, held
    held[0].continue_()
    page.wait_for_timeout(1000)   # 甲的响应落地
    page.unroute(pattern)
    expect(panel).to_contain_text("E2E乙 呋塞米")
    expect(panel).not_to_contain_text("E2E甲")   # 修前被甲的医嘱盖掉
    expect(page.locator("#inp-orders-title")).to_have_text(f"医嘱单 · 住院 #{b['id']}")


def test_surgery_full_flow(page, base_url, seed, admin_read):
    """手术麻醉（T2.3）：申请 → 审批 → 排班 → 术中记录，状态逐级推进。

    **申请与审批必须是两个人**：`approve_request` 明确拒绝"审批本人提出的
    手术申请"（职责分离）。用例此前用 admin 一个人从头做到尾，那条规则加进来
    之后就一直红着——这不是应用的问题，是用例没跟上业务规则。
    申请改由医师经接口提出（见 seed），页面只驱动审批→排班→术中记录。

    P2-1116：术中记录原先不录手术起止时刻，做手术那天（术后随访起算、手术质量指标归月）恒取排班日。现在起止时刻缺省带出
    这台的排班日期与时段、按实际改——这台顺延一天做，术后随访按实际那天起算。缺省取自「今天及以后」的排班表，所以排班日
    写远期：写死的过去日子不在表里，带不出来。

    P2-1307：术中记录原先不录术者 / 助手，术者恒取申请单上的拟施术者（缺省即申请人）。现在缺省带出、按实际改。
    """
    _login(page, base_url)
    _open_page(page, "surgery", "手术麻醉")
    # 申请由医师经接口提出（见 seed），页面上应当能看到这条待审批的申请
    expect(page.locator("#page-body")).to_contain_text("待审批")

    page.click("button[data-approve]")
    expect(page.locator("#page-body")).to_contain_text("已审批")

    # 2026-09-24 前排班与术中记录都是连续多个原生弹窗、用 `_answers` 按序喂值；
    # 换成 `spdModal` 之后改用 `_spd_modal`（两者互斥，见 CLAUDE.md §7）。
    page.click("button[data-schedule]")
    # 排班日写远期：术中记录的起止缺省取自「今天及以后」的排班表（P2-1116），写死的过去日子带不出来
    _spd_modal(page, {"room_id": str(seed["room"]["id"]), "scheduled_date": "2099-09-01",
                      "start_time": "09:00", "end_time": "11:00"})
    expect(page.locator("#page-body")).to_contain_text("已排班")
    expect(page.locator("#page-body")).to_contain_text("E2E一号手术间")

    # 术中记录：转归此前被写死成"好转"（P1-66），这里选"治愈"并填术前/术后诊断，
    # 然后在记录详情里读回来——证明选的值真的落了库
    page.click("button[data-record]")
    # 手术起止时刻缺省带出这台的排班日期与时段（P2-1116），按实际改：这台顺延一天做
    modal = _modal(page)
    expect(modal.locator('[name="start_at"]')).to_have_value("2099-09-01 09:00")
    expect(modal.locator('[name="end_at"]')).to_have_value("2099-09-01 11:00")
    # 术者缺省带出申请单上的拟施术者（P2-1307）：seed 那张由 e2e_doctor 提出、没填拟施术者，即申请人；实际换人主刀
    expect(modal.locator('[name="surgeon_name"]')).to_have_value("E2E外科医生")
    # 术中所见写超了（后端 2048 字）：框自己提交，报错写在框里、框不关、填了一整张的记录都在（P2-607 第八批）
    form = _spd_modal_rejected(page, {"actual_surgery_name": "腹腔镜阑尾切除术", "anesthetist_name": "麻醉科周医生",
                                      "surgeon_name": "E2E主刀医生", "assistants": "E2E一助医生",
                                      "start_at": "2099-09-02 09:30", "end_at": "2099-09-02 10:40",
                                      "findings": "所" * 2049, "blood_loss_ml": "20", "outcome": "治愈",
                                      "preop_diagnosis": "急性阑尾炎", "postop_diagnosis": "急性化脓性阑尾炎"})
    expect(form.locator('[name="postop_diagnosis"]')).to_have_value("急性化脓性阑尾炎")
    expect(form.locator('[name="start_at"]')).to_have_value("2099-09-02 09:30")
    _spd_modal(page, {"findings": "阑尾化脓"})
    expect(page.locator("#page-body")).to_contain_text("已完成")
    page.click("button[data-view]")
    expect(page.locator("#surg-detail-body")).to_contain_text("治愈")
    expect(page.locator("#surg-detail-body")).to_contain_text("2099-09-02 09:30 ~ 2099-09-02 10:40")   # 修前「起止」恒空
    rid = seed["surgery_request"]["id"]
    # 修前术者恒为申请人「E2E外科医生」、助手恒空
    expect(page.locator("#surg-detail-body tr", has_text="术者")).to_contain_text("E2E主刀医生")
    expect(page.locator("#surg-detail-body tr", has_text="助手")).to_contain_text("E2E一助医生")
    record = admin_read(f"/api/surgery/requests/{rid}/record")
    assert (record["surgeon_name"], record["assistants"]) == ("E2E主刀医生", "E2E一助医生"), record
    # 术后随访按实际开始那天起算（修前按排班日 9-01 起算，到期 9-15）
    (task,) = [t for t in admin_read("/api/followups?category=surgery&limit=500") if t["source_id"] == rid]
    assert task["due_date"] == "2099-09-16", task


def test_提手术申请时勾得上非计划重返手术室(page, base_url, admin_call, admin_read):
    """P2-172：手册写「非计划重返手术室：提手术申请时如实勾选」，申请表单上原先没有这一项——
    接口收 `unplanned_return`，页面从不送，质量指标「非计划重返手术室率」恒为 0。

    P2-1398：重返是同一次住院内的再次手术，本次住院此前没有手术的勾不上（422），故先经接口提一台。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E重返县医院", "org_type": "lead_hospital", "level": "county"})
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": "E2E重返外科"})
    bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "R1"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E重返患者", "id_card": "320981198505051721"})
    adm = admin_call("POST", "/api/inpatient/admissions", {
        "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "胆囊结石"})
    admin_call("POST", "/api/surgery/requests", {"admission_id": adm["id"], "surgery_name": "E2E腹腔镜胆囊切除术"})

    _login(page, base_url)
    _open_page(page, "surgery", "手术麻醉")
    form = page.locator("#surg-form")
    form.locator("input[name=admission_id]").fill(str(adm["id"]))
    form.locator("input[name=surgery_name]").fill("E2E胆漏再探查术")
    form.locator("input[name=unplanned_return]").check()
    _submit(page, "#surg-form button")
    expect(page.locator("#page-body")).to_contain_text("非计划重返")

    rows = admin_read(f"/api/surgery/requests?admission_id={adm['id']}")
    assert [(r["surgery_name"], r["unplanned_return"]) for r in rows] == [
        ("E2E胆漏再探查术", True), ("E2E腹腔镜胆囊切除术", False)]   # 修前无处可勾


def test_提手术申请时填得上拟施术者(page, base_url, admin_call, admin_read):
    """P2-1307（第三十八批扫描 AB3-3）：申请表单原先没有拟施术者，申请单的术者恒为申请人——住院医提的申请，术中记录缺省
    带出的就是住院医。现在选填，空着照旧由后端取申请人。排在 `test_surgery_full_flow` 之后：它点的是页面上第一个「审批通过」。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E拟施术者县医院", "org_type": "lead_hospital", "level": "county"})
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": org["id"], "name": "E2E拟施术者外科"})
    bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "S1"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E拟施术者患者", "id_card": "320981199304071514"})
    adm = admin_call("POST", "/api/inpatient/admissions", {
        "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "腹股沟疝"})

    _login(page, base_url)
    _open_page(page, "surgery", "手术麻醉")
    form = page.locator("#surg-form")
    form.locator("input[name=admission_id]").fill(str(adm["id"]))
    form.locator("input[name=surgery_name]").fill("E2E疝修补术")
    form.locator("input[name=surgeon_name]").fill("E2E拟施术者")
    _submit(page, "#surg-form button")

    rows = admin_read(f"/api/surgery/requests?admission_id={adm['id']}")
    assert [(r["surgery_name"], r["surgeon_name"]) for r in rows] == [("E2E疝修补术", "E2E拟施术者")]   # 修前无处可填


@pytest.fixture(scope="session")
def admin_read(base_url):
    """以 admin 身份按接口读回（核对"点了取消确实没动"用）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    return lambda path: call(path, None, admin)


def test_followup_center_flow(page, base_url, seed, admin_read):
    """随访中心（T2.4）：术后随访任务自动派生，可在页面完成并计入统计。

    P2-38：「完成」的随访结果改在页内表单里填（多行）；「取消」任务原先点一下就生效、没有任何确认，
    现在先确认。先点完成再取消、点取消任务再保留，按接口核对任务仍待随访，最后完成。"""
    _login(page, base_url)
    _open_page(page, "followups", "随访中心")
    expect(page.locator("#page-body")).to_contain_text("术后随访")

    tid = page.locator("button[data-done]").first.get_attribute("data-done")

    def status():
        (row,) = [t for t in admin_read("/api/followups?limit=500") if str(t["id"]) == tid]
        return row["status"]

    modal = page.locator("form.panel:has(button[data-cancel])")
    page.click(f'button[data-done="{tid}"]')
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    page.click(f'#page-body button[data-cancel="{tid}"]')
    expect(modal).to_contain_text("不能恢复")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert status() == "pending", "点了取消 / 保留却照样动了任务"

    page.click(f'button[data-done="{tid}"]')
    form = _spd_modal_rejected(page, {"result": "随" * 1025})   # 写超了（后端 1024 字）：框不关、写的还在（P2-607 第八批）
    expect(form.locator('[name="result"]')).to_have_value("随" * 1025)
    assert status() == "pending"
    # 等整页重画完再按接口读回（`_redrawn` 的 docstring 说的就是这个）：原先只等页面上出现「已完成」，
    # 可别的行本来就可能带这三个字，于是写请求还在路上就读回、读到 pending（CI run 689 实测红一次）
    _redrawn(page, lambda: _spd_modal(page, {"result": "切口愈合良好，无发热\n嘱两周后门诊复查"}))
    expect(page.locator("#page-body")).to_contain_text("已完成")
    assert status() == "done"


def test_慢病在管名单能看随访记录(page, base_url, seed, admin_call):
    """P2-478：慢病随访原先只能录、不能看——上次量的血压多少、给过什么指导，页面上查不到。"""
    patient = admin_call("POST", "/api/patients", {"name": "E2E随访史患者", "id_card": "320981197006062477", "gender": "女"})
    chronic = admin_call("POST", "/api/chronic", {"patient_id": patient["id"], "disease": "hypertension",
                                                  "managed_by_org_id": seed["org"]["id"]})
    for body in ({"sbp": 176, "dbp": 102, "guidance": "E2E 限盐、加服氨氯地平"},
                 {"sbp": 138, "dbp": 86, "metrics": {"adherence_score": 4}}):
        admin_call("POST", f"/api/chronic/{chronic['id']}/followups", body)

    _login(page, base_url)
    _open_page(page, "chronic", "慢病管理")
    page.click(f'button[data-fuhist="{chronic["id"]}"]')   # 修前名单上只有「风险评分」
    history = page.locator("#fu-history")
    expect(history).to_contain_text("随访记录（2 次）")
    expect(history.locator("tr", has_text="176/102")).to_contain_text("E2E 限盐、加服氨氯地平")
    expect(history.locator("tr", has_text="138/86")).to_contain_text("用药依从性")   # 指标按病种目录的名字显示


@pytest.fixture(scope="session")
def chronic_seed(seed, admin_call):
    """慢病随访两端表单的前置：E2E 患者的一份高血压档案。"""
    return admin_call("POST", "/api/chronic", {"patient_id": seed["patient"]["id"], "disease": "hypertension",
                                               "managed_by_org_id": seed["org"]["id"]})


def test_下次随访日两端都用日期控件填_选的那一天真的落库(page, base_url, chronic_seed, admin_read):
    """P2-55：两端随访表单的「下次随访日」原先是自由文本框，「2026/10/1」「10月1日」照存，按字符串比较的
    超期名单对它失效（「10月1日」没到期就超期、「2026/10/1」超期了却不在名单里）。换成日期控件后送出的
    一定是 YYYY-MM-DD：桌面端录一次、医生移动端再录一次，每次按接口读回档案的下次随访日。

    P2-1545：手填的下次随访日限在 [今天, 今天 + 3650 天]，桌面端日期框的 min / max 同口径——日子按今天起算，写死的日子
    过了那天就交不上。"""
    from datetime import date, timedelta

    cid = chronic_seed["id"]
    desk_due = (date.today() + timedelta(days=57)).isoformat()
    mobile_due = (date.today() + timedelta(days=102)).isoformat()

    def next_due():
        (row,) = [c for c in admin_read("/api/chronic?limit=500") if c["id"] == cid]
        return row["next_due"]

    _login(page, base_url)
    _open_page(page, "chronic", "慢病管理")
    form = page.locator("#fu-form")
    expect(form.locator("input[name=next_due]")).to_have_attribute("type", "date")
    expect(form.locator("input[name=next_due]")).to_have_attribute("min", date.today().isoformat())
    expect(form.locator("input[name=next_due]")).to_have_attribute(
        "max", (date.today() + timedelta(days=3650)).isoformat())
    form.locator("input[name=chronic_id]").fill(str(cid))
    form.locator("input[name=sbp]").fill("128")
    form.locator("input[name=dbp]").fill("82")
    form.locator("input[name=next_due]").fill(desk_due)
    seen = []

    def on_dialog(dialog):
        seen.append(dialog.message)
        dialog.accept()

    page.once("dialog", on_dialog)
    _submit(page, "#fu-form button")
    assert seen and f"下次随访：{desk_due}" in seen[0], seen
    assert next_due() == desk_due

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    page.click('a.tab-btn[data-tab="chronic"]')
    expect(page.locator("#fu-next")).to_have_attribute("type", "date")
    page.locator("#fu-chronic").select_option(str(cid))
    page.locator("#fu-next").fill(mobile_due)
    page.click("#fu-form button[type=submit]")
    expect(page.locator("#fu-msg")).to_contain_text(f"下次随访 {mobile_due}")
    assert next_due() == mobile_due


@pytest.fixture(scope="session")
def edu_material(admin_call):
    """慢专病定时宣教的前置：一份启用中的宣教素材。"""
    return admin_call("POST", "/api/spd/edu-materials",
                      {"code": "e2e_p1100", "title": "E2E限盐宣教", "content": "每日盐不超过5克"})


def test_时间戳两端都用日期时间控件填_落库是空格写法(page, base_url, seed, edu_material, admin_read):
    """P1-100：定时宣教的推送时间、体征测量时刻原先是自由文本框——「2026-10-1 8:00」照存，定时派发按字符串比较
    晚 9 天才发，体温单按字符串排序把「8:00」排到「14:00」之后。换成日期时间控件后控件给的是 `T` 写法，界面换成
    空格再送：桌面端定一次宣教推送、医生移动端查房录一次体征，每次按接口读回。"""
    _login(page, base_url)
    _open_page(page, "spdmember", "服务团队成员端·日常服务")
    form = page.locator("#spd-edu-form")
    expect(form.locator("input[name=send_at]")).to_have_attribute("type", "datetime-local")
    form.locator("select[name=material_id]").select_option(str(edu_material["id"]))
    form.locator("input[name=patient_ids]").fill(str(seed["patient"]["id"]))
    form.locator("select[name=channel]").select_option("app")
    form.locator("input[name=send_at]").fill("2030-10-01T08:00")
    _submit(page, "#spd-edu-form button")
    pushes = admin_read(f"/api/spd/edu-pushes?material_id={edu_material['id']}")
    assert [(p["send_at"], p["status"]) for p in pushes] == [("2030-10-01 08:00", "pending")], pushes

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    page.click('a.tab-btn[data-tab="round"]')
    adm = seed["admission"]["id"]
    page.locator("#round-adm").select_option(str(adm))
    page.click("#round-pick button[type=submit]")
    expect(page.locator("#rv-at")).to_have_attribute("type", "datetime-local")
    page.locator("#rv-at").fill("2026-08-12T09:30")
    page.locator("#rv-temp").fill("37.2")
    page.click("#round-vital button[type=submit]")
    expect(page.locator("#round-vital-msg")).to_contain_text("体征已录入")   # 体征表单自己的消息行（P2-1093）
    vitals = admin_read(f"/api/inpatient/admissions/{adm}/vitals")
    assert ("2026-08-12 09:30", 37.2) in [(v["measured_at"], v["temperature"]) for v in vitals], vitals


def test_doctor_mobile_workbench_loads(page, base_url, seed):
    """医生移动工作台（块4）：登录后进入待办页签。"""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    expect(page.locator("#who")).to_contain_text("待办")


@pytest.fixture(scope="session")
def surgery_mobile_seed(base_url, seed):
    """医生移动端"填写术中记录"的前置：再排一台已排班的手术（seed 那台由管理端用例做完）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    req = call("/api/surgery/requests",
               {"admission_id": seed["admission"]["id"], "surgery_name": "E2E移动端疝修补术",
                "incision_level": "I", "anesthesia_type": "spinal"}, doctor)
    call(f"/api/surgery/requests/{req['id']}/approve", {"approved": True}, admin)
    # 排班日写远期：术中记录的起止缺省取自「今天及以后」的排班表（P2-1116），写死的过去日子带不出来
    call(f"/api/surgery/requests/{req['id']}/schedule",
         {"room_id": seed["room"]["id"], "scheduled_date": "2099-09-02",
          "start_time": "13:00", "end_time": "14:00"}, admin)
    return {"request": req, "read": lambda path: call(path, None, admin)}


def test_医生移动端术中记录在卡片内表单里填_转归可选(page, base_url, surgery_mobile_seed):
    """P2-38 / P1-66：移动端"填写术中记录"原先四连问、**转归写死"好转"**。换成卡片内表单后
    转归可选、术式预填；出血量写错由后端报人话。最后经接口读回，证明选的转归真的落了库。
    P2-1116：手术起止时刻原先不送（做手术那天恒取排班日），现在缺省带出这台的排班日期与时段、照送。
    P2-1307：术者 / 助手原先不送（术者恒取申请单上的），现在术者缺省带出申请单上的、按实际改。"""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    page.click('a.tab-btn[data-tab="surgery"]')

    card = page.locator(".m-card", has_text="E2E移动端疝修补术")
    card.locator("button[data-record]").click()
    form = card.locator("form.surg-record-form")
    expect(form.locator("input[name=actual_surgery_name]")).to_have_value("E2E移动端疝修补术")
    # 起止时刻缺省带出这台的排班日期与时段（P2-1116）；这台按排班做完，不改
    expect(form.locator("input[name=start_at]")).to_have_value("2099-09-02 13:00")
    expect(form.locator("input[name=end_at]")).to_have_value("2099-09-02 14:00")
    # 术者缺省带出申请单上的拟施术者（P2-1307）：这台由 e2e_doctor 提出、没填拟施术者，即申请人；实际换人主刀
    expect(form.locator("input[name=surgeon_name]")).to_have_value("E2E外科医生")
    form.locator("input[name=surgeon_name]").fill("E2E移动端主刀")
    form.locator("input[name=assistants]").fill("E2E移动端一助")
    form.locator("select[name=outcome]").select_option("未愈")
    form.locator("input[name=postop_diagnosis]").fill("腹股沟斜疝")
    form.locator("input[name=blood_loss_ml]").fill("五十")
    form.locator("button[type=submit]").click()
    # 报错写在这张卡的表单里（P2-1093），不写到两张列表下方的整页消息行
    expect(form.locator("[data-card-msg]")).to_contain_text("blood_loss_ml")
    expect(page.locator("#surgery-msg")).not_to_contain_text("blood_loss_ml")
    form.locator("input[name=blood_loss_ml]").fill("50")
    form.locator("button[type=submit]").click()
    expect(page.locator("#surgery-msg")).to_contain_text("术中记录已提交")

    request_id = surgery_mobile_seed["request"]["id"]
    record = surgery_mobile_seed["read"](f"/api/surgery/requests/{request_id}/record")
    assert record["outcome"] == "未愈" and record["postop_diagnosis"] == "腹股沟斜疝", record
    # P2-179：麻醉方式与切口等级原先不送、恒记成全麻 II 类；现在缺省带出申请时填的（椎管内、I 类）
    assert (record["anesthesia_type"], record["incision_level"]) == ("spinal", "I"), record
    assert (record["start_at"], record["end_at"]) == ("2099-09-02 13:00", "2099-09-02 14:00"), record   # 修前两项都是空串
    # P2-1307：术者、助手原先不送——术者恒为申请人「E2E外科医生」、助手恒空
    assert (record["surgeon_name"], record["assistants"]) == ("E2E移动端主刀", "E2E移动端一助"), record


@pytest.fixture(scope="session")
def critical_mobile_seed(base_url, seed):
    """医生移动端出报告 / 危急值处置的前置：一张待出报告的检验申请单。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    doctor = call("/api/auth/login", {"username": "e2e_doctor", "password": "passw0rd1"})["access_token"]
    req = call("/api/exams", {"patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"],
                              "center_type": "lab", "item_code": "E2E-M-K",
                              "item_name": "E2E移动端血钾测定"}, doctor)
    return {"request": req, "read": lambda path: call(path, None, admin)}


def test_医生移动端出报告与危急值处置都在卡片内表单里填_取消即放弃(page, base_url, critical_mobile_seed):
    """P2-38：移动端出报告原先是"结论输入框 + confirm「确定=危急值」"——想放弃时点取消，报告照样出、
    还被记成**非危急值**；危急值"处置反馈"原先点取消照样提交，危急值就此闭环、处置措施为空。
    换成卡片内表单后取消就是放弃（按接口核对状态没动），危急值是显式选择。"""
    read = critical_mobile_seed["read"]
    request_id = critical_mobile_seed["request"]["id"]
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()

    page.click('a.tab-btn[data-tab="exam"]')
    card = page.locator(".m-card", has_text="E2E移动端血钾测定")
    card.locator("button[data-report]").first.click()
    form = card.locator("form.exam-report-form")
    form.locator("button[data-cancel]").click()
    expect(form).to_have_count(0)
    pending = [r["id"] for r in read("/api/exams?status=pending")]
    assert request_id in pending, "取消之后申请单却不在待出报告里了"
    card.locator("button[data-report]").first.click()
    form.locator("textarea[name=conclusion]").fill("E2E移动端血钾 6.9mmol/L")
    form.locator("textarea[name=finding]").fill("已排除溶血")
    form.locator("select[name=critical]").select_option("1")
    form.locator("button[type=submit]").click()
    expect(page.locator("#exam-msg")).to_contain_text("危急值已通知申请机构")

    page.click('a.tab-btn[data-tab="critical"]')
    crit = page.locator(".m-card", has_text="E2E移动端血钾 6.9mmol/L")
    crit.locator("button[data-ack]").click()
    expect(page.locator("#critical-msg")).to_contain_text("已确认接收")
    crit.locator("button[data-resolve]").click()
    resolve_form = crit.locator("form.crit-resolve-form")
    resolve_form.locator("button[data-cancel]").click()
    expect(resolve_form).to_have_count(0)
    (report,) = [r for r in read("/api/exams/critical") if r["conclusion"] == "E2E移动端血钾 6.9mmol/L"]
    assert report["critical_status"] == "acknowledged", report
    crit.locator("button[data-resolve]").click()
    resolve_form.locator("textarea[name=note]").fill("已电话通知患者返院复查")
    resolve_form.locator("button[type=submit]").click()
    expect(page.locator("#critical-msg")).to_contain_text("危急值闭环完成")
    actions = [a["action"] for a in read(f"/api/exams/reports/{report['id']}/critical-actions")]
    assert "处置反馈：已电话通知患者返院复查" in actions, actions


def test_医生移动端卡片内表单提交失败_报错写在这张卡的表单里(page, base_url, seed, admin_call):
    """P2-1093（第三十一批 C3-4）：医生移动端的卡片内表单（出报告、危急值处置、转诊审核 / 撤回 / 发起）失败时，报错原先写到
    整页消息行——手机上列表 30～40 张卡片，消息行在屏幕外，卡片上什么变化都没有。修后写进这张卡的表单里，填的都在。"""
    req = admin_call("POST", "/api/exams", {"patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"],
                                            "center_type": "lab", "item_code": "E2E-P21093", "item_name": "E2E卡片内报错"})
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    page.click('a.tab-btn[data-tab="exam"]')
    card = page.locator(".m-card", has_text="E2E卡片内报错")
    card.locator("button[data-report]").first.click()
    form = card.locator("form.exam-report-form")
    admin_call("POST", f"/api/exams/{req['id']}/report", {   # 表单开着的时候别人先出了报告
        "finding": "", "conclusion": "E2E 别人先出的", "critical": False, "reported_by": "检验科"})
    form.locator("textarea[name=conclusion]").fill("E2E 我写的结论")
    form.locator("button[type=submit]").click()
    expect(form.locator("[data-card-msg]")).not_to_have_text("")   # 修前报错落到整页消息行，这里没有
    expect(form.locator("textarea[name=conclusion]")).to_have_value("E2E 我写的结论")


def test_医生移动端换人登录_不留上一位速查的档案_顶部写的是这一位(page, base_url, seed, admin_call):
    """P2-1218（第三十五批扫描 T1-3）：医生移动端退出原先只清 sessionStorage、藏起工作台——换人登录落回 hash 指的页签，
    「速查」进页不取数，后一位看到的就是前一位查的患者档案（姓名、诊断、危急值；自己查是 403），卡号框里还是那个卡号，
    顶部 #who 还写着前一位。修后退出即把工作台清回刚载入的样子、页签复位到待办；会话失效（api() 的 401 分支）同样清。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E换人乙镇卫生院", "org_type": "township", "level": "township"})
    admin_call("POST", "/api/users", {"username": "e2e_p21218_doc", "password": "passw0rd1", "role": "doctor",
                                      "full_name": "E2E乙镇医生", "org_id": org["id"]})
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    page.click('a.tab-btn[data-tab="patient"]')
    page.fill("#pt-ehc", seed["patient"]["ehc_no"])
    page.click("#pt-form button[type=submit]")
    expect(page.locator("#pt-result")).to_contain_text("E2E患者")
    page.click("#btn-logout")
    expect(page.locator("#login-page")).to_be_visible()
    page.fill("#lg-user", "e2e_p21218_doc")
    page.fill("#lg-pass", "passw0rd1")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    expect(page.locator("#who")).to_contain_text("e2e_p21218_doc")   # 修前一直写着 admin
    expect(page.locator("#tab-todo")).to_be_visible()   # 落在待办，进页即取数
    expect(page.locator("#pt-result")).to_have_text("")   # 修前是前一位查的档案
    expect(page.locator("#pt-ehc")).to_have_value("")
    # 会话失效：速查框里填了卡号，Cookie 没了，点待办 → 401 → 回登录页，同样不留
    page.click('a.tab-btn[data-tab="patient"]')
    page.fill("#pt-ehc", seed["patient"]["ehc_no"])
    page.context.clear_cookies()
    page.click('a.tab-btn[data-tab="todo"]')
    expect(page.locator("#login-page")).to_be_visible()
    expect(page.locator("#pt-ehc")).to_have_value("")
    expect(page.locator("#who")).to_have_text("")


def test_校验失败的报错是人话而不是object_Object(page, base_url):
    """P2-39：请求体校验失败的 422，`detail` 是数组；三端请求帮手原先直接
    `new Error(data.detail)`，页面上显示的是 "[object Object]"。

    在真浏览器里验：共用的 `errorText` 在三端页面上都在、格式对；管理端的 `api()`
    打一个真请求（随访的 `due_date` 只写了 8 个字符，过不了长度校验），
    抛出来的错误是一句带字段名的话。
    """
    _login(page, base_url)
    text = page.evaluate(
        "() => errorText([{loc: ['body', 'voucher_date'],"
        " msg: 'Value error, 日期 2026-02-31 不存在（请检查月份天数）'}], '兜底')"
    )
    assert text == "voucher_date：日期 2026-02-31 不存在（请检查月份天数）"
    message = page.evaluate(
        """async () => {
          try {
            await api('/api/followups', {method: 'POST', body: JSON.stringify(
              {patient_id: 1, org_id: 1, category: 'chronic', due_date: '2026-9-1'})});
            return 'no error';
          } catch (e) { return e.message; }
        }"""
    )
    assert "[object Object]" not in message, message
    assert message.startswith("due_date："), message

    # P1-109：必填文本只填了空格，pydantic 原话是 "String should match pattern '\S'"——认出来换成人话
    blank = page.evaluate(
        """async () => {
          try {
            await api('/api/organizations', {method: 'POST', body: JSON.stringify(
              {name: '\\u3000\\u3000', org_type: 'township', level: 'township'})});
            return 'no error';
          } catch (e) { return e.message; }
        }"""
    )
    assert blank == "name：不能只填空格", blank
    empty = page.evaluate(
        "() => errorText([{type: 'string_too_short', loc: ['body', 'items', 0, 'drug_code'],"
        " msg: 'String should have at least 1 character', ctx: {min_length: 1}}], '兜底')"
    )
    assert empty == "items.0.drug_code：不能为空", empty  # P1-110：必填留空

    # 居民端与医生端各自的 api() 也改成了走它：页面上确实加载到了这个函数
    for path in ("/m/", "/m/doctor"):
        page.goto(f"{base_url}{path}")
        assert page.evaluate("() => typeof errorText") == "function", path


def test_页内表单的数字框收得了小数_以DRG调权为例(page, base_url):
    """P1-67：`spdModal` 的数字框此前不带 `step`，浏览器按默认步长 1 校验——1.25、12.80、6.1
    一律提交不了，只弹一句"两个最接近的有效值分别为 1 和 2"。后端收小数的十个字段（DRG 基准
    权重、收费调价、会诊计费、基金池总额与预付比例、慢专病管理目标上下限、服务包价格、考核指标
    权重、课程考核得分）从界面都只能填整数。以调权为例：种子里的基准权重本身全是小数
    （BR23 是 1.35），调成 1.25 要能提交、目录里要看得见。"""
    _login(page, base_url)
    _open_page(page, "drgs", "DRGs分析")
    row = page.locator("tr:has(button[data-drg-weight])", has_text="BR23")
    row.locator("button[data-drg-weight]").click()
    # 判据自证：确实是数字框——换成文本框这条用例照样会绿，却什么也没验
    expect(page.locator('form.panel input[name="base_weight"]')).to_have_attribute("type", "number")
    _spd_modal(page, {"base_weight": "1.25"})  # 修复前：浏览器拦下提交，遮罩不走，这里超时
    expect(page.locator("tr:has(button[data-drg-weight])", has_text="BR23")).to_contain_text("1.25")


def test_DRG分组目录能在界面上增补(page, base_url, admin_read):
    """P2-93（动词级孤儿）：分组目录原先只能调权——增补分组的接口（仅管理员）一直在，界面上没有入口，
    县里要加一个本地常见的分组只能靠接口调用方。"""
    _login(page, base_url)
    _open_page(page, "drgs", "DRGs分析")
    form = page.locator("#drg-group-form")
    form.locator('[name="code"]').fill("E2EZ9")
    form.locator('[name="name"]').fill("E2E 鼻息肉摘除组")
    form.locator('[name="base_weight"]').fill("0.85")   # 小数要提交得了（P1-67 同一口径）
    form.locator('[name="keywords"]').fill("鼻息肉,鼻窦炎")
    form.locator('[name="procedure_keywords"]').fill("息肉摘除")
    form.locator('[name="require_procedure"]').check()
    _submit(page, "#drg-group-form button")
    created = next(g for g in admin_read("/api/drgs/groups") if g["code"] == "E2EZ9")
    assert (created["name"], created["base_weight"], created["keywords"], created["require_procedure"]) == (
        "E2E 鼻息肉摘除组", 0.85, "鼻息肉,鼻窦炎", True), created
    expect(page.locator("tr:has(button[data-drg-weight])", has_text="E2EZ9")).to_contain_text("必须")


def test_DRG分组目录能编辑关键词与停用_预检随之变(page, base_url, admin_read, admin_call):
    """P2-1535（第四十五批扫描 AI4-6）：分组目录原先只给「调权」——关键词、主手术关键词、必须命中主手术、MDC、名称、启停都
    没有入口，建错的组改不了也停不掉、继续吃病例。现在普通组的行上有「编辑」（只送改了的项）与「停用 / 启用」。"""
    group = admin_call("POST", "/api/drgs/groups", {"code": "E2EZ8", "name": "E2E 编辑试验组", "base_weight": 1.05,
                                                    "keywords": "E2E甲病"})
    _login(page, base_url)
    _open_page(page, "drgs", "DRGs分析")

    def row():
        return page.locator("tr:has(button[data-drg-weight])", has_text="E2EZ8")

    def pre_check(diagnosis):
        page.fill('#drg-pre-form [name="diagnosis"]', diagnosis)
        page.click("#drg-pre-form button")

    row().locator("button[data-drg-edit]").click()
    expect(_modal(page).locator('[name="keywords"]')).to_have_value("E2E甲病")   # 预填原值
    _redrawn(page, lambda: _spd_modal(page, {"keywords": "E2E乙病"}))
    saved = next(g for g in admin_read("/api/drgs/groups") if g["id"] == group["id"])
    assert (saved["keywords"], saved["name"], saved["active"]) == ("E2E乙病", "E2E 编辑试验组", True), saved
    expect(row()).to_contain_text("E2E乙病")
    pre_check("E2E乙病")
    expect(page.locator("#drg-pre")).to_contain_text("E2EZ8")   # 按新词命中

    _redrawn(page, lambda: row().locator("button[data-drg-toggle]").click())
    expect(row().locator("button[data-drg-toggle]")).to_have_text("启用")
    assert next(g for g in admin_read("/api/drgs/groups") if g["id"] == group["id"])["active"] is False
    pre_check("E2E乙病")
    expect(page.locator("#drg-pre")).to_contain_text("未匹配到任何分组")   # 停用的组不再命中
    # 兜底组不摆编辑与停用（入组按编码取、不看启停），调权照旧
    fallback = page.locator("tr:has(button[data-drg-weight])", has_text="QY")
    expect(fallback).to_have_count(1)
    expect(fallback.locator("button[data-drg-edit], button[data-drg-toggle]")).to_have_count(0)


def test_DRG页对医生照样打得开_事前提示够得着_不摆调权(page, base_url, seed):
    """P2-459：机构 CMI 等统计只给管理层，原先与分组目录放在同一个 Promise.all 里——医生打开 DRGs 分析页，统计一个 403
    整页报错，同页给一线的事中预警（在院病例住院日超标）、事前提示（按拟诊断预判入组）都够不着；管理员才能点的「调权」
    又对所有人摆着。修后统计取不到就在原位说为什么，其余照常；调权只给管理员。"""
    _login(page, base_url, "e2e_doctor", "passw0rd1")
    _open_page(page, "drgs", "DRGs分析")
    expect(page.locator("#page-body")).to_contain_text("需要以下角色之一：管理层")   # 修前整页只剩一句错误
    expect(page.locator("#drg-alert-form")).to_be_visible()
    expect(page.locator("button[data-drg-weight]")).to_have_count(0)
    page.fill('#drg-pre-form [name="diagnosis"]', "社区获得性肺炎")
    page.click("#drg-pre-form button")
    expect(page.locator("#drg-pre")).to_contain_text("ES31")


#: 经办在入库登记里自由填写的条码：带一个双引号就能越出属性、往页面里塞标签
XSS_BARCODE = 'XSS"><img src=x onerror="window.__xss=1">'


@pytest.fixture(scope="session")
def xss_seed(base_url, seed):
    """两件在库耗材：一件条码是注入探针，一件条码带 `#`（进 URL 路径不编码就被当成片段截掉）。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    org_id = seed["org"]["id"]
    post("/api/materials/consumables", {"barcode": XSS_BARCODE, "name": "E2E转义探针", "org_id": org_id}, token)
    post("/api/materials/consumables", {"barcode": "E2E#HV-2", "name": "E2E井号条码", "org_id": org_id}, token)


def test_耗材条码原样回到按钮上_不再被当成标记(page, base_url, xss_seed):
    """P0-18：物资页「使用登记」按钮把条码裸插进 `data-use` 属性——条码是经办在入库登记里
    自由填写的，打开这一页的院长、管理员都会执行它塞进来的东西。转义之后：页面里没有被注入的
    元素，按钮上读回的条码与登记时一字不差（转义只作用于标记层，dataset 取到的仍是原值）。
    条码进 URL 路径同样要编码：`E2E#HV-2` 不编码，`#` 之后被当成片段截掉，按半截条码查成 404。"""
    _login(page, base_url)
    _open_page(page, "materials", "物资采购与耗材")
    expect(page.locator("#page-body")).to_contain_text("E2E转义探针")
    expect(page.locator("#page-body img")).to_have_count(0)  # 修复前：按钮里多出一个 <img>
    assert page.evaluate("() => window.__xss") is None
    assert page.evaluate(
        "(bc) => [...document.querySelectorAll('button[data-use]')].some((b) => b.dataset.use === bc)",
        XSS_BARCODE,
    )

    page.fill("#trace-form input[name=barcode]", "E2E#HV-2")
    page.click("#trace-form button")
    expect(page.locator("#trace-result")).to_contain_text("E2E井号条码")


def test_会计页存下的期间被拒时回落本月_切换框先验再存(page, base_url):
    """P1-62：三个报表口径的 `period` 收严之后，localStorage 里早先存下的坏值
    （切换框是自由文本）会让整页那个 Promise.all 失败——而切换框画在它之后，
    不兜底就是一张连改正入口都没有的白页。只对 422 回落本月并清掉坏值；
    切换时先让后端判合不合法，坏值不存、报人话。"""
    _login(page, base_url)
    page.evaluate("() => localStorage.setItem('medplat_acc_period', '2026-9')")
    _open_page(page, "accounting", "会计核算")
    this_month = page.evaluate("() => new Date().toISOString().slice(0, 7)")
    expect(page.locator('#acc-period input[name="period"]')).to_have_value(this_month)
    assert page.evaluate("() => localStorage.getItem('medplat_acc_period')") is None

    page.fill('#acc-period input[name="period"]', "2026/09")
    page.click("#acc-period button")
    expect(page.locator("#acc-period-msg")).to_contain_text("period")
    assert page.evaluate("() => localStorage.getItem('medplat_acc_period')") is None


def test_凭证作废在页内表单里填原因_明细看得到作废留痕(page, base_url, seed, admin_read):
    """P2-522：作废原先 `confirm()` 一下就翻状态，谁作废的、为什么都不留；换成页内表单写明原因，取消即不作废，
    明细里看得到作废人、时间与原因。"""
    import json
    from datetime import datetime, timezone
    from urllib.request import Request

    def call(path, payload, token=""):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    today = datetime.now(timezone.utc).date().isoformat()   # 会计页默认看本月（与页面同样取 UTC 的年月）
    voucher = call("/api/accounting/vouchers", {
        "org_id": seed["org"]["id"], "voucher_no": "E2E-P2522", "voucher_date": today, "summary": "E2E 作废留痕",
        "entries": [{"subject_code": "1001", "debit": 50}, {"subject_code": "4004", "credit": 50}]}, admin)
    call(f"/api/accounting/vouchers/{voucher['id']}/post", {}, admin)

    def status():
        return admin_read(f"/api/accounting/vouchers/{voucher['id']}")

    _login(page, base_url)
    _open_page(page, "accounting", "会计核算")
    page.click(f'button[data-void="{voucher["id"]}"]')
    expect(_modal(page)).to_contain_text("不能恢复")
    _cancel_modal(page)
    assert status()["status"] == "posted", "点了取消却照样作废了"
    page.click(f'button[data-void="{voucher["id"]}"]')
    _redrawn(page, lambda: _spd_modal(page, {"reason": "E2E 科目记错"}))
    trail = status()
    assert (trail["status"], trail["void_reason"]) == ("void", "E2E 科目记错"), trail
    page.click(f'button[data-detail="{voucher["id"]}"]')
    expect(page.locator("#voucher-detail-body")).to_contain_text("E2E 科目记错")


def test_会计科目能在界面上增建_凭证分录即可选用(page, base_url, admin_read):
    """P2-93（动词级孤儿）：科目原先只在凭证分录的下拉里出现、页面上建不了——种子只放一级科目，docstring 写着
    「明细科目交由管理员按需增建」，建科目的接口（仅管理员）却没有入口。"""
    _login(page, base_url)
    _open_page(page, "accounting", "会计核算")
    form = page.locator("#acc-subject-form")
    form.locator('[name="code"]').fill("100299")
    form.locator('[name="name"]').fill("E2E 银行存款·医保专户")
    form.locator('[name="category"]').select_option("asset")
    form.locator('[name="direction"]').select_option("debit")
    _submit(page, "#acc-subject-form button")
    created = next(s for s in admin_read("/api/accounting/subjects") if s["code"] == "100299")
    assert (created["name"], created["category"], created["direction"]) == ("E2E 银行存款·医保专户", "asset", "debit")
    # 凭证分录的科目下拉即可选用（每行一个下拉，看第一行）
    expect(page.locator(".e-subject").first.locator('option[value="100299"]')).to_have_count(1)


def test_会计页凭证行写明机构_按机构筛凭证与试算平衡(page, base_url, seed, admin_call):
    """P2-1438：会计页给全域账号看的是全县各家的凭证，原先哪里都不写是哪家（两家的同号凭证分不清），试算平衡也只看得到
    全县合计。修后凭证清单有「机构」列；在「会计期间与机构」里选一家、点切换，凭证清单与试算平衡表只看这一家，重画后
    下拉仍选着它。"""
    _login(page, base_url)
    today = page.evaluate("() => localToday()")   # 会计页默认看本月（本地日历，与页面同一个取法）
    other = admin_call("POST", "/api/organizations", {"name": "E2E乙卫生院P21438", "org_type": "township",
                                                      "level": "township"})
    made = {}
    for org_id, amount in ((seed["org"]["id"], 1000), (other["id"], 20)):   # 两家同一个凭证号
        voucher = admin_call("POST", "/api/accounting/vouchers", {
            "org_id": org_id, "voucher_no": "E2E-P21438", "voucher_date": today, "summary": "E2E 两家同号",
            "entries": [{"subject_code": "1001", "debit": amount}, {"subject_code": "4004", "credit": amount}]})
        admin_call("POST", f"/api/accounting/vouchers/{voucher['id']}/post", {})
        made[org_id] = voucher["id"]
    _open_page(page, "accounting", "会计核算")
    for org_id, name in ((seed["org"]["id"], seed["org"]["name"]), (other["id"], "E2E乙卫生院P21438")):
        expect(page.locator(f'tr:has(button[data-detail="{made[org_id]}"]) td').nth(2)).to_have_text(name)
    page.locator('#acc-period select[name="org_id"]').select_option(str(other["id"]))
    _redrawn(page, lambda: page.click("#acc-period button"))
    expect(page.locator(f'button[data-detail="{made[other["id"]]}"]')).to_be_visible()
    expect(page.locator(f'button[data-detail="{made[seed["org"]["id"]]}"]')).to_have_count(0)
    expect(page.locator("#page-body h3", has_text="试算平衡表（E2E乙卫生院P21438，仅统计已过账）")).to_have_count(1)
    expect(page.locator('#acc-period select[name="org_id"]')).to_have_value(str(other["id"]))


def test_成本页存下的期间被拒时回落本月_切换框先验再存(page, base_url):
    """与会计页同一个坑（P1-62 修了会计页，成本页是 P1-61 收 `CostIn.period` 时查出来的）：
    切换框是自由文本、存进 localStorage 不校验，存下 `2026/09` 之后整页那个 Promise.all 422，
    切换框又画在它之后——一张连改正入口都没有的白页。"""
    _login(page, base_url)
    page.evaluate("() => localStorage.setItem('medplat_cost_period', '2026/09')")
    _open_page(page, "cost", "成本核算")
    this_month = page.evaluate("() => new Date().toISOString().slice(0, 7)")
    expect(page.locator('#cost-period input[name="period"]')).to_have_value(this_month)
    assert page.evaluate("() => localStorage.getItem('medplat_cost_period')") is None

    page.fill('#cost-period input[name="period"]', "2026/09")
    page.click("#cost-period button")
    expect(page.locator("#cost-period-msg")).to_contain_text("period")
    assert page.evaluate("() => localStorage.getItem('medplat_cost_period')") is None


def test_决策指标页存下的期间被拒时回落本月_切换框先验再存(page, base_url):
    """与会计 / 成本页同一个坑（P1-62），本页漏了（P2-586）：存下 `2026`（绩效考核页的「周期 YYYY 或 YYYY-MM」
    收它）之后整页只剩一行报错、切换框画不出来——每次进来都是这样，只能清站点数据。"""
    _login(page, base_url)
    page.evaluate("() => localStorage.setItem('medplat_ana_period', '2026')")
    _open_page(page, "analytics", "决策指标扩展")
    this_month = page.evaluate("() => localToday().slice(0, 7)")
    expect(page.locator('#ana-period input[name="period"]')).to_have_value(this_month)
    assert page.evaluate("() => localStorage.getItem('medplat_ana_period')") is None

    page.fill('#ana-period input[name="period"]', "2026/09")
    page.click("#ana-period button")
    expect(page.locator("#ana-period-msg")).to_contain_text("period")
    assert page.evaluate("() => localStorage.getItem('medplat_ana_period')") is None


@pytest.fixture(scope="session")
def cost_rule_seed(base_url, seed):
    """成本分摊规则改删的前置：后勤 + 内科两个科室，后勤分给内科 60%（P1-116）。

    科室建在**自己的机构**里、不挂共用的 seed 机构：人财物页的「挂科室」下拉只列员工所在机构的科室，
    那条用例靠「本机构只有一个科室、默认即选中」——往 seed 机构里多塞两个科室，它就挂错科室（第三十轮实测）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    org_id = call("/api/organizations", {"name": "E2E成本分摊院", "org_type": "lead_hospital", "level": "county"},
                  admin)["id"]
    hq = call("/api/mgmt/departments", {"org_id": org_id, "code": "E2E-CST-HQ", "name": "E2E分摊后勤",
                                        "category": "admin"}, admin)
    nk = call("/api/mgmt/departments", {"org_id": org_id, "code": "E2E-CST-NK", "name": "E2E分摊内科"}, admin)
    rule = call("/api/cost/allocation-rules", {"from_dept_id": hq["id"], "to_dept_id": nk["id"], "ratio_pct": 60},
                admin)
    return {"rule": rule, "read": lambda path: call(path, None, admin)}


def test_成本分摊规则改比例走页内表单_删除先确认取消即不删(page, base_url, cost_rule_seed):
    """P1-116：分摊规则原先只能建、不能改也不能删——比例或科室填错就每一期都照错的分。
    改比例走页内表单；删除先确认，点取消规则还在，确认后才删（按接口核对）。"""
    rule_id = cost_rule_seed["rule"]["id"]

    def rule():
        rows = cost_rule_seed["read"]("/api/cost/allocation-rules")
        return next((r for r in rows if r["id"] == rule_id), None)

    _login(page, base_url)
    _open_page(page, "cost", "成本核算")
    page.click(f'button[data-alloc-edit="{rule_id}"]')
    _redrawn(page, lambda: _spd_modal(page, {"ratio_pct": "45.5"}))
    assert rule()["ratio_pct"] == 45.5

    page.once("dialog", lambda dialog: dialog.dismiss())
    page.click(f'button[data-alloc-del="{rule_id}"]')
    expect(page.locator(f'button[data-alloc-del="{rule_id}"]')).to_be_visible()
    assert rule() is not None, "点了取消却照样删了规则"

    page.once("dialog", lambda dialog: dialog.accept())
    _redrawn(page, lambda: page.click(f'button[data-alloc-del="{rule_id}"]'))
    expect(page.locator(f'button[data-alloc-del="{rule_id}"]')).to_have_count(0)
    assert rule() is None


@pytest.fixture(scope="session")
def maternal_seed(base_url):
    """妇幼页用例的前置数据：一位孕妇（UI 只驱动建册之后的动作，与本文件约定一致）。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    return post("/api/patients",
                {"name": "E2E孕妇", "id_card": "320981199505052226", "gender": "女"}, token)


def test_妇幼页的访视分娩新筛都在页内表单里录入(page, base_url, seed, maternal_seed):
    """P2-38：孕产妇页原先靠浏览器原生弹窗连问录入（访视三连、分娩四连、新筛输序号再加
    确认框），录不了多字段、输错没有提示。换成页内表单后顺带补上了孕周、新生儿数、儿童
    身高体重等一直没入口的字段。主链路：建册 → 产检（收缩压 150 自动标高危）→ 分娩登记
    （机构从下拉里选）→ 儿童建档 → 儿童访视（体重写错报人话、写对照收）→ 新筛异常进高危儿清单。"""
    _login(page, base_url)
    _open_page(page, "maternal", "妇幼保健")
    page.fill("#mat-form input[name=patient_id]", str(maternal_seed["id"]))
    _submit(page, "#mat-form button")

    # 两个模态框都等整页重画完（`_redrawn`）：页面上别的档案也可能是「已分娩」（P2-594 的用例留下的），只等这几个字
    # 出现，写请求还在路上就去填儿童建档表单，填进了马上要被重画丢弃的那份 DOM（第九十五轮全量端到端实测）
    page.click("button[data-visit]")
    _redrawn(page, lambda: _spd_modal(page, {"visit_type": "prenatal", "gest_week": "30", "bp": "150/95",
                                             "visit_date": "2026-09-20"}))
    expect(page.locator("#page-body")).to_contain_text("妊娠期高血压可能")

    page.click("button[data-delivery]")
    _redrawn(page, lambda: _spd_modal(page, {"org_id": str(seed["org"]["id"]), "delivery_date": "2026-09-22",
                                             "delivery_mode": "cesarean", "newborn_count": "2"}))
    expect(page.locator("#page-body")).to_contain_text("已分娩")

    page.fill("#child-form input[name=name]", "E2E新生儿")
    page.fill("#child-form input[name=birth_date]", "2026-09-22")
    _submit(page, "#child-form button")

    # 体重写成中文：此前 Number() 得到 NaN、序列化成 null，值被悄悄丢掉也不报错。写错时框不关（P2-607）：报错写在框里、
    # 已填的身长与备注都还在，只改体重再交——原先框一关、报错落到页面消息行，要重新点开从头再填
    page.click("button[data-cvisit]")
    form = _spd_modal_rejected(page, {"visit_type": "newborn", "height_cm": "50.5", "weight_kg": "三点五",
                                      "note": "E2E 新生儿访视备注"})
    expect(form.locator("[data-modal-msg]")).to_contain_text("weight_kg")
    expect(form.locator('[name="height_cm"]')).to_have_value("50.5")
    expect(form.locator('[name="note"]')).to_have_value("E2E 新生儿访视备注")
    _spd_modal(page, {"weight_kg": "3.4"})
    expect(page.locator("#mat-msg")).to_have_text("")  # 成功即整页重画，报错行清空

    page.click("button[data-screen]")
    _spd_modal(page, {"item": "hearing", "result": "abnormal", "screen_date": "2026-09-23"})
    expect(page.locator("#page-body")).to_contain_text("高危儿专案清单")
    expect(page.locator("#page-body")).to_contain_text("听力筛查阳性/可疑")


def test_人财物页的挂科室_变动_合同_出入库都在页内表单里录入(page, base_url, seed):
    """P2-38：人财物页原先手输科室 ID、变动输序号再三连问、签合同三连问、出入库输序号再两连问。
    换成页内表单后，科室只列员工所在机构的（后端本就只收同机构科室）、调入机构从下拉里选；
    规则不在前端另抄——调动没选机构、合同止期写错，都由后端报人话。"""
    from datetime import date, timedelta

    org_id = str(seed["org"]["id"])
    _login(page, base_url)
    _open_page(page, "hrfinance", "人财物管理")
    page.fill("#dept-form input[name=org_id]", org_id)
    page.fill("#dept-form input[name=code]", "E2E-NK")
    page.fill("#dept-form input[name=name]", "E2E内科")
    _submit(page, "#dept-form button")
    page.fill("#emp-form input[name=org_id]", org_id)
    page.fill("#emp-form input[name=name]", "E2E员工甲")
    _submit(page, "#emp-form button")
    row = page.locator("tr", has_text="E2E员工甲")

    row.locator("button[data-empdept]").click()
    _spd_modal(page, {})  # 下拉只列本机构科室，只有一个，默认即选中
    expect(page.locator("tr", has_text="E2E员工甲")).to_contain_text("E2E内科")

    page.locator("tr", has_text="E2E员工甲").locator("button[data-empchg]").click()
    # P2-607 第十一批：框自己提交——调动却没选调入机构，报错写在框里、框不关，写好的说明还在（原先报在页面消息行、框已关）
    form = _spd_modal_rejected(page, {"change_type": "transfer", "detail": "试用期满"})
    expect(form.locator("[data-modal-msg]")).to_contain_text("调动须指定调入机构")
    expect(form.locator('[name="detail"]')).to_have_value("试用期满")
    _redrawn(page, lambda: _spd_modal(page, {"change_type": "regularize", "effective_date": "2026-09-24"}))
    expect(page.locator("#hrf-msg")).to_have_text("")
    page.locator("tr", has_text="E2E员工甲").locator("button[data-emphist]").click()
    expect(page.locator("#empchg-list")).to_contain_text("转正")
    expect(page.locator("#empchg-list")).to_contain_text("2026-09-24")

    page.locator("tr", has_text="E2E员工甲").locator("button[data-empct]").click()
    _spd_modal(page, {"contract_no": "E2E-HT-1", "start_date": "2026-01-01", "end_date": "2026-02-31"})
    expect(page.locator("#hrf-msg")).to_contain_text("end_date")
    end = (date.today() + timedelta(days=30)).isoformat()  # 落进 60 天到期提醒，才看得见落了库
    page.locator("tr", has_text="E2E员工甲").locator("button[data-empct]").click()
    _spd_modal(page, {"contract_no": "E2E-HT-1", "start_date": "2026-01-01", "end_date": end})
    expect(page.locator("#page-body")).to_contain_text("E2E-HT-1")

    page.fill("#asset-form input[name=org_id]", org_id)
    page.fill("#asset-form input[name=code]", "E2E-ZC-1")
    page.fill("#asset-form input[name=name]", "E2E打印机")
    page.fill("#asset-form input[name=quantity]", "3")
    _submit(page, "#asset-form button")
    page.locator("tr", has_text="E2E打印机").locator("button[data-assetmv]").click()
    # P2-607 第十一批：框自己提交——领用超过现存量（3 件领 5 件）报错写在框里、框不关，写好的备注还在
    form = _spd_modal_rejected(page, {"movement_type": "issue", "quantity": "5", "note": "门诊领用"})
    expect(form.locator('[name="note"]')).to_have_value("门诊领用")
    # 等整页重画完再点「记录」：提示行此前就是空的，`to_have_text("")` 当场就过，挡不住随后那次 route()
    # 把刚打开的出入库记录面板重画成空（CI run 703 实测，本地碰巧绿）
    _redrawn(page, lambda: _spd_modal(page, {"quantity": "2"}))
    expect(page.locator("#hrf-msg")).to_have_text("")
    page.locator("tr", has_text="E2E打印机").locator("button[data-assethist]").click()
    expect(page.locator("#assetmv-list")).to_contain_text("门诊领用")


def test_公文起草带正文_公文表里展开看得到(page, base_url):
    """P2-1595：起草表单原先只有标题、类型、发文单位三栏，公文表不显示正文——从页面起草的公文只有一行标题。
    正文框在起草表单里、真浏览器的 FormData 收得到多行框；公文表在标题下折叠，展开后照原样换行、标签按字面显示。"""
    text = "第一条：<b>照常</b>\n第二条：另行通知"
    _login(page, base_url)
    _open_page(page, "oaqc", "行政与质控")
    page.fill("#doc-form input[name=title]", "E2E公文带正文")
    page.fill("#doc-form textarea[name=body]", text)
    _submit(page, "#doc-form button")
    row = page.locator("tr", has_text="E2E公文带正文")
    shown = row.locator("details div")
    expect(shown).to_be_hidden()   # 折叠着，不撑高公文表
    row.locator("summary").click()
    expect(shown).to_be_visible()
    assert shown.evaluate("el => el.textContent") == text   # 换行照原样、标签按字面（经 esc）


@pytest.fixture(scope="session")
def materials_seed(base_url, seed):
    """物资页用例的前置数据：在用供应商、经办提出的采购申请（审批人不能是申请人）、一件在库耗材。"""
    import json
    from datetime import date, timedelta
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    org_id = seed["org"]["id"]
    supplier = post("/api/pharmacy/suppliers", {"name": "E2E医疗器械公司"}, token)
    post("/api/users", {"username": "e2e_mat_op", "password": "passw0rd1", "role": "operator",
                        "full_name": "E2E物资经办", "org_id": org_id}, token)
    op_token = post("/api/auth/login", {"username": "e2e_mat_op", "password": "passw0rd1"})["access_token"]
    purchase = post("/api/materials/purchases",
                    {"org_id": org_id, "item_name": "E2E一次性手术衣", "quantity": 10, "estimated_price": 12.5},
                    op_token)
    expire = (date.today() + timedelta(days=365)).isoformat()
    post("/api/materials/consumables",
         {"barcode": "E2E-HV-1", "name": "E2E冠脉支架", "org_id": org_id, "expire_date": expire}, token)
    return {"supplier": supplier, "purchase": purchase}


def test_物资页的签合同_验收_使用登记都在页内表单里录入(page, base_url, seed, materials_seed):
    """P2-38：物资页原先签合同三连问（手输供应商 ID）、验收两连问、使用登记两连问。换成页内表单后
    供应商从在用清单里选；验收数量默认按采购量、超量由后端报人话；合同金额是带分的小数
    （P1-67 修之前的数字框提交不了）。主链路：审批 → 签合同 → 验收（先超量被拒）→ 耗材使用登记。"""
    pid = materials_seed["purchase"]["id"]
    _login(page, base_url)
    _open_page(page, "materials", "物资采购与耗材")

    page.click(f'button[data-approve="{pid}"]')
    page.click(f'button[data-contract="{pid}"]')
    _spd_modal(page, {"supplier_id": str(materials_seed["supplier"]["id"]),
                      "contract_no": "E2E-CG-001", "contract_amount": "12345.67"})
    expect(page.locator("tr", has_text="E2E一次性手术衣")).to_contain_text("E2E-CG-001")
    amount = page.evaluate(
        "async (id) => (await api('/api/materials/purchases')).find((p) => p.id === id).contract_amount", pid)
    assert amount == 12345.67, amount

    page.click(f'button[data-receive="{pid}"]')
    # 验收数量超了：框自己提交，后端的话写在框里、框不关、备注还在（P2-607 第八批；修前框关、报错落到页面消息行）
    form = _spd_modal_rejected(page, {"received_quantity": "11", "note": "E2E到货验收"})
    expect(form.locator("[data-modal-msg]")).to_have_text("验收数量不得超过采购数量")
    expect(form.locator('[name="note"]')).to_have_value("E2E到货验收")
    _spd_modal(page, {"received_quantity": "10"})
    expect(page.locator("tr", has_text="E2E一次性手术衣")).to_contain_text("已验收")

    page.click('button[data-use="E2E-HV-1"]')
    _spd_modal(page, {"patient_id": str(seed["patient"]["id"])})  # 手术可空
    expect(page.locator("tr", has_text="E2E冠脉支架")).to_contain_text("E2E患者")


def test_物资采购申请能在页面上驳回_先确认_取消即不动(page, base_url, materials_seed, admin_call, admin_read):
    """P2-425：审批接口收 approved=false 早就置「已取消」，页面却只给「审批」——不该买的申请只能一直挂在「待审批」。
    驳回作废后不能恢复，先确认；点取消即不动。"""
    import json
    from urllib.request import Request

    op_token = admin_call("POST", "/api/auth/login", {"username": "e2e_mat_op", "password": "passw0rd1"})["access_token"]
    req = Request(f"{base_url}/api/materials/purchases", method="POST",
                  data=json.dumps({"org_id": materials_seed["purchase"]["org_id"], "item_name": "E2E不该买的仪器",
                                   "quantity": 1, "estimated_price": 9.9}).encode(),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {op_token}"})
    with urlopen(req, timeout=10) as resp:
        pid = json.loads(resp.read())["id"]

    def status():
        (row,) = [p for p in admin_read("/api/materials/purchases") if p["id"] == pid]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "materials", "物资采购与耗材")
    reject = page.locator(f'button[data-reject="{pid}"]')
    reject.click()
    expect(_modal(page)).to_contain_text("不能恢复")
    _cancel_modal(page)
    assert status() == "requested", "点了取消却照样驳回了"
    _redrawn(page, lambda: (reject.click(), _spd_modal(page, {})))
    assert status() == "cancelled"
    expect(page.locator("tr", has_text="E2E不该买的仪器")).to_contain_text("已取消")


def test_物资采购的审批按钮只给管理层(page, base_url, materials_seed, admin_call):
    """P2-425：审批接口只收管理层（管理员放行），经办原先也看得到「审批」，点下去一次 403。"""
    import json
    from urllib.request import Request

    op_token = admin_call("POST", "/api/auth/login", {"username": "e2e_mat_op", "password": "passw0rd1"})["access_token"]
    req = Request(f"{base_url}/api/materials/purchases", method="POST",
                  data=json.dumps({"org_id": materials_seed["purchase"]["org_id"], "item_name": "E2E待审批的纱布",
                                   "quantity": 2, "estimated_price": 3.5}).encode(),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {op_token}"})
    with urlopen(req, timeout=10) as resp:
        pid = json.loads(resp.read())["id"]
    _login(page, base_url, "e2e_mat_op", "passw0rd1")
    _open_page(page, "materials", "物资采购与耗材")
    expect(page.locator("tr", has_text="E2E待审批的纱布")).to_contain_text("待管理层审批")
    expect(page.locator(f'button[data-approve="{pid}"], button[data-reject="{pid}"]')).to_have_count(0)


def test_物资采购本人提的申请不摆审批按钮(page, base_url, materials_seed, admin_call):
    """P2-1506：审批接口对申请人本人 403「不得审批本人提出的采购申请」，页面原先只按状态与角色摆「审批 / 驳回」——管理员
    本人提的申请照样摆，点下去 403。清单行末尾补了 `requested_by_me`，本人的行写明「本人提出，待其他管理层审批」，别人提的照旧摆。"""
    import json
    from urllib.request import Request

    org_id = materials_seed["purchase"]["org_id"]
    mine = admin_call("POST", "/api/materials/purchases", {"org_id": org_id, "item_name": "E2E本人申请的监护仪",
                                                          "quantity": 1})
    op_token = admin_call("POST", "/api/auth/login", {"username": "e2e_mat_op", "password": "passw0rd1"})["access_token"]
    req = Request(f"{base_url}/api/materials/purchases", method="POST",
                  data=json.dumps({"org_id": org_id, "item_name": "E2E经办申请的输液泵", "quantity": 1}).encode(),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {op_token}"})
    with urlopen(req, timeout=10) as resp:
        other = json.loads(resp.read())["id"]
    _login(page, base_url)
    _open_page(page, "materials", "物资采购与耗材")
    expect(page.locator("tr", has_text="E2E本人申请的监护仪")).to_contain_text("本人提出，待其他管理层审批")
    expect(page.locator(f'button[data-approve="{mine["id"]}"], button[data-reject="{mine["id"]}"]')).to_have_count(0)
    expect(page.locator(f'button[data-approve="{other}"], button[data-reject="{other}"]')).to_have_count(2)


@pytest.fixture(scope="session")
def inpatient_seed(base_url, seed):
    """住院页用例的前置数据：一个病区两张床、一位住在第一张床上的患者（第二张空着，供转床）。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    org_id = seed["org"]["id"]
    patient = post("/api/patients", {"name": "E2E住院患者", "id_card": "320981198003033338", "gender": "男"}, token)
    ward = post("/api/inpatient/wards", {"org_id": org_id, "name": "E2E内科病区"}, token)
    bed1 = post("/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "N-01"}, token)
    bed2 = post("/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "N-02"}, token)
    admission = post("/api/inpatient/admissions",
                     {"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed1["id"],
                      "doctor_name": "E2E内科医生", "diagnosis_name": "慢性心力衰竭急性加重"}, token)
    return {"admission": admission, "bed2": bed2}


def test_住院页的转床_开医嘱_病案首页都在页内表单里录入(page, base_url, inpatient_seed):
    """P2-38 / P1-68：转床原先手输病区与床位 ID；开医嘱问完内容再弹「确定=长期，取消=临时」——
    想放弃时点取消反而开出一条临时医嘱；病案首页四连问、**从不送转归**，后端默认"好转"，
    质量指标的住院治愈好转率与死亡率取的正是它。现在转床从本机构空闲床位里选、医嘱类型下拉、
    首页转归可选，费用带分（P1-67）。"""
    adm_id = inpatient_seed["admission"]["id"]
    _login(page, base_url)
    _open_page(page, "inpatient", "住院管理")

    bed2 = inpatient_seed["bed2"]["id"]
    page.click(f'button[data-transfer="{adm_id}"]')
    _redrawn(page, lambda: _spd_modal(page, {"bed_id": str(bed2)}))
    row = page.locator("tr", has=page.locator(f'button[data-transfer="{adm_id}"]'))
    # 病区 床号、姓名（P2-1335）：原先印「E2E内科病区 / {床位主键}」、患者列只有患者 ID
    expect(row).to_contain_text("E2E内科病区 N-02")
    expect(row).to_contain_text("E2E住院患者")

    page.click(f'button[data-order="{adm_id}"]')
    _redrawn(page, lambda: _spd_modal(page, {"order_type": "temp", "content": "E2E呋塞米 20mg iv st"}))
    page.click(f'button[data-orders="{adm_id}"]')
    expect(page.locator("#inp-orders tr", has_text="E2E呋塞米 20mg iv st")).to_contain_text("临时")

    page.click(f'button[data-summary="{adm_id}"]')
    # P2-607 第十一批：框自己提交——总费用填成负数报错写在框里、框不关，填好的诊断、转归、备注都在
    form = _spd_modal_rejected(page, {"discharge_diagnosis": "E2E慢性心力衰竭", "total_cost": "-1",
                                      "drug_cost": "3000.25", "outcome": "死亡", "note": "E2E抢救无效"})
    expect(form.locator('[name="discharge_diagnosis"]')).to_have_value("E2E慢性心力衰竭")
    expect(form.locator('[name="note"]')).to_have_value("E2E抢救无效")
    _redrawn(page, lambda: _spd_modal(page, {"total_cost": "8888.5"}))
    summary = page.evaluate(
        "async (id) => await api(`/api/inpatient/admissions/${id}/case-summary`)", adm_id)
    assert (summary["outcome"], summary["total_cost"], summary["note"]) == ("死亡", 8888.5, "E2E抢救无效"), summary

    # 填过的再点「病案首页」给只读的首页（P2-617）：原先照样弹填写表单，填完点确定 409「病案首页已填写」
    page.click(f'button[data-summary="{adm_id}"]')
    modal = _modal(page)
    expect(modal).to_contain_text("已填写")
    expect(modal).to_contain_text("E2E慢性心力衰竭")
    expect(modal).to_contain_text("转归：死亡")
    expect(modal.locator("input, textarea, select")).to_have_count(0)
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)


def test_住院页出院先确认_取消仍在院_确定才出院(page, base_url, seed, admin_read, admin_call):
    """P2-1334（第三十九批扫描 AC4-2）：「出院」原先点一下就办完——停掉全部执行中医嘱、释放床位、派出院随访并通知患者、
    发出院事件，而出院撤不回（P2-753）。现在先弹确认写明后果、标「不可撤销」：先点取消，按接口核对仍在院；再点确定才出院。"""
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": seed["org"]["id"], "name": "E2E出院确认病区"})
    bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "D-01"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E出院确认患者", "id_card": "320981198104045552", "gender": "女"})
    adm = admin_call("POST", "/api/inpatient/admissions", {
        "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "社区获得性肺炎"})
    # 出院门禁：病案首页已填、没有未结清的费用
    admin_call("POST", f"/api/inpatient/admissions/{adm['id']}/case-summary",
               {"discharge_diagnosis": "社区获得性肺炎", "outcome": "治愈"})

    def status():
        (row,) = admin_read(f"/api/inpatient/admissions?patient_id={patient['id']}")
        return row["status"]

    _login(page, base_url)
    _open_page(page, "inpatient", "住院管理")
    _confirm_then(page, lambda: page.click(f'button[data-discharge="{adm["id"]}"]'), "不可撤销",
                  lambda: status() == "admitted", lambda: status() == "discharged")   # 修前一点就出院，等不到确认框


def test_住院页停医嘱先确认_取消仍执行中_确定才停止(page, base_url, seed, admin_read, admin_call):
    """P2-1695（第五十批扫描 AN4-3）：医嘱单上的「停止」原先点一下就停，而停了撤不回（只能重新开立一条）。现在先弹确认，写明
    是哪条医嘱与「停止后不可恢复」：先点取消，按接口核对仍执行中；再点确定才停止。"""
    ward = admin_call("POST", "/api/inpatient/wards", {"org_id": seed["org"]["id"], "name": "E2E停医嘱病区"})
    bed = admin_call("POST", "/api/inpatient/beds", {"ward_id": ward["id"], "bed_no": "S-01"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E停医嘱患者", "id_card": "320981198205056955", "gender": "男"})
    adm = admin_call("POST", "/api/inpatient/admissions", {
        "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "社区获得性肺炎"})
    order = admin_call("POST", "/api/inpatient/orders", {
        "admission_id": adm["id"], "order_type": "long", "content": "E2E停医嘱 头孢呋辛 1.5g ivgtt bid"})

    def status():
        (row,) = [o for o in admin_read(f"/api/inpatient/orders?admission_id={adm['id']}") if o["id"] == order["id"]]
        return row["status"]

    _login(page, base_url)
    _open_page(page, "inpatient", "住院管理")
    page.click(f'button[data-orders="{adm["id"]}"]')
    stop = page.locator(f'button[data-stop-order="{order["id"]}"]')
    expect(stop).to_be_visible()
    stop.click()
    expect(_modal(page)).to_contain_text("E2E停医嘱 头孢呋辛 1.5g ivgtt bid")   # 写明是哪一条
    expect(_modal(page)).to_contain_text("停止后不可恢复")
    _cancel_modal(page)
    assert status() == "active", "点了取消却照样停了"   # 修前一点就停，等不到确认框
    stop.click()
    _redrawn(page, lambda: _spd_modal(page, {}))
    assert status() == "stopped"


@pytest.fixture(scope="session")
def consent_seed(base_url, seed):
    """两份待签的门诊告知书：一份用来记签署，一份用来记拒签。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    common = {"patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "consent_type": "surgery",
              "content": "E2E手术风险告知正文", "doctor_name": "E2E外科医生"}
    return [post("/api/outpatient/consents", {**common, "title": f"E2E告知书{i}"}, token) for i in (1, 2)]


def test_载入就诊后开的告知书挂到这次就诊上_完整度数得到(page, base_url, admin_read, admin_call):
    """P2-161：就诊文书完整度的「告知书 / 待签署」按挂在本次就诊上的告知书数，页面开告知书的表单原先从不挂——
    两格恒为 0。载入就诊后，表单带出这次就诊的患者、默认勾上「关联本次就诊」。"""
    org = admin_call("POST", "/api/organizations",
                     {"name": "E2E告知书县医院", "org_type": "lead_hospital", "level": "county"})
    patient = admin_call("POST", "/api/patients", {"name": "E2E输液患者", "id_card": "320981199001016161"})
    encounter = admin_call("POST", "/api/encounters",
                           {"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "急性胃肠炎"})

    _login(page, base_url)
    _open_page(page, "outpatientdocs", "门急诊文书")
    page.fill("#od-pick input[name=encounter_id]", str(encounter["id"]))
    _submit(page, "#od-pick button")
    form = page.locator("#od-consent")
    expect(form.locator("input[name=patient_id]")).to_have_value(str(patient["id"]))
    form.locator("input[name=org_id]").fill(str(org["id"]))
    form.locator("input[name=title]").fill("静脉输液知情告知")
    form.locator("input[name=content]").fill("输液可能出现静脉炎、过敏反应")
    _submit(page, "#od-consent button")

    done = admin_read(f"/api/outpatient/encounters/{encounter['id']}/completeness")
    assert (done["consents_total"], done["consents_pending"]) == (1, 1), done   # 修前 (0, 0)


def test_门诊告知书的签署与拒签都在页内表单里录入_关系可选(page, base_url, consent_seed):
    """P2-38 / P1-70：签署原先要手打关系代码（self/spouse/…，打错就 422），拒签则把关系
    **写死成"本人"**——家属或委托人代为拒签的，证据上记成了患者本人拒签。现在两处都从
    本人/配偶/父母/子女/委托人里选，按接口读回核对。"""
    to_sign, to_refuse = (c["id"] for c in consent_seed)
    _login(page, base_url)
    _open_page(page, "outpatientdocs", "门急诊文书")

    page.click(f'button[data-csign="{to_sign}"]')
    _redrawn(page, lambda: _spd_modal(page, {"signer_name": "E2E配偶甲", "signer_relation": "spouse"}))
    page.click(f'button[data-crefuse="{to_refuse}"]')
    _redrawn(page, lambda: _spd_modal(page, {"signer_name": "E2E父亲乙", "signer_relation": "parent",
                                             "refuse_reason": "E2E家属要求转上级医院再议"}))

    rows = page.evaluate("async () => await api('/api/outpatient/consents?limit=50')")
    by_id = {r["id"]: r for r in rows}
    assert (by_id[to_sign]["status"], by_id[to_sign]["signer_relation"]) == ("signed", "spouse"), by_id[to_sign]
    refused = by_id[to_refuse]
    assert (refused["status"], refused["signer_relation"], refused["refuse_reason"]) == (
        "refused", "parent", "E2E家属要求转上级医院再议"), refused


@pytest.fixture(scope="session")
def improvement_seed(base_url, seed):
    """一条待整改的绩效改进任务。"""
    import json
    from datetime import date, timedelta
    from urllib.request import Request

    req = Request(f"{base_url}/api/auth/login", data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as resp:
        token = json.loads(resp.read())["access_token"]
    body = {"org_id": seed["org"]["id"], "problem": "E2E门诊处方合格率低于 90%", "owner_name": "E2E质控科",
            "due_date": (date.today() + timedelta(days=30)).isoformat()}
    req = Request(f"{base_url}/api/performance/improvements", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def test_绩效改进任务的进展_完成_退回都在页内表单里录入_取消即放弃(page, base_url, improvement_seed):
    """P2-38：四处原生弹窗原先点"取消"照样提交——"确认关闭"点了取消任务照样关、"退回"点了取消照样
    退回且没有理由。换成页内表单后取消就是放弃；主链路：登记进展 → 提交完成 → 退回（带理由）。"""
    tid = improvement_seed["id"]

    def task():
        return page.evaluate(
            "async (id) => (await api('/api/performance/improvements')).find((t) => t.id === id)", tid)

    _login(page, base_url)
    _open_page(page, "performance", "绩效考核")

    page.click(f'button[data-impprog="{tid}"]')
    page.locator("form.panel button[data-cancel]").click()
    expect(page.locator("form.panel:has(button[data-cancel])")).to_have_count(0)
    assert task()["status"] == "open"  # 取消就是放弃，没有落任何东西

    page.click(f'button[data-impprog="{tid}"]')
    form = _spd_modal_rejected(page, {"measures": "措" * 1025})   # 写超了（后端 1024 字）：框不关、写的还在（P2-607 第九批）
    expect(form.locator('[name="measures"]')).to_have_value("措" * 1025)
    assert task()["status"] == "open"
    _redrawn(page, lambda: _spd_modal(page, {"measures": "E2E已组织专项培训"}))
    page.click(f'button[data-impdone="{tid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"completion_note": "E2E整改完成，抽查复核达标"}))
    assert task()["status"] == "completed"

    page.click(f'button[data-impno="{tid}"]')
    page.locator("form.panel button[data-cancel]").click()
    expect(page.locator("form.panel:has(button[data-cancel])")).to_have_count(0)
    assert task()["status"] == "completed"  # 原先点取消照样退回

    page.click(f'button[data-impno="{tid}"]')
    form = _spd_modal_rejected(page, {"comment": "退" * 513})   # 理由写超了：框不关、没退回
    expect(form.locator('[name="comment"]')).to_have_value("退" * 513)
    assert task()["status"] == "completed"
    _redrawn(page, lambda: _spd_modal(page, {"comment": "E2E抽查样本不足，补充后再报"}))
    after = task()
    assert (after["status"], after["measures"]) == ("in_progress", "E2E已组织专项培训"), after
    # P2-462：退回理由与退回人显示在这一行上；被驳回的整改结果说明不再挂在整改中的任务上
    row = page.locator(f'tr:has(button[data-impprog="{tid}"])')
    expect(row).to_contain_text("已退回")
    expect(row).to_contain_text("E2E抽查样本不足，补充后再报")
    expect(row).to_contain_text("E2E已组织专项培训")
    expect(row).not_to_contain_text("E2E整改完成，抽查复核达标")


@pytest.fixture(scope="session")
def rx_seed(base_url, seed):
    """两张转药师审的处方（同方重复药品编码即转人工审）：一张用来审方，一张用来点评。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    body = {"patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "diagnosis_name": "E2E上呼吸道感染",
            "items": [{"drug_code": "E2E-AMX", "drug_name": "E2E阿莫西林", "daily_dose": 1.5},
                      {"drug_code": "E2E-AMX", "drug_name": "E2E阿莫西林胶囊", "daily_dose": 1.5}]}
    return [post("/api/prescriptions", body, token) for _ in range(2)]


def test_集中审方的审方与点评都在页内表单里录入_取消即放弃(page, base_url, rx_seed):
    """P2-38：审方的"药师意见"原生弹窗点取消照样提交通过/退回；点评是 alert 看要点 → confirm
    「确定=合理，取消=不合理」→ 两连问，想放弃时点取消记下的却是"不合理"，这一路没有放弃的出口。
    换成页内表单：点评要点放在表单上方照着填，取消就是放弃；不合理却什么都没写由后端报人话。"""
    to_review, to_comment = (p["id"] for p in rx_seed)

    def status(pid):
        return page.evaluate(
            "async (id) => (await api('/api/prescriptions?limit=200')).find((p) => p.id === id).status", pid)

    _login(page, base_url)
    _open_page(page, "rx", "集中审方")
    page.click(f'button[data-approve="0"][data-id="{to_review}"]')
    page.locator("form.panel button[data-cancel]").click()
    expect(page.locator("form.panel:has(button[data-cancel])")).to_have_count(0)
    assert status(to_review) == "pending_review"  # 取消就是放弃，没有退回
    page.click(f'button[data-approve="1"][data-id="{to_review}"]')
    form = _spd_modal_rejected(page, {"comment": "意" * 257})   # 药师意见写超了：框不关、写的还在（P2-607 第七批）
    expect(form.locator('[name="comment"]')).to_have_value("意" * 257)
    assert status(to_review) == "pending_review"
    _redrawn(page, lambda: _spd_modal(page, {"comment": "E2E同意，已电话确认医师用意"}))
    assert status(to_review) == "approved"

    page.click(f'button[data-rxcomment="{to_comment}"]')
    expect(page.locator("form.panel:has(button[data-cancel])")).to_contain_text("点评要点")
    # 不合理却什么都没写：框自己提交，后端的话写在框里、框不关（P2-607 第七批；修前框关、报错落到页面消息行）
    form = _spd_modal_rejected(page, {"grade": "unreasonable"})
    expect(form.locator("[data-modal-msg]")).to_have_text("不合理处方须注明问题类型或点评意见")
    _redrawn(page, lambda: _spd_modal(page, {"issues": "E2E重复用药"}))
    reviews = page.evaluate("async () => await api('/api/prescriptions/comment-reviews')")
    mine = [r for r in reviews if r["prescription_id"] == to_comment]
    assert [(r["grade"], r["issues"]) for r in mine] == [("unreasonable", "E2E重复用药")], mine


def test_审方规则的改动记录在规则表上查得到(page, base_url):
    """P2-578：规则按 drug_code 整条覆盖，覆盖之后旧值原先无处可查。规则表每行的「改动记录」列出新建、导入覆盖、
    停用、恢复各改了哪几项、谁、何时——文案取自后端。"""
    _login(page, base_url)
    page.evaluate("""async () => {
      await api('/api/prescriptions/rules', { method: 'POST', body: JSON.stringify(
        { drug_code: 'E2E-RULELOG', max_daily_dose: 2, dose_unit: 'g' }) });
      await api('/api/prescriptions/rules/import', { method: 'POST', body: JSON.stringify(
        [{ drug_code: 'E2E-RULELOG', max_daily_dose: 3, dose_unit: 'g' }]) });
    }""")
    _open_page(page, "rx", "集中审方")
    page.click('button[data-rulelog="E2E-RULELOG"]')
    box = page.locator("#rulelog-box")
    expect(box).to_contain_text("E2E-RULELOG 的改动记录")
    expect(box.locator("tr", has_text="导入")).to_contain_text("日剂量上限：2.0 → 3.0")
    expect(box.locator("tr", has_text="新建")).to_contain_text("平台管理员")


def test_集中审方页开方可开多行_同方相互作用转药师审(page, base_url, seed):
    """P2-1214（第三十五批扫描 T4-5）：开方表单原先只有一组药品框（面板标题「开方（单药演示）」），一张方只开得出一味药，
    同方相互作用、同方重复两条审方规则在页面上永远触发不到。改成可增删的多行明细：「添加一行」加一行、「删除本行」删自己
    那行，只剩一行时不摆删除；提交送出全部行，天数留空不送（后端缺省 1）。华法林 B01AA03 的种子规则把布洛芬 M01AE01
    列为相互作用药，两行开进同一张方即转药师审。"""
    _login(page, base_url)
    _open_page(page, "rx", "集中审方")
    form = page.locator("#rx-form")
    rows = form.locator(".rx-item")
    expect(rows).to_have_count(1)
    expect(rows.nth(0).locator("[data-rxdelrow]")).to_be_hidden()   # 只剩一行：不摆「删除本行」
    form.locator('[name="patient_id"]').fill(str(seed["patient"]["id"]))
    form.locator('[name="org_id"]').fill(str(seed["org"]["id"]))
    form.locator('[name="diagnosis_name"]').fill("E2E心房颤动;腰痛")
    form.locator("#rx-add-item").click()
    form.locator("#rx-add-item").click()
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator("[data-rxdelrow]")).to_be_visible()
    for i, (code, name, dose) in enumerate((
            ("B01AA03", "华法林", "3"), ("M01AE01", "布洛芬", "1200"), ("E2E-EXTRA", "E2E多加的一行", "1"))):
        rows.nth(i).locator('[name="drug_code"]').fill(code)
        rows.nth(i).locator('[name="drug_name"]').fill(name)
        rows.nth(i).locator('[name="daily_dose"]').fill(dose)
    rows.nth(1).locator('[name="days"]').fill("")                    # 天数留空：不送，后端缺省 1
    rows.nth(2).locator("[data-rxdelrow]").click()                   # 多加的一行删掉
    expect(rows).to_have_count(2)
    expect(rows.nth(1).locator('[name="drug_code"]')).to_have_value("M01AE01")
    _submit(page, '#rx-form button:has-text("提交处方")')
    msg = page.locator("#rx-msg")
    expect(msg).to_contain_text("转入药师审核")
    expect(msg).to_contain_text("药物相互作用：华法林 与 布洛芬")           # 修前页面只开得出一味，两张各自系统审通过
    made = page.evaluate("""async () => (await api('/api/prescriptions?limit=200'))
      .filter((p) => p.diagnosis_name === 'E2E心房颤动;腰痛')""")
    assert [(p["status"], [(i["drug_code"], i["days"]) for i in p["items"]]) for p in made] == [
        ("pending_review", [("B01AA03", 7), ("M01AE01", 1)])], made


@pytest.fixture(scope="session")
def consult_seed(base_url, seed):
    """一条待回复的续方咨询，以及同一患者一张已自动通过审方的处方（续方要关联它）。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    pid, oid = seed["patient"]["id"], seed["org"]["id"]
    rx = post("/api/prescriptions", {"patient_id": pid, "org_id": oid, "diagnosis_name": "E2E高血压",
                                     "items": [{"drug_code": "E2E-AML", "drug_name": "E2E氨氯地平", "daily_dose": 5}]},
              token)
    consult = post("/api/telemedicine/consults",
                   {"patient_id": pid, "org_id": oid, "consult_type": "repeat_rx", "question": "E2E降压药吃完了，想续方"},
                   token)
    return {"rx": rx, "consult": consult}


def test_互联网诊疗回复在页内表单里录入_医师必填_处方号写错报人话(page, base_url, consult_seed):
    """P2-38：回复原先三连问，医师姓名留空就记成"医师"（事后查不出是谁答的），处方号写成认不出的
    样子会被 Number() 变成 NaN、序列化成 null——悄悄变成"不关联"。现在医师必填，处方号原样交后端判。"""
    cid, rx_id = consult_seed["consult"]["id"], consult_seed["rx"]["id"]
    assert consult_seed["rx"]["status"] == "auto_passed", consult_seed["rx"]  # 续方只收已通过审方的
    _login(page, base_url)
    _open_page(page, "telemedicine", "互联网+诊疗")

    page.click(f'button[data-reply="{cid}"]')
    # P2-607 第十批：框自己提交——处方号写错报错写在框里、框不关，写好的回复还在（原先报在页面消息行、框已关）
    form = _spd_modal_rejected(page, {"reply": "E2E可续方", "doctor_name": "E2E全科医生", "prescription_id": "三十一"})
    expect(form.locator("[data-modal-msg]")).to_contain_text("prescription_id")
    expect(form.locator('[name="reply"]')).to_have_value("E2E可续方")
    _redrawn(page, lambda: _spd_modal(page, {"reply": "E2E可续方，按原剂量", "prescription_id": str(rx_id)}))
    consults = page.evaluate("async () => await api('/api/telemedicine/consults')")
    got = next(c for c in consults if c["id"] == cid)
    assert (got["status"], got["doctor_name"], got["prescription_id"]) == ("replied", "E2E全科医生", rx_id), got


@pytest.fixture(scope="session")
def pathology_seed(base_url, seed):
    """一张病理申请单下两份待核收的标本：一份走完核收 → 取材 → 制片，一份拒收。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    req = post("/api/exams", {"patient_id": seed["patient"]["id"], "from_org_id": seed["org"]["id"],
                              "center_type": "pathology", "item_code": "E2E-BL", "item_name": "E2E活检"}, token)
    return [post("/api/pathology/specimens", {"request_id": req["id"], "site": site}, token)
            for site in ("E2E胃窦", "E2E结肠")]


def test_病理标本核收_拒收_推进都在页内表单里录入(page, base_url, pathology_seed):
    """P2-38：拒收原因后端只收五个标准项之一，原生弹窗却让人手打——差一个字就 422；推进一律问
    "蜡块数或切片数"、点取消照样推进。现在拒收从后端给的标准项里选，推进按环节只问该环节的数。"""
    keep, drop = (s["id"] for s in pathology_seed)

    def specimen(sid):
        return page.evaluate(
            "async (id) => (await api('/api/pathology/specimens')).find((s) => s.id === id)", sid)

    _login(page, base_url)
    _open_page(page, "pathology", "病理标本")
    page.click(f'button[data-reject="{drop}"]')
    _redrawn(page, lambda: _spd_modal(page, {"reject_reason": "未加固定液"}))
    assert (specimen(drop)["status"], specimen(drop)["reject_reason"]) == ("rejected", "未加固定液")

    page.click(f'button[data-receive="{keep}"]')
    _redrawn(page, lambda: _spd_modal(page, {"received_by": "E2E病理技师"}))
    page.click(f'button[data-advance="{keep}"]')
    expect(page.locator("form.panel:has(button[data-cancel])")).to_contain_text("蜡块数")
    _redrawn(page, lambda: _spd_modal(page, {"block_count": "3"}))
    page.click(f'button[data-advance="{keep}"]')
    expect(page.locator("form.panel:has(button[data-cancel])")).to_contain_text("切片数")
    _redrawn(page, lambda: _spd_modal(page, {"slide_count": "6"}))
    got = specimen(keep)
    assert (got["status"], got["received_by"], got["block_count"], got["slide_count"]) == (
        "slided", "E2E病理技师", 3, 6), got



# ---------------------------------------------------------------- 阶段十二


@pytest.fixture(scope="session")
def workflow_seed(base_url):
    """流程引擎页的前置：一条两节点的流程定义，发起两个实例（一个用来推进、一个用来终止）。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    admin = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    call("/api/workflows/definitions", {"key": "e2e_wf", "name": "E2E两级审批", "nodes": [
        {"key": "apply", "name": "E2E申请", "next": "approve"},
        {"key": "approve", "name": "E2E审批", "next": ""}]}, admin)
    advance = call("/api/workflows/instances", {"definition_key": "e2e_wf", "business_type": "e2e",
                                                "title": "E2E待推进事项"}, admin)
    cancel = call("/api/workflows/instances", {"definition_key": "e2e_wf", "business_type": "e2e",
                                               "title": "E2E待终止事项"}, admin)
    return {"advance": advance, "cancel": cancel, "read": lambda path: call(path, None, admin)}


def test_流程推进与终止都在页内表单里填_取消即不动(page, base_url, workflow_seed):
    """P2-38：流程引擎"推进"原先点意见框的取消照样推到下一节点；"终止"在确认框之后再问原因，
    原因框点取消照样终止。换成页内表单后取消就是不动——按接口核对节点与状态，再走完并读回意见。"""
    read = workflow_seed["read"]
    adv_id, cancel_id = workflow_seed["advance"]["id"], workflow_seed["cancel"]["id"]

    def instance(iid):
        (row,) = [i for i in read("/api/workflows/instances?limit=500") if i["id"] == iid]
        return row

    _login(page, base_url)
    _open_page(page, "workflows", "流程引擎")
    modal = page.locator("form.panel:has(button[data-cancel])")
    adv_row = page.locator("tr", has_text="E2E待推进事项").first
    adv_row.locator("button[data-advance]").click()
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert instance(adv_id)["current_node"] == "apply", "点了取消却照样推进了"
    adv_row.locator("button[data-advance]").click()
    form = _spd_modal_rejected(page, {"comment": "意" * 513})   # 意见写超了（后端 512 字）：框不关、没推进（P2-607 第八批）
    expect(form.locator('[name="comment"]')).to_have_value("意" * 513)
    assert instance(adv_id)["current_node"] == "apply"
    _redrawn(page, lambda: _spd_modal(page, {"comment": "材料齐全，同意"}))
    assert instance(adv_id)["current_node"] == "approve"
    history = read(f"/api/workflows/instances/{adv_id}/history")
    assert [h["comment"] for h in history if h["from_node"] == "apply"] == ["材料齐全，同意"], history

    cancel_row = page.locator("tr", has_text="E2E待终止事项").first
    cancel_row.locator("button[data-cancel]").click()
    expect(modal).to_contain_text("不能恢复")
    modal.locator("button[data-cancel]").click()
    expect(modal).to_have_count(0)
    assert instance(cancel_id)["status"] == "running", "点了取消却照样终止了"
    cancel_row.locator("button[data-cancel]").click()
    form = _spd_modal_rejected(page, {"comment": "终" * 513})   # 原因写超了：框不关、没终止
    expect(form.locator('[name="comment"]')).to_have_value("终" * 513)
    assert instance(cancel_id)["status"] == "running"
    _redrawn(page, lambda: _spd_modal(page, {"comment": "重复发起"}))
    assert instance(cancel_id)["status"] == "cancelled"
    history = read(f"/api/workflows/instances/{cancel_id}/history")
    assert "重复发起" in [h["comment"] for h in history], history


@pytest.mark.e2e
def test_拆分脚本后每一页都还渲染得出来(page, base_url):
    """app.js 按业务域拆成 5 个文件之后的回归。

    拆文件的风险全在**加载顺序**上：页面注册表求值时就要拿到每个 renderX 的
    引用，而函数声明的提升只在同一个文件内生效——顺序错了，表现是整个管理端
    白屏，而任何后端测试都发现不了。

    所以这条用例挨个点开导航里的每一页，断言两件事：**没有 JS 报错**，
    且**每一页都渲染出了内容**（不是停在"加载中…"）。
    """
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda m: errors.append(f"console: {m.text[:160]}") if m.type == "error" else None,
    )
    _login(page, base_url)
    page.wait_for_selector("#nav a")
    page_ids = page.eval_on_selector_all("#nav a", "els => els.map(e => e.dataset.page)")
    assert len(page_ids) > 60, f"导航项只有 {len(page_ids)} 个，注册表可能没加载全"

    # 每页的等待不用固定 sleep（慢机器上 400ms 常常不够渲染完，会把好页面误判成
    # 空白）：给当前 #page-body 打标记，等到出现一个**没有标记且不是"加载中…"**的
    # #page-body——那才是"这一页真的重画出了内容"。导航按 hash 路由，循环里每次
    # 点击的都是与上一页不同的页，hashchange 必然触发重画，标记必然被换掉。
    blank = []
    for page_id in page_ids:
        page.eval_on_selector("#page-body", "el => el.dataset.stamp = 'e2e-prev-page'")
        page.click(f'#nav a[data-page="{page_id}"]')
        try:
            page.wait_for_function(
                "() => { const el = document.querySelector('#page-body');"
                " if (!el || el.dataset.stamp === 'e2e-prev-page') return false;"
                " const text = el.textContent.trim();"
                " return text !== '' && text !== '加载中…'; }",
                timeout=5000,
            )
        except PlaywrightTimeoutError:
            blank.append(page_id)
    assert blank == [], f"这些页面没有渲染出内容：{blank}"
    assert errors == [], "管理端有 JS 报错：\n" + "\n".join(errors[:10])


# ============================================================
# 全域慢专病（P3-2）：三种身份各走一条真实链路。
# 基础数据（机构/患者/账号/已发布路径模板）走接口预置，UI 只驱动关键动作——
# 与本文件既有约定一致。
# ============================================================


@pytest.fixture(scope="session")
def spd_seed(base_url):
    """慢专病端到端的前置数据：机构、患者、医生账号、已发布路径、待办任务、在途转诊。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None, method=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else b"{}",
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
            method=method or "POST",
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = call("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    org = call("/api/organizations",
               {"name": "E2E慢专病卫生院", "org_type": "township", "level": "township"}, token)
    # 居民端登录靠手机号验证码 + 实名绑定，患者必须带手机号且证件号可核验
    patient = call("/api/patients", {
        "name": "慢专病E2E患者", "id_card": "320981197206064321", "gender": "女",
        "birth_date": "1972-06-06", "phone": "13788990011"}, token)
    call("/api/users", {"username": "e2e_spd_doc", "password": "passw0rd1", "role": "doctor",
                        "full_name": "E2E慢专病医生", "org_id": org["id"]}, token)
    doctor_id = None
    # 找到医生 id（创建任务时直接指派用）：用户列表按机构过滤
    for u in call(f"/api/users?org_id={org['id']}", None, token, method="GET"):
        if u["username"] == "e2e_spd_doc":
            doctor_id = u["id"]
    # 三级机构树（ADR-0004/0005）：转诊分级审核按机构树 parent_id 逐级上收——
    # 只有"当前机构的直接上级"能把单子推进一格。要让医生端（乡镇卫生院）的
    # "通过"真正生效，单子必须由其**子机构**（村卫生室）发起；此前由同一机构的
    # 医生自发自审，点"通过"实际收到 403，列表纹丝不动，用例却因断言太弱而全绿。
    village = call("/api/organizations",
                   {"name": "E2E慢专病村卫生室", "org_type": "village", "level": "village",
                    "parent_id": org["id"]}, token)
    call("/api/users", {"username": "e2e_spd_vill", "password": "passw0rd1", "role": "doctor",
                        "full_name": "E2E村医", "org_id": village["id"]}, token)
    village_login = call("/api/auth/login", {"username": "e2e_spd_vill", "password": "passw0rd1"})

    # 已发布的演示路径（UI 只驱动"启动实例→办结任务"）
    programs = call("/api/spd/programs", None, token, method="GET")
    hyp = next(p for p in programs if p["code"] == "hypertension")
    template = call("/api/spd/path-templates", {
        "program_id": hyp["id"], "code": "e2e_hyp_path", "name": "E2E高血压路径",
        "scene": "followup"}, token)
    call(f"/api/spd/path-templates/{template['id']}/nodes",
         {"key": "assess", "name": "首次评估", "seq": 1, "due_days": 7}, token)
    call(f"/api/spd/path-templates/{template['id']}/status", {"status": "published"}, token)

    # 医生移动端的待办：创建时直接带 assignee（spawn_task 保持 pending，可接收）。
    # 不能建完再走 /assign 指派——assign 会把 pending 顺手置成 claimed，
    # 移动端点"接收"永远 409"不处于可接收状态"，而旧用例对此毫无察觉。
    task = call("/api/spd/tasks", {
        "patient_id": patient["id"], "title": "E2E随访任务", "task_type": "followup",
        "org_id": org["id"], "due_days": 7, "assignee_id": doctor_id}, token)
    # 患者 ↔ 村卫生室的服务关系记录（visibility 的 service 依据）：患者是 admin
    # 建的、与村卫生室尚无任何业务关联，村医直接发起转诊会被档案调阅校验 403。
    # 任何"patient_id + 机构外键"的业务记录都构成依据，这里用一条村级任务垫底。
    call("/api/spd/tasks", {
        "patient_id": patient["id"], "title": "E2E村级建档服务", "task_type": "followup",
        "org_id": village["id"], "due_days": 7}, token)
    # 在途转诊：由村医发起、目标机构=乡镇卫生院，落 submitted（"待卫生院审核"）。
    # target_org_id 必填：医生的可见范围只有本机构（无子树），乡镇卫生院医生要在
    # 列表里看到这张村级发起的单子，只能靠 target 命中自己。
    referral = call("/api/spd/referrals", {
        "patient_id": patient["id"], "program_code": "hypertension", "direction": "up",
        "reason": "E2E演示转诊", "target_org_id": org["id"]},
        village_login["access_token"])
    return {"org": org, "village": village, "patient": patient, "template": template,
            "task": task, "referral": referral, "doctor_id": doctor_id,
            "read": lambda path: call(path, None, token, method="GET")}


def test_spd_admin_screen_enroll_path_task(page, base_url, spd_seed):
    """管理端：筛查登记 → 签约纳管 → 启动路径 → 办结节点任务（spdModal 表单）。"""
    _login(page, base_url)

    _open_page(page, "spdpatients", "筛查建档与纳管")
    page.fill('#spd-screen-form input[name="patient_id"]', str(spd_seed["patient"]["id"]))
    page.select_option('#spd-screen-form select[name="program_code"]', "hypertension")
    _submit(page, "#spd-screen-form button")

    _open_page(page, "spdpatients", "筛查建档与纳管")
    page.fill('#spd-enroll-form input[name="patient_id"]', str(spd_seed["patient"]["id"]))
    page.select_option('#spd-enroll-form select[name="program_code"]', "hypertension")
    page.fill('#spd-enroll-form input[name="org_id"]', str(spd_seed["org"]["id"]))
    _submit(page, "#spd-enroll-form button")

    # 拿刚建的纳管档案 id（UI 列表异步画出，直接查接口更稳）
    # 取刚建的纳管档案 id。**在浏览器里发这个请求**而不是用 urllib 带令牌：
    # 会话已经是 HttpOnly Cookie（G3/P1-23），JS 与用例都读不到令牌，
    # 只有同源 fetch 才会自动带上它。
    enrollments = page.evaluate(
        "async () => (await fetch('/api/spd/enrollments?program_code=hypertension',"
        " {credentials: 'same-origin'})).json()"
    )
    enrollment = next(
        e for e in enrollments if e["patient_id"] == spd_seed["patient"]["id"]
    )

    _open_page(page, "spdpath", "标准路径与任务中心")
    page.fill('#spd-inst-form input[name="enrollment_id"]', str(enrollment["id"]))
    page.select_option('#spd-inst-form select[name="template_id"]',
                       str(spd_seed["template"]["id"]))
    _submit(page, "#spd-inst-form button")

    _open_page(page, "spdpath", "标准路径与任务中心")
    # 防哑火诊断（CI 上此处曾无声超时过一次、复跑即绿，机理未钉死）：先经 API
    # 证实任务已存在，把"任务根本没生成"（后端/种子）与"UI 没画出来"（前端/
    # 时序）分开——下一次失败自带凶手名单，而不是一句 Timeout。路径实例的
    # 首节点任务是 POST 内同步生成的（tasks.start_path_instance → start_path
    # → commit 后才返回），API 里查不到即后端问题实锤。
    tasks = page.evaluate(
        "async () => (await fetch('/api/spd/tasks?open_only=true&limit=100',"
        " {credentials: 'same-origin'})).json()"
    )
    # 子串匹配：任务标题是 spawn_task 拼的「模板名·节点名」（如
    # E2E高血压路径·首次评估），与 UI 行过滤 has_text 同一判定口径
    assert any("首次评估" in (t.get("title") or "") for t in tasks), (
        f"路径实例已建但任务 API 里没有『首次评估』，现有任务：{[t.get('title') for t in tasks]}"
    )
    # 分派给不存在的责任人（P2-607）：框不关、报错写在框里、写好的备注还在；取消后照常办结
    page.locator("#spd-task-list tr", has_text="首次评估").locator("[data-task-assign]").click()
    form = _spd_modal_rejected(page, {"assignee_id": "987654", "note": "E2E 分派说明"})
    expect(form.locator("[data-modal-msg]")).to_contain_text("责任人")
    expect(form.locator('[name="note"]')).to_have_value("E2E 分派说明")
    form.locator("[data-cancel]").click()
    expect(form).to_have_count(0)
    # 精确点到"首次评估"那一行的办结按钮：任务中心里还躺着 seed 预置的其他任务
    # （按 priority/due/id 排序），拍第一个按钮拍到谁取决于排序细节，太脆。
    page.locator("#spd-task-list tr", has_text="首次评估").locator(
        "[data-task-done]").click()  # 打开 spdModal 办结表单
    modal = page.locator("form.panel").last
    expect(modal).to_be_visible()
    modal.locator('textarea[name="note"]').fill("E2E 完成首次评估")
    modal.locator('button[type="submit"]').click()
    # postAction 成功后 route() 整页重画——用重试断言等"已完成"出现，
    # 固定 sleep 在慢机器上会在重画完成前就抓取正文
    expect(page.locator("#page-body")).to_contain_text("已完成")


def _resident_login(page, base_url, patient):
    """居民端：验证码登录 → 实名绑定，落到档案页。"""
    page.goto(f"{base_url}/m/")
    page.click('[data-tab="archive"]')
    page.fill("#in-phone", patient["phone"])
    page.click("#btn-send-code")  # console 短信通道：演示验证码自动回填
    expect(page.locator("#in-code")).not_to_have_value("")
    page.click('#sms-form button[type="submit"]')
    # 实名绑定（姓名 + 身份证与预置患者一致）；手机号与档案匹配时会自动绑定，
    # 直接进入档案页——两种落点都合法
    page.wait_for_selector("#pane-bind:not(.hidden), #pane-archive:not(.hidden)")
    if page.locator("#pane-bind").is_visible():
        page.fill("#in-name", patient["name"])
        page.fill("#in-idcard", patient["id_card"])
        page.click('#bind-form button[type="submit"]')
    expect(page.locator("#pane-archive")).to_be_visible()


def test_spd_resident_selfscreen_apply_measure(page, base_url, spd_seed):
    """居民端：验证码登录 → 实名绑定 → 高危自查（顺手申请服务）→ 自报监测数据。"""
    _resident_login(page, base_url, spd_seed["patient"])

    # 自查：高危答案 → 结果提示；确认弹窗即"申请专病管理服务"
    page.on("dialog", lambda d: d.accept())
    page.click('[data-tab="spd"]')
    page.click('[data-spd="screen"]')
    page.wait_for_selector("#spd-scale")
    # 答糖尿病问卷（P2-559）：上面管理端用例已把这位居民按高血压签约纳管，在管病种的自查不再提示申请——
    # 「顺手申请服务」这条链路要走一个没在管的病种
    page.select_option("#spd-scale", "scr_diabetes")
    # 每题默认「（未答）」（P1-136）：原先默认选中第一个选项，一题没碰就交卷等于每题都答了「是」
    assert all(sel.input_value() == "" for sel in page.locator("[data-q]").all())
    for sel in page.locator("[data-q]").all():
        sel.select_option("是")
    page.click("#spd-screen-submit")
    # 先断言链路的**稳定终态**：申请单卡片"待受理"（只有筛查+申请两步都成功才会出现）。
    expect(page.locator("#spd-result")).to_contain_text("待受理")
    # 自查结论在重画之后写回（P2-1676）：原先确认申请后 `await loadSpd()` 重画自查分段，`#spd-screen-msg`
    # 被重建为空，风险等级与健康建议只活在"POST 返回 → 重画完成"的几十毫秒里（当年这里因此不敢断言它，
    # 断言就跟自家重画抢时间、在 CI 上 flake 过）。现在先重画、再把结论连同申请结果写回，是稳定终态
    expect(page.locator("#spd-screen-msg")).to_contain_text("风险等级")
    expect(page.locator("#spd-screen-msg")).to_contain_text("已申请专病管理服务")
    # 分段渲染竞态（提交链路半途切分段互相盖写）已由 m.js loadSpd 串行化修复
    # （序号+互斥+收尾补画，与管理端 route() 同构）；竞态窗口取决于机器时序，
    # e2e 关不稳这扇窗——**确定性的防拆卸由静态守卫
    # test_mobile_render_serialization.py 承担**，本用例负责真实路径可用。

    # 自报监测：血压 165 → 落库为待医生处置的异常值。
    # 保存成功后整个分段会重画（提示语随之被抹掉），所以断言重画后的列表里
    # 有这条数值，而不是抓那条一闪而过的提示
    page.click('[data-spd="measure"]')
    page.wait_for_selector("#spd-measure-form")
    page.fill("#spd-value", "165")
    page.click('#spd-measure-form button[type="submit"]')
    # 保存成功后分段重画、数值落进记录列表——重试断言等它出现（替代固定 sleep）
    expect(page.locator("#spd-result")).to_contain_text("165")


def test_居民端线上自助随访按问卷逐题作答_判出异常(page, base_url, spd_seed, admin_call, admin_read):
    """P1-122 ②：手机页的自助随访原先只问一道「恢复情况」、交 {recovery}，问卷的异常规则一条也碰不到。
    现在按这条随访挂的问卷逐题作答，疼痛 9 分判重度。

    用自己的居民：验证码单号冷却 60 秒，与别的居民端用例共用一个手机号，紧挨着跑就收不到码。"""
    person = {"name": "自助随访E2E居民", "id_card": "320981197508084123", "phone": "13788990022"}
    resident = admin_call("POST", "/api/patients", {**person, "gender": "女", "birth_date": "1975-08-08"})
    admin_call("POST", "/api/spd/questionnaires", {
        "code": "E2E_Q122M", "name": "E2E 居民自助问卷",
        "items": [{"key": "pain", "title": "疼痛评分", "type": "number"},
                  {"key": "fever", "title": "是否发热", "type": "single", "options": [{"label": "否"}, {"label": "是"}]}],
        "abnormal_rules": [{"when": {"field": "pain", "op": ">=", "value": 7}, "level": "high", "action": "通知主管医师"}]})
    rule = admin_call("POST", "/api/spd/followup-rules", {
        "code": "E2E_R122M", "name": "E2E 居民自助随访", "points": [0], "questionnaire_code": "E2E_Q122M"})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": resident["id"], "rule_id": rule["id"], "base_date": "2001-01-01",
        "org_id": spd_seed["org"]["id"]})
    record_id = plan["items"][0]["id"]

    _resident_login(page, base_url, person)
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click('[data-tab="spd"]')
    page.click('[data-spd="followup"]')
    page.click(f'[data-spd-self="{record_id}"]')
    form = page.locator(".m-card .inline-input")
    expect(form).to_contain_text("疼痛评分")
    expect(form).to_contain_text("是否发热")
    form.locator('[data-q="0"]').fill("9")
    form.locator('button[type="submit"]').click()
    expect(page.locator(f'[data-spd-self="{record_id}"]')).to_have_count(0)   # 提交后分段重画，这条已完成
    record = admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]
    assert (record["status"], record["channel"], record["abnormal_level"], record["answers"]) == (
        "done", "self", "high", {"pain": 9})
    assert messages and "重度" in messages[-1], messages


def test_居民端自助随访提交失败_表单留着_原因写在表单里(page, base_url, spd_seed, admin_call, admin_read):
    """P2-1014：居民端卡片内表单原先一提交就先移除、调用方才发请求——这条随访其间已被医护执行（409），只弹一句，
    逐题作答全没了。修后表单留到提交成功，失败原因写在表单里、填过的还在。"""
    person = {"name": "自助失败E2E居民", "id_card": "320981197509092029", "phone": "13788990044"}
    resident = admin_call("POST", "/api/patients", {**person, "gender": "女", "birth_date": "1975-09-09"})
    admin_call("POST", "/api/spd/questionnaires", {
        "code": "E2E_Q1014", "name": "E2E 提交失败问卷",
        "items": [{"key": "pain", "title": "疼痛评分", "type": "number"}], "abnormal_rules": []})
    rule = admin_call("POST", "/api/spd/followup-rules", {
        "code": "E2E_R1014", "name": "E2E 提交失败随访", "points": [0], "questionnaire_code": "E2E_Q1014"})
    plan = admin_call("POST", "/api/spd/followup-plans", {
        "patient_id": resident["id"], "rule_id": rule["id"], "base_date": "2001-01-01",
        "org_id": spd_seed["org"]["id"]})
    record_id = plan["items"][0]["id"]

    _resident_login(page, base_url, person)
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click('[data-tab="spd"]')
    page.click('[data-spd="followup"]')
    page.click(f'[data-spd-self="{record_id}"]')
    form = page.locator(".m-card .inline-input")
    form.locator('[data-q="0"]').fill("3")
    # 居民填着的时候，医护电话随访执行了这一条
    admin_call("POST", f"/api/spd/followup-records/{record_id}/execute", {"channel": "phone", "result": "电话已随访"})
    form.locator('button[type="submit"]').click()
    expect(form.locator("[data-inline-msg]")).not_to_be_empty()   # 修前表单已移除、只弹一句
    expect(form.locator('[data-q="0"]')).to_have_value("3")      # 填过的还在
    assert not messages, messages
    record = admin_read(f"/api/spd/followup-records/{record_id}/context")["record"]
    assert (record["status"], record["channel"]) == ("done", "phone"), record


def test_居民端退回重做的任务_卡片上能重新填报提交(page, base_url, spd_seed, admin_call, admin_read):
    """P1-127：医护审核「退回」的任务回到居民手里重做。修前居民端卡片上状态显示原码 rejected、只看得到审核意见，
    「填报并提交」「上传凭证」两个按钮只给待办 / 办理中 / 超期的任务——退回的任务在手机上重做不了。

    用自己的居民：验证码单号冷却 60 秒，与别的居民端用例共用一个手机号，紧挨着跑就收不到码。"""
    person = {"name": "退回重做E2E居民", "id_card": "320981198003034125", "phone": "13788990033"}
    resident = admin_call("POST", "/api/patients", {**person, "gender": "男", "birth_date": "1980-03-03"})
    task = admin_call("POST", "/api/spd/tasks", {
        "patient_id": resident["id"], "title": "E2E上传一周血压", "task_type": "report",
        "org_id": spd_seed["org"]["id"]})
    admin_call("POST", f"/api/spd/tasks/{task['id']}/submit", {"result": {"note": "只量了一天"}})
    admin_call("POST", f"/api/spd/tasks/{task['id']}/review", {"approved": False, "note": "请补齐一周的血压"})

    _resident_login(page, base_url, person)
    page.click('[data-tab="spd"]')
    page.click('[data-spd="task"]')
    card = page.locator(".m-card", has_text="E2E上传一周血压")
    expect(card).to_contain_text("已退回，请按审核意见重新提交")
    expect(card).to_contain_text("请补齐一周的血压")
    card.locator("[data-spd-task]").click()   # 修前退回的任务卡片上没有这个按钮
    card.locator(".inline-input textarea").fill("已补齐 7 天血压")
    card.locator('.inline-input button[type="submit"]').click()
    expect(page.locator(".m-card", has_text="E2E上传一周血压")).to_contain_text("已提交待审核")
    detail = admin_read(f"/api/spd/tasks/{task['id']}")
    assert (detail["status"], detail["result"]) == ("submitted", {"note": "已补齐 7 天血压"}), detail


def test_居民端在慢专病页签退出或掉线_本人档案不留在屏幕上(page, base_url, admin_call):
    """P2-1219（第三十五批扫描 T1-5）：居民端退出原先重画了档案、服务、问卷、通知四个登录态页签，漏了慢专病——在「慢专病」
    页签点退出，已登录标记已清，#spd-body 照旧显示本人的姓名、卡号、电话、诊断，登录引导反而藏着；authApi 的 401 分支只重画
    档案页，后台红点轮询碰上会话过期同样留着。修后两条路走同一段：五个页签一起重画，慢专病结果区清空、登录引导露出来。

    两位居民各用一个手机号：验证码单号冷却 60 秒，同一个号紧挨着登两次收不到码。"""
    leaver = {"name": "退出E2E居民", "id_card": "320981196802183013", "phone": "13788990121"}
    expired = {"name": "掉线E2E居民", "id_card": "320981196903194029", "phone": "13788990122"}
    admin_call("POST", "/api/patients", {**leaver, "gender": "男", "birth_date": "1968-02-18"})
    admin_call("POST", "/api/patients", {**expired, "gender": "女", "birth_date": "1969-03-19"})

    def open_spd_archive(person):
        _resident_login(page, base_url, person)
        page.click('[data-tab="spd"]')
        page.click('[data-spd="archive"]')
        expect(page.locator("#spd-result")).to_contain_text(person["name"])

    def signed_out():
        expect(page.locator("#spd-guard")).to_be_visible()   # 修前登录引导藏着
        expect(page.locator("#spd-body")).to_be_hidden()   # 修前照旧显示
        expect(page.locator("#spd-result")).to_have_text("")

    open_spd_archive(leaver)
    page.click("#btn-logout")
    signed_out()
    # 掉线：会话 Cookie 没了，后台 5 分钟一次的红点轮询碰上 401
    open_spd_archive(expired)
    page.context.clear_cookies()
    page.evaluate("refreshNotifyDot()")
    signed_out()


def test_spd_doctor_mobile_todo_and_referral(page, base_url, spd_seed):
    """医生移动端：登录 → 慢专病待办接收 → 转诊复核通过（prompt 应答意见）。

    两步都断言**动作成功后的新状态**，而不是"页面还是老样子"：

    - 接收：任务状态从"待接收"翻到"已接收"。claim 要求 pending 且指派给本人，
      seed 必须在创建任务时就带 assignee——先建再 /assign 会被顺手置成 claimed，
      "接收"永远 409；
    - 复核通过：单据从"待卫生院审核"（submitted）推进到"待县级接收"
      （township_reviewed）。分级审核按机构树 parent_id 上收（ADR-0004/0005）：
      单子由村卫生室（子机构）发起，登录的乡镇卫生院医生才是有权审核的上级。

    旧断言"通过后列表仍显示待卫生院审核或暂无在途转诊"恰好把 403/409 的静默失败
    （spdPost 失败不重画列表）也判成通过——两步实际都没发生，用例常年全绿。
    """
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_spd_doc")
    page.fill("#lg-pass", "passw0rd1")
    page.click('#login-form button[type="submit"]')
    expect(page.locator("#workbench")).to_be_visible()

    page.click('[data-tab="spd"]')
    page.wait_for_selector("[data-spd-claim]")
    expect(page.locator("#spd-list")).to_contain_text("待接收")
    page.click("[data-spd-claim]")
    # spdPost 成功后整块重画——等新状态出现（显式等待，替代固定 sleep）
    expect(page.locator("#spd-list")).to_contain_text("已接收")

    page.click('[data-dspd="referral"]')
    expect(page.locator("#spd-list")).to_contain_text("待卫生院审核")
    # 按钮按状态给（P2-101）：待审核的单子只有通过 / 退回，没有「登记到院」「承接随访」（原先四个一律摆着、点了 409）
    expect(page.locator("[data-spd-arrive]")).to_have_count(0)
    expect(page.locator("[data-spd-recv]")).to_have_count(0)
    # 意见在卡片内表单里填（P2-38）。原先弹窗点"取消"照样提交——想反悔的人反而把单子退了回去。
    # 先点「退回」再取消，按接口核对单子没动；再点「退回」后改点「通过」，表单要跟着换成通过的。
    page.click("[data-spd-reject]")
    form = page.locator("form.spd-review-form")
    expect(form.locator("textarea[name=opinion]")).to_have_attribute("placeholder", "退回理由")
    form.locator("button[data-cancel]").click()
    expect(form).to_have_count(0)
    referral_path = f"/api/spd/referrals/{spd_seed['referral']['id']}"
    assert spd_seed["read"](referral_path)["status"] == "submitted"
    page.click("[data-spd-reject]")
    page.click("[data-spd-pass]")
    expect(form).to_have_count(1)
    expect(form.locator("button[type=submit]")).to_have_text("确认通过")
    form.locator("textarea[name=opinion]").fill("同意上转")
    form.locator("button[type=submit]").click()
    # 通过即推进一格：待卫生院审核 → 待县级接收（重画完成的确定信号）
    expect(page.locator("#spd-list")).to_contain_text("待县级接收")
    steps = spd_seed["read"](referral_path)["steps"]
    assert [(x["action"], x["opinion"]) for x in steps if x["action"] in ("pass", "reject")] \
        == [("pass", "同意上转")], steps


def test_医生移动端_发起人撤回自己的转诊单_不摆审核(page, base_url, spd_seed):
    """P2-794：转诊卡片原先按状态摆「通过 / 退回」——村医发起的上转单自己卡片上也有，点了 403；发起人要撤回，移动端又没有
    按钮（接口收）。修后按清单行上的 `actions` 摆：发起人只看到「撤回」，确认后单子撤回。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json",
                               **({"Authorization": f"Bearer {token}"} if token else {})})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    village = call("/api/auth/login", {"username": "e2e_spd_vill", "password": "passw0rd1"})["access_token"]
    case = call("/api/spd/referrals", {"patient_id": spd_seed["patient"]["id"], "program_code": "hypertension",
                                       "direction": "up", "reason": "E2E 发起人撤回", "target_org_id": spd_seed["org"]["id"]},
                village)
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_spd_vill")
    page.fill("#lg-pass", "passw0rd1")
    page.click('#login-form button[type="submit"]')
    expect(page.locator("#workbench")).to_be_visible()
    page.click('[data-tab="spd"]')
    page.click('[data-dspd="referral"]')
    card = page.locator(".m-card", has_text="E2E 发起人撤回")
    expect(card.locator(f'[data-spd-withdraw="{case["id"]}"]')).to_be_visible()   # 修前移动端没有撤回
    expect(card.locator(f'[data-spd-pass="{case["id"]}"], [data-spd-reject="{case["id"]}"]')).to_have_count(0)
    card.locator(f'[data-spd-withdraw="{case["id"]}"]').click()
    form = card.locator("form.spd-withdraw-form")
    form.locator("button[type=submit]").click()
    expect(page.locator("#spd-msg")).to_contain_text("操作成功")
    assert spd_seed["read"](f"/api/spd/referrals/{case['id']}")["status"] == "withdrawn"


def test_医生移动端_村医发起上转_退回后看意见再重新发起(page, base_url, seed, spd_seed, admin_call):
    """P2-795：村医手册写「在『转诊办理』里发起上转，写清理由」「转诊单退回：看退回理由，补材料后重新发起」，手机上的
    转诊办理原先只有在途单的审核 / 到院 / 承接按钮——发起要回电脑上的管理端，退回的单子（已结束）从清单里消失、
    退回意见看不到。修后：「发起上转」从本人名下的在管患者里选人、写理由；本人发起、被退回的单列出来，能看退回意见、
    带着上次的内容重新发起。"""
    import json
    from urllib.request import Request

    def call(path, payload=None, token=None):
        req = Request(f"{base_url}{path}", data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json",
                               **({"Authorization": f"Bearer {token}"} if token else {})})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    read = spd_seed["read"]
    vill_id = next(u["id"] for u in read(f"/api/users?org_id={spd_seed['village']['id']}")
                   if u["username"] == "e2e_spd_vill")
    patient = admin_call("POST", "/api/patients", {"name": "E2E移动端上转患者", "id_card": "330127196505052795"})
    admin_call("POST", "/api/spd/enrollments", {"patient_id": patient["id"], "program_code": "hypertension",
                                                "org_id": spd_seed["village"]["id"], "doctor_user_id": vill_id})

    def cases():
        return [c for c in read(f"/api/spd/referrals?patient_id={patient['id']}&open_only=false")]

    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_spd_vill")
    page.fill("#lg-pass", "passw0rd1")
    page.click('#login-form button[type="submit"]')
    expect(page.locator("#workbench")).to_be_visible()
    page.click('[data-tab="spd"]')
    page.click('[data-dspd="referral"]')
    page.click("[data-spd-ref-new]")   # 修前这一页没有发起入口
    form = page.locator("form.spd-ref-new-form")
    form.locator('select[name="enrollment"]').select_option(f"{patient['id']}|hypertension")
    form.locator('select[name="target_org_id"]').select_option(label="E2E县人民医院")
    form.locator('textarea[name="reason"]').fill("E2E 移动端上转：血压控制不佳")
    form.locator("button[type=submit]").click()
    # 等提交后整块重画出新单子（消息行会留着上一次的「操作成功」，不能拿它当信号）
    expect(page.locator("#spd-list")).to_contain_text("E2E 移动端上转：血压控制不佳")
    [first] = cases()
    assert (first["status"], first["initiator_id"], first["reason"], first["target_org_id"]) == (
        "submitted", vill_id, "E2E 移动端上转：血压控制不佳", seed["org"]["id"]), first

    township = call("/api/auth/login", {"username": "e2e_spd_doc", "password": "passw0rd1"})["access_token"]
    call(f"/api/spd/referrals/{first['id']}/review", {"action": "reject", "opinion": "请补充近一周血压记录"}, township)
    page.click('[data-dspd="referral"]')
    page.click(f'[data-spd-reject-view="{first["id"]}"]')   # 修前退回的单子在手机上看不到
    expect(page.locator(f'[data-spd-reject-note="{first["id"]}"]')).to_contain_text("请补充近一周血压记录")
    page.click(f'[data-spd-ref-again="{first["id"]}"]')
    again = page.locator("form.spd-ref-new-form")
    expect(again.locator('textarea[name="reason"]')).to_have_value("E2E 移动端上转：血压控制不佳")
    again.locator('textarea[name="reason"]').fill("E2E 移动端上转：已补一周血压记录")
    again.locator("button[type=submit]").click()
    expect(page.locator("#spd-list")).to_contain_text("E2E 移动端上转：已补一周血压记录")
    latest = max(cases(), key=lambda c: c["id"])
    assert (latest["status"], latest["reason"], latest["target_org_id"]) == (
        "submitted", "E2E 移动端上转：已补一周血压记录", first["target_org_id"]), latest


def test_医生移动端要佐证的任务_传了佐证才办得结(page, base_url, spd_seed, admin_call, admin_read, tmp_path):
    """P2-84：医生移动端待办卡片原先一律摆着「接收」「办结」、没有上传佐证的入口——要佐证的任务在手机上点办结恒 422
    「该任务要求上传佐证材料后才能办结」，只能回管理端传。现在按状态给按钮：要佐证的多一个「上传佐证」（与管理端同一走法：
    传成附件、以草稿写进任务的佐证清单），传了才办得结；已接收的不再摆「接收」。"""
    task = admin_call("POST", "/api/spd/tasks", {
        "patient_id": spd_seed["patient"]["id"], "title": "E2E上门测压留照", "task_type": "followup",
        "org_id": spd_seed["org"]["id"], "due_days": 7, "assignee_id": spd_seed["doctor_id"],
        "require_evidence": True})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_spd_doc")
    page.fill("#lg-pass", "passw0rd1")
    page.click('#login-form button[type="submit"]')
    expect(page.locator("#workbench")).to_be_visible()
    page.click('[data-tab="spd"]')
    card = page.locator("#spd-list .m-card", has_text="E2E上门测压留照")
    expect(card).to_contain_text("办结前须上传照片或报告")
    # 上一条用例接收过的随访任务已是「已接收」：不再摆注定 409 的「接收」
    expect(page.locator("#spd-list .m-card", has_text="E2E随访任务").locator("[data-spd-claim]")).to_have_count(0)

    card.locator("[data-spd-done]").click()   # 没传佐证先办结：后端拒，卡片不动
    card.locator("form.spd-done-form button[type=submit]").click()
    # 拒绝写在这张卡的表单里（P2-1093），不写到屏幕外的整页消息行
    expect(card.locator("form.spd-done-form [data-card-msg]")).to_contain_text("该任务要求上传佐证材料后才能办结")
    expect(page.locator("#spd-msg")).not_to_contain_text("该任务要求上传佐证材料后才能办结")
    assert admin_read(f"/api/spd/tasks/{task['id']}")["status"] == "pending"

    photo = tmp_path / "bp.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0e2e-p284-evidence")
    with page.expect_file_chooser() as chooser:
        card.locator("[data-spd-evidence]").click()   # 修前卡片上没有这个按钮
    chooser.value.set_files(str(photo))
    expect(page.locator("#spd-msg")).to_contain_text("佐证已上传（共 1 份）")
    expect(card).to_contain_text("已传 1 份")
    card.locator("[data-spd-done]").click()
    card.locator("form.spd-done-form textarea[name=note]").fill("血压 132/84，已留照")
    card.locator("form.spd-done-form button[type=submit]").click()
    expect(page.locator("#spd-list")).not_to_contain_text("E2E上门测压留照")   # 办结即出了待办
    detail = admin_read(f"/api/spd/tasks/{task['id']}")
    assert (detail["status"], detail["result"], len(detail["evidence"])) == (
        "done", {"note": "血压 132/84，已留照"}, 1), detail
    assert [e["attachment_id"] for e in detail["evidence_urls"]] == detail["evidence"]   # 管理端审核页看得到这份佐证


def test_医生移动端退回的任务_卡片上重新提交_不再给办结(page, base_url, spd_seed, admin_call, admin_read):
    """P2-1361（第四十批扫描 AD2-1）：审核人退回的任务，医生移动端卡片上原先只有「办结」、没有重新提交的入口——点一下就
    绕过了再审，随访日回写、计分照记。修后退回卡片上换成「重新提交」（与管理端「提交」同一个接口、同一份取数），提交即回到
    待审核、等审核人再审；办结接口对已退回的 409。"""
    task = admin_call("POST", "/api/spd/tasks", {
        "patient_id": spd_seed["patient"]["id"], "title": "E2E退回重提随访", "task_type": "followup",
        "org_id": spd_seed["org"]["id"], "due_days": 7, "assignee_id": spd_seed["doctor_id"]})
    admin_call("POST", f"/api/spd/tasks/{task['id']}/submit", {"result": {"note": "只量了一次血压"}})
    admin_call("POST", f"/api/spd/tasks/{task['id']}/review", {"approved": False, "note": "请复测血压并核实服药"})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_spd_doc")
    page.fill("#lg-pass", "passw0rd1")
    page.click('#login-form button[type="submit"]')
    expect(page.locator("#workbench")).to_be_visible()
    page.click('[data-tab="spd"]')
    card = page.locator("#spd-list .m-card", has_text="E2E退回重提随访")
    expect(card).to_contain_text("已退回")
    expect(card).to_contain_text("请复测血压并核实服药")
    expect(card.locator("[data-spd-done]")).to_have_count(0)   # 修前退回卡片上只有「办结」
    card.locator("[data-spd-resubmit]").click()   # 修前没有这个按钮
    card.locator("form.spd-resubmit-form textarea[name=note]").fill("已复测 132/80，已核实服药")
    card.locator("form.spd-resubmit-form button[type=submit]").click()
    expect(page.locator("#spd-list .m-card", has_text="E2E退回重提随访")).to_contain_text("待审核")
    detail = admin_read(f"/api/spd/tasks/{task['id']}")
    assert (detail["status"], detail["result"], detail["review_note"]) == (
        "submitted", {"note": "已复测 132/80，已核实服药"}, "请复测血压并核实服药"), detail   # 退回意见不被重提盖掉


def test_任务详情里的佐证按上传时的文件名下载(page, base_url, spd_seed, admin_call, tmp_path):
    """P2-683：任务详情的「佐证 #N」原先把下载文件名写死成 task-{任务号}-evidence-{附件号}——没有扩展名、上传时的原名
    丢了，照片 / PDF 下到电脑上打不开；同一份附件在附件清单里下载用的是原名。修后不另起名字，用后端回的上传原名。"""
    admin_call("POST", "/api/spd/tasks", {
        "patient_id": spd_seed["patient"]["id"], "title": "E2E佐证原名下载", "task_type": "recall",
        "org_id": spd_seed["org"]["id"], "due_days": 1, "priority": 3, "require_evidence": True})
    photo = tmp_path / "E2E上门血压照片.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0e2e-p2683-evidence")
    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    page.select_option("#spd-task-filter select[name=task_type]", "recall")
    page.click("#spd-task-filter button.secondary")
    row = page.locator("#spd-task-list tr", has_text="E2E佐证原名下载")
    with page.expect_file_chooser() as chooser:
        row.locator("[data-task-evidence]").click()
    chooser.value.set_files(str(photo))
    expect(page.locator("#spd-task-msg")).to_contain_text("已挂到任务")
    page.locator("#spd-task-list tr", has_text="E2E佐证原名下载").locator("[data-task-detail]").click()
    with page.expect_download() as download:
        page.locator("button[data-attdl]", has_text="佐证 #").first.click()
    assert download.value.suggested_filename == "E2E上门血压照片.jpg"   # 修前 task-{id}-evidence-{附件号}


def test_慢专病页面的推送频率与病种显示中文(page, base_url, spd_seed, admin_call, admin_read):
    """P2-684：推送任务表的「频率」一栏原样显示 weekly（同一页新建表单的下拉写的是「每周」）；任务详情的「病种」一栏
    显示 hypertension。修后表格与下拉同一套文案，任务详情显示病种名称（与任务导出同一个换算）。"""
    tpl = next(t for t in admin_read("/api/spd/report-templates") if t["active"])
    admin_call("POST", "/api/spd/report-tasks", {"template_id": tpl["id"], "name": "E2E周报频率文案", "frequency": "weekly"})
    admin_call("POST", "/api/spd/tasks", {
        "patient_id": spd_seed["patient"]["id"], "title": "E2E病种名称显示", "task_type": "edu",
        "program_code": "hypertension", "org_id": spd_seed["org"]["id"], "due_days": 1, "priority": 3})
    _login(page, base_url)
    _open_page(page, "spdreport", "智能辅助报告端")
    row = page.locator("tr", has_text="E2E周报频率文案")
    expect(row).to_contain_text("每周")
    expect(row).not_to_contain_text("weekly")   # 修前这一格就是 weekly

    _open_page(page, "spdpath", "标准路径与任务中心")
    page.select_option("#spd-task-filter select[name=task_type]", "edu")
    page.click("#spd-task-filter button.secondary")
    page.locator("#spd-task-list tr", has_text="E2E病种名称显示").locator("[data-task-detail]").click()
    detail = page.locator("#spd-task-detail")
    expect(detail).to_contain_text("高血压 / 宣教")
    expect(detail).not_to_contain_text("hypertension")   # 修前「病种 / 类型：hypertension / 宣教」


def test_任务中心按团队筛选(page, base_url, spd_seed, admin_call):
    """P2-685：中心调度手册写「任务中心按机构 / 团队 / 类型筛出超期任务」，筛选栏原先只有类型、状态、只看我的。
    补上机构、团队两项（清单接口本就收），导出跟着同一组筛选走。"""
    team = admin_call("POST", "/api/spd/teams", {"name": "E2E筛选团队", "org_id": spd_seed["org"]["id"]})
    admin_call("POST", "/api/spd/tasks", {
        "patient_id": spd_seed["patient"]["id"], "title": "E2E团队筛选任务", "task_type": "followup",
        "org_id": spd_seed["org"]["id"], "team_id": team["id"], "due_days": 7})
    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    page.select_option("#spd-task-filter select[name=team_id]", str(team["id"]))   # 修前没有这一栏
    page.click("#spd-task-filter button.secondary")
    expect(page.locator("#spd-task-list")).to_contain_text("E2E团队筛选任务")
    expect(page.locator("#spd-task-list")).not_to_contain_text("E2E随访任务")   # 同机构、不在这个团队的


def test_团队工作台待办三格点进档案清单_列的就是那几份(page, base_url, admin_call):
    """P2-1316（第三十八批扫描 AB1-3 之一）：团队工作台「待评估 / 待定目标 / 待建路径」原先只是数字，档案清单也没有对应的
    筛选——是哪几份档案无处可查。修后三格可点：带着当前视角（成员端 = 主管医生是我）与这一格的判据跳到「筛查建档与纳管」，
    筛选栏预选好、首屏就按它查，列出的与卡片上的数同一句。"""
    org = admin_call("POST", "/api/organizations", {
        "name": "E2E P21316 卫生院", "org_type": "township", "level": "township"})
    doctor = admin_call("POST", "/api/users", {"username": "e2e_p21316_doc", "password": "passw0rd1", "role": "doctor",
                                               "full_name": "E2E P21316 医生", "org_id": org["id"]})
    admin_call("POST", "/api/spd/programs", {"code": "e2e_p21316", "name": "E2E P21316 未配阶段", "category": "chronic"})
    for n, program in enumerate(("e2e_p21316", "hypertension")):   # 没配阶段的病种建档落空串阶段：只有第一份待定目标
        patient = admin_call("POST", "/api/patients", {
            "name": f"E2E待定目标患者{n}", "id_card": f"32098119800101{1316 + n:04d}"})
        admin_call("POST", "/api/spd/enrollments", {
            "patient_id": patient["id"], "program_code": program, "org_id": org["id"], "doctor_user_id": doctor["id"]})
    _login(page, base_url, "e2e_p21316_doc", "passw0rd1")
    _open_page(page, "spdteam", "服务团队端·基层执行")
    card = page.locator('.card[data-spd-jump="target"]')   # 修前这一格没有去处
    expect(card).to_contain_text("待定目标")
    expect(card.locator(".value")).to_have_text("1")
    card.click()
    expect(page.locator("#main h2")).to_have_text("筛查建档与纳管")
    filter_form = page.locator("#spd-enroll-filter")
    expect(filter_form.locator('[name="team_role"]')).to_have_value("member")
    expect(filter_form.locator('[name="pending"]')).to_have_value("target")
    listed = page.locator("#spd-enroll-list")
    expect(listed).to_contain_text("E2E待定目标患者0")
    expect(listed).not_to_contain_text("E2E待定目标患者1")   # 已有阶段的那份不在
    expect(listed.locator("tbody tr")).to_have_count(1)


def test_团队工作台成员改角色弹框_视角不被改坏_坏值按成员端(page, base_url, spd_seed, admin_call):
    """P2-1332：成员表「改角色」按钮原先也带 data-role，页面的点击处理先按 [data-role] 认视角切换按钮——点了不弹框，
    把成员角色（doctor）写进 localStorage 的 spd_team_role，工作台 422、整页只剩「role：格式不对」，刷新也一样。
    修后按钮改用 data-member-role；已经存着坏值的浏览器打开即按成员端。"""
    team = admin_call("POST", "/api/spd/teams", {"name": "E2E改角色团队", "org_id": spd_seed["org"]["id"]})
    admin_call("POST", f"/api/spd/teams/{team['id']}/members", {"user_id": spd_seed["doctor_id"], "member_role": "doctor"})
    _login(page, base_url)
    _open_page(page, "spdteam", "服务团队端·基层执行")
    page.locator("#page-body tr", has_text="E2E改角色团队").locator("[data-team-members]").click()
    page.locator("#spd-team-detail [data-tm-edit]").first.click()
    form = _modal(page)
    expect(form).to_be_visible()   # 修前不弹框，整页先被视角切换重画成报错
    expect(form.locator('[name="member_role"]')).to_have_value("doctor")
    _cancel_modal(page)
    expect(form).to_have_count(0)
    assert page.evaluate("localStorage.getItem('spd_team_role')") in (None, "member")   # 修前 doctor
    expect(page.locator('#page-body [data-role="member"]')).to_be_visible()
    # 修前已经被改坏的浏览器：存着不认得的视角，回到这一页照常画、按成员端
    page.evaluate("localStorage.setItem('spd_team_role', 'doctor')")
    _open_page(page, "spdpath", "标准路径与任务中心")
    _open_page(page, "spdteam", "服务团队端·基层执行")
    expect(page.locator('#page-body [data-role="expert"]')).to_be_visible()   # 修前整页只剩报错，视角按钮画不出来
    expect(page.locator("#page-body")).not_to_contain_text("格式不对")
    assert "secondary" not in page.locator('#page-body [data-role="member"]').get_attribute("class").split()   # 当前视角


@pytest.fixture(scope="module")
def today_lists_seed(admin_call):
    """P2-1317：同一家卫生院两位医生，今天各有一条随访、一条复诊（患者姓名分得开），供桌面看板与医生移动端两段用。"""
    org = admin_call("POST", "/api/organizations", {
        "name": "E2E P21317 卫生院", "org_type": "township", "level": "township"})
    rule = admin_call("POST", "/api/spd/followup-rules", {"code": "E2E_P21317", "name": "E2E 当日随访", "points": [0]})
    day = admin_call("GET", "/api/spd/workbench/doctor-mobile")["calendar"]["today"]   # 服务端的业务日
    names = {}
    for key in ("me", "other"):
        doctor = admin_call("POST", "/api/users", {"username": f"e2e_p21317_{key}", "password": "passw0rd1",
                                                   "role": "doctor", "full_name": f"E2E P21317 {key}", "org_id": org["id"]})
        names[key] = f"E2E今日{'本人' if key == 'me' else '别人'}患者"
        patient = admin_call("POST", "/api/patients", {
            "name": names[key], "id_card": f"32098119800202{1317 + len(names):04d}"})
        admin_call("POST", "/api/spd/enrollments", {
            "patient_id": patient["id"], "program_code": "hypertension", "org_id": org["id"], "doctor_user_id": doctor["id"]})
        admin_call("POST", "/api/spd/followup-plans", {
            "patient_id": patient["id"], "rule_id": rule["id"], "org_id": org["id"], "executor_id": doctor["id"],
            "base_date": day})
        admin_call("POST", "/api/spd/revisits", {
            "patient_id": patient["id"], "program_code": "hypertension", "plan_date": day, "doctor_user_id": doctor["id"]})
    return {"day": day, "names": names}


def test_医生移动端今日随访今日复诊两段_列的就是那两个数(page, base_url, today_lists_seed):
    """P2-1317（第三十八批扫描 AB1-3 之二）：工作台报「今日随访 1 / 今日复诊 1」，手机上原先只有待办、转诊、患者、积分
    四段，是哪几位无处可看。修后加两段，按「本人 + 工作台的今天 + 没做完」取，与那两个数同一句。"""
    names = today_lists_seed["names"]
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "e2e_p21317_me")
    page.fill("#lg-pass", "passw0rd1")
    page.click('#login-form button[type="submit"]')
    expect(page.locator("#workbench")).to_be_visible()
    page.click('[data-tab="spd"]')
    expect(page.locator("#spd-wb .kv", has_text="今日随访")).to_contain_text("1")
    expect(page.locator("#spd-wb .kv", has_text="今日复诊")).to_contain_text("1")
    page.click('[data-dspd="followup"]')   # 修前没有这一段
    listed = page.locator("#spd-list")
    expect(listed).to_contain_text(names["me"])
    expect(listed).to_contain_text("待随访")
    expect(listed).not_to_contain_text(names["other"])
    expect(listed.locator(".m-card")).to_have_count(1)
    page.click('[data-dspd="revisit"]')
    expect(listed).to_contain_text(names["me"])
    expect(listed).not_to_contain_text(names["other"])
    expect(listed.locator(".m-card")).to_have_count(1)


def test_桌面随访与复诊看板_只看本人加计划日送既有参数(page, base_url, today_lists_seed):
    """P2-1317：随访看板原先只有状态 / 场景 / 只看超期，复诊看板只有状态 / 只看逾期，筛不出「本人 + 今天」。修后两块看板
    加「只看本人」「计划日」，提交时送清单的 mine 与 date_from / date_to（同一天）。"""
    day, names = today_lists_seed["day"], today_lists_seed["names"]
    _login(page, base_url, "e2e_p21317_me", "passw0rd1")
    _open_page(page, "spdfollowup", "智能随访服务端")
    page.wait_for_function("() => !routing")   # 首屏那次不带筛选的取数（render 末尾）跑完再查，别让它后到、盖掉筛选结果
    board = page.locator("#spd-fu-filter")
    board.locator('[name="mine"]').check()   # 修前没有这两栏
    board.locator('[name="day"]').fill(day)
    with page.expect_request(lambda r: "/api/spd/followup-records?" in r.url and "mine=true" in r.url) as sent:
        board.locator("button").click()
    assert f"date_from={day}" in sent.value.url and f"date_to={day}" in sent.value.url, sent.value.url
    expect(page.locator("#spd-fu-list")).to_contain_text(names["me"])
    expect(page.locator("#spd-fu-list")).not_to_contain_text(names["other"])   # 同机构别人的

    _open_page(page, "spdmanager", "个案管理师端·专属衔接")
    page.wait_for_function("() => !routing")
    board = page.locator("#spd-revisit-filter")
    board.locator('[name="mine"]').check()
    board.locator('[name="day"]').fill(day)
    with page.expect_request(lambda r: "/api/spd/revisits?" in r.url and "mine=true" in r.url) as sent:
        board.locator("button").click()
    assert f"date_from={day}" in sent.value.url and f"date_to={day}" in sent.value.url, sent.value.url
    expect(page.locator("#spd-revisit-list")).to_contain_text(names["me"])
    expect(page.locator("#spd-revisit-list")).not_to_contain_text(names["other"])


def test_任务中心只看已升级_随访看板只看异常_筛得出标红那一格(page, base_url, spd_seed, admin_call):
    """P2-1318（第三十八批扫描 AB1-3 之三）：任务中心「已升级」、随访看板「异常随访」两张卡片标红，筛选栏里却没有这一项。
    修后两页各加一项：「只看已升级」送既有参数的组合 escalated=true&open_only=true（与卡片同一句，escalated 单用的语义不改），
    「只看异常」送 abnormal=true。"""
    made = [admin_call("POST", "/api/spd/tasks", {
        "patient_id": spd_seed["patient"]["id"], "title": f"E2E升级任务{n}", "task_type": "followup",
        "org_id": spd_seed["org"]["id"], "due_days": 7}) for n in range(2)]
    for task in made:
        admin_call("POST", f"/api/spd/tasks/{task['id']}/escalate")
    admin_call("POST", "/api/spd/tasks/batch", {"task_ids": [made[1]["id"]], "action": "cancel", "note": "E2E 取消"})
    _login(page, base_url)
    _open_page(page, "spdpath", "标准路径与任务中心")
    page.wait_for_function("() => !routing")
    page.locator('#spd-task-filter [name="escalated"]').check()   # 修前没有这一栏
    with page.expect_request(lambda r: "/api/spd/tasks?" in r.url and "escalated=true" in r.url
                             and "open_only=true" in r.url):   # 两个参数一起送，合起来是卡片那一句
        page.click("#spd-task-filter button.secondary")
    listed = page.locator("#spd-task-list")
    expect(listed).to_contain_text("E2E升级任务0")
    expect(listed).not_to_contain_text("E2E升级任务1")   # 已取消的升级任务不在（卡片不数它）

    _open_page(page, "spdfollowup", "智能随访服务端")
    page.wait_for_function("() => !routing")
    page.locator('#spd-fu-filter [name="abnormal"]').check()   # 修前没有这一栏
    with page.expect_request(lambda r: "/api/spd/followup-records?" in r.url and "abnormal=true" in r.url):
        page.click("#spd-fu-filter button")


@pytest.fixture(scope="module")
def batch_seed(base_url, seed):
    """同一个药两个批号入库：台账按批号查只剩那一批（P1-148）。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    return [post("/api/pharmacy/batches", {
        "org_id": seed["org"]["id"], "drug_code": "E2E-P1148", "drug_name": "E2E召回药", "batch_no": batch_no,
        "expire_date": "2029-03-31", "quantity": qty}, token) for batch_no, qty in (("E2E-BT-1", 5), ("E2E-BT-2", 7))]


def test_药房批次台账按批号查到那一批_召回回执报召回前的余量(page, base_url, batch_seed):
    """P1-148：台账默认只列前 200 个批次，第 201 个起召不回、也查不出发给了谁；加了按药品编码 / 批号查。
    顺带：召回回执原先印 done.available（召回之后恒 0），永远是「退出可用汇总 0 → 0」。"""
    _login(page, base_url)
    _open_page(page, "pharmacy", "中心药房")
    page.fill('#batch-filter input[name="batch_no"]', "E2E-BT-2")
    page.click("#batch-filter button")
    ledger = page.locator("#batch-ledger")
    expect(ledger).not_to_contain_text("E2E-BT-1")
    expect(ledger).to_contain_text("E2E-BT-2")
    target = batch_seed[1]["id"]
    page.click(f'button[data-recall="{target}"]')
    form = _spd_modal_rejected(page, {"reason": "召" * 257})   # 原因写超了（后端 256 字）：框不关、写的还在（P2-607 第七批）
    expect(form.locator('[name="reason"]')).to_have_value("召" * 257)
    _spd_modal(page, {"reason": "E2E 厂家召回"})
    expect(page.locator("#batch-msg")).to_contain_text("退出可用汇总 7 → 0")


@pytest.fixture(scope="module")
def dispense_seed(admin_call, seed):
    """两张已发药的处方（P2-1539）：一张给退药用例冲销，一张一直是已发药——医师看得到这两条记录、行上没有「退药」。"""
    admin_call("POST", "/api/pharmacy/batches", {
        "org_id": seed["org"]["id"], "drug_code": "E2E-P1539", "drug_name": "E2E退药药", "batch_no": "E2E-RV-1",
        "expire_date": "2029-06-30", "quantity": 10})
    out = []
    for _ in range(2):
        rx = admin_call("POST", "/api/prescriptions", {
            "patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "diagnosis_name": "E2E高血压",
            "items": [{"drug_code": "E2E-P1539", "drug_name": "E2E退药药", "daily_dose": 1, "days": 2}]})
        out.append(admin_call("POST", "/api/dispense", {"prescription_id": rx["id"]}))
    return out


def test_中心药房的操作表单与召回按角色给(page, base_url, batch_seed, dispense_seed):
    """P2-430：汇总入库只收管理员、批次入库 / 发药 / 退药 / 调拨只收经办 / 药师、召回只收药师 / 管理层，
    页面原先对谁都摆着——医师打开中心药房，每个按钮点下去都是 403。修后医师只看得到台账与记录。
    退药冲销改成发药记录行上的「退药」按钮之后（P2-1539），口径照旧：医师看得到已发药的记录，行上没有「退药」。"""
    _login(page, base_url, "e2e_doctor", "passw0rd1")
    _open_page(page, "pharmacy", "中心药房")
    expect(page.locator("#batch-ledger")).to_be_visible()   # 查询类照常给
    for form in ("#stock-form", "#batch-form", "#dispense-form", "#transfer-form"):
        expect(page.locator(form)).to_have_count(0)
    expect(page.locator("tr", has_text="E2E退药药 E2E-RV-1×2")).to_have_count(2)   # 记录照常给
    expect(page.locator("button[data-reverse]")).to_have_count(0)
    expect(page.locator("button[data-recall]")).to_have_count(0)
    expect(page.locator("button[data-trace]").first).to_be_visible()   # 「发给了谁」是查询，照常给


def test_退药冲销在发药记录行上点退药_框里写明处方患者批号_原因必填(page, base_url, dispense_seed, admin_read):
    """P2-1539（第四十五批扫描 AI3-4 退药那半 + AI3-8）：退药冲销原先是一张只收「发药记录ID」的表单，点了直接提交——敲错
    一位就冲掉别人的发药，冲销又撤不回；发药记录表也看不出是谁的药、谁发的、谁冲的。修后已发药的行上摆「退药」，点了弹页内框
    （照同页「召回」），框头写明处方号、患者、批号×数量；原因必填、框自己提交；冲销后这一行印出冲销人与原因、不再摆「退药」。"""
    target, kept = dispense_seed
    _login(page, base_url)
    _open_page(page, "pharmacy", "中心药房")
    expect(page.locator("#reverse-form")).to_have_count(0)                     # 修前：只收发药记录号的表单
    page.click(f'button[data-reverse="{target["id"]}"]')
    form = _modal(page)
    expect(form).to_contain_text(f"处方 {target['prescription_id']} · 患者 E2E患者")
    expect(form).to_contain_text("批号×数量：E2E退药药 E2E-RV-1×2")
    form.locator("button[type=submit]").click()                                # 没写原因
    expect(form.locator("[data-modal-msg]")).to_have_text("冲销原因必填")
    assert [d["status"] for d in admin_read(f"/api/dispense?prescription_id={target['prescription_id']}")] == [
        "dispensed"]                                                           # 请求没发
    _redrawn(page, lambda: _spd_modal(page, {"reason": "E2E 发错药当场收回"}))
    row = page.locator("tr", has_text="E2E 发错药当场收回")
    expect(row).to_contain_text("已冲销")
    expect(row).to_contain_text("平台管理员")                                    # 冲销人
    expect(row.locator("button[data-reverse]")).to_have_count(0)
    expect(page.locator(f'button[data-reverse="{kept["id"]}"]')).to_be_visible()   # 没冲销的那条照旧摆着
    assert [d["status"] for d in admin_read(f"/api/dispense?prescription_id={target['prescription_id']}")] == [
        "reversed"]


@pytest.fixture(scope="module")
def vaccine_seed(base_url, seed):
    """一个可用的疫苗批次：接种登记从下拉里选它（P1-154）。"""
    import json
    from urllib.request import Request

    def post(path, payload, token=None):
        req = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    token = post("/api/auth/login", {"username": "admin", "password": "admin123"})["access_token"]
    return post("/api/vaccine-supply/batches", {
        "vaccine_code": "E2E-HEPB", "vaccine_name": "E2E乙肝疫苗", "batch_no": "E2E-HB-2409",
        "expire_date": "2029-12-31", "org_id": seed["org"]["id"], "quantity": 10}, token)


def test_接种登记从下拉选批次_带出疫苗与机构_登记即扣一支(page, base_url, seed, vaccine_seed, admin_read):
    """P1-154：接种登记表单原先没有批次，「批次三查」（过期 / 封存 / 库存）从界面上一次都不执行，
    这一针也挂不到批号上。现在从可用批次里选，选了带出疫苗编码、名称与接种机构。"""
    _login(page, base_url)
    _open_page(page, "vaccination", "疫苗接种")
    form = page.locator("#vac-form")
    form.locator("#vac-batch").select_option(str(vaccine_seed["id"]))
    expect(form.locator('[name="vaccine_code"]')).to_have_value("E2E-HEPB")
    expect(form.locator('[name="vaccine_name"]')).to_have_value("E2E乙肝疫苗")
    expect(form.locator('[name="org_id"]')).to_have_value(str(seed["org"]["id"]))
    form.locator('[name="patient_id"]').fill(str(seed["patient"]["id"]))
    form.locator('[name="site"]').fill("左上臂三角肌")
    form.locator('[name="vaccinator"]').fill("E2E接种员")
    _redrawn(page, lambda: form.locator("button").click())
    records = admin_read(f"/api/vaccination/records?patient_id={seed['patient']['id']}")
    mine = [r for r in records if r["vaccine_code"] == "E2E-HEPB"]
    assert [(r["batch_no"], r["site"], r["vaccinator"]) for r in mine] == [("E2E-HB-2409", "左上臂三角肌", "E2E接种员")]
    batch = next(b for b in admin_read("/api/vaccine-supply/batches?vaccine_code=E2E-HEPB") if b["id"] == vaccine_seed["id"])
    assert (batch["used_quantity"], batch["remaining"]) == (1, 9)            # 修前库存不扣


def test_接种史行点上报AEFI_带着这一剂去AEFI表单_上报带出批号(page, base_url, seed, admin_call, admin_read):
    """P2-1499（第四十四批扫描 AH1-1）：AEFI 表单原先要手填「接种记录ID」，全站却没有一处页面显示记录号——从界面上报的
    AEFI 关联不到剂次、批号恒空。修后接种史表印记录号与批号，每行「上报 AEFI」带着这一剂去「疫苗批次与冷链」页，患者号与
    剂次预填好；AEFI 表单的剂次是下拉，填好患者号即列出他的各剂次。上报后这一例带出批号。"""
    patient = admin_call("POST", "/api/patients",
                         {"name": "E2E接种史AEFI受种者", "id_card": "320981202501011499", "birth_date": "2025-01-01"})
    batch = admin_call("POST", "/api/vaccine-supply/batches", {
        "vaccine_code": "E2E-MMR", "vaccine_name": "E2E麻腮风疫苗", "batch_no": "E2E-MMR-1499",
        "expire_date": "2099-12-31", "org_id": seed["org"]["id"], "quantity": 5})
    dose = admin_call("POST", "/api/vaccination/records", {
        "patient_id": patient["id"], "vaccine_code": "E2E-MMR", "vaccine_name": "E2E麻腮风疫苗",
        "org_id": seed["org"]["id"], "batch_id": batch["id"], "vaccinated_date": "2026-09-01"})
    label = "E2E麻腮风疫苗 第1剂 2026-09-01 批号 E2E-MMR-1499"

    _login(page, base_url)
    _open_page(page, "vaccinesupply", "疫苗批次与冷链")
    page.fill("#aefi-pid", str(patient["id"]))
    page.locator("#aefi-pid").blur()                                  # 填好患者号（change）即列出他的各剂次
    expect(page.locator("#aefi-dose option")).to_have_count(2)
    expect(page.locator("#aefi-dose option").nth(1)).to_have_text(label)
    expect(page.locator("#aefi-dose")).to_have_value("")             # 首项「不关联」，不替人选

    _open_page(page, "vaccination", "疫苗接种")
    page.fill("#vac-hist input[name=patient_id]", str(patient["id"]))
    page.click("#vac-hist button")
    row = page.locator("#vac-hist-result tr", has_text="E2E-MMR-1499")
    expect(row).to_contain_text(str(dose["id"]))                     # 修前接种史表没有记录号、批号
    row.locator(f'button[data-aefi-dose="{dose["id"]}"]').click()
    expect(page.locator("#main h2")).to_have_text("疫苗批次与冷链")
    expect(page.locator("#aefi-pid")).to_have_value(str(patient["id"]))
    expect(page.locator("#aefi-dose")).to_have_value(str(dose["id"]))
    form = page.locator("#aefi-form")
    form.locator('[name="symptom"]').fill("E2E 接种后高热")
    form.locator('[name="onset_date"]').fill("2026-09-02")
    form.locator('[name="org_id"]').fill(str(seed["org"]["id"]))
    _redrawn(page, lambda: form.locator("button").click())
    reports = admin_read(f"/api/vaccine-supply/aefi?patient_id={patient['id']}")
    assert [(r["record_id"], r["batch_no"], r["vaccine_code"]) for r in reports] == [
        (dose["id"], "E2E-MMR-1499", "E2E-MMR")], reports


def test_前端取今天按本地日历_东八区早上8点前不取成昨天(browser, base_url):
    """P2-228：前端取「今天 / 本月」原先拿 `toISOString()` 截（UTC）——东八区早上 8 点前截到的是昨天，每月 1 日截出
    上个月；复诊「完成」写进库的实际日期、对账日、报表月份的默认值都跟着错。真浏览器里把时区拨到东八区、时钟拨到
    10-01 07:30（UTC 还在 09-30），shared.js 的 `localToday()` 得 10-01。"""
    context = browser.new_context(timezone_id="Asia/Shanghai")
    try:
        page = context.new_page()
        page.clock.install(time="2026-09-30T23:30:00Z")
        page.goto(base_url)
        assert page.evaluate("localToday()") == "2026-10-01"
        assert page.evaluate("localToday().slice(0, 7)") == "2026-10"
        assert page.evaluate("new Date().toISOString().slice(0, 10)") == "2026-09-30"   # 修前的写法同一时刻取到昨天
    finally:
        context.close()


def test_体检没总检的排在最前_总检后这一行改标已总检(page, base_url, seed, admin_call, admin_read):
    """P2-409：体检清单只回最新 200 条、行上看不出总检了没有——挤出窗口的那次体检原先就再没有一行给「总检」。
    现在没总检的单独取一遍排在最前，「总检」列标着状态；总检完只改这一行的标记，下方回显结论（不整页重画）。"""
    chk = admin_call("POST", "/api/checkups", {"patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"],
                                               "exam_date": "2026-09-01", "summary": "E2E总检用例"})
    _login(page, base_url)
    _open_page(page, "certs", "证明与体检")
    state = page.locator(f'td[data-chkstate="{chk["id"]}"]')
    expect(state).to_contain_text("待总检")
    page.click(f'button[data-chkreview="{chk["id"]}"]')
    form = _spd_modal_rejected(page, {"final_conclusion": "总" * 1025})   # 写超了（后端 1024 字）：框不关（P2-607 第九批）
    expect(form.locator('[name="final_conclusion"]')).to_have_value("总" * 1025)
    expect(state).to_contain_text("待总检")
    _spd_modal(page, {"final_conclusion": "E2E总检：未见明显异常"})
    expect(state).to_contain_text("已总检")
    expect(page.locator("#chk-detail-body")).to_contain_text("总检结论已保存")
    rows = admin_read(f"/api/checkups?patient_id={seed['patient']['id']}&reviewed=true")
    assert [r["reviewed"] for r in rows if r["id"] == chk["id"]] == [True]


def test_体检只按分项标了异常_清单上列出异常分项而不是空标签(page, base_url, seed, admin_call):
    """P2-422：异常口径是「汇总异常串非空或任一分项异常」，清单显示的却是录入的汇总串——录了分项、没另写汇总的
    体检，清单上是一个空的红标签，看不出哪项异常。修后显示后端给的 `abnormal_text`（与打印件同一口径）。"""
    chk = admin_call("POST", "/api/checkups", {
        "patient_id": seed["patient"]["id"], "org_id": seed["org"]["id"], "exam_date": "2026-09-02",
        "items": [{"item_code": "HB", "item_name": "E2E血红蛋白", "result_value": "95", "abnormal": True},
                  {"item_code": "GLU", "item_name": "E2E空腹血糖", "result_value": "5.1", "abnormal": False}]})
    _login(page, base_url)
    _open_page(page, "certs", "证明与体检")
    row = page.locator(f'tr:has(td[data-chkstate="{chk["id"]}"])')
    expect(row.locator(".tag.red")).to_have_text("E2E血红蛋白")   # 修前是空的红标签


def test_体检登记表单带两条分项_重复编码报在体检面板_回读两条(page, base_url, seed, admin_read):
    """P2-1404（第四十一批扫描 AE3-3）：登记表单原先没有分项框（接口早就收 `items`），页面登记的体检分项永远是 0 条；同一项目
    可以重复录；登记的报错写到页面最上方证明面板的 `#cert-msg`。修后表单可增删分项行（首屏不摆空行），提交时逐行组成
    `items`；同一次体检里项目编码重复整单 422，报在体检面板自己的消息行。开三行、第三行与第一行同编码 → 报错、证明面板的
    消息行不动 → 删掉第三行再登记 → `/items` 回两条，清单上标出勾了异常的那项。"""
    _login(page, base_url)
    _open_page(page, "certs", "证明与体检")
    form = page.locator("#chk-form")
    rows = form.locator(".chk-item")
    expect(rows).to_have_count(0)                                    # 分项选填：首屏不摆空行
    form.locator('[name="patient_id"]').fill(str(seed["patient"]["id"]))
    form.locator('[name="org_id"]').fill(str(seed["org"]["id"]))
    form.locator('[name="exam_date"]').fill("2026-09-05")
    form.locator('[name="summary"]').fill("E2E分项登记")
    for _ in range(3):
        form.locator("#chk-add-item").click()
    expect(rows).to_have_count(3)
    for i, (code, name, value, unit, ref, abnormal) in enumerate((
            ("GLU", "E2E空腹血糖", "5.2", "mmol/L", "3.9-6.1", False),
            ("SBP", "E2E收缩压", "182", "mmHg", "90-139", True),
            ("GLU", "E2E空腹血糖", "12.8", "mmol/L", "3.9-6.1", False))):
        for key, text in (("item_code", code), ("item_name", name), ("result_value", value), ("unit", unit),
                          ("ref_range", ref)):
            rows.nth(i).locator(f'[data-item="{key}"]').fill(text)
        if abnormal:
            rows.nth(i).locator('[data-item="abnormal"]').check()
    form.locator('button:has-text("登记")').click()
    expect(page.locator("#chk-msg")).to_contain_text("同一次体检里项目编码重复：GLU")   # 修前 201，两条 GLU 都收
    expect(page.locator("#cert-msg")).to_have_text("")                               # 不再写到页面最上方
    expect(rows).to_have_count(3)                                                    # 没重画：填好的三行都在
    rows.nth(2).locator("[data-chkdelrow]").click()
    expect(rows).to_have_count(2)
    _submit(page, '#chk-form button:has-text("登记")')
    made = [c for c in admin_read(f"/api/checkups?patient_id={seed['patient']['id']}") if c["summary"] == "E2E分项登记"]
    assert [(c["has_abnormal"], c["abnormal_text"]) for c in made] == [(True, "E2E收缩压")], made
    items = admin_read(f"/api/checkups/{made[0]['id']}/items")
    assert [(i["item_code"], i["result_value"], i["unit"], i["ref_range"], i["abnormal"]) for i in items] == [
        ("GLU", "5.2", "mmol/L", "3.9-6.1", False), ("SBP", "182", "mmHg", "90-139", True)], items
    row = page.locator(f'tr:has(td[data-chkstate="{made[0]["id"]}"])')
    expect(row.locator(".tag.red")).to_have_text("E2E收缩压")


@pytest.fixture(scope="session")
def consent_page_seed(seed, admin_call):
    """知情同意页的前置：经办、管理层各一个账号；种子患者名下两条窗口代录的有效同意（经办撤一条，管理层看另一条）。"""
    for username, role in (("e2e_p2786_op", "operator"), ("e2e_p2786_dir", "director")):
        _p2429_user(admin_call, seed, username, role)
    return [admin_call("POST", "/api/consents", {"patient_id": seed["patient"]["id"], "scene": "family_delegate",
                                                 "evidence": f"E2E签字影像P2786-{i}"}) for i in range(2)]


def test_知情同意页经办打得开_查台账能撤回(page, base_url, seed, admin_call, consent_page_seed):
    """P2-786：更正 / 注销申请的待审清单只给管理层，原先渲染末尾不分角色地取——经办一进页整页只剩「需要以下角色之一：
    管理层」，台账查询、撤回一样也用不了。修后待审清单处说明由管理层审核，台账照常查，「撤回」照常给经办。"""
    target = consent_page_seed[0]["id"]
    _login(page, base_url, "e2e_p2786_op", "passw0rd1")
    _open_page(page, "consents", "知情同意与行权")
    expect(page.locator("#cr-table")).to_contain_text("由管理层审核")   # 修前整页只剩一句 403
    page.fill('#ct-search [name="patient_id"]', str(seed["patient"]["id"]))
    page.click("#ct-search button")
    with _answers(page, [""]):
        page.click(f'button[data-revoke-consent="{target}"]')
        expect(page.locator("#cr-msg")).to_contain_text("已撤回")
    rows = admin_call("GET", f"/api/consents?patient_id={seed['patient']['id']}")
    assert [r["revoked_at"] is not None for r in rows if r["id"] == target] == [True]


def test_知情同意页管理层不摆撤回(page, base_url, seed, consent_page_seed):
    """P2-786：「撤回」只收经办 / 医师 / 公卫，管理层点了必 403，原先照样摆着；待审清单照旧给管理层。"""
    keep = consent_page_seed[1]["id"]
    _login(page, base_url, "e2e_p2786_dir", "passw0rd1")
    _open_page(page, "consents", "知情同意与行权")
    page.fill('#ct-search [name="patient_id"]', str(seed["patient"]["id"]))
    page.click("#ct-search button")
    expect(page.locator(f'button[data-print-consent="{keep}"]')).to_be_visible()
    expect(page.locator("button[data-revoke-consent]")).to_have_count(0)   # 修前每条有效同意都摆着
    expect(page.locator("#cr-table")).not_to_contain_text("由管理层审核")


#: 管理端的内置角色（admin 什么页都看得见、什么接口都调得动，不在此列）。
ROLE_SWEEP = ("director", "doctor", "pharmacist", "public_health", "operator")

#: 已登记、还没修的「导航给了、一进页整页报错」：页面 id → 报错的角色。只减不增——修好一页就划掉一页，不划掉也红。
#: 第二十一批扫描抓到的三页已清零（P1-220 手术麻醉、P2-786 知情同意与行权、P2-787 专病专家端）。
KNOWN_BROKEN_PAGES: dict[str, set[str]] = {}


@pytest.mark.parametrize("role", ROLE_SWEEP)
def test_导航里给了的页_这个角色打开都不整页报错(page, base_url, seed, admin_call, role):
    """P1-220 的防复发（第二十一批「页面给出的动作 vs 后端允许的角色与状态」扫描 N4）：页面注册表对这个角色放行，一进页
    却整页只剩一行报错——render 抛出的错被 `route()` 整页换成一句（core.js）。成因是同一个形状：render 连带取了一份只给
    别的角色的数据、没兜住 403（P1-175 医保协同、P2-459 DRGs 修过；手术麻醉是 P1-220）。静态看不全（取数可能藏在 render
    末尾调用的内层函数里），于是逐角色、逐页真打开一遍：导航里给了的页，打开后不得只剩整页报错。"""
    username = f"e2e_sweep_{role}"
    _p2429_user(admin_call, seed, username, role)
    _login(page, base_url, username, "passw0rd1")
    page.wait_for_function("() => !routing")
    ids = page.eval_on_selector_all("#nav a[data-page]", "links => links.map((a) => a.dataset.page)")
    assert len(ids) >= 40, ids   # 判据自证：确实按角色建出了导航
    broken = {}
    for page_id in ids:
        page.evaluate("""(id) => new Promise((resolve) => {
            if (location.hash === "#" + id) { resolve(); return; }
            window.addEventListener("hashchange", () => resolve(), { once: true });
            location.hash = id;
        })""", page_id)
        page.wait_for_function("() => !routing")   # 等 render 整个跑完（含末尾的内层取数），不是等「加载中…」消失
        error = page.locator("#page-body > p.msg.err:only-child")
        if error.count():
            broken[page_id] = error.inner_text()
    known = {page_id for page_id, roles in KNOWN_BROKEN_PAGES.items() if role in roles}
    assert set(broken) == known, (broken, known)   # 修前 surgery 对医师、药师、公卫、经办都整页报错
