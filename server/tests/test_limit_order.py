"""截断取数（LIMIT / OFFSET）不排序：开发库按插入序取前 N 条，生产库按堆序取任意 N 条（P2-69）。

SQLite 没有 ORDER BY 时按 rowid（≈ 插入序）吐行；PostgreSQL 按堆里的物理位置——**UPDATE 会把行的新版本写到堆尾**，
改过的行就挪到了最后。于是一条 `.limit(N)` 不带 ORDER BY 的查询：

- 开发、测试（SQLite）：永远是最早建的那 N 条、按建立先后排，特征化网照这个钉；
- 生产（PostgreSQL）：取哪 N 条、什么顺序都看堆——改过一次的行就从列表前面消失，截断时换一批人。

2026-09-25 全量单元套件在真 PG 上跑（一次性扫描）实测：资源总览 `/api/resources/catalog` 的通用资源两行，开发库
按编号（工勤服务 1、通用设备 2），真 PG 是（通用设备 2、工勤服务 1）——编号 1 那行被发布改过一次，挪到了堆尾。

修法：截断取数一律带 ORDER BY；本身收查询参数的帮手（`paginate` / `_section`）由调用方排好序，这里逐个核对调用点。
**本文件**：派生零基线闸门 + 按设计名单（只减不增，逐条写理由）+ 资源总览的端点回归。
**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 用 `MEDPLAT_LIMITORDER_PG_URL` 把本文件换到 PG 上
再跑一遍。
"""
import ast
import os
import pathlib

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前（同 test_body_numeric_capacity）
_PG_URL = os.environ.get("MEDPLAT_LIMITORDER_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-25 实测（修前 e4384e3）：截断取数不排序 8 处（资源总览 5、报告超期任务段、工作人员通知收件人、宣教定时派发）→ 0。
BASELINE = 0

#: 按设计不排序的（`文件:函数`）。**只减不增**，每条写明为什么。
BY_DESIGN = {
    "routers/portal.py:_autobind_by_phone":
        "limit(2) 只数同一手机号有几份档案（判是否唯一），不关心是哪几份",
    "spd/routers/population.py:add_group_members":
        "P1-86 待裁定：按规则吸入只看 1000 个在管患者是性能闸，续扫方式（分批游标 / 后台任务）未定",
    "spd/routers/population.py:auto_screen":
        "与 P1-86 同形状待裁定：一次最多扫 body.limit 人（回显 scanned），续扫方式未定",
}

#: 收查询参数、在里面截断的帮手：它们本身不排序，调用方传进来的查询必须已排好序（第二道判据逐个核对调用点）。
QUERY_HELPERS = {"paginate", "_section"}


def _inner(call: ast.AST) -> tuple[list[str], ast.AST]:
    names = []
    while isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
        names.append(call.func.attr)
        call = call.func.value
    return names, call


def _ordered(expr: ast.AST, fn: ast.AST | None) -> bool:
    """这条链（或链底那个变量在同一函数里的赋值）有没有 ORDER BY。"""
    names, base = _inner(expr)
    if "order_by" in names:
        return True
    if isinstance(base, ast.Name) and fn is not None:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and ".order_by(" in ast.unparse(node.value)
                    and any(isinstance(t, ast.Name) and t.id == base.id for t in node.targets)):
                return True
    return False


def _functions(tree: ast.AST) -> dict[int, ast.AST]:
    owner: dict[int, ast.AST] = {}
    # ast.walk 是广度优先：外层函数先登记、内层后覆盖，记下的是最内层那个函数
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(scope):
                owner[id(n)] = scope
    return owner


def limit_without_order(root: pathlib.Path = APP) -> set[str]:
    """`文件:函数`：`.limit()` / `.offset()` 前面的链与链底变量都没有 ORDER BY（帮手函数本身除外）。"""
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _functions(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("limit", "offset")):
                continue
            fn = owner.get(id(node))
            if fn is not None and fn.name in QUERY_HELPERS:
                continue   # 帮手：查询是参数，由第二道判据核对调用方
            if not _ordered(node.func.value, fn):
                found.add(f"{path.relative_to(root).as_posix()}:{fn.name if fn else '<module>'}")
    return found


def helper_calls_without_order(root: pathlib.Path = APP) -> set[str]:
    """`文件:函数→帮手`：调 paginate / _section 时传进去的查询没排序。"""
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _functions(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in QUERY_HELPERS and node.args):
                fn = owner.get(id(node))
                if not _ordered(node.args[0], fn):
                    found.add(f"{path.relative_to(root).as_posix()}:{fn.name if fn else '<module>'}→{node.func.id}")
    return found


# ================================================================ 闸门
def test_截断取数一律带排序():
    bad = sorted(limit_without_order() - BY_DESIGN.keys())
    assert len(bad) <= BASELINE, (
        "这些 `.limit()` / `.offset()` 前面没有 ORDER BY——开发库按插入序取前 N 条，生产库按堆序取任意 N 条"
        "（改过的行会挪到最后）：\n  " + "\n  ".join(bad)
        + "\n\n在截断之前接 `.order_by(...)`（至少按主键兜底）；确实只关心条数不关心是哪几条的，进 BY_DESIGN 写明理由。"
    )


def test_帮手的调用方传进来的查询都排好了序():
    bad = sorted(helper_calls_without_order())
    assert bad == [], "传给分页 / 分段帮手的查询没排序：\n  " + "\n  ".join(bad)


def test_按设计名单只减不增():
    stale = sorted(BY_DESIGN.keys() - limit_without_order())
    assert stale == [], "这些已经带上排序或挪走了，请从 BY_DESIGN 划掉：\n  " + "\n  ".join(stale)


def test_判据自证_认得出缺序_放过链内排序与变量排序(tmp_path):
    (tmp_path / "probe.py").write_text(
        "def a(db):\n"
        "    return db.query(T).filter(T.x == 1).limit(5).all()\n"
        "def b(db):\n"
        "    return db.query(T).order_by(T.id).limit(5).all()\n"
        "def c(db):\n"
        "    rows = db.query(T).order_by(T.id.desc())\n"
        "    return rows.limit(5).all()\n"
        "def d(db, response):\n"
        "    return paginate(db.query(T), response)\n"
        "def e(db, response):\n"
        "    return paginate(db.query(T).order_by(T.id), response)\n"
        "def paginate(query, response):\n"
        "    return query.offset(0).limit(10).all()\n",
        encoding="utf-8",
    )
    assert limit_without_order(tmp_path) == {"probe.py:a"}
    assert helper_calls_without_order(tmp_path) == {"probe.py:d→paginate"}


def test_判据自证_扫得到真实代码里的截断():
    """防空转：一条截断都认不出时，上面的闸门会空转成绿。"""
    total = sum(p.read_text(encoding="utf-8").count(".limit(") for p in APP.rglob("*.py"))
    assert total >= 150, total


# ================================================================ 端点回归（真 PG 上修前改过的行挪到末尾）
def test_资源总览按编号列出_改过的行不挪位(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P269 医院", "org_type": "lead_hospital", "level": "county"}).json()
    ids = []
    for code, rtype in (("P269A", "logistics"), ("P269B", "equipment")):
        resp = client.post("/api/resources", headers=admin,
                           json={"org_id": org["id"], "resource_type": rtype, "code": code, "name": f"P269 {code}"})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    published = client.post(f"/api/resources/{ids[0]}/publish", headers=admin)   # 改第一行：PG 上新版本写到堆尾
    assert published.status_code == 200, published.text
    body = client.get("/api/resources/catalog", headers=admin,
                      params={"org_id": org["id"], "resource_kind": "general"}).json()
    assert [i["id"] for i in body["items"]] == ids   # 修前真 PG：[ids[1], ids[0]]
