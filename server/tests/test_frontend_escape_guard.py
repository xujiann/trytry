"""P2-23 防复发：innerHTML 插值里 `MAP[x] || x` 形状的未转义兜底不得再出现。

这个形状的坑：映射命中时输出的是代码里的常量文案（安全），映射**未命中**时
`|| x` 把接口返回的原始值原样插进 DOM——后端加一个新枚举值、或值本身来自
用户输入（机构名、事件类型等），就成了存储型 XSS。本轮清掉了 6 处
（core.js 244/466、pages-public 39、pages-clinical 1729、pages-mgmt 115×2），
安全写法是把整个表达式包进 esc()：`${esc(MAP[x] || x)}`。

只匹配"兜底是裸标识符/属性链"的形态：`MAP[x] || esc(x)`（兜底已转义）与
`MAP[x] || "字面量"`（兜底是常量）都不命中，不误伤。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: ${IDENT[...] || bare.chain} —— 整体未包 esc()、兜底未转义
BARE_MAP_FALLBACK = re.compile(
    r"\$\{\s*[A-Za-z_$][\w$]*\[[^\]\n]+\]\s*\|\|\s*[A-Za-z_$][\w$.]*\s*\}"
)


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
