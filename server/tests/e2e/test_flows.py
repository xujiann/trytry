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
    form = page.locator("form.panel:has(button[data-cancel])")
    expect(form).to_be_visible()
    for name, value in values.items():
        field = form.locator(f'[name="{name}"]')
        if field.evaluate("el => el.tagName") == "SELECT":
            field.select_option(value)
        else:
            field.fill(value)
    form.locator("button[type=submit]").click()
    expect(form).to_have_count(0)


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
    expect(page.locator("#login-error")).not_to_be_empty()
    expect(page.locator("#login-view")).to_be_visible()


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
    _spd_modal(page, {"note": "已联系患者急诊复查并降钾治疗"})
    expect(page.locator("#page-body")).to_contain_text("已处置")

    # 6) 处置留痕轨迹可查（确认接收 + 处置反馈两条）
    page.click("button[data-trail]")
    expect(page.locator("#crit-trail")).to_contain_text("确认接收")
    expect(page.locator("#crit-trail")).to_contain_text("处置反馈")


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
    _redrawn(page, lambda: _spd_modal(page, {"decision": "decline", "reason": "患者要求本院复查"}))
    declined = orders()[-1]
    assert declined["id"] != source_id and declined["status"] == "pending", declined
    assert declined["recognition_declined_reason"] == "患者要求本院复查", declined

    fill_and_submit()
    _redrawn(page, lambda: _spd_modal(page, {"decision": "accept"}))
    accepted = orders()[-1]
    assert accepted["status"] == "recognized" and accepted["recognized_from_id"] == source_id, accepted


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
    _spd_modal(page, {"comment": ""})
    expect(page.locator("#cr-msg")).to_contain_text("拒绝申请必须填写审核意见")
    assert status(deact_id) == "pending"
    page.click(button(deact_id, "rejected"))
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
        _redrawn(page, lambda: _spd_modal(page, {"comment": comment}))
        row = app(aid)
        assert (row["status"], row["review_comment"]) == (status, comment), row


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
    row = point()
    assert (row["handled"], row["handle_reason"], row["corrective_action"]) == (
        True, "质控品复溶后放置过久", "更换质控品复测在控"), row


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
    _spd_modal(page, {"service_note": ""})
    expect(page.locator("#hv-msg")).to_contain_text("service_note")
    assert order(done_id)["status"] == "dispatched"
    page.click(f'button[data-hvdone="{done_id}"]')
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
    _redrawn(page, lambda: _spd_modal(page, {"score": "86.5", "comment": "进针角度规范"}))
    (item,) = board()["items"]
    assert (item["score"], item["passed"]) == (86.5, True), item


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

    page.fill("#vital-form input[name=measured_at]", "2026-08-12 08:00")
    page.fill("#vital-form input[name=temperature]", "37.4")
    page.fill("#vital-form input[name=pulse]", "86")
    _submit(page, "#vital-form button")

    expect(page.locator("#page-body")).to_contain_text("文书完整")


def test_surgery_full_flow(page, base_url, seed):
    """手术麻醉（T2.3）：申请 → 审批 → 排班 → 术中记录，状态逐级推进。

    **申请与审批必须是两个人**：`approve_request` 明确拒绝"审批本人提出的
    手术申请"（职责分离）。用例此前用 admin 一个人从头做到尾，那条规则加进来
    之后就一直红着——这不是应用的问题，是用例没跟上业务规则。
    申请改由医师经接口提出（见 seed），页面只驱动审批→排班→术中记录。
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
    _spd_modal(page, {"room_id": str(seed["room"]["id"]), "scheduled_date": "2026-09-01",
                      "start_time": "09:00", "end_time": "11:00"})
    expect(page.locator("#page-body")).to_contain_text("已排班")
    expect(page.locator("#page-body")).to_contain_text("E2E一号手术间")

    # 术中记录：转归此前被写死成"好转"（P1-66），这里选"治愈"并填术前/术后诊断，
    # 然后在记录详情里读回来——证明选的值真的落了库
    page.click("button[data-record]")
    _spd_modal(page, {"actual_surgery_name": "腹腔镜阑尾切除术", "anesthetist_name": "麻醉科周医生",
                      "findings": "阑尾化脓", "blood_loss_ml": "20", "outcome": "治愈",
                      "preop_diagnosis": "急性阑尾炎", "postop_diagnosis": "急性化脓性阑尾炎"})
    expect(page.locator("#page-body")).to_contain_text("已完成")
    page.click("button[data-view]")
    expect(page.locator("#surg-detail-body")).to_contain_text("治愈")


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
    _spd_modal(page, {"result": "切口愈合良好，无发热\n嘱两周后门诊复查"})
    expect(page.locator("#page-body")).to_contain_text("已完成")
    assert status() == "done"


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
               {"admission_id": seed["admission"]["id"], "surgery_name": "E2E移动端疝修补术"}, doctor)
    call(f"/api/surgery/requests/{req['id']}/approve", {"approved": True}, admin)
    call(f"/api/surgery/requests/{req['id']}/schedule",
         {"room_id": seed["room"]["id"], "scheduled_date": "2026-09-02",
          "start_time": "13:00", "end_time": "14:00"}, admin)
    return {"request": req, "read": lambda path: call(path, None, admin)}


def test_医生移动端术中记录在卡片内表单里填_转归可选(page, base_url, surgery_mobile_seed):
    """P2-38 / P1-66：移动端"填写术中记录"原先四连问、**转归写死"好转"**。换成卡片内表单后
    转归可选、术式预填；出血量写错由后端报人话。最后经接口读回，证明选的转归真的落了库。"""
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
    form.locator("select[name=outcome]").select_option("未愈")
    form.locator("input[name=postop_diagnosis]").fill("腹股沟斜疝")
    form.locator("input[name=blood_loss_ml]").fill("五十")
    form.locator("button[type=submit]").click()
    expect(page.locator("#surgery-msg")).to_contain_text("blood_loss_ml")
    form.locator("input[name=blood_loss_ml]").fill("50")
    form.locator("button[type=submit]").click()
    expect(page.locator("#surgery-msg")).to_contain_text("术中记录已提交")

    request_id = surgery_mobile_seed["request"]["id"]
    record = surgery_mobile_seed["read"](f"/api/surgery/requests/{request_id}/record")
    assert record["outcome"] == "未愈" and record["postop_diagnosis"] == "腹股沟斜疝", record


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

    page.click("button[data-visit]")
    _spd_modal(page, {"visit_type": "prenatal", "gest_week": "30", "bp": "150/95",
                      "visit_date": "2026-09-20"})
    expect(page.locator("#page-body")).to_contain_text("妊娠期高血压可能")

    page.click("button[data-delivery]")
    _spd_modal(page, {"org_id": str(seed["org"]["id"]), "delivery_date": "2026-09-22",
                      "delivery_mode": "cesarean", "newborn_count": "2"})
    expect(page.locator("#page-body")).to_contain_text("已分娩")

    page.fill("#child-form input[name=name]", "E2E新生儿")
    page.fill("#child-form input[name=birth_date]", "2026-09-22")
    _submit(page, "#child-form button")

    # 体重写成中文：此前 Number() 得到 NaN、序列化成 null，值被悄悄丢掉也不报错
    page.click("button[data-cvisit]")
    _spd_modal(page, {"visit_type": "newborn", "weight_kg": "三点五"})
    expect(page.locator("#mat-msg")).to_contain_text("weight_kg")
    page.click("button[data-cvisit]")
    _spd_modal(page, {"visit_type": "newborn", "height_cm": "50.5", "weight_kg": "3.4"})
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
    _spd_modal(page, {"change_type": "transfer"})  # 调动却没选调入机构
    expect(page.locator("#hrf-msg")).to_contain_text("调动须指定调入机构")
    page.locator("tr", has_text="E2E员工甲").locator("button[data-empchg]").click()
    _spd_modal(page, {"change_type": "regularize", "effective_date": "2026-09-24", "detail": "试用期满"})
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
    _spd_modal(page, {"movement_type": "issue", "quantity": "2", "note": "门诊领用"})
    expect(page.locator("#hrf-msg")).to_have_text("")
    page.locator("tr", has_text="E2E打印机").locator("button[data-assethist]").click()
    expect(page.locator("#assetmv-list")).to_contain_text("门诊领用")


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
    _spd_modal(page, {"received_quantity": "11"})
    expect(page.locator("#mat-msg")).to_contain_text("验收数量不得超过采购数量")
    page.click(f'button[data-receive="{pid}"]')
    _spd_modal(page, {"note": "E2E到货验收"})  # 数量默认就是采购量 10
    expect(page.locator("tr", has_text="E2E一次性手术衣")).to_contain_text("已验收")

    page.click('button[data-use="E2E-HV-1"]')
    _spd_modal(page, {"patient_id": str(seed["patient"]["id"])})  # 手术可空
    expect(page.locator("tr", has_text="E2E冠脉支架")).to_contain_text("E2E患者")


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
    expect(row).to_contain_text(f"E2E内科病区 / {bed2}")

    page.click(f'button[data-order="{adm_id}"]')
    _redrawn(page, lambda: _spd_modal(page, {"order_type": "temp", "content": "E2E呋塞米 20mg iv st"}))
    page.click(f'button[data-orders="{adm_id}"]')
    expect(page.locator("#inp-orders tr", has_text="E2E呋塞米 20mg iv st")).to_contain_text("临时")

    page.click(f'button[data-summary="{adm_id}"]')
    _redrawn(page, lambda: _spd_modal(page, {"discharge_diagnosis": "E2E慢性心力衰竭", "total_cost": "8888.5",
                                             "drug_cost": "3000.25", "outcome": "死亡", "note": "E2E抢救无效"}))
    summary = page.evaluate(
        "async (id) => await api(`/api/inpatient/admissions/${id}/case-summary`)", adm_id)
    assert (summary["outcome"], summary["total_cost"], summary["note"]) == ("死亡", 8888.5, "E2E抢救无效"), summary


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
    _redrawn(page, lambda: _spd_modal(page, {"measures": "E2E已组织专项培训"}))
    page.click(f'button[data-impdone="{tid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"completion_note": "E2E整改完成，抽查复核达标"}))
    assert task()["status"] == "completed"

    page.click(f'button[data-impno="{tid}"]')
    page.locator("form.panel button[data-cancel]").click()
    expect(page.locator("form.panel:has(button[data-cancel])")).to_have_count(0)
    assert task()["status"] == "completed"  # 原先点取消照样退回

    page.click(f'button[data-impno="{tid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"comment": "E2E抽查样本不足，补充后再报"}))
    after = task()
    assert (after["status"], after["measures"]) == ("in_progress", "E2E已组织专项培训"), after


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
    _redrawn(page, lambda: _spd_modal(page, {"comment": "E2E同意，已电话确认医师用意"}))
    assert status(to_review) == "approved"

    page.click(f'button[data-rxcomment="{to_comment}"]')
    expect(page.locator("form.panel:has(button[data-cancel])")).to_contain_text("点评要点")
    _spd_modal(page, {"grade": "unreasonable"})  # 不合理却什么都没写
    expect(page.locator("#rx-msg")).to_contain_text("不合理处方须注明问题类型或点评意见")
    page.click(f'button[data-rxcomment="{to_comment}"]')
    _redrawn(page, lambda: _spd_modal(page, {"grade": "unreasonable", "issues": "E2E重复用药"}))
    reviews = page.evaluate("async () => await api('/api/prescriptions/comment-reviews')")
    mine = [r for r in reviews if r["prescription_id"] == to_comment]
    assert [(r["grade"], r["issues"]) for r in mine] == [("unreasonable", "E2E重复用药")], mine


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
    _spd_modal(page, {"reply": "E2E可续方", "doctor_name": "E2E全科医生", "prescription_id": "三十一"})
    expect(page.locator("#tm-msg")).to_contain_text("prescription_id")

    page.click(f'button[data-reply="{cid}"]')
    _redrawn(page, lambda: _spd_modal(page, {"reply": "E2E可续方，按原剂量", "doctor_name": "E2E全科医生",
                                             "prescription_id": str(rx_id)}))
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


def test_spd_resident_selfscreen_apply_measure(page, base_url, spd_seed):
    """居民端：验证码登录 → 实名绑定 → 高危自查（顺手申请服务）→ 自报监测数据。"""
    page.goto(f"{base_url}/m/")
    page.click('[data-tab="archive"]')
    page.fill("#in-phone", spd_seed["patient"]["phone"])
    page.click("#btn-send-code")  # console 短信通道：演示验证码自动回填
    expect(page.locator("#in-code")).not_to_have_value("")
    page.click('#sms-form button[type="submit"]')
    # 实名绑定（姓名 + 身份证与预置患者一致）；手机号与档案匹配时会自动绑定，
    # 直接进入档案页——两种落点都合法
    page.wait_for_selector("#pane-bind:not(.hidden), #pane-archive:not(.hidden)")
    if page.locator("#pane-bind").is_visible():
        page.fill("#in-name", spd_seed["patient"]["name"])
        page.fill("#in-idcard", spd_seed["patient"]["id_card"])
        page.click('#bind-form button[type="submit"]')
    expect(page.locator("#pane-archive")).to_be_visible()

    # 自查：高危答案 → 结果提示；确认弹窗即"申请专病管理服务"
    page.on("dialog", lambda d: d.accept())
    page.click('[data-tab="spd"]')
    page.click('[data-spd="screen"]')
    page.wait_for_selector("#spd-scale")
    for sel in page.locator("[data-q]").all():
        sel.select_option("是")
    page.click("#spd-screen-submit")
    # 只断言链路的**稳定终态**：申请单卡片"待受理"。不断言"风险等级"提示——
    # 那是瞬态文本：确认申请后 `await loadSpd()` 重画自查分段，`#spd-screen-msg`
    # 被重建为空，提示只活在"POST 返回 → 重画完成"的几十毫秒里，断言它等于
    # 跟自家重画抢时间（本地常赢、CI 慢半拍就输——真在 CI 上 flake 过一次，
    # 而"待受理"只有筛查+申请两步都成功才会出现，覆盖不打折）。
    expect(page.locator("#spd-result")).to_contain_text("待受理")
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
