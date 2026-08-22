"""培训手册截图：在演示环境上跑一遍，把 19 张图落到 docs/培训手册/img/。

用法（先起服务 + 灌演示数据）：

    uvicorn app.main:app --port 8055
    python scripts/seed_demo.py http://127.0.0.1:8055
    python scripts/capture_manual_shots.py http://127.0.0.1:8055

做成脚本而不是手工截图，是因为界面会改：手册里的图迟早和实物对不上，
而"重截一遍"如果要靠人点二十次，就永远不会发生。这个脚本重跑一次即可全量刷新。

浏览器用容器里预装的 chromium（`PLAYWRIGHT_CHROMIUM_PATH` 可覆盖）。
截图只截**演示数据**，不含任何真实患者信息。
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8055"
OUT = pathlib.Path(__file__).resolve().parents[2] / "docs" / "培训手册" / "img"
CHROMIUM = "/opt/pw-browsers/chromium"

#: 管理端：(文件名, 导航页 id, 进页面后要不要多等一会儿)
ADMIN_SHOTS = [
    ("成员-01-服务团队端工作台", "spdteam"),
    ("成员-02-筛查登记与目标池", "spdpatients"),
    ("成员-03-路径实例与任务中心", "spdpath"),
    ("调度-01-全程管理中心工作台", "spdcenter"),
    ("调度-02-任务中心筛选与批量操作", "spdpath"),
    ("调度-03-呼叫任务台账", "spdfollowup"),
    ("调度-04-报告模板与推送任务", "spdreport"),
    ("卫健-01-卫健管理端工作台", "spdhc"),
    ("卫健-02-考核方案与得分明细", "spdassess"),
    ("卫健-03-报告实例", "spdreport"),
    ("个案-01-个案管理师工作台", "spdteam"),
    ("个案-02-评估与自动派生的干预复诊", "spdteam"),
    ("个案-03-生命周期处置与迁入确认", "spdpatients"),
    ("成员-04-患者分组与规则编辑器", "spdpatients"),
]

#: 医生移动端：(文件名, 页签, 慢专病分段)
DOCTOR_SHOTS = [
    ("村医-02-慢专病工作台首屏", "spd", "todo"),
    ("村医-03-任务办结弹窗", "spd", "todo"),
    ("村医-04-转诊办理列表", "spd", "referral"),
    ("村医-05-积分与绩效页", "spd", "perf"),
]


def shot(page, name, full_page=False):
    OUT.mkdir(parents=True, exist_ok=True)
    # 截图前回到页首：点导航后侧边栏会滚动到当前项，不回顶会截到半截页面
    page.evaluate("() => { window.scrollTo(0, 0);"
                  " document.querySelectorAll('nav, #nav, aside')"
                  ".forEach(e => { e.scrollTop = 0; }); }")
    page.wait_for_timeout(150)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full_page)
    print("已截图", path.relative_to(OUT.parents[2]))


def capture_admin(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_default_timeout(20000)
    page.goto(BASE)
    page.fill("#login-username", "admin")
    page.fill("#login-password", "admin123")
    page.click("#login-form button[type=submit]")
    page.wait_for_selector("#app-view")
    for name, page_id in ADMIN_SHOTS:
        page.click(f'#nav a[data-page="{page_id}"]')
        # 等到真的画出内容为止（"加载中…"不算），否则截到的是空白页
        page.wait_for_function(
            "() => { const el = document.querySelector('#page-body');"
            " return el && el.textContent.trim() && el.textContent.trim() !== '加载中…'; }"
        )
        page.wait_for_timeout(700)  # 图表与二次请求
        # 只截一屏而不是整页：整页会把左侧那条很长的导航一起拉成三四千像素高，
        # 手册里根本看不清。一屏里既有导航位置、又有页面首屏内容，正合适
        shot(page, name)
    context.close()


def capture_doctor(browser):
    context = browser.new_context(viewport={"width": 420, "height": 900},
                                  device_scale_factor=2, is_mobile=True,
                                  has_touch=True)
    page = context.new_page()
    page.set_default_timeout(20000)
    page.goto(f"{BASE}/m/doctor")
    shot(page, "村医-01-医生移动端登录页")
    page.fill("#lg-user", "doc_village")
    page.fill("#lg-pass", "doctor123")
    page.click('#login-form button[type="submit"]')
    page.wait_for_selector("#workbench:not(.hidden)")
    for name, tab, segment in DOCTOR_SHOTS:
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_timeout(600)
        page.click(f'[data-dspd="{segment}"]')
        page.wait_for_timeout(900)
        if name.endswith("任务办结弹窗"):
            # 办结走 prompt：截图前把它接住，免得弹窗把页面挡住又截不到内容
            page.once("dialog", lambda d: d.dismiss())
            buttons = page.locator("[data-spd-done]")
            if buttons.count():
                buttons.first.click()
                page.wait_for_timeout(400)
        shot(page, name)
    context.close()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        try:
            capture_admin(browser)
            capture_doctor(browser)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
