"""P2-25：裸 `${MAP[服务端字段]}` 查不到时，页面上显示字面量 `undefined`。

与 P2-23/24 同族（都是**查表没兜底**），但**故意分成两条守卫**：那两条管的是
**未转义**（存储型 XSS），这条管的是**没兜底**（显示缺陷）。严重度不同的东西
挤进一条守卫，迟早因为噪声被加豁免——那条守卫就废了。

    修之前: <td>undefined</td>          ← 后端加一个新枚举值就这样
    修之后: <td>chemical</td>

**兜底必须经 `esc()`，这不是风格是必须**：`MAP[x] || x` 的 `x` 是服务端数据，
不转义就正好变成 P2-23 那个洞——

    兜底若不转义: <td><img src=x onerror=alert(1)></td>   ← 这就是 P2-23
    现在的写法:   <td>&lt;img src=x onerror=alert(1)&gt;</td>

所以安全写法只有一种：`${esc(MAP[x] || x)}`。这也不是新发明——
`pages-mgmt.js:884` 的 `esc(NOTIFY_CATEGORIES[n.category] || n.category)`
本来就是这么写的，本轮是把其余 14 处拉齐。

## 判据刻意排除三种「查不到不可能发生」的形状

一开始按 `${MAP[任意表达式]}` 扫，27 处命中里超过一半是误报。逐类排掉：

1. **数组下标**：`colors[si % colors.length]`、`trendColors[i]`、`r[1]`——
   取模或解构出来的下标不会越界；
2. **键来自 `Object.keys(自己)` 的循环**：`ROLE_NAMES[k]` 这类，`k` 天然是合法键；
3. **被三元守着**：`NOTIFY_LINK_PAGE[x] ? …NOTIFY_LINK_PAGE[x]… : ""`
   （`pages-mgmt.js:888`）——查不到就整段不渲染。

排完只剩 **14 处**，键全部来自服务端字段（`waste_type` / `center_type` /
`bill_type` / `status` / `domain` / `infection_site` / `package` / `resource_type`）。
**判据比缺陷宽会淹在误报里**，这条与越权那边的教训是同一条，只是方向相反。

## 扫描必须先剥注释，而且要保行号

本轮已经第七次栽在「注释不是代码」上。而且**剥注释时要把块注释换成等量换行**：
直接删掉会让报出来的行号与原文件对不上——第一版扫描就是这么给出一串错行号的。
"""
import glob
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

#: `${MAP[expr]}` —— 中括号查表、整体没有 `esc(` 包裹、没有 `||`/`??` 兜底
_LOOKUP = re.compile(r"\$\{\s*([A-Za-z_$][\w$]*)\s*\[([^\]\[]{1,60})\]\s*\}")
#: 数组下标：数字、单字母游标、含 `%` 的取模表达式
_INDEXY = re.compile(r"\d+|[a-z]{1,3}|.*%.*")


def _strip_comments(src: str) -> str:
    """去掉注释，**保行号**（块注释换成等量换行）。

    第一版直接把块注释删掉，于是报出来的行号与原文件差了几十行，
    照着去看根本对不上。注释不是代码，但注释占的行数是。
    """
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def bare_map_lookups() -> list[str]:
    """返回「查表没兜底、键来自服务端」的插值位置。"""
    found = []
    files = sorted(glob.glob(os.path.join(STATIC, "*.js")))
    files += sorted(glob.glob(os.path.join(STATIC, "m", "*.js")))
    for path in files:
        lines = _strip_comments(open(path, encoding="utf-8").read()).splitlines()
        for lineno, line in enumerate(lines, 1):
            for match in _LOOKUP.finditer(line):
                name, key = match.group(1), match.group(2).strip()
                if _INDEXY.fullmatch(key):
                    continue  # 数组下标，不会查不到
                context = "\n".join(lines[max(0, lineno - 4) : lineno])
                if re.search(rf"Object\.keys\(\s*{re.escape(name)}\s*\)", context):
                    continue  # 键来自映射表自己，不会查不到
                if re.search(rf"{re.escape(name)}\s*\[[^\]]+\]\s*\n?\s*\?", context + line):
                    continue  # 被三元守着，查不到就不渲染
                rel = os.path.relpath(path, STATIC)
                found.append(f"{rel}:{lineno}  {match.group(0)}")
    return found


def test_扫描必须覆盖全部前端文件():
    """自证：三套前端（管理端 / 居民端 / 医生端）都要扫到。"""
    files = sorted(glob.glob(os.path.join(STATIC, "*.js")))
    files += sorted(glob.glob(os.path.join(STATIC, "m", "*.js")))
    names = {os.path.basename(f) for f in files}
    assert {"core.js", "m.js", "doctor.js"} <= names, f"漏扫了前端文件：{names}"


def test_块注释不得打乱行号():
    """自证：剥注释要保行号，否则报出来的位置对不上原文件。"""
    src = "a\n/* x\ny */\nb\n"
    assert len(_strip_comments(src).splitlines()) == len(src.splitlines())


def test_查表插值必须带兜底且经esc():
    """零基线：这个形状一处都不许有。

    安全写法 `${esc(MAP[x] || x)}` 三件事一次做完：查不到时显示原始码而不是
    `undefined`、原始码经过转义、且与 `pages-mgmt.js:884` 既有写法一致。
    """
    bare = bare_map_lookups()
    assert not bare, (
        "这些查表插值没有兜底，键查不到时页面会显示字面量 undefined；"
        "改成 `${esc(MAP[x] || x)}`（兜底必须经 esc，否则就是 P2-23 那个洞）：\n  "
        + "\n  ".join(bare)
    )
