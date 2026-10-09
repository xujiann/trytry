"""病种「版本历史」每行写成「vN → vN+1（修改人：说明）」，表顶是现行版，各版规则可展开（P2-1644，第四十八批扫描 AL2-9）。

`PATCH /api/spd/programs/{id}` 改规则时先存**改前**那一版的快照、再升版：版本行的 `version` / `snapshot` 是 vN，
`changed_by` / `note` / `created_at` 却是把它改走、升出 vN+1 的那一笔。运行中枢页的版本历史原先逐行只印「版本 / 修改人 /
说明 / 时间」。扫描实测（修前）：v2 是 dir1 定的（年龄≥40），表里 v2 一行却写「admin · 门槛提到60」（那是改出 v3 的
理由）；现行的 v3 不在表里；规则内容一处都看不到；接口最多回 50 版、超了不说。那份快照的用处是「半年后要能回答这批人
当初按哪版规则纳的管」。

修法只动页面、接口不动：`pages-spd.js` 的 `spdProgramVersionsHtml` 每行写「vN → vN+1（修改人：说明）」（去向取上一行
即更新那版的版本号，最新一行的去向是现行版），表顶补现行版（取病种详情），各版规则放进 `<details>` 可展开、一律 esc()，
取满 50 行写明只列最近 50 版。这里把这个函数拿到 node 里跑（先加载 shared.js 与 core.js 的 `table`），数据取自真接口。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jssrc import strip_comments

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PAGES = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _function(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 2]


def _render(cur: dict, rows: list, meta) -> str:
    if shutil.which("node") is None:
        pytest.skip("没有 node 执行前端渲染")
    script = (
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + (STATIC / "shared.js").read_text(encoding="utf-8")
        + _function((STATIC / "core.js").read_text(encoding="utf-8"), "function table(")
        + "const SPD_PROGRAM_VERSION_LIMIT = 50;\n"
        + _function(PAGES, "function spdProgramVersionsHtml(")
        + "const [cur, rows, meta] = JSON.parse(process.argv[1]);\n"
        + "process.stdout.write(spdProgramVersionsHtml(cur, rows, meta));\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps([cur, rows, meta], ensure_ascii=False)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture(scope="module")
def history(client, admin):
    """管理员建病种（v1 无规则）→ dir1 改出 v2（年龄≥40）→ admin 改出 v3（年龄≥60）。说明与条件说明里夹着尖括号，看转义。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21644 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = client.post("/api/users", headers=admin, json={
        "username": "p21644_dir", "password": "passw0rd1", "role": "director", "org_id": org})
    assert made.status_code in (200, 201), made.text
    login = client.post("/api/auth/login", json={"username": "p21644_dir", "password": "passw0rd1"})
    director = {"Authorization": f"Bearer {login.json()['access_token']}"}
    program = client.post(f"{B}/programs", headers=admin, json={"code": "P21644", "name": "P21644 慢性肾病"})
    assert program.status_code == 201, program.text
    pid = program.json()["id"]
    first = client.patch(f"{B}/programs/{pid}", headers=director, json={
        "include_rules": [{"field": "age", "op": ">=", "value": 40, "label": "年龄<40起>"}], "note": "加年龄门槛40"})
    assert first.status_code == 200, first.text
    second = client.patch(f"{B}/programs/{pid}", headers=admin, json={
        "include_rules": [{"field": "age", "op": ">=", "value": 60, "label": "年龄≥60"}], "note": "门槛<b>提到60</b>"})
    assert second.status_code == 200, second.text
    meta = client.get(f"{B}/meta", headers=admin).json()
    return {
        "cur": client.get(f"{B}/programs/{pid}", headers=admin).json(),
        "rows": client.get(f"{B}/programs/{pid}/versions", headers=admin).json(),
        "meta": {"fields": meta["fields"], "operators": meta["operators"]},
    }


def _cells(html: str) -> list[str]:
    """表体每行第一格（版本变更那一格）的文字。"""
    body = html.split("<tbody>", 1)[1]
    return [row.split("<td>", 1)[1].split("</td>", 1)[0] for row in body.split("<tr>")[1:]]


def test_接口行仍是改前那一版_修改人说明是改走它的那一笔(history):
    """接口不动：v2 一行的快照是 dir1 定的年龄≥40，修改人 / 说明却是改出 v3 的 admin 与「门槛提到60」。"""
    assert history["cur"]["version"] == "v3"
    assert [(v["version"], v["changed_by"], v["note"]) for v in history["rows"]] == [
        ("v2", "admin", "门槛<b>提到60</b>"), ("v1", "p21644_dir", "加年龄门槛40")]
    assert history["rows"][0]["snapshot"]["include_rules"][0]["value"] == 40


def test_每行写成vN到vN加1_修改人与说明挂在这一步上(history):
    html = _render(history["cur"], history["rows"], history["meta"])
    assert _cells(html) == [   # 修前一行印「v2 / admin / 门槛提到60」，人与理由错位一版
        _esc("v2 → v3（admin：门槛<b>提到60</b>）"), _esc("v1 → v2（p21644_dir：加年龄门槛40）")]


def test_表顶有现行版及其规则_各版规则经esc展开(history):
    html = _render(history["cur"], history["rows"], history["meta"])
    age = next(f["name"] for f in history["meta"]["fields"] if f["key"] == "age")
    gte = next(o["name"] for o in history["meta"]["operators"] if o["key"] == ">=")
    top, table = html.split("<table>", 1)
    assert "现行版 <b>v3</b>" in top   # 修前现行版不在表里
    assert "纳入规则：" + _esc(f"{age} {gte} 60（年龄≥60）") in top
    v2_rules = table.split("展开 v2 的规则", 1)[1].split("</details>", 1)[0]
    assert "纳入规则：" + _esc(f"{age} {gte} 40（年龄<40起>）") in v2_rules   # 快照规则可展开
    v1_rules = table.split("展开 v1 的规则", 1)[1].split("</details>", 1)[0]
    assert "纳入规则：无" in v1_rules and "排除规则：无" in v1_rules
    assert html.count("<details>") == 3   # 现行版 + 两行快照
    assert "<b>提到60</b>" not in html and "<40起>" not in html   # 用户写的说明、条件说明一律转义
    assert "只列最近" not in html   # 没取满 50 行不写截断


def test_取满50行写明只列最近50版():
    rows = [{"id": 60 - i, "version": f"v{60 - i}", "changed_by": "admin", "note": "", "snapshot": {},
             "created_at": "2026-10-09T08:00:00"} for i in range(50)]
    html = _render({"name": "P21644", "version": "v61", "include_rules": []}, rows, None)
    assert "只列最近 50 版" in html
    assert _cells(html)[:2] == [_esc("v60 → v61（admin：—）"), _esc("v59 → v60（admin：—）")]


def test_版本按钮接上了这个画法_现行版取病种详情():
    pages = strip_comments(PAGES)
    handler = pages[pages.index("if (progVersions) {"):pages.index("if (progTargets) return")]
    assert "api(`/api/spd/programs/${id}`)" in handler and "api(`/api/spd/programs/${id}/versions`)" in handler
    assert "spdProgramVersionsHtml(cur, rows, ruleMeta)" in handler   # 修前 table(["版本", "修改人", "说明", "时间"], …)
    assert '"修改人"' not in handler
