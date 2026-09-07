"""P2-21：存量选择（localStorage 里的 id）没跟这次拉到的列表对过账。

管理端把「当前选中哪条」存进 localStorage 当隐式参数。这些键**跨会话、跨标签页
持久**，而它指向的东西会消失——住院记录会出院，基金池 / 协作分组 / 专病目录会被
删或变得不可见。不校验有两种坏法，都不报错：

1. **静默指错人**（住院临床文书）。`<select>` 只列在院记录，存量 id 出院之后
   没有一个 option 带 `selected`，浏览器于是显示第一条；而下面四个面板与三个
   写入表单仍然指向那条旧记录。取证（`scripts/render_diff.js --dump`，
   夹具 `clinicaldocs_stale`：存量选 #78，#78 已出院、#77 在院）：

       修之前  <select …><option value="77" >#77 患者31 …</option></select>
               病程记录面板 = "这是已出院的78号的病程" / 钱医生
       修之后  <select …><option value="77" selected>#77 患者31 …</option></select>
               病程记录面板 = #77 的记录 / 张医生

   屏幕上写着 #77 患者31，`#note-form` 却 POST 到
   `/api/inpatient/admissions/78/progress-notes`——**病程记录写进了另一个人的档案**。

2. **永久卡死**（基金池 / 协作分组 / 专病 / 门急诊文书 / 统一申请单）。子资源取数
   排在 `#page-body` 赋值之前，后端对不存在的对象一律 404
   （`_pool` / `_get` / `_program` / `encounter_completeness`），对看不到的患者
   403（`assert_patient_visible`）。一抛，`route()` 的 catch 就把整个 `#page-body`
   换成一行错误——连"换一个"的那张列表、那个输入框都渲染不出来；而 id 在
   localStorage 里不会自己消失，于是这一页对这个用户**每次进来都是同一行错误**。
   统一申请单最容易撞上：手输一个本机构没关系的患者 ID 就是 403。

修法分两类，见下面 `KEYS` 的分类：有列表可校验的走 `pickedId()`（core.js），
手输的没有列表可校验，就让取数失败退化成"那一段报错"而不是掀掉整页。

本文件四条：注册表覆盖（新键必须表态）、列表型必须过 `pickedId`、
`pickedId` 只有一份且真做了成员校验、手输型的取数必须容错。
"""
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

#: 管理端 SPA 的全部脚本（`m/` 是居民端与医师端，各有各的存储约定，不在本文范围）。
FILES = ["core.js", "app.js", "pages-clinical.js", "pages-mgmt.js",
         "pages-public.js", "pages-spd.js"]

#: 每个 localStorage 键的分类。**新增键必须在这里表态**，否则第一条用例转红。
#:
#: - ``list``  路径段 id，页面本来就拉了权威列表 → 必须过 `pickedId()` 校验；
#: - ``typed`` 手输 id（没有列表可校验）→ 所在 render 必须容错，不许掀掉整页；
#: - ``state`` 不是 id（期间 / 分类 / 角色 / 令牌），指不到具体对象，无从校验。
#:
#: ``list`` 与 ``typed`` 只许变少：把一个键从 ``state`` 挪进来是收紧，反过来要写理由。
KEYS = {
    "medplat_doc_adm": ("list", "住院记录 id；页面已拉在院列表 inHospital"),
    "medplat_group_id": ("list", "协作分组 id；页面已拉 /api/org-groups"),
    "medplat_fund_pool": ("list", "基金池 id；页面已拉 /api/fund/pools"),
    "medplat_program": ("list", "专病目录 id；页面已拉 /api/disease-programs"),
    "medplat_od_encounter": ("typed", "就诊 id 手输，没有就诊列表可校验"),
    "medplat_sr_patient": ("typed", "患者 id 手输，后端走 assert_patient_visible 会 403"),
    "medplat_cost_org": ("typed", "机构 id 手输；单位成本那次取数本来就 .catch(() => null)"),
    "medplat_acc_period": ("state", "会计期间 YYYY-MM，不是对象 id"),
    "medplat_cost_period": ("state", "成本期间 YYYY-MM，不是对象 id"),
    "medplat_ana_period": ("state", "指标期间 YYYY-MM，不是对象 id"),
    "medplat_group_type": ("state", "分组类型枚举，取值来自本页 <select>"),
    "spd_team_role": ("state", "团队端视角枚举，取值来自本页按钮 dataset"),
    "medplat_role": ("state", "登录角色标记，不是 UI 状态（G3/P1-23）"),
    "medplat_token": ("state", "旧版令牌存量兜底（G3/P1-23），不是 UI 状态"),
    "CSRF_KEY": ("state", "CSRF token（常量名，值为 medplat_csrf），不是 UI 状态"),
}

#: 键 → 它所属的 render 函数名（`typed` 那三条要按函数取正文查容错）。
TYPED_OWNER = {
    "medplat_od_encounter": "renderOutpatientDocs",
    "medplat_sr_patient": "renderServiceRequests",
    "medplat_cost_org": "renderCost",
}


def _strip_comments(src: str) -> str:
    """去掉注释，**保行号**（块注释换等量换行）。

    本轮已经九次栽在「注释不是代码」上，而本次新写的注释里就原样写着
    `localStorage.getItem("medplat_doc_adm")` 在讲缺陷——不剥就会自证通过。
    """
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _code(name: str) -> str:
    return _strip_comments(open(os.path.join(STATIC, name), encoding="utf-8").read())


def _all_code() -> dict[str, str]:
    return {f: _code(f) for f in FILES}


def _body(code: str, fn: str) -> str:
    """取一个 render 函数的正文：从它的声明起，到下一个顶层函数声明为止。"""
    start = code.index(f"function {fn}(")
    nxt = re.search(r"\n(?:async )?function ", code[start + 1:])
    return code[start:start + 1 + nxt.start()] if nxt else code[start:]


def test_每个localStorage键都得在注册表里表态():
    """新增一个键就必须分类——分类本身就是「这是不是个 id」的那次思考。

    唯一的空洞是 `pickedId` 自己那行 `getItem(key)`：`key` 是形参不是键名。
    按**区间**跳过而不是按名字跳过——否则任何一处写成 `getItem(key)` 的裸读
    都能从这条用例底下溜过去。
    """
    unknown = []
    codes = _all_code()
    helper = _body(codes["core.js"], "pickedId")
    helper_span = (codes["core.js"].index(helper), codes["core.js"].index(helper) + len(helper))
    for name, code in codes.items():
        for m in re.finditer(r"localStorage\.(?:get|set|remove)Item\(\s*([^,)]+)", code):
            if name == "core.js" and helper_span[0] <= m.start() < helper_span[1]:
                continue
            key = m.group(1).strip().strip("\"'")
            if key not in KEYS:
                line = code[:m.start()].count("\n") + 1
                unknown.append(f"{name}:{line}  {key}")
    assert not unknown, (
        "有 localStorage 键没在 KEYS 里分类。先回答一个问题：它是不是一个"
        "**指向某条记录的 id**？是就填 list/typed 并按对应规矩收口，"
        "不是就填 state 写明理由：\n  " + "\n  ".join(unknown)
    )


def test_列表型存量选择必须过pickedId():
    """`list` 类的键不许再裸 `getItem` ——裸读就是没跟列表对账。"""
    raw, missing = [], []
    codes = _all_code()
    for key, (kind, _why) in KEYS.items():
        if kind != "list":
            continue
        if not any(f'pickedId("{key}"' in c for c in codes.values()):
            missing.append(key)
        for name, code in codes.items():
            for m in re.finditer(rf'localStorage\.getItem\(\s*"{re.escape(key)}"', code):
                raw.append(f"{name}:{code[:m.start()].count(chr(10)) + 1}  {key}")
    assert not raw, (
        "列表型存量选择又被裸读了——出院/删除之后它仍然是个数字，页面要么静默"
        "指错对象、要么被子资源 404 掀掉且再也换不回来。过 pickedId()：\n  "
        + "\n  ".join(raw)
    )
    assert not missing, f"这些键分类为 list 却没有任何 pickedId() 调用：{missing}"


def test_pickedId只有一份且真做了成员校验():
    """闭环：别让它退化成「换了个名字的 getItem」。"""
    codes = _all_code()
    defs = [n for n, c in codes.items() if re.search(r"function pickedId\s*\(", c)]
    assert defs == ["core.js"], f"pickedId 应当只在 core.js 定义一份，实际：{defs}"
    body = _body(codes["core.js"], "pickedId")
    assert ".some(" in body and "localStorage.getItem" in body, (
        "pickedId 里看不到「拿存量值 + 在列表里找一遍」这两步了——"
        f"它一旦不做成员校验，上面那条用例就只是在数括号：\n{body}"
    )


def test_手输型id的取数必须容错():
    """`typed` 类没有列表可校验，那就不许让它掀掉整页。

    判据是所在 render 函数里出现 `catch`（`try/catch` 或 `.catch(...)` 都算）。
    这条判得粗，但它守的是一个很具体的形状：取数排在 `#page-body` 赋值之前，
    一抛就连筛选框都没了，而 id 在 localStorage 里不会自己消失。
    """
    codes = _all_code()
    naked = []
    for key, fn in TYPED_OWNER.items():
        assert KEYS[key][0] == "typed", f"{key} 不再是 typed，TYPED_OWNER 该跟着改"
        owner = next((c for c in codes.values() if f"function {fn}(" in c), None)
        assert owner is not None, f"找不到 {fn}——键 {key} 换页面了？"
        if "catch" not in _body(owner, fn):
            naked.append(f"{fn}（键 {key}）")
    assert not naked, (
        "手输 id 的取数没有任何容错——写错一个数字（或撞上 403）就把整页换成"
        "一行错误，连改回来的输入框都渲染不出来，而 id 存在 localStorage 里"
        "不会自己消失，这一页就对这个用户永久卡死了：\n  " + "\n  ".join(naked)
    )
