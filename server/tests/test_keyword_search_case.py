"""关键词检索的大小写：开发库不分、生产库区分——诊断字典搜 `i10` 在生产库什么都搜不到（P2-66）。

SQLite 的 `LIKE` 对 ASCII 不分大小写，PostgreSQL 区分。一族关键词检索写的是 `col.like(f"%{kw}%")` /
`col.contains(kw)`：开发、测试一律「大小写都搜得到」，上了生产库就只认原样的大小写。

2026-09-25 实测（修前代码）：诊断字典（ICD-10 种子 100 条）按 `I10` 搜——两个库都命中；按 `i10`、`e11` 搜——
开发库命中 `I10` / `E11`，**真 PG 返回空**。医生在诊断选择器里敲小写编码就查不到诊断；「CT」「HbA1c」「COPD」这类
夹在中文名里的缩写（收费项目、知识库、病种、宣教材料）同病。

修法：`deps.keyword_like(col, kw)`——两边都转小写再 `LIKE`，两库同一口径（开发库结果一字不变）；平台与慢专病的
用户关键词检索一律经它。**本文件**：①派生零基线闸门——字符串列上的 `like / contains / startswith …` 不经它、
又不在按设计名单里的，一律点名；②修过的端点的回归。

**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 用 `MEDPLAT_KWCASE_PG_URL`
把本文件换到 PG 上再跑一遍。
"""
import ast
import os
import pathlib

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前（同 test_body_numeric_capacity）
_PG_URL = os.environ.get("MEDPLAT_KWCASE_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
METHODS = {"like", "ilike", "notlike", "notilike", "contains", "startswith", "endswith"}

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-25 实测（修前 c9acf81）：字符串列上的裸模糊匹配 31 处（按 文件:函数:列.方法 计）——用户关键词检索 26 处
#: 改走 keyword_like，余下 5 处按设计（见 BY_DESIGN）→ 0。
BASELINE = 0

#: 按设计不转小写的（`文件:函数:模型.列.方法`）。**只减不增**，每条写明为什么不是用户关键词检索。
BY_DESIGN = {
    "routers/admin_mgmt.py:budget_execution:FinanceEntry.period.like":
        "按年份前缀 `YYYY-` 取当年的财务流水，纯数字，不涉大小写",
    "routers/analytics.py:_efficiency_rows:Employee.position.like":
        "代码里的常量关键词（DOCTOR_POSITION_KEYWORDS，全是汉字），不是用户输入",
    "routers/medwaste.py:_next_trace_code:MedicalWaste.trace_code.like":
        "生成下一个追溯码时按系统自己拼的前缀取当日最大号，前缀大小写由代码定；前缀匹配还要留着走索引",
    "routers/rbac.py:grant_permissions:Permission.builtin_roles.like":
        "按内置角色键（代码常量，小写）匹配权限的默认授予名单，不是用户输入",
    "spd/routers/followup.py:list_call_tasks:SpdCallTask.phone.contains":
        "电话号码只有数字，不涉大小写",
}


def _string_columns() -> dict[str, set[str]]:
    import app.models  # noqa: F401  先平台再 spd，见 P2-51
    import app.spd.models  # noqa: F401
    from sqlalchemy import String

    from app.database import Base

    return {m.class_.__name__: {c.key for c in m.columns if isinstance(c.type, String)}
            for m in Base.registry.mappers}


def raw_fuzzy_matches(root: pathlib.Path = APP, strcols: dict[str, set[str]] | None = None) -> set[str]:
    """`文件:函数:模型.列.方法`：直接在字符串列上调 like / contains / startswith …（不经 keyword_like）。"""
    strcols = _string_columns() if strcols is None else strcols
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner: dict[int, str] = {}
        # ast.walk 是广度优先：外层函数先登记、内层后覆盖，记下的是最内层那个函数
        for scope in ast.walk(tree):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(scope):
                    owner[id(n)] = scope.name
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in METHODS):
                continue
            recv = n.func.value
            if (isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name)
                    and recv.attr in strcols.get(recv.value.id, ())):
                rel = path.relative_to(root).as_posix()
                found.add(f"{rel}:{owner.get(id(n), '<module>')}:{recv.value.id}.{recv.attr}.{n.func.attr}")
    return found


# ================================================================ 闸门
def test_字符串列上的关键词模糊匹配一律经keyword_like():
    bad = sorted(raw_fuzzy_matches() - BY_DESIGN.keys())
    assert len(bad) <= BASELINE, (
        "这些模糊匹配直接落在字符串列上——开发库 SQLite 不分大小写、生产库 PostgreSQL 区分：\n  " + "\n  ".join(bad)
        + "\n\n用户关键词检索改成 `keyword_like(列, 关键词)`（`from app.deps import keyword_like`）；"
        "确实不是用户输入的（代码常量、系统生成的前缀、纯数字），进 BY_DESIGN 写明理由。"
    )


def test_按设计名单只减不增():
    stale = sorted(BY_DESIGN.keys() - raw_fuzzy_matches())
    assert stale == [], "这些已经不在了（改走 keyword_like、挪了函数或删了），请从 BY_DESIGN 划掉：\n  " + "\n  ".join(stale)


def test_判据自证_认得出裸调用_不认keyword_like(tmp_path):
    (tmp_path / "probe.py").write_text(
        "def search(q, kw):\n"
        "    a = q.filter(Drug.name.like(f'%{kw}%'))\n"
        "    b = q.filter(Drug.name.contains(kw))\n"
        "    c = q.filter(keyword_like(Drug.name, kw))\n"
        "    d = q.filter(Drug.qty.like('1%'))\n",
        encoding="utf-8",
    )
    found = raw_fuzzy_matches(tmp_path, {"Drug": {"name"}})
    assert found == {"probe.py:search:Drug.name.like", "probe.py:search:Drug.name.contains"}


def test_keyword_like两边都转小写():
    from sqlalchemy import Column, MetaData, String, Table

    from app.deps import keyword_like

    t = Table("t", MetaData(), Column("name", String(16)))
    expr = keyword_like(t.c.name, "HbA1c")
    sql = str(expr.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "lower(t.name) like '%hba1c%'" in sql, sql


# ================================================================ 端点回归（真 PG 上修前查不到）
@pytest.mark.parametrize("keyword, code", [("i10", "I10"), ("e11", "E11"), ("I10", "I10")])
def test_诊断字典按小写编码也搜得到(client, admin, keyword, code):
    resp = client.get("/api/dictionaries/diagnosis/entries", headers=admin, params={"keyword": keyword})
    assert resp.status_code == 200, resp.text
    assert code in {e["code"] for e in resp.json()}   # 修前真 PG：小写两条返回空


def test_知识库标题里的缩写按小写也搜得到(client, admin):
    created = client.post("/api/knowledge", headers=admin,
                          json={"category": "clinical_guideline", "title": "P266 HbA1c 控制目标"})
    assert created.status_code == 201, created.text
    resp = client.get("/api/knowledge", headers=admin, params={"q": "p266 hba1c"})
    assert resp.status_code == 200, resp.text
    assert created.json()["id"] in {e["id"] for e in resp.json()}


def test_慢专病病种名里的缩写按小写也搜得到(client, admin):
    created = client.post("/api/spd/programs", headers=admin,
                          json={"code": "p266_copd", "name": "P266 慢阻肺（COPD）", "category": "chronic"})
    assert created.status_code == 201, created.text
    resp = client.get("/api/spd/programs", headers=admin, params={"keyword": "p266 慢阻肺（copd"})
    assert resp.status_code == 200, resp.text
    assert "p266_copd" in {p["code"] for p in resp.json()}


def test_患者按小写健康卡号也搜得到(client, admin):
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P266 检索", "id_card": "330192198001010768"}).json()
    resp = client.get("/api/patients", headers=admin, params={"keyword": patient["ehc_no"].lower()})
    assert resp.status_code == 200, resp.text
    assert patient["id"] in {p["id"] for p in resp.json()}
