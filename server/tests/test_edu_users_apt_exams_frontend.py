"""继续教育 / 用户与审计 / 预约 / 检查检验 的界面入口守卫（P1-40，四模块清零）。

全平台棘轮只回答"这条路径有没有人调用过"。这四块各有它看不见、而写错了后果
不轻的事：

* **健康宣教的编制侧长期不存在**：居民端 H5 一直在读
  `/api/portal/health-articles`（`test_mobile.py` 断言发布后免登录可读），而
  发布侧一个界面都没有——真环境里那个宣教板块只能永远空着。补上的是整条
  「存稿 → 看得见草稿 → 发布」，不是一个按钮。
* **角色下拉必须来自 roles 表**：建号与调角色后端都对 `roles` 表现查
  （`users.py:_check_role_exists`），只列内置六个的话，角色管理页刚建的自定义
  角色在这里根本选不到，建完没处用。
* **审计链校验要把能力边界一起显示**：哈希链发现得了"记录被改过"，拦不住有库
  权限且知道密钥的人重算整条链。只显示一个绿色"通过"而不显示 caliber，等于把
  它当成了"审计不可篡改"的证明。
* **锚点两个参数成对**：后端缺一报 422，界面先拦一道，免得人对着 422 猜。
* **黑名单删除要带 domain**：同一患者可能同时在「预约爽约」和「缺药不取」两个
  域里，漏掉 domain 会删错那条。
* **报告修订的危急标记是三态**：不传＝不动，传 false＝解除并清空闭环状态。
  做成复选框（只有 true/false）会让"只改结论"变成"顺手解除了危急值"。
* **样本物流只对检验类**：后端对非 lab 一律 422；界面给出会被拒的按钮，等于把
  报错当交互。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
CLINICAL = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
PUBLIC = (STATIC / "pages-public.js").read_text(encoding="utf-8")


def _block(src: str, fn: str, size: int = 14000) -> str:
    """取某个 render/draw 函数往后一段源码。

    按函数名定位而不是全文搜——全文里"这个字符串出现过"证明不了"它出现在
    该页面上"：上一轮就栽过两次，断言匹配到的其实是别的页面或事件分发器里
    的同名片段，变异掉真正的调用点后测试照样绿。
    """
    start = src.index(fn)
    return src[start:start + size]


USERS = _block(CLINICAL, "async function renderUsers()")
AUDIT = _block(CLINICAL, "async function renderAudit()", 8000)
EDU = _block(CLINICAL, "async function renderEducation()", 6000)
ARTICLES = _block(PUBLIC, "async function drawHealthArticles()", 4000)
APT = _block(CORE, "async function renderAppointments()")
EXAM = _block(CORE, "async function renderExams()")


# ---------------------------------------------------------------- 调用点在位

PAGES = {"edu": EDU, "articles": ARTICLES, "users": USERS, "audit": AUDIT, "apt": APT, "exam": EXAM}


@pytest.mark.parametrize(
    ("page", "path"),
    [
        ("edu", "/api/education/courses/${d.custats}/stats"),
        ("edu", "/api/education/live-sessions/${d.liverec}/recording"),
        ("edu", "/api/education/live-sessions/${d.livefb}/feedback"),
        ("edu", "/api/education/live-sessions/${d.livefblist}/feedback"),
        ("articles", "/api/education/articles"),
        ("articles", "/api/education/articles/${hapub}/publish"),
        ("users", "/api/users/roles"),
        ("users", "/api/rbac/roles"),
        ("users", "/api/users/${setstatus}/status"),
        ("users", "/api/users/${pwdreset}/reset-password"),
        ("audit", "/api/audit/verify?"),
        ("audit", "/api/audit/export?"),
        ("audit", "/api/audit/logins?limit=100"),
        ("apt", "/api/appointments/slots/batch"),
        ("apt", "/api/appointments/doctors"),
        ("apt", "/api/appointments/blacklist"),
        ("apt", "/api/appointments/blacklist/${blrm}?domain=${bldomain}"),
        ("exam", "/api/exams/templates"),
        ("exam", "/api/exams/reports/${f.get(\"report_id\")}"),
        ("exam", "/api/exams/reports/${reportId}/revisions"),
        ("exam", "/api/exams/${sample}/sample/advance"),
    ],
)
def test_端点在对应页面上有调用点(page, path):
    assert path in PAGES[page], f"{page} 页面上找不到 {path} 的调用点"


# ---------------------------------------------------------------- 各自的判据

def test_宣教草稿列表与发布在同一块界面上():
    """发布流程是"建稿 → 看见草稿 → 发布"。只有建稿表单而没有草稿列表时，
    发布只能靠"记住刚才返回的 id 再手填"——那正是这块长期空着的根因。"""
    assert 'api("/api/education/articles")' in ARTICLES, "缺少草稿清单的读取"
    assert 'data-hapub="' in ARTICLES, "草稿行上没有发布按钮"
    assert "/publish" in ARTICLES
    # 发布是对不特定公众的一次投放，居民端立刻就能读到，撤不回来
    assert "confirm(" in ARTICLES, "发布前没有二次确认"


def test_宣教编制不复用居民端免登录接口():
    """居民端 `/api/portal/health-articles` 免登录，给它加 status 参数就等于
    把未发布的稿子挂到公网上。编制侧必须是另一条鉴权接口。"""
    # 判的是"有没有真的去调它"，不是"这串字有没有出现过"——本文件与被测代码
    # 的注释里都会提到这条路径，按裸子串判会把注释当成调用点。
    assert 'api("/api/portal/health-articles"' not in ARTICLES


def test_直播回放与反馈只在已结束时给出():
    """后端对排期中的直播一律 409（挂上回放学员点进去是空的）。
    界面给出会被拒的按钮，等于把报错当交互。"""
    assert 'const done = s.status === "finished";' in EDU
    assert "done\n" in EDU or ": done" in EDU, "回放/反馈按钮没有绑到 finished 分支"
    # 按钮本身在 done 分支里
    seg = EDU[EDU.index("const done ="):EDU.index("const rec =")]
    assert "data-liverec=" in seg and "data-livefb=" in seg


def test_角色下拉来自roles表而不是前端硬编码():
    """建号与调角色后端对 roles 表现查；下拉只列内置六个的话，
    角色管理页刚建的自定义角色在这里选不到。"""
    assert 'api("/api/rbac/roles")' in USERS
    assert "const roleOptions = assignable.map(" in USERS
    # 调角色的候选也必须取自同一份，否则"能建不能调"
    assert "const keys = assignable.map((r) => r.key);" in USERS
    # 不再从前端常量展开下拉（ROLE_NAMES 只剩离线兜底）
    assert "Object.entries(ROLE_NAMES).map" not in USERS


def test_停用与重置口令说清即时生效():
    """停用即时生效并吊销既有令牌（deps 每请求校验 status）——本人正在用的
    会话下一次请求就断。说成"下次登录才生效"会让人挑错时机点这个按钮。"""
    assert 'data-setstatus="' in USERS and 'data-to="' in USERS
    assert "即时生效" in USERS
    assert "confirm(" in USERS
    assert "must_change_password" in USERS, "重置口令没有把'须先改密'回给经办人"


def test_审计链校验显示能力边界而不是只显示通过():
    """caliber 字段就是"哈希链拦不住有库权限者重算整条链"这句话。
    只显示绿色"通过"，等于把它当成了"审计不可篡改"的证明。"""
    assert "r.caliber" in AUDIT
    assert "partial_segment" in AUDIT, "抽查片段没有标注，会被当成全量结论"
    assert "anchor_match" in AUDIT, "锚点对账结果没有显示（唯一能抓末尾截断的口径）"


def test_锚点两参数成对由界面先拦():
    """后端缺一报 422，且 422 不会说是哪个没填。"""
    assert "!!aid !== !!ah" in AUDIT
    assert "成对" in AUDIT


def test_黑名单删除带业务域():
    """同一患者可能同时在两个域里，漏掉 domain 会删错那条。"""
    assert "?domain=${bldomain}" in APT
    assert 'data-bldomain="' in APT


def test_批量排班把跳过数说成跳过而不是失败():
    """后端幂等：已有号源的日期跳过而不是报错（开办期常要补生成某几天）。
    把 skipped 报成失败数，会让人以为生成漏了又重跑一遍。"""
    assert "r.skipped" in APT
    assert "已存在" in APT


def test_寻医页面不加机构过滤的理由写在界面上():
    """`find_doctors` 的 docstring 明写横向隔离不设限——在卫生院帮患者约
    县医院的号正是它的用途。没号的医师也一并列出并标注。"""
    assert "跨机构不设限" in APT
    assert "d.bookable" in APT, "没号的医师没有标注，居民会以为这位医师不存在"


def test_报告修订的危急标记是三态():
    """不传＝不动，传 false＝解除并清空闭环状态。做成复选框（只有 true/false）
    会让"只改结论"变成"顺手解除了危急值"。"""
    assert 'if (f.get("critical")) body.critical = f.get("critical") === "true";' in EXAM
    assert "危急标记不变" in EXAM, "下拉里没有'不变'这一档"
    # 修订历史（前值留痕）要能看
    assert "prev_conclusion" in EXAM and "prev_critical" in EXAM


def test_样本物流只对检验类申请给出按钮():
    """后端对非 lab 一律 422。"""
    assert 'r.center_type === "lab" && nextSample' in EXAM
    assert '["pending", "diagnosing"].includes(r.status)' in EXAM, "出报告后仍给推进按钮"


def test_样本状态口径与后端一致():
    """`_SAMPLE_FLOW = {"": "collected", "collected": "in_transit", "in_transit": "received"}`"""
    from app.routers.exams import _SAMPLE_FLOW

    assert '{ "": "collected", collected: "in_transit", in_transit: "received" }' in CORE
    assert set(_SAMPLE_FLOW) == {"", "collected", "in_transit"}
    assert set(_SAMPLE_FLOW.values()) == {"collected", "in_transit", "received"}
