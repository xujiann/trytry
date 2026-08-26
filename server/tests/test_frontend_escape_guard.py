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

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: ${IDENT[...] || bare.chain} —— 整体未包 esc()、兜底未转义
BARE_MAP_FALLBACK = re.compile(
    r"\$\{\s*[A-Za-z_$][\w$]*\[[^\]\n]+\]\s*\|\|\s*[A-Za-z_$][\w$.]*\s*\}"
)

#: const [text, color] = MAP[…] || [兜底, ""]  —— 捕获变量名与兜底表达式
DESTRUCTURED_FALLBACK = re.compile(
    r"const \[(\w+), *\w+\] *= *[A-Z_][\w]*\[[^\]\n]+\]\s*\|\|\s*\[([^\]\n]*?),\s*\"\"\]"
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
            path.read_text(encoding="utf-8").splitlines(), 1
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
        src = path.read_text(encoding="utf-8")
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
