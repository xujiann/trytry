"""成功回执写进消息行、紧跟着整页重画，回执当场被冲掉（P2-1013，第二十九批「页面状态残留与失败处理」扫描 E2-2）。

`route()` 一进门就把 `#main` 整块换成「加载中…」，消息元素连同刚写的字一起没了；居民端 `loadSpd()` 重画同一块也一样。
`core.js` 自己的注释写着「不在这里 setMsg：下面紧接着 route() 会整页重画，写了也当场被冲掉」，P2-914 / P2-367 / P2-251
各修过一处（先 `await route()` 再写回执）。同形还有十处：开方的肝肾功能提示（后端写明「只随本次响应返回、不入库」，页面
这一行是它唯一的出口——真浏览器实测从来没显示过）、号源批量生成的跳过数、报告修订、审方规则导入、支付流水号 / 失败原因、
日终对账、退款单号、村医批量建档的跳过原因、居民自测「指标偏高，请关注」、转诊规则试算勾了开单时的命中明细；宣教推送走
`postAction`，回执里的送达 / 失败条数直接丢掉。

修法：一律先重画、再写回执。这条闸门扫全部前端文件：写回执（`setMsg(` / `.textContent =`）的那一句后面紧跟
`route()` / `loadSpd()`（等不等都一样，重画是同步换掉的）就红。
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


def offenders_in(src: str, label: str) -> set[str]:
    found = set()
    for m in WRITE.finditer(src):
        end = _stmt_end(src, m.end() - 1 if m.group(0).startswith("setMsg") else m.end())
        if end < len(src) and src[end] == ";" and REDRAW.match(src, end + 1):
            found.add(f"{label}:{src.count(chr(10), 0, m.start()) + 1}: {src[m.start():end + 1][:60]}")
    return found


def test_写完回执不紧跟重画():
    found = set()
    for path in FILES:
        found |= offenders_in(strip_comments(path.read_text(encoding="utf-8")), str(path.relative_to(STATIC)))
    assert not found, "以下回执写完紧跟着整页重画，当场被冲掉（先 await route() 再写）：\n  " + "\n  ".join(sorted(found))


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


def _src(name):
    return strip_comments((STATIC / name).read_text(encoding="utf-8"))


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
