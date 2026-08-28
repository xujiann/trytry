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
- SPA 的出报告/处置反馈用 prompt/confirm 交互，统一注册 dialog 处理器应答。
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
    marker = "e2e-stale"
    page.eval_on_selector("#page-body", f"el => el.dataset.stamp = '{marker}'")
    page.click(selector)
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

    # 3) 出报告并标记为危急值（prompt 填结论 + confirm 选“是”）
    #
    # 这里原先注册了两个 `page.once`，以为一个接 prompt、一个接 confirm。
    # 实际上 Playwright 会把**同一个** dialog 事件派发给当时注册着的全部监听器：
    # 第一个 prompt 一弹，两个 once 同时被消耗掉，随后的 confirm 无人应答被自动
    # 取消——于是"是否危急值"选了否，报告不是危急值，断言当然找不到"危急值"。
    # 本文件开头的 `_answers` 就是为这个坑写的（见其 docstring），这里也用它。
    with _answers(page, ["血钾 7.2mmol/L，危急", ""]):
        page.click("button[data-report]")
        expect(page.locator("#page-body")).to_contain_text("危急值")

    # 4) 危急值操作台：确认接收
    _open_page(page, "critical", "危急值操作台")
    expect(page.locator("#page-body")).to_contain_text("血钾 7.2mmol/L")
    page.click("button[data-ack]")
    expect(page.locator("#page-body")).to_contain_text("已确认")

    # 5) 处置反馈闭环
    page.once("dialog", lambda d: d.accept("已联系患者急诊复查并降钾治疗"))
    page.click("button[data-resolve]")
    expect(page.locator("#page-body")).to_contain_text("已处置")

    # 6) 处置留痕轨迹可查（确认接收 + 处置反馈两条）
    page.click("button[data-trail]")
    expect(page.locator("#crit-trail")).to_contain_text("确认接收")
    expect(page.locator("#crit-trail")).to_contain_text("处置反馈")


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

    # 排班与术中记录都用连续多个 prompt 收集参数，用一个按序应答的处理器统一喂值
    with _answers(page, [str(seed["room"]["id"]), "2026-09-01", "09:00", "11:00"]):
        page.click("button[data-schedule]")
        expect(page.locator("#page-body")).to_contain_text("已排班")
    expect(page.locator("#page-body")).to_contain_text("E2E一号手术间")

    with _answers(page, ["腹腔镜阑尾切除术", "麻醉科周医生", "阑尾化脓", "20"]):
        page.click("button[data-record]")
        expect(page.locator("#page-body")).to_contain_text("已完成")


def test_followup_center_flow(page, base_url, seed):
    """随访中心（T2.4）：术后随访任务自动派生，可在页面完成并计入统计。"""
    _login(page, base_url)
    _open_page(page, "followups", "随访中心")
    expect(page.locator("#page-body")).to_contain_text("术后随访")

    page.once("dialog", lambda d: d.accept("切口愈合良好，无发热"))
    page.click("button[data-done]")
    expect(page.locator("#page-body")).to_contain_text("已完成")


def test_doctor_mobile_workbench_loads(page, base_url, seed):
    """医生移动工作台（块4）：登录后进入待办页签。"""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/m/doctor")
    page.fill("#lg-user", "admin")
    page.fill("#lg-pass", "admin123")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#workbench")).to_be_visible()
    expect(page.locator("#who")).to_contain_text("待办")



# ---------------------------------------------------------------- 阶段十二


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
            "task": task, "referral": referral, "doctor_id": doctor_id}


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
    expect(page.locator("#spd-screen-msg")).to_contain_text("风险等级")
    # 确认弹窗被自动应答成"申请专病管理服务"后，提交链路还有半截在跑：
    # 申请 POST 成功 → `await loadSpd()` 把自查分段**再重画一次**（列出申请单）。
    # m.js 的 loadSpd 没有管理端 route() 那样的串行化，这时立刻切"监测"分段，
    # 两次异步渲染会竞写同一个 #spd-result，后完成的自查重画把监测表单整个
    # 盖掉（约四成概率复现；这是居民端的产品级竞态，见报告）。用例侧等申请
    # 重画的终点信号——申请单卡片"待受理"——落定后再切分段。
    expect(page.locator("#spd-result")).to_contain_text("待受理")

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

    with _answers(page, ["同意上转"]):
        page.click('[data-dspd="referral"]')
        expect(page.locator("#spd-list")).to_contain_text("待卫生院审核")
        page.click("[data-spd-pass]")
        # 通过即推进一格：待卫生院审核 → 待县级接收（重画完成的确定信号）
        expect(page.locator("#spd-list")).to_contain_text("待县级接收")
