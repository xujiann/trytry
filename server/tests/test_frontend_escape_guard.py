"""P2-23 防复发：innerHTML 插值里 `MAP[x] || x` 形状的未转义兜底不得再出现。

这个形状的坑：映射命中时输出的是代码里的常量文案（安全），映射**未命中**时
`|| x` 把接口返回的原始值原样插进 DOM——后端加一个新枚举值、或值本身来自
用户输入（机构名、事件类型等），就成了存储型 XSS。本轮清掉了 6 处
（core.js 244/466、pages-public 39、pages-clinical 1729、pages-mgmt 115×2），
安全写法是把整个表达式包进 esc()：`${esc(MAP[x] || x)}`。

只匹配"兜底是裸标识符/属性链"的形态：`MAP[x] || esc(x)`（兜底已转义）与
`MAP[x] || "字面量"`（兜底是常量）都不命中，不误伤。

---------------------------------------------------------------------------
补一条（2026-08-26，ADR-0009 逐页迁移时发现）：**同一个缺陷还有第二种写法**，
上面那条正则一条都抓不到——先解构、再插值：

    const [text, color] = MAP[x.status] || [x.status, ""];
    …
    <span class="tag ${color}">${text}</span>      // ← text 是兜底出来的服务端数据

插值处只剩一个裸变量名 `${text}`，`MAP[...]||...` 那个形状根本不在同一行。
2026-08-22 迁第一页时人眼撞见过**一处**（pages-mgmt 的 UNIFIED_STATUS），
当时按个案修掉了；这次按形状全仓库扫，发现同样的写法还有 **33 处**，
散在 core / pages-clinical / pages-mgmt / pages-public / m 五个文件里——
也就是说个案修复漏掉了 97%。教训是：撞见一处未转义，要立刻把**这个形状**
扫一遍，而不是只修眼前这一处。

下面 `test_解构出来的映射兜底不得裸插值` 盯住第二种写法。判据与第一条一致：
兜底是**服务端数据**才算（`|| [x.status, ""]`），兜底是字面量的不算
（`m/m.js` 的 `|| ["未分级", ""]` 就不该命中——它永远不含服务端数据）。
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: ${IDENT[...] || bare.chain} —— 整体未包 esc()、兜底未转义
BARE_MAP_FALLBACK = re.compile(
    r"\$\{\s*[A-Za-z_$][\w$]*\[[^\]\n]+\]\s*\|\|\s*[A-Za-z_$][\w$.]*\s*\}"
)

#: const [text, …] = 任意映射[…] || [兜底, …]  —— 捕获变量名与兜底表达式
#:
#: 判据刻意**不**限定映射名首字母大写、也**不**限定兜底数组的第二个元素是 `""`。
#: 初版两条都限了，自审时用合成用例试出来：`st[p.status] || [p.status, ""]`
#: （小写映射名，本仓库 `spdTag(map, key)` 就是小写形参）和
#: `ST[p.status] || [p.status, "orange"]`（兜底带默认配色）都能绕过去——
#: 而这两种写法的缺陷与被抓到的那 33 处**一模一样**。
#: 守卫的判据比缺陷窄，等于给缺陷留了个拼写上的后门；本轮的教训正是"扫形状"，
#: 那就不该让形状被首字母大小写这种无关的东西挡住。
DESTRUCTURED_FALLBACK = re.compile(
    r"const \[(\w+),[^\]\n]*\] *= *[A-Za-z_$][\w$]*\[[^\]\n]+\]\s*\|\|\s*\[([^\]\n]*?)(?:,[^\]\n]*)?\]"
)


def _strip_comments(src: str) -> str:
    """去掉块注释与整行注释——注释里的字不是代码。

    被自己抓了个现行：`shared.js` 的 `statusTag()` 文档注释里，为了说明"这个组件
    要替换掉的是什么"而**原样抄了一段有缺陷的写法**，扫描当场把它报成了真缺陷。
    `test_frontend_chart_escaping.py` 的文件注释里记着同一个坑（注释里的字面
    `<text>` 标签被正则当成真标签），这里是第二次踩。

    刻意**只**去块注释与整行注释，不做"行内 `//` 到行尾"那种粗暴处理：
    这是安全守卫，过度剥离会**藏起真缺陷**（比如把含 `://` 的模板字符串截断，
    后面真正的裸插值就扫不到了）。宁可少剥一点。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(
        "" if line.lstrip().startswith(("//", "*")) else line
        for line in src.splitlines()
    )


def _enclosing_block(src: str, start: int) -> str:
    """从解构语句往后取到**它所在那层花括号结束**为止。

    一开始用的是"往下数 N 行"的窗口，被自己的用例抓了个现行：`pages-public.js`
    里解构出的 `t` 在 16 行之外还有一个**同名但无关**的 `t`
    （`Object.entries(CHANNELS).map(([v, t]) => …)`，在另一个函数里），
    窗口一放宽就误报。变量作用域的边界是**块**不是行数，所以按括号深度收。
    深度回到 -1 即离开了解构所在的那层，后面的同名变量与这里无关。
    """
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
            if depth < 0:
                return src[start:i]
    return src[start:]


def test_映射兜底不得裸插值():
    offenders = []
    for path in sorted(STATIC.rglob("*.js")):
        for lineno, line in enumerate(
            _strip_comments(path.read_text(encoding="utf-8")).splitlines(), 1
        ):
            if BARE_MAP_FALLBACK.search(line):
                offenders.append(
                    f"{path.relative_to(STATIC)}:{lineno}: {line.strip()[:120]}"
                )
    assert offenders == [], (
        "以下插值用 `MAP[x] || x` 兜底且未过 esc()——映射未命中时用户数据"
        "原样进 DOM（P2-23），应改为 ${esc(MAP[x] || x)}：\n  "
        + "\n  ".join(offenders)
    )


def _destructured_offenders(files):
    """找出"解构出服务端兜底、又把它裸插进模板"的位置。"""
    offenders = []
    for path in files:
        src = _strip_comments(path.read_text(encoding="utf-8"))
        for hit in DESTRUCTURED_FALLBACK.finditer(src):
            var, fallback = hit.group(1), hit.group(2).strip()
            if fallback.startswith(('"', "'")):
                continue        # 兜底是字面量，永远不含服务端数据
            block = _enclosing_block(src, hit.end())
            for bare in re.finditer(r"\$\{" + re.escape(var) + r"\}", block):
                lineno = src[: hit.end() + bare.start()].count("\n") + 1
                offenders.append(
                    f"{path.name}:{lineno}: ${{{var}}} 未转义"
                    f"（兜底是 {fallback}）"
                )
    return offenders


def test_解构出来的映射兜底不得裸插值():
    """第二种写法：`const [text] = MAP[x] || [x.status, ""]` 之后 `${text}` 裸插。

    与本文件第一条守的是同一个缺陷，只是换了个写法就绕开了那条正则——
    2026-08-26 按形状全仓库扫出 33 处（见模块 docstring）。
    """
    offenders = _destructured_offenders(sorted(STATIC.rglob("*.js")))
    assert offenders == [], (
        "以下变量是从 `MAP[x] || [x, \"\"]` 解构出来的——映射未命中时它就是"
        "后端原始值，必须写成 ${esc(变量)}：\n  " + "\n  ".join(offenders)
    )


def test_守卫本身没瞎(tmp_path):
    """防空转：把缺陷重新植回去，上面那条必须抓得到；改成安全写法必须放过。

    没有这条的话，正则写错（比如捕获组挪了位）会让扫描恒返回空列表，
    用例照样绿——那正是"个案修复漏掉 97%"能发生的原因。
    """
    bad = tmp_path / "bad.js"
    bad.write_text(
        'const [text, color] = ST[r.status] || [r.status, ""];\n'
        'return `<span class="tag ${color}">${text}</span>`;\n',
        encoding="utf-8",
    )
    assert _destructured_offenders([bad]), "植回缺陷却没抓到，扫描是空转的"

    good = tmp_path / "good.js"
    good.write_text(
        'const [text, color] = ST[r.status] || [r.status, ""];\n'
        'return `<span class="tag ${color}">${esc(text)}</span>`;\n',
        encoding="utf-8",
    )
    assert _destructured_offenders([good]) == [], "安全写法被误报了"

    literal = tmp_path / "literal.js"
    literal.write_text(
        'const [label, color] = LEVEL_TAGS[c.level] || ["未分级", ""];\n'
        'return `<span class="tag ${color}">${label}</span>`;\n',
        encoding="utf-8",
    )
    assert _destructured_offenders([literal]) == [], (
        "兜底是字面量却被报了——它永远不含服务端数据，误报会逼人加豁免"
    )


@pytest.mark.parametrize(
    "label,src",
    [
        # 初版判据限定了"映射名首字母大写"。本仓库的 `spdTag(map, key)` 就用小写形参，
        # 照着它写一个不安全的版本立刻绕过去。
        ("小写映射名", 'const [text, color] = st[p.status] || [p.status, ""];'),
        # 初版判据要求兜底数组第二个元素**恰好**是 `""`。带个默认配色就绕过去了，
        # 而缺陷一模一样。
        ("兜底带默认配色", 'const [text, color] = ST[p.status] || [p.status, "orange"];'),
        # 初版判据写死了"两元解构"。
        ("三元解构", 'const [text, color, x] = ST[p.status] || [p.status, "", 1];'),
    ],
)
def test_判据不得被无关的拼写差异绕开(tmp_path, label, src):
    """同一个缺陷换个拼写就漏掉，等于给它留后门。

    这三条都是自审时用合成用例试出来的——初版判据（大写映射名 + 兜底恰好是 `""`
    + 两元解构）三条全能绕过。本轮的教训是"扫形状"，那形状就不该被首字母大小写、
    默认配色、解构元数这类与缺陷无关的东西挡住。
    """
    f = tmp_path / "bypass.js"
    f.write_text(src + '\nreturn `<span class="tag ${color}">${text}</span>`;\n', encoding="utf-8")
    assert _destructured_offenders([f]), f"「{label}」这种写法绕过了判据"


# ---------------------------------------------------------------------------
# 第三种失败方式：**根本没有兜底**
#
# 上面两条盯的是"兜底了但没转义"（XSS）。同一次形状扫描顺手带出了第三种写法——
# 查表**不写兜底**：
#
#     const [t, col] = SS[s.status];      // SS 里没有这个 status → undefined
#
# 后果比未转义更直接：解构 undefined 抛 TypeError，而它在 `table()` 的行渲染
# 回调里，于是**整页白屏**，不是这一行降级。实测过一处真的：`renderMedication`
# 的缺药登记，`SS` 只有 registered/purchasing/delivered 三个流转态，而后端会写
# collected / no_show / cancelled（`medication.py` 的 `shortage.status = body.result`）
# ——只要有一条缺药登记结了案，这一页就再也打不开。
#
# 判据只认"**数组解构** + 查表 + 无 `||`"——因为会抛的正是解构这一步。
# 别的查表写法（`MAP[x]` 取单值、`MAP[x]?.y`）不在此列：它们拿到 undefined
# 不会抛，只会把 "undefined" 显示出来。那是另一类问题（显示缺陷而非崩溃），
# 已登记在 docs/TECH_DEBT.md，不混进这条守卫——一条守卫混两种严重度，
# 迟早会因为噪声被加豁免。

#: 判据同样不限定大小写、不要求分号（JS 有 ASI，不写分号照样跑）、不限定解构元数。
#: 初版三条都限了，合成用例一试就绕过去：`ss[s.status];`（小写）、
#: `SS[s.status]`（无分号）、`const [t, col, x] = SS[s.status];`（三元解构）——
#: 三种在 `table()` 回调里都照样整页白屏。
NO_FALLBACK_DESTRUCTURE = re.compile(
    r"const \[\w+,[^\]\n]*\] *= *([A-Za-z_$][\w$]*)\[([^\]\n]+)\]\s*(?:;|$)",
    re.MULTILINE,
)


def _no_fallback_offenders(files):
    offenders = []
    for path in files:
        for lineno, line in enumerate(_strip_comments(path.read_text(encoding="utf-8")).splitlines(), 1):
            hit = NO_FALLBACK_DESTRUCTURE.search(line)
            if hit:
                offenders.append(
                    f"{path.name}:{lineno}: {hit.group(1)}[{hit.group(2)}] 没有兜底"
                )
    return offenders


def test_查表解构必须带兜底否则整页白屏():
    """映射查不到时解构 undefined 会抛 TypeError，整页渲染失败。

    安全写法与本仓库其余 30+ 处一致：`|| [x.status, ""]`，再把兜底出来的值
    过一遍 `esc()`（后端原始状态码是服务端数据）。
    """
    offenders = _no_fallback_offenders(sorted(STATIC.rglob("*.js")))
    assert offenders == [], (
        "以下查表解构没写兜底——映射未命中时解构 undefined 抛 TypeError，"
        "整页白屏而不是这一行降级：\n  " + "\n  ".join(offenders)
    )


def test_无兜底守卫本身没瞎(tmp_path):
    bad = tmp_path / "bad.js"
    bad.write_text("const [t, col] = SS[s.status];\n", encoding="utf-8")
    assert _no_fallback_offenders([bad]), "植回缺陷却没抓到，扫描是空转的"

    good = tmp_path / "good.js"
    good.write_text('const [t, col] = SS[s.status] || [s.status, ""];\n', encoding="utf-8")
    assert _no_fallback_offenders([good]) == [], "带兜底的写法被误报了"


@pytest.mark.parametrize(
    "label,src",
    [
        ("小写映射名", "const [t, col] = ss[s.status];"),
        # JS 有 ASI，不写分号照样跑；初版判据要求行尾分号。
        ("无分号(ASI)", "const [t, col] = SS[s.status]\n"),
        ("三元解构", "const [t, col, x] = SS[s.status];"),
    ],
)
def test_无兜底判据不得被无关的拼写差异绕开(tmp_path, label, src):
    """三种写法在 `table()` 回调里都照样整页白屏，判据不能只认其中一种。"""
    f = tmp_path / "bypass.js"
    f.write_text(src, encoding="utf-8")
    assert _no_fallback_offenders([f]), f"「{label}」这种写法绕过了判据"
