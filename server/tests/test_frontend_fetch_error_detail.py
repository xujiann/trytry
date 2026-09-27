"""前端绕过 `api()` 直接 `fetch` 的地方，失败时要把后端的 `detail` 报出来（P2-420）。

附件下载原先 `if (!resp.ok) throw new Error(`下载失败(${resp.status})`)`——病毒扫描隔离的附件后端回 410
「附件已被病毒扫描隔离（…），禁止下载」、文件缺失回 404「附件文件缺失（存储目录可能被清理）」，页面上只剩
「下载失败(410)」，经办分不清是没权限、被隔离还是文件丢了；慢专病量表 / 村医二维码同样只给状态码。兄弟几处
（上传附件、打印页、导出 CSV、两端的 `api()`）早就照 `errorText(data.detail, …)` 报人话。

判据：`app/static` 下每一处 `if (!resp.ok)` 的分支都要经 `errorText(` 取 `detail`；确实不该报后端原话的
登记进 `EXEMPT` 写明为什么（只减不增）。
"""
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。2026-09-27 实测（修前 ece367e）：2 处 → 0。
BASELINE = 0

#: 分支不经 errorText 取 detail 的，写明为什么（只减不增）
EXEMPT = {
    "verify.html": "扫码验真的公开页：抛出的错误在同一个 try 里被接住、换成固定的「核验服务暂不可用…本结果不代表"
                   "单据真伪」提示，抛出的文字从不显示——对扫码的公众只给这一句是刻意的。",
}

_OK_CHECK = re.compile(r"if\s*\(\s*!\s*resp\.ok\s*\)")


def _branch(source: str, start: int) -> str:
    """`if (!resp.ok)` 之后的分支：`{…}` 块按括号配对取整块，单句取到分号。"""
    rest = source[start:].lstrip()
    if rest.startswith("{"):
        depth = 0
        for index, char in enumerate(rest):
            depth += {"{": 1, "}": -1}.get(char, 0)
            if depth == 0:
                return rest[:index + 1]
    return rest.split(";", 1)[0]


def offenders_in(source: str, label: str) -> list[str]:
    out = []
    for match in _OK_CHECK.finditer(source):
        branch = _branch(source, match.end())
        if "errorText(" not in branch or "detail" not in branch:
            line = source.count("\n", 0, match.start()) + 1
            out.append(f"{label}:{line}：{branch.splitlines()[0][:80]}")
    return out


def all_offenders() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted([*STATIC.rglob("*.js"), *STATIC.rglob("*.html")]):
        label = path.relative_to(STATIC).as_posix()
        found = offenders_in(path.read_text(encoding="utf-8"), label)
        if found:
            out[label] = found
    return out


def test_直接fetch的失败分支_报后端detail而不是只给状态码():
    unregistered = {k: v for k, v in all_offenders().items() if k not in EXEMPT}
    assert sum(map(len, unregistered.values())) <= BASELINE, (
        "绕过 api() 直接 fetch 的失败分支只给了状态码：照 `const data = await resp.json().catch(() => ({}));"
        " throw new Error(errorText(data.detail, `…失败(${resp.status})`))` 报后端的原话（410「已被病毒扫描隔离」"
        f"这类只有后端知道的原因），确实不该报的登记进 EXEMPT 写明为什么：\n{unregistered}"
    )


def test_豁免名单不得腐烂():
    stale = set(EXEMPT) - set(all_offenders())
    assert not stale, f"这些已不再命中判据，从豁免名单里划掉：{stale}"


def test_判据自证():
    bare = "async function f() {\n  const resp = await fetch(p);\n  if (!resp.ok) throw new Error(`下载失败(${resp.status})`);\n}"
    assert offenders_in(bare, "probe.js")
    single = ("async function f() {\n  const resp = await fetch(p);\n  const data = await resp.json().catch(() => ({}));\n"
              "  if (!resp.ok) throw new Error(errorText(data.detail, `上传失败(${resp.status})`));\n}")
    assert not offenders_in(single, "probe.js")
    block = ("async function f() {\n  const resp = await fetch(p);\n  if (!resp.ok) {\n"
             "    const data = await resp.json().catch(() => ({}));\n"
             "    throw new Error(errorText(data.detail, `下载失败(${resp.status})`));\n  }\n}")
    assert not offenders_in(block, "probe.js")
    # 块里有 errorText 却没取 detail（拿状态码拼了一句）：照样红
    no_detail = "if (!resp.ok) { throw new Error(errorText(null, `下载失败(${resp.status})`)); }"
    assert offenders_in(no_detail, "probe.js")
