"""设备台账、村医档案、路径模板三张配置表续页取全，不再截在第一页（P2-1641，第四十八批扫描 AL2-3）。

修前三张表都只取一页、不读总数、不写截断：
- 运行中枢页设备台账取 `devices?limit=100`，清单按编号升序——第 101 台起新登记的设备不出现，「绑定患者」无从点起；
- 服务团队页村医档案取 `village-doctors?limit=100`，按编号升序——第 101 位起的村医改不了档、出不了绑定码
  （批量导入一次就收 500 行）；
- 路径与任务页路径模板取 `path-templates?limit=30`，按编号倒序——过了 30 个模板，最早发布的那版从表里消失，启动路径的
  下拉里却还有它，想照 P2-933 手工「停用」旧版无处可点。
实测（修前）：105 台设备拿到 100 台、X-Total-Count 105，BP-0105 不在；31 个模板拿到 30 个，最早已发布的不在；
105 位村医拿到 100 位，村医105 不在。

三张都是逐行操作的配置表、数量级以采购台数 / 辖区村数 / 模板版本数封顶，照 P2-1610（同 P2-1546）改走 shared.js 的
`fetchAllPages` 续页取全；后端缺省排序不动（别的页面、下拉也在用）。这里把三处页面取数的那一句原样拿到 node 里跑
（先加载 shared.js，与浏览器同序），请求转给真接口。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PAGES = STATIC / "pages-spd.js"

#: (渲染函数, 清单地址)：页面取这张表的那一句只能是 `api("地址…")`（修前，只有一页）或 `fetchAllPages(api, "地址…")`
TABLES = (
    ("renderSpdAdmin", "/api/spd/devices"),
    ("renderSpdTeam", "/api/spd/village-doctors"),
    ("renderSpdPath", "/api/spd/path-templates"),
)


def _renderer(name: str) -> str:
    source = strip_comments(PAGES.read_text(encoding="utf-8"))
    start = source.index(f"async function {name}(")
    return source[start:source.index("\nasync function ", start + 1)]


def _fetch_expr(name: str, path: str) -> str:
    found = re.search(r'(?:fetchAllPages\(api, |\bapi\()"' + re.escape(path) + r'(?:\?[^"]*)?"\)', _renderer(name))
    assert found, f"{name} 里找不到取 {path} 的那一句"
    return found.group(0)


def _run_page_fetch(client, headers, expression):
    """在 node 里原样执行页面取数的那一句（先加载 shared.js），页面的 `api` 经管道转给真接口。返回 (取回的 id, 请求过的地址)。"""
    shared = (STATIC / "shared.js").read_text(encoding="utf-8")
    script = (
        # shared.js 顶层只碰 document.addEventListener（原生提交兜底）
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + shared
        + "\nconst rl = require('readline').createInterface({ input: process.stdin });\n"
        "const lines = rl[Symbol.asyncIterator]();\n"
        "async function api(path) {\n"
        "  process.stdout.write(JSON.stringify({ get: path }) + '\\n');\n"
        "  return JSON.parse((await lines.next()).value);\n"
        "}\n"
        f"(async () => {{ const rows = await {expression};\n"
        "  process.stdout.write(JSON.stringify({ ids: rows.map((r) => r.id) }) + '\\n'); rl.close(); })();\n"
    )
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    requested = []
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{expression}"
            message = json.loads(line)
            if "ids" in message:
                return message["ids"], requested
            requested.append(message["get"])
            assert len(requested) <= 10, f"翻页停不下来：{requested}"
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, resp.text
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.fixture(scope="module")
def world(client, admin):
    """105 台设备、105 位村医、31 个路径模板（最早那个已发布）——三张表都比修前那一页多出几行。"""
    from app.database import SessionLocal
    from app.models import User
    from app.spd.models import SpdDevice, SpdPathTemplate, SpdProgram, SpdVillageDoctor

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21641 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:   # 逐个走接口太慢（村医要先建账号），直接落库
        program = db.query(SpdProgram).order_by(SpdProgram.id).first().id
        devices = [SpdDevice(sn=f"P21641-BP-{i:04d}", device_type="bp", org_id=org) for i in range(1, 106)]
        users = [User(username=f"p21641_vd{i:03d}", password_hash="x", full_name=f"P21641 村医{i:03d}",
                      role="doctor", org_id=org) for i in range(1, 106)]
        templates = [SpdPathTemplate(program_id=program, code=f"P21641_T{i:02d}", name=f"P21641 路径{i}",
                                     status="published" if i == 1 else "draft") for i in range(1, 32)]
        db.add_all(devices + users + templates)
        db.flush()
        doctors = [SpdVillageDoctor(user_id=u.id, org_id=org, village=f"村{i}") for i, u in enumerate(users, 1)]
        db.add_all(doctors)
        db.commit()
        return {
            "/api/spd/devices": max(d.id for d in devices),             # 最新登记的那台（升序清单的最后一行）
            "/api/spd/village-doctors": max(v.id for v in doctors),     # 最新开通的那位（升序清单的最后一行）
            "/api/spd/path-templates": min(t.id for t in templates),    # 最早发布的那版（倒序清单的最后一行）
        }


def test_修前那一页列不到_接口总数比一页多(client, admin, world):
    for path, legacy in (("/api/spd/devices", 100), ("/api/spd/village-doctors", 100), ("/api/spd/path-templates", 30)):
        page = client.get(path, headers=admin, params={"limit": legacy})
        assert page.status_code == 200, page.text
        assert int(page.headers["X-Total-Count"]) > legacy, path
        assert world[path] not in [r["id"] for r in page.json()], path   # 修前页面取的就是这一页：那一行不在


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的取数")
@pytest.mark.parametrize(("renderer", "path"), TABLES)
def test_页面按自己的取法列得到最新那一行_一个不落(client, admin, world, renderer, path):
    expression = _fetch_expr(renderer, path)
    got, requested = _run_page_fetch(client, admin, expression)
    assert world[path] in got, (expression, requested, len(got))   # 修前 100 / 100 / 30 行，那一行不在
    whole = client.get(path, headers=admin, params={"limit": 500})
    assert got == [r["id"] for r in whole.json()], (expression, requested)   # 一个不落、不重复，与接口同序
    assert len(got) == int(whole.headers["X-Total-Count"])


@pytest.mark.parametrize(("renderer", "path"), TABLES)
def test_三张配置表用续页帮手取(renderer, path):
    expression = _fetch_expr(renderer, path)
    assert expression == f'fetchAllPages(api, "{path}")', expression   # 修前 api("…?limit=100") / api("…?limit=30")
