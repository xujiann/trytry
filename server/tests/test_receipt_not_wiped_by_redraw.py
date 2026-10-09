"""成功回执写进消息行、紧跟着整页重画，回执当场被冲掉（P2-1013，第二十九批「页面状态残留与失败处理」扫描 E2-2）。

`route()` 一进门就把 `#main` 整块换成「加载中…」，消息元素连同刚写的字一起没了；居民端 `loadSpd()` 重画同一块也一样。
`core.js` 自己的注释写着「不在这里 setMsg：下面紧接着 route() 会整页重画，写了也当场被冲掉」，P2-914 / P2-367 / P2-251
各修过一处（先 `await route()` 再写回执）。同形还有十处：开方的肝肾功能提示（后端写明「只随本次响应返回、不入库」，页面
这一行是它唯一的出口——真浏览器实测从来没显示过）、号源批量生成的跳过数、报告修订、审方规则导入、支付流水号 / 失败原因、
日终对账、退款单号、村医批量建档的跳过原因、居民自测「指标偏高，请关注」、转诊规则试算勾了开单时的命中明细；宣教推送走
`postAction`，回执里的送达 / 失败条数直接丢掉。

修法：一律先重画、再写回执。这条闸门扫全部前端文件：写回执（`setMsg(` / `.textContent =`）的那一句后面紧跟
`route()` / `loadSpd()`（等不等都一样，重画是同步换掉的）就红。

重画隔在紧跟的块里同样冲掉（P2-1676，第四十九批扫描 AM1-5）：居民端自查先写结论（风险等级、健康建议、排除原因），
紧跟 `if (r.can_apply && confirm(…)) { 申请; await loadSpd(); }`——上面那条只认「下一句就是重画」，没认出来，结论在
居民点「确定」申请之后当场没了。判据扩到：写回执那一句后面紧跟的复合语句（if / else / try / for / while / switch，
连同 else / catch / finally）自己的语句体里（嵌套函数体不算：挂监听、回调里的重画不是当场发生的）有重画、且最后一次
重画之后没再写回执。`return` 着写的提前退出（`if (!x) return setMsg(…)`）后面的语句走不到，不算。
"""
import re
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FILES = sorted([*STATIC.glob("*.js"), *(STATIC / "m").glob("*.js")])

WRITE = re.compile(r"\bsetMsg\(|\.textContent\s*=(?!=)")
REDRAW = re.compile(r"\s*(?:await\s+)?(?:route|loadSpd)\(\)\s*;")


def _stmt_end(src: str, i: int) -> int:
    """从 `i` 起找这一句的结尾 `;`（跳过字符串与模板，模板里的 `${…}` 按代码配对）；先碰到外层右括号就返回它的位置。"""
    stack: list[str] = []
    j, n = i, len(src)
    while j < n:
        ch = src[j]
        top = stack[-1] if stack else None
        if top == "`":
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                stack.pop()
            elif ch == "$" and src[j + 1:j + 2] == "{":
                stack.append("${")
                j += 2
                continue
            j += 1
            continue
        if ch in "'\"":
            k = j + 1
            while k < n and src[k] != ch:
                k += 2 if src[k] == "\\" else 1
            j = k + 1
            continue
        if ch == "`":
            stack.append("`")
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack:
                return j
            stack.pop()
        elif ch == ";" and not stack:
            return j
        j += 1
    return j


#: 复合语句的开头与它的续段（P2-1676）
COMPOUND = re.compile(r"\s*(?:if|else|try|for|while|switch|catch|finally)\b")
TAIL = re.compile(r"\s*(?:else|catch|finally)\b")
#: 块里任何位置的重画（不要求紧跟）；嵌套函数体的开头（箭头函数 `=> {`、`function … {`）
REDRAW_IN = re.compile(r"\b(?:route|loadSpd)\(\)\s*;")
FUNC_OPEN = re.compile(r"=>\s*\{|\bfunction\b[^{;]*\{")
#: 这一行写回执之前是 `return …`：提前退出，后面的语句走不到
RETURN_WRITE = re.compile(r"\breturn\b[^;{}]*$")


def _block_close(src: str, brace: int) -> int:
    """`brace` 处是 `{`，返回与它配对的 `}` 的位置。"""
    j = brace + 1
    while True:
        e = _stmt_end(src, j)
        if e >= len(src) or src[e] != ";":
            return e
        j = e + 1


def _compound_after(src: str, pos: int) -> str:
    """`pos` 起（跳过空白）若是复合语句，返回它连同 else / catch / finally 续段的全文；不是就返回空串。"""
    m = COMPOUND.match(src, pos)
    if not m:
        return ""
    end = m.end()
    while True:
        k = end + len(src[end:]) - len(src[end:].lstrip())
        head = COMPOUND.match(src, k)   # `else if (…)`
        if head:
            k = end = head.end()
            k += len(src[k:]) - len(src[k:].lstrip())
        if src.startswith("(", k):
            k = _stmt_end(src, k + 1) + 1
            k += len(src[k:]) - len(src[k:].lstrip())
        end = _block_close(src, k) + 1 if src.startswith("{", k) else _stmt_end(src, k) + 1
        tail = TAIL.match(src, end)
        if not tail:
            return src[pos:end]
        end = tail.end()


def _without_functions(text: str) -> str:
    """去掉嵌套函数体（只留函数头）：挂监听、回调里的重画不是当场发生的。"""
    out, i = [], 0
    while (m := FUNC_OPEN.search(text, i)) is not None:
        out.append(text[i:m.end()])
        i = _block_close(text, m.end() - 1)
    return "".join(out) + text[i:]


def _redraw_in_block_unwritten(text: str) -> bool:
    """复合语句体里有重画、且最后一次重画之后没再写回执（P2-1676）。"""
    body = _without_functions(text)
    redraws = list(REDRAW_IN.finditer(body))
    return bool(redraws) and not WRITE.search(body, redraws[-1].end())


def offenders_in(src: str, label: str) -> set[str]:
    found = set()
    for m in WRITE.finditer(src):
        end = _stmt_end(src, m.end() - 1 if m.group(0).startswith("setMsg") else m.end())
        if not (end < len(src) and src[end] == ";"):
            continue
        line_head = src[src.rfind("\n", 0, m.start()) + 1:m.start()]
        if REDRAW.match(src, end + 1) or (
                not RETURN_WRITE.search(line_head) and _redraw_in_block_unwritten(_compound_after(src, end + 1))):
            found.add(f"{label}:{src.count(chr(10), 0, m.start()) + 1}: {src[m.start():end + 1][:60]}")
    return found


def test_写完回执不紧跟重画():
    found = set()
    for path in FILES:
        found |= offenders_in(strip_comments(path.read_text(encoding="utf-8")), str(path.relative_to(STATIC)))
    assert not found, ("以下回执写完紧跟着整页重画（或紧跟的块里重画），当场被冲掉（先 await route() 再写）：\n  "
                       + "\n  ".join(sorted(found)))


def test_判据自证():
    wiped = """
    const r = await api("/x", { method: "POST" });
    setMsg("#m", `导入 ${r.created} 条${r.skipped ? `，跳过 ${r.skipped.length} 条` : ""}`);
    route();"""
    assert offenders_in(wiped, "probe.js"), "模板里嵌模板的回执也要认出来"
    assert offenders_in(wiped.replace("    route();", "    await route();"), "probe.js"), "等不等都一样被冲掉"
    text = """
      $("#msg").textContent =
        r.level === "normal" ? "已保存" : `已保存，指标${r.level}`;
      await loadSpd();"""
    assert offenders_in(text, "probe.js")
    fixed = """
    const r = await api("/x", { method: "POST" });
    await route();
    setMsg("#m", `导入 ${r.created} 条`);"""
    assert not offenders_in(fixed, "probe.js")


def test_判据自证_重画隔在紧跟的块里():
    """P2-1676：写回执与重画之间隔着一个 `if (confirm(…))` 块，原判据只认「下一句就是重画」。"""
    gated = """
      $("#msg").textContent = `风险等级：${r.level}`;
      if (r.can_apply && confirm("申请？")) {
        await api("/apply", { method: "POST" });
        await loadSpd();
      }"""
    assert offenders_in(gated, "probe.js"), "重画隔在紧跟的 if 块里也要认出来"
    assert offenders_in(gated.replace("await loadSpd();", "if (ok) { route(); }"), "probe.js"), "块里再嵌一层也算"
    assert offenders_in(gated.replace("if (r.can_apply", "if (x) { y(); } else if (r.can_apply"), "probe.js"), \
        "else if 续段里的也算"
    rewritten = gated.replace("await loadSpd();", 'await loadSpd();\n        setMsg("#msg", "已申请");')
    assert not offenders_in(rewritten, "probe.js"), "重画之后又写回去的不算"
    listener = """
      setMsg("#m", "已保存");
      if (box) { box.onclick = async () => { await route(); }; }"""
    assert not offenders_in(listener, "probe.js"), "嵌套函数体里的重画不是当场发生的"
    early = """
      if (!x) return setMsg("#m", "请选择", false);
      try { await api("/x"); route(); } catch (err) { setMsg("#m", err.message, false); }"""
    assert not offenders_in(early, "probe.js"), "return 着写的提前退出，后面的语句走不到"


def _src(name):
    return strip_comments((STATIC / name).read_text(encoding="utf-8"))


def test_居民自查结论在申请后的重画之后写回_申请失败接在结论后面():
    """P2-1676：结论原先写在 `confirm` 之前、申请成功后 `await loadSpd()` 整块重画冲掉，申请失败又被错误文案盖掉。"""
    src = _src("m/m.js")
    body = src[src.index('$("#spd-screen-submit").addEventListener("click"'):]
    body = body[:body.index("\n  });\n")]
    assert body.index("await loadSpd();") < body.index("if (msg) msg.textContent = `${verdict}已申请专病管理服务")
    assert '$("#spd-screen-msg").textContent = `${verdict}申请专病管理服务没有提交成功：${err.message}`;' in body


def test_开方的肝肾功能提示在重画之后写():
    src = _src("core.js")
    body = src[src.index('$("#rx-form").onsubmit'):]
    body = body[:body.index("\n  };\n")]
    assert body.index("await route();") < body.index('setMsg("#rx-msg", base + tips')


def test_试算开了单先重画_再画命中明细():
    src = _src("pages-spd.js")
    body = src[src.index('$("#spd-refcheck-form").onsubmit'):]
    body = body[:body.index("\n  };\n")]
    assert body.index("if (r.case) await route();") < body.index('$("#spd-refcheck-box").innerHTML = `')
    assert "if (r.case) route();" not in body


def test_宣教推送回执说出送达与失败条数():
    src = _src("pages-spd.js")
    body = src[src.index('$("#spd-edu-form").onsubmit'):]
    body = body[:body.index("\n  };\n")]
    assert "postAction(" not in body
    assert body.index("await route();") < body.index('setMsg("#spd-edu-msg"')
    assert "${r.sent}" in body and "${r.failed}" in body
