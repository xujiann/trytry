"""页面上的异步事件处理不接 `api()` 的错误：查不到 / 无权 / 校验不过时一声不吭，上一次的结果照旧挂着（P2-378）。

`api()`（三套前端各一个）遇非 2xx 一律抛错，全局没有兜底（没有 unhandledrejection 处理）。事件处理里直接
`await api(...)` 不接，出错时页面什么也不说：查询类的，结果区还是上一次的——接种前评估查另一个孩子失败，
页面上挂着的仍是上一位的「可以接种，本次为第 N 剂」；列表类的，看着像「就这么多」。P2-358 修过药事页一处，
同形状还有十几处（中医辅助辨证、接种史、名老中医医案检索、手术间空档、公卫提醒、患者 / 证书检索、慢专病的
指标趋势 / 评估取量表 / 复诊筛选 / 健康处方查询、居民端解除代管）。

修法：逐个接住（查询类先清空结果区，出错把后端的原因写出来）；这条闸门扫全部前端文件的异步事件处理，
函数体里**每一处** `await api(` / `await authApi(` / `await draw…(` 都要落在某个 `try { … }` 块里、或就地
`.catch(`，否则就算欠账——名单只减不增。原先只看第一处（之前有没有 `try`、整个函数体里有没有 `.catch(`）：
一个处理函数里前面的分支接住了，后面分支的 `await` 就不再看，项目页「撤销完成」与慢专病报告「查看」就漏在
这里（P2-423）。判据是启发式的（按函数体文本、不做真正的语法分析）：它漏报的形状由逐个人工核对兜底，
它报出来的每一条都应当是真的。

P2-1009 补了两种原先数不到的形状：**非 async 的处理函数**（`onsubmit = (e) => { …; drawList(pid); }`）与**表达式体的
处理函数**（`onchange = (e) => draw(e.target.value)`）调本文件里一个会把错误抛出来的 async 函数，不 await、不 try、
也不就地 `.catch(`——接种禁忌清单换号查询失败，清单照旧挂着上一位的禁忌、「解除」按钮挂着上一位的患者号，点解除解的
是上一位的长期禁忌（第二十九批 E2-3 实测）。「会抛」按函数体认：有没接住的 `await api(` / `await authApi(`，或没接住地
await 了另一个会抛的函数；函数自己接住了的，调它的地方不用再接。同名函数（两个页面各有一个 `draw`）按调用处之前最近的
那份声明认。
"""
import re
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FILES = sorted([*STATIC.glob("*.js"), *(STATIC / "m").glob("*.js")])

HANDLER = re.compile(
    r"(?:\.(?:onsubmit|onclick|onchange)\s*=\s*|addEventListener\(\"(?:submit|click|change)\",\s*)"
    r"async\s*\([^)]*\)\s*=>\s*\{")
AWAIT = re.compile(r"await\s+(?:api|authApi|draw\w*)\(")
TRY = re.compile(r"\btry\s*\{")
#: 任意事件处理：async 与否、花括号体或表达式体都算（P2-1009）
HANDLER_ANY = re.compile(
    r"(?:\.(?:onsubmit|onclick|onchange)\s*=\s*|addEventListener\(\"(?:submit|click|change)\",\s*)"
    r"(async\s*)?\([^)]*\)\s*=>\s*")
#: 本文件里声明的 async 函数：顶层 `async function f(`，局部 `const f = async (…) => {`
ASYNC_DECL = re.compile(r"async function (\w+)\s*\(|(?:const|let) (\w+) = async\s*\([^)]*\)\s*=>\s*\{")
API_AWAIT = re.compile(r"await\s+(?:api|authApi)\(")

#: 欠账名单（文件:处理函数开头那一行的文本片段）。只减不增：接住一处划掉一处。
KNOWN_UNGUARDED: set[str] = set()


def _body(src: str, brace: int) -> str:
    """从 `{` 起按括号配对取函数体（模板串的 `${…}` 成对出现，不影响配对）。"""
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    return src[brace:]


def _call_end(body: str, open_paren: int) -> int:
    depth = 0
    for i in range(open_paren, len(body)):
        if body[i] == "(":
            depth += 1
        elif body[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(body) - 1


def _caught(body: str, await_match: re.Match) -> bool:
    """这一处 await 落在某个 `try { … }` 块里，或就地 `.catch(`。"""
    pos = await_match.start()
    for t in TRY.finditer(body):
        block = _body(body, t.end() - 1)
        if t.end() - 1 < pos < t.end() - 1 + len(block):
            return True
    end = _call_end(body, await_match.end() - 1)
    return body[end + 1:].lstrip().startswith(".catch(")


def _expr_body(src: str, start: int) -> str:
    """表达式体（`=> draw(x)`）：到同一层的 `;` 或外层的右括号为止。"""
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return src[start:i]
        elif ch == ";" and depth == 0:
            return src[start:i]
    return src[start:]


def _decls(src: str) -> list[tuple[int, str, str, int]]:
    """本文件的 async 函数声明：（声明位置, 名字, 函数体, 函数体起点）。"""
    out = []
    for m in ASYNC_DECL.finditer(src):
        brace = src.index("{", m.end() - 1)
        out.append((m.start(), m.group(1) or m.group(2), _body(src, brace), brace))
    return out


def _resolve(decls, name: str, pos: int) -> int | None:
    """`pos` 处调用的 `name` 指哪份声明：之前最近的一份；之前没有就取之后第一份（顶层函数先用后声明）。"""
    before = [d[0] for d in decls if d[1] == name and d[0] < pos]
    if before:
        return max(before)
    after = [d[0] for d in decls if d[1] == name and d[0] > pos]
    return min(after) if after else None


def _throwing(decls) -> set[int]:
    """会把错误抛给调用方的声明（按声明位置记）：有没接住的 `await api(`，或没接住地 await 了另一个会抛的函数。"""
    throwing: set[int] = set()
    changed = True
    while changed:
        changed = False
        for start, _name, body, brace in decls:
            if start in throwing:
                continue
            bad = any(not _caught(body, a) for a in API_AWAIT.finditer(body)) or any(
                _resolve(decls, c.group(1), brace + c.start()) in throwing and not _caught(body, c)
                for c in re.finditer(r"await\s+(\w+)\(", body))
            if bad:
                throwing.add(start)
                changed = True
    return throwing


def offenders_in(src: str, label: str) -> set[str]:
    found = set()
    for m in HANDLER.finditer(src):
        body = _body(src, m.end() - 1)
        if all(_caught(body, a) for a in AWAIT.finditer(body)):
            continue
        head = src[src.rfind("\n", 0, m.start()) + 1:m.end()].strip()
        found.add(f"{label}:{head}")
    # 调了本文件里会抛的 async 函数、没接住（P2-1009）：async 与否、表达式体都看
    decls = _decls(src)
    throwing = _throwing(decls)
    names = sorted({name for start, name, _b, _c in decls if start in throwing})
    if not names:
        return found
    call = re.compile(r"(?<![\w.$])(%s)\(" % "|".join(map(re.escape, names)))
    for m in HANDLER_ANY.finditer(src):
        body = _body(src, m.end()) if src[m.end()] == "{" else _expr_body(src, m.end())
        for c in call.finditer(body):
            if body[max(0, c.start() - 9):c.start()].endswith("function "):
                continue
            if _resolve(decls, c.group(1), m.end() + c.start()) in throwing and not _caught(body, c):
                head = src[src.rfind("\n", 0, m.start()) + 1:m.end()].strip()
                found.add(f"{label}:{head}")
                break
    return found


def _offenders() -> set[str]:
    found = set()
    for path in FILES:
        found |= offenders_in(strip_comments(path.read_text(encoding="utf-8")), str(path.relative_to(STATIC)))
    return found


def test_遍历本身没瞎():
    """守卫的守卫：一个处理函数都没认出来，下面那条就成了永远绿。"""
    total = sum(len(HANDLER.findall(p.read_text(encoding="utf-8"))) for p in FILES)
    assert total >= 50, total
    every = sum(len(HANDLER_ANY.findall(p.read_text(encoding="utf-8"))) for p in FILES)
    assert every > total, (every, total)   # 非 async / 表达式体的处理函数也数到了
    decls = sum(len(_decls(strip_comments(p.read_text(encoding="utf-8")))) for p in FILES)
    assert decls >= 100, decls


def test_异步事件处理接住_api_的错误():
    offenders = _offenders()
    new = offenders - KNOWN_UNGUARDED
    assert not new, "以下异步事件处理 await api(...) 却不接错误（出错时页面一声不吭）：\n  " + "\n  ".join(sorted(new))
    fixed = KNOWN_UNGUARDED - offenders
    assert not fixed, "以下欠账已接住，请从 KNOWN_UNGUARDED 划掉：\n  " + "\n  ".join(sorted(fixed))


def test_判据自证_每一处await都要接住():
    """前一个分支接住了、后一个分支没接（P2-423 的原形）：照样红；就地 .catch( 与整段 try 都算接住。"""
    later_branch = """
  $("#page-body").onclick = async (e) => {
    if (a) {
      try { await api("/a", { method: "POST" }); } catch (err) { return setMsg("#m", err.message, false); }
      return route();
    }
    if (b) {
      await api("/b", { method: "POST" });
      return route();
    }
  };"""
    assert offenders_in(later_branch, "probe.js")
    chained = """
  $("#f").onsubmit = async (e) => {
    const rows = await api("/x").catch(() => []);
    await drawThing(rows);
  };"""
    assert offenders_in(chained, "probe.js"), "第二处 await 没接住"
    whole = """
  $("#f").onsubmit = async (e) => {
    try {
      const rows = await api("/x");
      await drawThing(rows);
    } catch (err) { setMsg("#m", err.message, false); }
  };"""
    assert not offenders_in(whole, "probe.js")
    assert not offenders_in(chained.replace("await drawThing(rows);", "await drawThing(rows).catch(() => {});"),
                            "probe.js")


def test_接种前评估查询失败不留上一位的结论():
    src = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = src.index('$("#vac-check").onsubmit')
    body = src[start:src.index("\n  };\n", start)]
    assert body.index('$("#vac-check-result").innerHTML = "";') < body.index("await api(")
    assert "catch (err)" in body and "esc(err.message)" in body


def test_判据自证_非异步处理调了会抛的函数也要接住():
    """P2-1009：禁忌清单的形状——非 async 处理、表达式体处理调一个会抛的 async 函数，不接就红。"""
    src = """
  const drawList = async (pid) => {
    const rows = await api(`/x?p=${pid}`);
    $("#r").innerHTML = rows.length;
  };
  $("#f").onsubmit = (e) => { e.preventDefault(); drawList(1); };
  $("#g").onchange = (e) => drawList(e.target.value);"""
    assert len(offenders_in(src, "probe.js")) == 2, offenders_in(src, "probe.js")
    safe = src.replace("const rows = await api(`/x?p=${pid}`);",
                       "let rows; try { rows = await api(`/x?p=${pid}`); } catch (err) { return; }")
    assert not offenders_in(safe, "probe.js"), "函数自己接住了，调它的地方不用再接"
    chained = src.replace("drawList(1);", "drawList(1).catch(() => {});").replace(
        "=> drawList(e.target.value);", "=> drawList(e.target.value).catch(() => {});")
    assert not offenders_in(chained, "probe.js")
    # 同名函数按调用处之前最近的那份声明认：后一份接住了，它之后的调用不算
    two = src + """
  const drawList = async (pid) => {
    try { await api("/y"); } catch (err) { return; }
  };
  $("#h").onclick = () => drawList(2);"""
    assert len(offenders_in(two, "probe.js")) == 2
    # 会抛是传递的：await 了会抛的函数又没接住，自己也会抛
    transitive = """
  const load = async () => { const r = await api("/z"); return r; };
  const show = async () => { const r = await load(); $("#x").innerHTML = r; };
  $("#b").onclick = () => show();"""
    assert offenders_in(transitive, "probe.js")


def _fn(path, marker, end="\n  };\n"):
    src = (STATIC / path).read_text(encoding="utf-8")
    start = src.index(marker)
    return src[start:src.index(end, start)]


def test_禁忌清单查询失败不留上一位的解除按钮():
    body = _fn("pages-clinical.js", "const drawContras = async")
    assert body.index('$("#contra-result").innerHTML = "";') < body.index("await api(")
    assert "catch (err)" in body and "esc(err.message)" in body


def test_监测记录查询失败不留上一位的记录():
    body = _fn("pages-spd.js", "const measQuery = async")
    assert body.index('$("#spd-meas-result").innerHTML = "";') < body.index("await api(")
    assert "catch (err)" in body
