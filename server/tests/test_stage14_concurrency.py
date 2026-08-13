"""第八轮：新增模块的并发写入缺陷（D-5/D-6/D-7）与防复发扫描。

平台早在阶段七就踩过一次 check-then-act（D-2 全域基金池并发建出两个），
阶段九修掉并把教训写进注释。结果阶段九·五新写的三个模块又各犯一遍——
**知道这个坑并不足以不掉进去**。所以除了修三处，还立了一条扫描用例：
往带唯一约束的表里写、却没处理约束冲突的，一律红。
"""
import ast
import os
import threading

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from conftest import reset_database

from app import models
from app.main import app

ROUTER_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "routers")


@pytest.fixture(scope="module")
def client():
    reset_database()
    # raise_server_exceptions=False：未捕获异常按 500 返回而不是把异常抛进用例。
    # 这里要断言的正是"并发下会不会出 500"，让它抛出来就看不到状态码了。
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "并发县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "并发受种者", "id_card": "320000199001012222"},
        headers=admin,
    ).json()


def _race(call, times=8):
    """并发跑同一个请求，返回排序后的状态码。

    用 `Barrier` 卡住所有线程、放行时才一起进——只靠"起 8 个线程"是不够的：
    线程创建本身有先后，前一个常常已经提交完了后一个才开始读，
    竞态窗口根本没打开。实测字典导入就是这样：不加栅栏时旧代码照样全绿，
    加了栅栏立刻现出 500。**测并发的用例自己必须先真的并发。**
    """
    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(times)

    def run():
        barrier.wait(timeout=30)
        code = call()
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=run) for _ in range(times)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(codes)


# ================================================================ D-5 医废追溯码


def test_并发收集医废不丢记录也不出500(client, admin, org):
    """D-5：追溯码由 `COUNT+1` 算出，并发下会算出同一个。

    实测（修复前）：8 个请求只落库 5 条，另外 3 条抛未捕获的 IntegrityError
    成 500——**医废记录丢了**，而医废条例要求的正是全过程可追溯，
    收集环节丢记录等于台账对不上。
    """
    body = {"org_id": org["id"], "waste_type": "infectious", "weight_kg": 1.0,
            "collected_date": "2026-08-12"}
    codes = _race(lambda: client.post("/api/medwaste", json=body, headers=admin).status_code)
    assert codes == [201] * 8, f"并发收集出现非 201：{codes}"

    rows = client.get("/api/medwaste", params={"org_id": org["id"]}, headers=admin).json()
    assert len(rows) == 8, f"8 次收集只落库 {len(rows)} 条"
    traces = [r["trace_code"] for r in rows]
    assert len(set(traces)) == 8, f"追溯码有重复：{traces}"


# ================================================================ D-6 批次超卖


def test_并发接种不会打穿批次库存(client, admin, org, patient):
    """D-6：原先"先读再判再 +1"，并发下每个请求都读到同一个 used_quantity。

    实测（修复前）：库存 1 支，4 个请求全部 201 接种成功，而 used_quantity
    只加到 1——**台账说还剩 0，实际打了 4 针**。疫苗是按批号强监管的品类，
    这种账实不符在召回时直接失去意义。
    """
    batch = client.post(
        "/api/vaccine-supply/batches",
        json={"vaccine_code": "VRACE", "vaccine_name": "并发疫苗", "batch_no": "BR1",
              "expire_date": "2099-01-01", "org_id": org["id"], "quantity": 1},
        headers=admin,
    ).json()
    body = {"patient_id": patient["id"], "vaccine_code": "VRACE", "vaccine_name": "并发疫苗",
            "dose_no": 1, "vaccinated_date": "2026-08-12", "org_id": org["id"],
            "batch_id": batch["id"]}
    codes = _race(
        lambda: client.post("/api/vaccination/records", json=body, headers=admin).status_code,
        times=6,
    )
    assert codes.count(201) == 1, f"库存 1 支却成功接种 {codes.count(201)} 次：{codes}"
    assert codes.count(409) == 5

    after = client.get(
        "/api/vaccine-supply/batches", params={"vaccine_code": "VRACE"}, headers=admin
    ).json()[0]
    assert after["used_quantity"] == 1 and after["remaining"] == 0

    # 台账与实际必须对得上：接种记录数 == 已用支数
    records = client.get(
        "/api/vaccination/records", params={"patient_id": patient["id"]}, headers=admin
    ).json()
    assert len([r for r in records if r["batch_no"] == "BR1"]) == 1


# ================================================================ D-7 症候群日报


def test_并发上报症候群不出500且仍是覆盖语义(client, admin, org):
    """D-7：先查有没有、没有就插，并发下两个请求都查不到就都去插。

    实测（修复前）：8 并发出一个 500。改为先试插、撞了再取回来更新，
    覆盖语义不变（同机构同症候群同日只有一条），并发下也不会 500。
    """
    body = {"org_id": org["id"], "syndrome": "fever", "case_count": 5,
            "threshold": 0, "record_date": "2026-08-12"}
    codes = _race(
        lambda: client.post("/api/surveillance/syndromes", json=body, headers=admin).status_code
    )
    assert codes == [201] * 8, f"并发上报出现非 201：{codes}"

    rows = client.get(
        "/api/surveillance/syndromes", params={"org_id": org["id"]}, headers=admin
    ).json()
    same_day = [r for r in rows if r["record_date"] == "2026-08-12" and r["syndrome"] == "fever"]
    assert len(same_day) == 1, "同机构同症候群同日应当只有一条（覆盖语义）"
    assert same_day[0]["case_count"] == 5


def test_覆盖语义在串行下照常生效(client, admin, org):
    """并发改造不能把"重复上报按覆盖"改成"重复上报报错"。"""
    body = {"org_id": org["id"], "syndrome": "diarrhea", "case_count": 10,
            "threshold": 20, "record_date": "2026-08-11"}
    first = client.post("/api/surveillance/syndromes", json=body, headers=admin).json()
    assert first["overwritten"] is False
    second = client.post(
        "/api/surveillance/syndromes", json={**body, "case_count": 14}, headers=admin
    ).json()
    assert second["overwritten"] is True
    assert second["id"] == first["id"]
    assert second["case_count"] == 14  # 覆盖，不是累加


# ================================================================ 批量导入


def test_并发字典导入不会整批回滚(client, admin):
    """批量导入是"一次 commit 提交整批"，一条撞车会让整批回滚。

    原写法先把已有编码查出来、不在里面的才插——两个导入并发跑读到同一份
    existing，都判定"不存在"就都去插，后插的抛 IntegrityError，
    **导入方看到 500 且一条都没进**。改用 SAVEPOINT 把冲突圈在单行内。
    """
    entries = [{"code": f"RACE{i:03d}", "name": f"并发条目{i}"} for i in range(30)]
    codes = _race(
        lambda: client.post(
            "/api/dictionaries/diagnosis/import", json=entries, headers=admin
        ).status_code,
        times=8,
    )
    assert codes == [200] * 8, f"并发导入出现非 200：{codes}"

    rows = client.get(
        "/api/dictionaries/diagnosis/entries", params={"keyword": "RACE"}, headers=admin
    ).json()
    landed = sorted(r["code"] for r in rows)
    assert landed == sorted(e["code"] for e in entries), "并发导入后条目不全"


def test_批量插入不得堆积未释放的savepoint():
    """insert_if_absent 成功时必须释放 SAVEPOINT。

    漏掉 `savepoint.commit()` 时每行都往外层事务上再压一层，`db.commit()`
    收束时递归深度等于行数——权限点同步 640 行直接 RecursionError，
    **应用起不来**。这个错误 5 行的样例试不出来，所以这里按真实规模压。
    """
    from app.concurrency import insert_if_absent
    from app.database import SessionLocal
    from app.models import Role

    db = SessionLocal()
    try:
        for i in range(800):
            assert insert_if_absent(db, Role(key=f"sp_{i}", name=f"压测角色{i}", builtin=False))
        db.commit()  # 未释放 SAVEPOINT 时这一句抛 RecursionError
        assert db.query(models.Role).filter(models.Role.key.like("sp_%")).count() == 800
    finally:
        db.query(Role).filter(Role.key.like("sp_%")).delete(synchronize_session=False)
        db.commit()
        db.close()


# ================================================================ 防复发扫描


def _tables_with_unique_constraint() -> set[str]:
    """带唯一约束（表级或列级）的表名。"""
    names = set()
    for table in models.Base.metadata.tables.values():
        if any(isinstance(c, sa.UniqueConstraint) for c in table.constraints):
            names.add(table.name)
            continue
        if any(c.unique for c in table.columns):
            names.add(table.name)
            continue
        if any(i.unique for i in table.indexes):
            names.add(table.name)
    return names


def _model_to_table() -> dict[str, str]:
    return {
        cls.__name__: cls.__tablename__
        for cls in models.Base.registry._class_registry.values()
        if hasattr(cls, "__tablename__")
    }


# 明确豁免：往带唯一约束的表里写、但**不会**撞约束的位置，逐个写明理由。
# 豁免必须写理由——一旦可以不写，这份清单很快会变成绕过检查的后门。
CONFLICT_SAFE = {
    "users.py:create_user": "用户名先查重且 409；并发重名撞约束的后果只是 500 一次，"
                            "不产生错账，暂按已知接受",
    "org_groups.py:add_member": "已捕获 IntegrityError",
}


def test_写唯一约束表的接口必须处理约束冲突():
    """防的是这一轮实测到的同一个错误再犯第四次。

    判据：函数体里出现 `db.add(SomeModel(...))`，而 `SomeModel` 的表带唯一约束，
    则该函数必须出现下列之一——捕获 `IntegrityError`，或调用
    `app/concurrency.py` 里的三个助手。判据会有漏网（例如通过变量间接构造
    的对象），但**不会误报**：命中的都确实是"往带唯一约束的表里直接插入"。
    """
    model_table = _model_to_table()
    unique_tables = _tables_with_unique_constraint()
    helpers = {"insert_or_conflict", "insert_with_retry", "upsert_unique"}
    offenders = []

    for name in sorted(os.listdir(ROUTER_DIR)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(ROUTER_DIR, name)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            key = f"{name}:{func.name}"
            if key in CONFLICT_SAFE:
                continue
            source = ast.dump(func)
            handled = "IntegrityError" in source or any(h in source for h in helpers)
            if handled:
                continue
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "add" or not node.args:
                    continue
                arg = node.args[0]
                if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)):
                    continue
                table = model_table.get(arg.func.id)
                if table and table in unique_tables:
                    offenders.append(f"{key} → {arg.func.id}({table})")
                    break

    assert offenders == [], (
        "以下接口往带唯一约束的表里直接插入，却没处理约束冲突"
        "（并发下会 500 并丢数据）：\n  " + "\n  ".join(offenders) +
        "\n请改用 app/concurrency.py 的 insert_or_conflict / insert_with_retry / "
        "upsert_unique，或显式捕获 IntegrityError；确属不会冲突的加进 CONFLICT_SAFE 并写明理由。"
    )


def test_豁免清单不得腐烂():
    """豁免条目对应的函数必须还存在——函数改名或删掉之后豁免会一直挂着。"""
    existing = set()
    for name in os.listdir(ROUTER_DIR):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(ROUTER_DIR, name), encoding="utf-8").read())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            existing.add(f"{name}:{func.name}")
    stale = sorted(set(CONFLICT_SAFE) - existing)
    assert stale == [], f"豁免清单里这些函数已不存在，应删除：{stale}"
