"""前端写出来的 `/api` 地址都得有路由，写明（或推得出）的动词后端都得接——零基线。

孤儿端点棘轮（`test_orphan_endpoints.py`）只守一个方向：每个端点都要有前端调用点。
反方向没人守——前端把地址拼错一个字母、把 PATCH 写成 PUT，`make verify` 全绿，
用户点下去才 404 / 405；e2e 只走得到几十条主链路，覆盖不到这九百多处调用。

2026-09-24 实测：三端前端里 977 处 `/api` 地址全部对得上运行期路由表，推得出动词的也全部
对得上——零基线立闸门，防的是以后。变异验证：把 `POST /api/users` 改成 PUT、把
`/api/mgmt/budgets` 少写一个 s，两条当场点名（下面的判据自证用例钉住这两种写法）。

判法：

- **地址**：字符串 / 模板字面量里以 `/api/` 开头的。模板里的 `${…}` 按配平整段取出、换成一段
  占位；去掉查询串与片段；末尾紧贴在段上的插值（`/export.csv${qs}`、`/home${spdQuery()}`）
  是拼查询串，去掉——只认表达式里带 `?`、或名字就是查询串（`q` / `qs` / `query` / `params` /
  `…Query()`）的；`/api/x/p${id}` 这种段里拼编号的不当查询串，计入「判不了」。与运行期路由表比（穿过 `_IncludedRouter`，含 `main.py` 直挂的路由）。
- **动词**：按接地址的函数认——`api` / `authApi` / `fetch` 看同一次调用里的选项对象写没写
  `method`，没写即 GET（三端的 `api` 都是 `options.method || "GET"`）；`postAction` 看第四个
  实参，缺省 POST；医生端 `spdPost` / `act` 恒 POST；`openPrintPage` / `downloadCsv` /
  `spdOpenSvg` 恒 GET。选项对象是变量、第四参不是字面量、或地址不在调用的第一个实参上的，
  只判地址。
- **判不了的**：插值夹在一段中间（`/api/x/pre${id}`），计入「判不了」，同样零基线——
  地址写成整段插值，闸门才看得见。
"""
import pathlib
import re
import warnings

from fastapi import APIRouter
from fastapi.routing import APIRoute

from app.main import app

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）
BASELINE = 0

FETCH_LIKE = {"api", "authApi", "fetch"}
POST_ONLY = {"spdPost", "act"}
GET_ONLY = {"openPrintPage", "downloadCsv", "spdOpenSvg"}


def _repo_files() -> dict[str, str]:
    paths = (
        sorted(STATIC.glob("*.js")) + sorted(STATIC.glob("*.html"))
        + sorted((STATIC / "m").glob("*.js")) + sorted((STATIC / "m").glob("*.html"))
    )
    return {str(p.relative_to(STATIC)): p.read_text(encoding="utf-8") for p in paths}


def _walk(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None and getattr(original, "routes", None):
            yield from _walk(original.routes)
        elif isinstance(route, APIRouter) and getattr(route, "routes", None):
            yield from _walk(route.routes)


def _route_table() -> list[tuple[set[str], re.Pattern[str], str]]:
    return [
        (route.methods - {"HEAD", "OPTIONS"}, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", route.path) + "$"),
         route.path)
        for route in _walk(app.routes)
    ]


def _literal(text: str, start: int, quote: str) -> tuple[str, int]:
    """从开引号之后取到收尾引号；模板里的 `${…}` 按配平整段带上。返回（内容，收尾引号之后的下标）。"""
    i, depth, out = start, 0, []
    while i < len(text):
        c = text[i]
        if depth == 0 and c == quote:
            return "".join(out), i + 1
        if depth == 0 and quote != "`" and c in "\n":
            break
        if quote == "`" and text.startswith("${", i):
            depth += 1
            out.append("${")
            i += 2
            continue
        if depth and c == "{":
            depth += 1
        elif depth and c == "}":
            depth -= 1
        out.append(c)
        i += 1
    return "".join(out), i


#: 末尾紧贴在段上的插值，长这样才算拼查询串：表达式里带 `?`，或名字就是查询串
_QUERY_SUFFIX = re.compile(r"\?|^\s*[\w.]*?(?:q|qs|query|params|Query\([^)]*\))\s*$")


def _normalize(raw: str) -> tuple[str, bool]:
    """插值换一段占位、去查询串；返回（地址，判不判得了）。"""
    url, last_expr = raw, ""
    while "${" in url:
        j = url.index("${")
        depth, k = 0, j
        while k < len(url):
            if url.startswith("${", k):
                depth += 1
                k += 2
                continue
            if url[k] == "{":
                depth += 1
            elif url[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        last_expr = url[j + 2: k]
        url = url[:j] + "\x00" + url[k + 1:]
    url = url.split("?")[0].split("#")[0]
    if re.search(r"[^/]\x00$", url) and _QUERY_SUFFIX.search(last_expr):
        url = url[:-1]  # `/export.csv${qs}`、`/home${spdQuery()}`：拼的是查询串，不是路径
    checkable = not re.search(r"[^/]\x00|\x00[^/]", url)
    return url.replace("\x00", "X"), checkable


def _call_args(text: str, i: int) -> list[str]:
    """从地址字面量之后走到本次调用的右括号，按顶层逗号切出实参（第一段是地址之后的残余）。"""
    args, cur, stack = [], [], []
    while i < len(text):
        c = text[i]
        top = stack[-1] if stack else None
        if top in ('"', "'", "`"):
            cur.append(c)
            if c == "\\":
                cur.append(text[i + 1: i + 2])
                i += 2
                continue
            if c == top:
                stack.pop()
            elif top == "`" and text.startswith("${", i):
                stack.append("{")
                cur.append("{")
                i += 2
                continue
            i += 1
            continue
        if c in "\"'`":
            stack.append(c)
        elif c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack:
                args.append("".join(cur))
                return args
            stack.pop()
        elif c == "," and not stack:
            args.append("".join(cur))
            cur = []
            i += 1
            continue
        elif c == ";" and not stack:
            break
        cur.append(c)
        i += 1
    args.append("".join(cur))
    return args


def _method(callee: str | None, args: list[str]) -> str | None:
    if callee in POST_ONLY:
        return "POST"
    if callee in GET_ONLY:
        return "GET"
    if callee in FETCH_LIKE:
        if len(args) < 2:
            return "GET"
        opts = args[1].strip()
        if not opts.startswith("{"):
            return None  # 选项对象是变量
        m = re.search(r"""\bmethod:\s*["'](\w+)["']""", opts)
        if m:
            return m.group(1).upper()
        return None if re.search(r"\bmethod\b", opts) else "GET"
    if callee == "postAction":
        if len(args) < 4:
            return "POST"
        m = re.fullmatch(r"""\s*["'](\w+)["']\s*""", args[3])
        return m.group(1).upper() if m else None
    return None


def scan(files: dict[str, str] | None = None) -> dict[str, list[str]]:
    files = _repo_files() if files is None else files
    table = _route_table()
    out: dict[str, list] = {"total": [], "no_route": [], "wrong_verb": [], "unchecked": [], "verb_checked": [],
                            "calls": []}
    for name, text in files.items():
        for m in re.finditer(r"""([`"'])(?=/api/)""", text):
            raw, end = _literal(text, m.end(), m.group(1))
            line = text[: m.start()].count("\n") + 1
            where = f"{name}:{line} {raw.splitlines()[0]}"
            out["total"].append(where)
            url, checkable = _normalize(raw)
            if not checkable:
                out["unchecked"].append(where)
                continue
            matches = [r for r in table if r[1].match(url)]
            if not matches:
                out["no_route"].append(where)
                continue
            head = re.search(r"([\w$.]+)\(\s*$", text[max(0, m.start() - 40): m.start()])
            callee = head.group(1).rsplit(".", 1)[-1] if head else None
            method = _method(callee, _call_args(text, end)) if callee else None
            if method is None:
                continue
            out["verb_checked"].append(where)
            # （动词, 路由）——动词级孤儿棘轮（test_orphan_endpoint_verbs）按它判「这个动词有没有入口」。
            # `/api/x/summary` 同时对得上 `/api/x/summary` 与 `/api/x/{id}`：静态段多的那条才是它调的
            fewest = min(r[2].count("{") for r in matches)
            out["calls"].extend((method, r[2]) for r in matches if r[2].count("{") == fewest)
            allowed = set().union(*(r[0] for r in matches))
            if method not in allowed:
                out["wrong_verb"].append(f"{where}  前端 {method}，后端只有 {sorted(allowed)}")
    return out


def test_前端写出来的地址都有路由():
    bad = scan()["no_route"]
    assert len(bad) <= BASELINE, (
        "以下前端地址在后端找不到路由（上线即 404）：\n  " + "\n  ".join(bad)
    )


def test_前端推得出的动词后端都接():
    bad = scan()["wrong_verb"]
    assert len(bad) <= BASELINE, (
        "以下前端调用的动词后端不接（上线即 405）：\n  " + "\n  ".join(bad)
    )


def test_判不了的地址写法也是零基线():
    bad = scan()["unchecked"]
    assert len(bad) <= BASELINE, (
        "以下地址把插值夹在一段中间，闸门判不了它对不对得上路由：\n  " + "\n  ".join(bad)
        + "\n\n把插值写成整段（`/api/x/${id}/y`），或拼查询串时紧贴在末尾（`/api/x${qs}`）。"
    )


def test_判据自证_写错地址与动词当场点名():
    src = "\n".join([
        'await api("/api/users", { method: "PUT", body: "{}" });',
        'postAction("/api/mgmt/budget", {}, "#m");',
        'await api(`/api/patients/${pid}/authorizations`, { method: "POST", body: "{}" });',
        'await api(`/api/certs${certType ? `?cert_type=${certType}` : ""}`);',
        'postAction(`/api/education/live-sessions/${id}/review?approve=${ok}`, null, "#m");',
        'await api(`/api/patients/p${pid}`);',
        'await api(`/api/infectious/cases/export.csv${qs ? `?${qs}` : ""}`);',
        'await authApi(`/api/portal/spd/home${spdQuery()}`);',
    ])
    out = scan({"自证.js": src})
    assert [w.split("  ")[0] for w in out["wrong_verb"]] == ["自证.js:1 /api/users"]
    assert out["no_route"] == ["自证.js:2 /api/mgmt/budget"]
    assert out["unchecked"] == ["自证.js:6 /api/patients/p${pid}"]
    assert len(out["total"]) == 8 and len(out["verb_checked"]) == 6


def test_覆盖面自证():
    out = scan()
    total, checked = len(out["total"]), len(out["verb_checked"])
    warnings.warn(
        "\n  [前端调用对路由闸门] 覆盖面自证\n"
        f"    扫描：{len(_repo_files())} 个前端文件，/api 地址 {total} 处（字符串 / 模板字面量，无抽样）\n"
        f"    地址对上路由：{total - len(out['no_route']) - len(out['unchecked'])} 处；判不了：{len(out['unchecked'])} 处\n"
        f"    推得出动词并比过：{checked} 处",
        UserWarning, stacklevel=1,
    )
    # 防空转：动词判定一旦失灵（认不出接地址的函数），比过的会骤降——过半是底线
    assert total and checked * 2 > total, (total, checked)
