"""需求对照表里写的接口都真实存在（P2-561，第十一批「居民端 vs 医护端」扫描 Y2-10）。

《全域慢专病全流程管理系统_需求对照表》是投标响应与验收核对用的：条目 → 实现落点。居民端 #8 原先写「复用
`/api/portal/me/encounters`、`/me/inpatient`、`/me/checkups`」，三个都 404；另有专家端的 `/api/outpatient-docs`、
中心端的 `/api/printing`、医生移动端的 `/api/users/me` 同样不存在。照着表验收的人拿到的是 404，而不是「这条没交付」。

逐条订正之后，本用例把表里反引号中出现的每个 `/api/…` 与路由表对一遍：写了就得存在；没交付的写「未交付」，
不写一个不存在的路径。路由分母复用 `test_api_contract_governance._iter_endpoints()`（它处理过 `app.routes`
的封装与 spd 子包，直接数 `app.routes` 只数得出寥寥几条）。

表里的写法（本用例认得的三种）：
* `METHOD|METHOD /api/x/{id}`：方法前缀可有可无，路径参数随便命名；
* `/api/x/{id}/a|b|c`：末段用竖线并列几个兄弟动作，逐个核对；
* `/api/x` 或 `/api/x/*`：一族接口的前缀，只要有一个路由在它底下就算。
"""
from __future__ import annotations

import re
from pathlib import Path

import test_api_contract_governance as contract

MATRIX = Path(__file__).resolve().parents[2] / "docs" / "全域慢专病全流程管理系统_需求对照表.md"
#: 反引号里的接口引用：可选的方法前缀 + /api/ 开头的路径（到反引号、空白、查询串或中文括号为止）
REF = re.compile(r"`(?:[A-Z]+(?:\|[A-Z]+)* )?(/api/[^`\s?（(]+)")


def _shape(path: str) -> str:
    """路径参数一律记成 {}：表里写 {id}、路由里写 {case_id} 是同一个位置。"""
    return re.sub(r"\{[^}]*\}", "{}", path.rstrip("/"))


def _cited() -> list[tuple[int, str]]:
    text = MATRIX.read_text(encoding="utf-8")
    cited = []
    for match in REF.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        path = match.group(1)
        head, _, last = path.rpartition("/")
        for alt in last.split("|"):
            cited.append((line, f"{head}/{alt}"))
    return cited


def test_对照表引用的接口都存在():
    routes = {_shape(route.path) for _, route in contract._iter_endpoints()}
    missing = []
    for line, path in _cited():
        family = path.endswith("/*")
        want = _shape(path[:-2] if family else path)
        if want in routes or any(r.startswith(want + "/") for r in routes):
            continue
        missing.append(f"第 {line} 行：{path}")
    assert not missing, "需求对照表引用了不存在的接口（没交付就写「未交付」，别写一个 404 的路径）：\n" + "\n".join(missing)


def test_对照表确实引用了接口():
    """防空转：正则或文件路径写坏了，上面那条会因为一条都没取到而恒绿。"""
    assert len(_cited()) > 150
