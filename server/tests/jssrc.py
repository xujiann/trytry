"""前端源码扫描共用的注释剥离（P2-649）：认得出字符串、模板字面量与正则字面量——引号里的 `/*`、`//` 不是注释。

原先六个前端闸门各抄一份 `re.sub(r"/\\*.*?\\*/", …)`（其中五份再加一刀行内 `//.*$`）：

* `input.accept = "image/*,.pdf";` 里的 `/*` 被当成块注释开头，一直「注释」到下一个 `*/`——`pages-spd.js` 任务办理一段
  54 行、医生端 `doctor.js` 39 行、居民端 `m.js` 27 行，从来没被任何一道前端闸门扫过（裸插值、状态码原样上页、
  查表不兜底……在这几十行里写什么都不会红）；
* 行内 `//` 那一刀从 `"https://…"` 这类字符串里的 `//` 截断整行，后面真正的代码也看不见。

这里只有一份：逐字符走一遍，字符串 / 模板 / 正则字面量原样保留（模板里 `${…}` 的代码照样认注释），字面量之外的
`/* … */` 与 `// …` 才去掉。块注释换成等量换行，行号与原文件一致——报错里的行号照着就能找到。
"""
from __future__ import annotations

#: 正则字面量只可能出现在这些字符之后（其余位置的 `/` 是除号）
_REGEX_AFTER = set("(,=:[!&|?{};+-*%~^<>")
#: 以及这些关键字之后
_REGEX_KEYWORDS = ("return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do")


def strip_comments(src: str) -> str:
    """去掉字面量之外的 `/* … */` 与 `// …`；块注释换成等量换行，其余字符（含全部字面量）原样保留。"""
    out: list[str] = []
    i = _code(src, 0, out, closing=None)
    assert i >= len(src)
    return "".join(out)


def _prev_significant(out: list[str]) -> str:
    """输出里最后一段非空白文字（判断 `/` 是不是正则字面量的开头用）。"""
    tail = "".join(out[-40:]).rstrip()
    return tail


def _starts_regex(out: list[str]) -> bool:
    tail = _prev_significant(out)
    if not tail:
        return True
    if tail[-1] in _REGEX_AFTER:
        return True
    return any(tail.endswith(k) and (len(tail) == len(k) or not (tail[-len(k) - 1].isalnum() or tail[-len(k) - 1] in "_$"))
               for k in _REGEX_KEYWORDS)


def _code(src: str, i: int, out: list[str], closing: str | None) -> int:
    """走一段代码直到文件结束，或（在模板的 `${…}` 里）走到与之配平的 `}`；返回停下的位置。"""
    n = len(src)
    depth = 0
    while i < n:
        c = src[i]
        if c in "\"'":
            j = _quoted(src, i)
            out.append(src[i:j])
            i = j
        elif c == "`":
            i = _template(src, i, out)
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, j))
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and _starts_regex(out):
            j = _regex(src, i)
            out.append(src[i:j])
            i = j
        else:
            if closing is not None:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if depth == 0:
                        return i
                    depth -= 1
            out.append(c)
            i += 1
    return i


def _quoted(src: str, i: int) -> int:
    """`"…"` / `'…'` 从开引号走到收尾引号之后；跨行（写坏了的）就停在行尾，别把后面整个文件吞掉。"""
    quote, j, n = src[i], i + 1, len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if c == "\n":
            return j
        j += 1
    return n


def _template(src: str, i: int, out: list[str]) -> int:
    """模板字面量：文字部分原样保留，`${…}` 里按代码走（那里面的注释照去）。"""
    n = len(src)
    out.append("`")
    j = i + 1
    while j < n:
        c = src[j]
        if c == "\\":
            out.append(src[j:j + 2])
            j += 2
            continue
        if c == "`":
            out.append("`")
            return j + 1
        if src.startswith("${", j):
            out.append("${")
            j = _code(src, j + 2, out, closing="}")
            if j < n:
                out.append("}")
                j += 1
            continue
        out.append(c)
        j += 1
    return n


def _regex(src: str, i: int) -> int:
    """正则字面量：走到不在字符类里的收尾 `/` 与其后的标志；同一行里找不到收尾就当它是除号（只吃掉这一个字符）。"""
    j, n, in_class = i + 1, len(src), False
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return i + 1
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            j += 1
            while j < n and (src[j].isalpha()):
                j += 1
            return j
        j += 1
    return i + 1
