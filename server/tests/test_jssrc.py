"""前端闸门共用的注释剥离认得出字面量（P2-649）。

六个前端闸门原先各抄一份正则剥注释：`"image/*,.pdf"` 里的 `/*` 被当成块注释开头、一直「注释」到下一个 `*/`，
`pages-spd.js`（任务办理一段）、`m/doctor.js`、`m/m.js` 各有几十行从来没被任何前端闸门扫过；行内 `//` 那一刀还会从
`"https://…"` 里截断整行。现在只有 `tests/jssrc.py` 一份，这里钉住它的判据。
"""
from pathlib import Path

import pytest

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
ALL_JS = sorted(STATIC.glob("*.js")) + sorted((STATIC / "m").glob("*.js"))


def test_字符串里的斜杠星号不是块注释():
    src = 'input.accept = "image/*,.pdf";\nconst x = `${esc(a)}`;\n/* 真注释 */ const y = 1;\n'
    out = strip_comments(src)
    assert "const x = `${esc(a)}`;" in out     # 修前被当成注释抹掉了
    assert "真注释" not in out and "const y = 1;" in out


def test_字符串里的双斜杠不是行注释():
    out = strip_comments('fetch("https://example.org/a"); render(b);  // 行尾注释\n')
    assert out == 'fetch("https://example.org/a"); render(b);  \n'


def test_正则字面量里的引号不开字符串():
    src = "const e = s.replace(/'/g, \"&#39;\").replace(/\"/g, \"&quot;\");  // 转义\nconst after = `${esc(x)}`;\n"
    out = strip_comments(src)
    assert "const after = `${esc(x)}`;" in out and "转义" not in out
    assert "a / b / c" in strip_comments("const r = a / b / c;  // 除号\n")


def test_模板插值里的注释照去_文字部分原样():
    src = "const t = `链接 http://x/y ${ /* 内联 */ esc(v) } 尾`;\n"
    assert strip_comments(src) == "const t = `链接 http://x/y ${  esc(v) } 尾`;\n"


def test_块注释换成等量换行_行号不变():
    src = "a();\n/* 第一行\n第二行\n第三行 */\nb();\n"
    out = strip_comments(src)
    assert out.splitlines() == ["a();", "", "", "", "b();"]


@pytest.mark.parametrize("path", ALL_JS, ids=lambda p: p.name)
def test_全部前端脚本剥完行数不变(path):
    src = path.read_text(encoding="utf-8")
    assert len(strip_comments(src).splitlines()) == len(src.splitlines())


def test_原先被整段抹掉的代码现在扫得到():
    spd = strip_comments((STATIC / "pages-spd.js").read_text(encoding="utf-8"))
    assert 'spdModal("办结任务"' in spd                          # 修前在「image/*」之后的五十几行里
    # 三处上传框原先写 `accept = "image/*,.pdf"`，字符串里的 `/*` 被当成注释开头、一路抹到下一个 `*/`。P2-1083 把这三处
    # 改成了附件白名单的写法，文件里不再有这个字符串——同一个形状用一段等价的源码钉住
    src = 'input.accept = "image/*,.pdf";\nconst done = () => spdModal("办结任务", []);\n/* 真注释 */\nnext();\n'
    out = strip_comments(src)
    assert 'spdModal("办结任务"' in out and "next();" in out and "真注释" not in out
    assert out.count("\n") == src.count("\n")
