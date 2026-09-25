"""GROUP BY 查询不排全序：开发库按分组键有序吐出，生产库哈希聚合顺序随库而定（P2-68）。

SQLite 做 GROUP BY 是先按分组键排序再逐组聚合，**结果天然按分组键升序**——这是实现细节，不是 SQL 的承诺；
PostgreSQL 多半走 HashAggregate，**吐出的顺序取决于哈希值**。于是一条没写 ORDER BY 的分组查询：

- 开发、测试（SQLite）：永远按键排好，特征化网把这个顺序当成契约钉住；
- 生产（PostgreSQL）：同一份数据换个顺序出来。统计表的行序、按状态分组的字典键序、「前 N 名」里并列的
  那几个谁进谁出，都跟着库走。

2026-09-25 全量单元套件在真 PG 上跑（`MEDPLAT_FULLPG_URL`，一次性扫描）实测：DRG 统计的 `groups` 在开发库是
BR23、ES31、QY，真 PG 上是 ES31、BR23、QY——`test_drgs_contract` 钉的键序在生产库从来不成立。

修法：分组查询的 ORDER BY 必须把分组键全排上（前面可以先按计数等聚合值排，分组键兜底成全序）。开发库的
结果一字不变（它本来就按分组键吐），生产库从此确定。注意生产库文本列按库的排序规则比（官方镜像默认
en_US.utf8，下划线、大小写与字节序略有出入）——顺序**确定**了，但与开发库的逐字节顺序在这类键上仍可能不同。

**本文件**：①派生零基线闸门——分组键没全进 ORDER BY 的分组查询一律点名；②DRG 统计的端点回归。
**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 用 `MEDPLAT_GBORDER_PG_URL` 把本文件
换到 PG 上再跑一遍。
"""
import ast
import os
import pathlib

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前（同 test_body_numeric_capacity）
_PG_URL = os.environ.get("MEDPLAT_GBORDER_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-25 实测（修前 287aa03）：分组查询 84 条，78 条没有 ORDER BY、4 条只按计数排（并列的顺序随库）→ 0。
BASELINE = 0

#: 子查询 / CTE 里的 ORDER BY 没有意义（外层才决定顺序），这类链不算。
_NESTED_TERMINALS = {"subquery", "scalar_subquery", "cte", "exists", "label"}


def _chain(call: ast.Call) -> list[ast.Call]:
    """一条方法链上的全部调用（从外往里）。"""
    calls = []
    node: ast.AST = call
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append(node)
        node = node.func.value
    return calls


def _bare(expr: ast.expr) -> str:
    """排序项去掉 `.desc()` / `.asc()` / `.nulls_last()` 这类包装后的源码。"""
    while (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and not expr.args
           and expr.func.attr in {"desc", "asc", "nulls_last", "nulls_first", "nullslast", "nullsfirst"}):
        expr = expr.func.value
    return ast.unparse(expr)


def groupby_without_total_order(root: pathlib.Path = APP) -> set[str]:
    """`文件:函数:group_by(键…)`：分组键没有全部出现在同一条链的 ORDER BY 里。"""
    found = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "group_by" not in source:
            continue
        tree = ast.parse(source)
        owner: dict[int, str] = {}
        # ast.walk 是广度优先：外层函数先登记、内层后覆盖，记下的是最内层那个函数
        for scope in ast.walk(tree):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(scope):
                    owner[id(n)] = scope.name
        inner: set[int] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)) or id(node) in inner:
                continue
            calls = _chain(node)
            inner.update(id(c) for c in calls)   # 链上里层的调用不再单独当一条链
            by_name = {c.func.attr: c for c in reversed(calls)}   # 同名取最外层那次
            group = by_name.get("group_by")
            if group is None or calls[0].func.attr in _NESTED_TERMINALS:
                continue
            keys = [ast.unparse(a) for a in group.args]
            order = {_bare(a) for c in calls if c.func.attr == "order_by" for a in c.args}
            if not set(keys) <= order:
                rel = path.relative_to(root).as_posix()
                found.add(f"{rel}:{owner.get(id(node), '<module>')}:group_by({', '.join(keys)})")
    return found


# ================================================================ 闸门
def test_分组查询按分组键排成全序():
    bad = sorted(groupby_without_total_order())
    assert len(bad) <= BASELINE, (
        "这些分组查询的 ORDER BY 没把分组键排全——开发库 SQLite 按分组键吐出、生产库 PostgreSQL 按哈希值吐出，"
        "两边顺序不同，生产库里还可能每次都不同：\n  " + "\n  ".join(bad)
        + "\n\n在 `.group_by(键…)` 后面接 `.order_by(键…)`；要按计数等聚合值排的，把分组键接在后面兜底："
        "`.order_by(func.count(X.id).desc(), 键…)`。"
    )


def test_判据自证_认得出缺序与半序_放过全序与子查询(tmp_path):
    (tmp_path / "probe.py").write_text(
        "def stats(db):\n"
        "    a = db.query(T.k, func.count(T.id)).group_by(T.k).all()\n"
        "    b = db.query(T.k, T.j).group_by(T.k, T.j).order_by(func.count(T.id).desc(), T.k).limit(5).all()\n"
        "    c = db.query(T.k).group_by(T.k).order_by(T.k.desc()).all()\n"
        "    d = db.query(T.k).group_by(T.k).order_by(func.count(T.id).desc(), T.k).all()\n"
        "    e = db.query(T.k).group_by(T.k).subquery()\n"
        "    f = (db.query(T.k)\n"
        "         .group_by(T.k)\n"
        "         .order_by(T.k)\n"
        "         .all())\n"
        "\n"
        "def other(db):\n"
        "    return db.query(U.v).group_by(U.v).all()\n",
        encoding="utf-8",
    )
    assert groupby_without_total_order(tmp_path) == {
        "probe.py:stats:group_by(T.k)",        # a：没有 ORDER BY
        "probe.py:stats:group_by(T.k, T.j)",   # b：只排了计数与一半的键
        "probe.py:other:group_by(U.v)",
    }


def test_判据自证_扫得到真实代码里的分组查询():
    """防空转：一条分组查询都认不出时，上面的闸门会空转成绿。"""
    total = 0
    for path in APP.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        total += source.count(".group_by(")
    assert total >= 80, total


# ================================================================ 端点回归（真 PG 上修前顺序是 ES31、BR23、QY）
@pytest.fixture(scope="module")
def drg_world(client, admin):
    """病例三组，与 test_drgs_contract 同一套诊断：肺炎入 ES31、脑梗死入 BR23、罕见病落 QY 兜底组。"""
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P268 医院", "org_type": "lead_hospital", "level": "county"}).json()
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org["id"], "name": "P268 病区"}).json()
    for i, diagnosis in enumerate(["社区获得性肺炎", "社区获得性肺炎", "急性脑梗死", "罕见代谢病"]):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": f"P268-{i}"})
        patient = client.post("/api/patients", headers=admin,
                              json={"name": f"P268 患者{i}", "id_card": f"33088119900101{8401 + i:04d}"})
        assert bed.status_code == 201 and patient.status_code == 201, (bed.text, patient.text)
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient.json()["id"], "ward_id": ward["id"], "bed_id": bed.json()["id"],
            "diagnosis_name": diagnosis})
        assert adm.status_code == 201, adm.text
        summary = client.post(f"/api/inpatient/admissions/{adm.json()['id']}/case-summary", headers=admin,
                              json={"discharge_diagnosis": diagnosis, "total_cost": 5000 + i, "outcome": "好转"})
        assert summary.status_code in (200, 201), summary.text
    return org


def test_DRG统计的分组行按编码排序(client, admin, drg_world):
    resp = client.get("/api/drgs/stats", headers=admin)
    assert resp.status_code == 200, resp.text
    codes = [g["drg_code"] for g in resp.json()["groups"]]
    assert codes == ["BR23", "ES31", "QY"], codes   # 修前真 PG：ES31、BR23、QY（按哈希值吐出）
