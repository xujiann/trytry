"""页面上的异步事件处理不接 `api()` 的错误：查不到 / 无权 / 校验不过时一声不吭，上一次的结果照旧挂着（P2-378）。

`api()`（三套前端各一个）遇非 2xx 一律抛错，全局没有兜底（没有 unhandledrejection 处理）。事件处理里直接
`await api(...)` 不接，出错时页面什么也不说：查询类的，结果区还是上一次的——接种前评估查另一个孩子失败，
页面上挂着的仍是上一位的「可以接种，本次为第 N 剂」；列表类的，看着像「就这么多」。P2-358 修过药事页一处，
同形状还有十几处（中医辅助辨证、接种史、名老中医医案检索、手术间空档、公卫提醒、患者 / 证书检索、慢专病的
指标趋势 / 评估取量表 / 复诊筛选 / 健康处方查询、居民端解除代管）。

修法：逐个接住（查询类先清空结果区，出错把后端的原因写出来）；这条闸门扫全部前端文件的异步事件处理，
函数体里第一处 `await api(` / `await authApi(` / `await draw…(` 之前没有 `try` 也没有 `.catch(` 的就算欠账——
名单只减不增。判据是启发式的（按函数体文本、不做真正的语法分析）：它漏报的形状由逐个人工核对兜底，
它报出来的每一条都应当是真的。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FILES = sorted([*STATIC.glob("*.js"), *(STATIC / "m").glob("*.js")])

HANDLER = re.compile(
    r"(?:\.(?:onsubmit|onclick|onchange)\s*=\s*|addEventListener\(\"(?:submit|click|change)\",\s*)"
    r"async\s*\([^)]*\)\s*=>\s*\{")
FIRST_AWAIT = re.compile(r"await\s+(?:api|authApi|draw\w*)\(")

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


def _offenders() -> set[str]:
    found = set()
    for path in FILES:
        src = path.read_text(encoding="utf-8")
        for m in HANDLER.finditer(src):
            body = _body(src, m.end() - 1)
            first = FIRST_AWAIT.search(body)
            if not first:
                continue
            before = body[:first.start()]
            if re.search(r"\btry\b", before) or ".catch(" in body:
                continue
            line = src[:m.start()].count("\n") + 1
            head = src[src.rfind("\n", 0, m.start()) + 1:m.end()].strip()
            found.add(f"{path.relative_to(STATIC)}:{head}")
            assert line   # 行号只为报错好找
    return found


def test_遍历本身没瞎():
    """守卫的守卫：一个处理函数都没认出来，下面那条就成了永远绿。"""
    total = sum(len(HANDLER.findall(p.read_text(encoding="utf-8"))) for p in FILES)
    assert total >= 50, total


def test_异步事件处理接住_api_的错误():
    offenders = _offenders()
    new = offenders - KNOWN_UNGUARDED
    assert not new, "以下异步事件处理 await api(...) 却不接错误（出错时页面一声不吭）：\n  " + "\n  ".join(sorted(new))
    fixed = KNOWN_UNGUARDED - offenders
    assert not fixed, "以下欠账已接住，请从 KNOWN_UNGUARDED 划掉：\n  " + "\n  ".join(sorted(fixed))


def test_接种前评估查询失败不留上一位的结论():
    src = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = src.index('$("#vac-check").onsubmit')
    body = src[start:src.index("\n  };\n", start)]
    assert body.index('$("#vac-check-result").innerHTML = "";') < body.index("await api(")
    assert "catch (err)" in body and "esc(err.message)" in body
