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
# 慢专病子系统的路由在自己的包里（app/spd/routers/）。**必须一并扫**：
# 子系统换个目录就绕过防复发扫描，是这类规则最典型的失效方式。
SPD_ROUTER_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "spd", "routers")
ROUTER_DIRS = (ROUTER_DIR, SPD_ROUTER_DIR)


def _router_files():
    """全部路由文件的 (显示名, 绝对路径)。显示名带子系统前缀，报错时一眼看出出处。"""
    files = []
    for directory in ROUTER_DIRS:
        label = "spd/" if directory is SPD_ROUTER_DIR else ""
        for name in sorted(os.listdir(directory)):
            if name.endswith(".py"):
                files.append((f"{label}{name}", os.path.join(directory, name)))
    return files



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


# ============================================ 扫描修好的存量站点（抽高风险的验）


def test_并发建册不会给同一个孕产妇建出两本(client, admin):
    """一个孕产妇两本册子，产检记录会分叉——两本各记一半，哪本都不全。

    建册本就是幂等的（查到既有就返回），但那是 check-then-act：
    并发下两个请求都查不到就都去插。
    """
    patient = client.post(
        "/api/patients", json={"name": "并发孕妇", "id_card": "320000199203034321"}, headers=admin
    ).json()
    body = {"patient_id": patient["id"], "lmp_date": "2026-01-01", "expected_date": "2026-10-08"}
    codes = _race(
        lambda: client.post("/api/maternal/records", json=body, headers=admin).status_code
    )
    assert all(c < 400 for c in codes), f"并发建册出现错误码：{codes}"

    rows = client.get(
        "/api/maternal/records", params={"patient_id": patient["id"]}, headers=admin
    ).json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    assert len(rows) == 1, f"同一孕产妇建出了 {len(rows)} 本册子"


def test_并发签发医学证明不会重号也不丢件(client, admin, org):
    """证明编号同样是 COUNT+1 算出来的（与医废追溯码同型）。

    死亡医学证明、出生缺陷登记都是对外出具的法定文书，
    重号或丢件都不是"重试一下"能了事的。
    """
    body = {"org_id": org["id"], "cert_type": "birth", "name": "并发新生儿",
            "event_date": "2026-08-12", "detail": "足月顺产"}
    codes = _race(lambda: client.post("/api/certs", json=body, headers=admin).status_code)
    assert all(c < 400 for c in codes), f"并发签发出现错误码：{codes}"

    rows = client.get("/api/certs", params={"cert_type": "birth"}, headers=admin).json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    nos = [r["cert_no"] for r in rows]
    assert len(nos) == 8, f"8 次签发只落库 {len(nos)} 件"
    assert len(set(nos)) == len(nos), f"证明编号有重复：{sorted(nos)}"


def test_并发入库不会把库存行建成两条也不丢批次(client, admin, org):
    """药品库存按 (org_id, drug_code) 唯一。两批药同时入库，原先都去建行，
    撞唯一约束 → 500，**两批都没入成**，而药还在库里。"""
    body = {"org_id": org["id"], "drug_code": "DRACE", "drug_name": "并发药",
            "quantity": 10, "threshold": 1}
    codes = _race(lambda: client.post("/api/pharmacy/stocks", json=body, headers=admin).status_code)
    assert all(c < 400 for c in codes), f"并发入库出现错误码：{codes}"

    rows = client.get("/api/pharmacy/stocks", params={"org_id": org["id"]}, headers=admin).json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    mine = [r for r in rows if r["drug_code"] == "DRACE"]
    assert len(mine) == 1, f"同机构同药品建出了 {len(mine)} 条库存行"


# ====================================================== D-9 读-改-写丢更新


def test_并发入库不丢数量(client, admin, org):
    """D-9：`stock.quantity += n` 是读-改-写，并发下后写的把先写的盖掉。

    实测（修复前）：建行 10 支后 8 路并发各入库 10 支，应为 90，实际 30，
    **凭空少了 60 支**。这类账实不符最难查——每一笔入库的日志都显示成功。
    """
    body = {"org_id": org["id"], "drug_code": "DSUM", "drug_name": "累加药",
            "quantity": 10, "threshold": 1}
    assert client.post("/api/pharmacy/stocks", json=body, headers=admin).status_code < 400
    codes = _race(lambda: client.post("/api/pharmacy/stocks", json=body, headers=admin).status_code)
    assert all(c < 400 for c in codes), f"并发入库出现错误码：{codes}"

    rows = client.get("/api/pharmacy/stocks", params={"org_id": org["id"]}, headers=admin).json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    stock = [r for r in rows if r["drug_code"] == "DSUM"][0]
    assert stock["quantity"] == 90, f"入库 9 次×10 支，应为 90，实际 {stock['quantity']}"


def test_并发出库不会扣成负库存(client, admin, org):
    """出库要"够才能扣"。原先先判够不够再 `-=`，并发下都判定够。"""
    asset = client.post(
        "/api/mgmt/assets",
        json={"org_id": org["id"], "code": "AST-RACE", "name": "并发物资",
              "category": "office", "quantity": 5},
        headers=admin,
    ).json()
    codes = _race(
        lambda: client.post(
            f"/api/mgmt/assets/{asset['id']}/movements",
            json={"movement_type": "issue", "quantity": 1},
            headers=admin,
        ).status_code,
        times=8,
    )
    ok = len([c for c in codes if c < 400])
    assert ok == 5, f"库存 5 件却出库成功 {ok} 次：{codes}"

    rows = client.get("/api/mgmt/assets", params={"org_id": org["id"]}, headers=admin).json()
    after = [r for r in rows if r["code"] == "AST-RACE"][0]
    assert after["quantity"] == 0, f"现存量应为 0，实际 {after['quantity']}"


def test_物资全部出库后清单仍打得开(client, admin, org):
    """D-10：出参不能直接继承入参的校验。

    `AssetCreate.quantity` 是 `ge=1`（不该建 0 件的物资，这没错），出参继承了它，
    于是**一件物资领完，整张物资清单连带 500**——别的物资也跟着看不见。
    这条是写并发用例时撞出来的，与并发无关，串行照样复现。
    """
    asset = client.post(
        "/api/mgmt/assets",
        json={"org_id": org["id"], "code": "AST-ZERO", "name": "领完的物资",
              "category": "office", "quantity": 2},
        headers=admin,
    ).json()
    client.post(
        f"/api/mgmt/assets/{asset['id']}/movements",
        json={"movement_type": "issue", "quantity": 2},
        headers=admin,
    )
    listing = client.get("/api/mgmt/assets", params={"org_id": org["id"]}, headers=admin)
    assert listing.status_code == 200, f"现存量 0 的物资把清单打挂了：{listing.text[:120]}"
    row = [r for r in listing.json() if r["code"] == "AST-ZERO"][0]
    assert row["quantity"] == 0

    # 放宽的只是出参：建档仍不接受 0 件
    rejected = client.post(
        "/api/mgmt/assets",
        json={"org_id": org["id"], "code": "AST-NEW", "name": "空物资",
              "category": "office", "quantity": 0},
        headers=admin,
    )
    assert rejected.status_code == 422


def test_不得再用读改写累加计数(client):
    """扫描：`obj.col += x` / `-= x` 形式的累加在路由层一律不允许。

    D-9 这一类的成因与 D-5～D-8 相同——正确做法（原子 UPDATE）平台早就有，
    只是没抽出来，于是每写一个新模块就再手写一遍读-改-写。
    抽出来之后要配一条规则盯住，否则下一个模块照旧。
    """
    allowed = {
        # 请求内新建、尚未提交的对象，不存在并发竞争
        "encounters.py:_accumulate_local",
    }
    offenders = []
    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            key = f"{name}:{func.name}"
            if key in allowed:
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.AugAssign):
                    continue
                if not isinstance(node.target, ast.Attribute):
                    continue
                if not isinstance(node.op, (ast.Add, ast.Sub)):
                    continue
                # 只管 ORM 列上的累加；本地变量/累加器（sum += x）不在此列
                if isinstance(node.target.value, ast.Name) and node.target.value.id in (
                    "self",
                    "totals",
                    "acc",
                ):
                    continue
                offenders.append(f"{key} → {ast.unparse(node)}")
    assert offenders == [], (
        "以下位置对数据库对象做读-改-写累加（并发下丢更新）：\n  "
        + "\n  ".join(offenders)
        + "\n请改用 app/concurrency.py 的 add_amount / take_amount / claim_quota。"
    )


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
    "org_groups.py:add_member": "已捕获 IntegrityError",
}


def _inserted_models(func: ast.FunctionDef, model_names: set[str]) -> set[str]:
    """函数里被 `db.add(...)` 插入的模型名。

    两种写法都要认：
        db.add(Model(...))            # 内联
        obj = Model(...); db.add(obj)  # 先赋值再插——**这才是绝大多数**

    第一版只认内联写法，结果 192 处插入里只看得见 22 处（11%），
    而变量写法里藏着 48 处真问题。**一条只覆盖 11% 的规则给出的是虚假的安全感**，
    比没有规则更糟——它会让人以为这一类已经被守住了。
    """
    assigned: dict[str, str] = {}
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in model_names
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.value.func.id

    inserted = set()
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
            if arg.func.id in model_names:
                inserted.add(arg.func.id)
        elif isinstance(arg, ast.Name) and arg.id in assigned:
            inserted.add(assigned[arg.id])
    return inserted


def test_写唯一约束表的接口必须处理约束冲突():
    """防的是这一轮实测到的同一个错误再犯第四次。

    判据：函数里 `db.add` 了某个模型，而该模型的表带唯一约束，则该函数必须
    出现下列之一——捕获 `IntegrityError`，或调用 `app/concurrency.py` 的助手。

    仍会有漏网（跨函数传对象、循环里从容器取对象），但**不会误报**：
    命中的都确实是"往带唯一约束的表里插入"。宁可漏也不误报——一条经常误报的
    规则会先被加豁免、再被加得没人看，最后被删掉。
    """
    model_table = _model_to_table()
    unique_tables = _tables_with_unique_constraint()
    helpers = {"insert_or_conflict", "insert_with_retry", "upsert_unique", "insert_if_absent"}
    offenders = []

    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        model_names = set(model_table)
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            key = f"{name}:{func.name}"
            if key in CONFLICT_SAFE:
                continue
            source = ast.dump(func)
            handled = "IntegrityError" in source or any(h in source for h in helpers)
            if handled:
                continue
            for model in sorted(_inserted_models(func, model_names)):
                table = model_table[model]
                if table in unique_tables:
                    offenders.append(f"{key} → {model}({table})")
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
    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            existing.add(f"{name}:{func.name}")
    stale = sorted(set(CONFLICT_SAFE) - existing)
    assert stale == [], f"豁免清单里这些函数已不存在，应删除：{stale}"


# ============================================ 第十轮 P1：实训报名超额（延迟登记的欠账）


_enroll_seq = [0]


def _mk_org_and_students(client, admin, n):
    """每次调用造一家名字唯一的机构与 n 个学员账号（机构名/用户名带序号，
    避免多个用例之间撞唯一约束）。"""
    k = _enroll_seq[0]
    _enroll_seq[0] += 1
    org = client.post(
        "/api/organizations",
        json={"name": f"实训并发院{k}", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    toks = []
    for i in range(n):
        uname = f"enr{k}_stu{i}"
        client.post(
            "/api/users",
            json={"username": uname, "password": "pw123456", "full_name": f"学员{i}",
                  "role": "doctor", "org_id": org["id"]},
            headers=admin,
        )
        tk = client.post(
            "/api/auth/login", json={"username": uname, "password": "pw123456"}
        ).json()["access_token"]
        toks.append({"Authorization": f"Bearer {tk}"})
    return org, toks


def test_并发报名不超实训名额(client, admin):
    """D-11：`COUNT(*) >= capacity` 是 check-then-act，并发下多人同时数到
    "还差一个"一起挤进来。实测（修复前）容量 2 报上 3 人。改用 claim_quota
    原子占额：判满与占位同一条 SQL。"""
    org, toks = _mk_org_and_students(client, admin, 6)
    plan = client.post(
        "/api/education/training-plans",
        json={"title": "并发实训", "capacity": 2, "org_id": org["id"], "plan_date": "2026-09-01"},
        headers=admin,
    ).json()
    pid = plan["id"]

    import threading
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(toks))

    def run(h):
        barrier.wait(timeout=30)
        code = client.post(f"/api/education/training-plans/{pid}/enroll", headers=h).status_code
        with lock:
            results.append(code)

    threads = [threading.Thread(target=run, args=(h,)) for h in toks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 2, f"容量 2 却报上 {results.count(201)} 人：{sorted(results)}"
    assert results.count(409) == 4

    enr = client.get(f"/api/education/training-plans/{pid}/enrollments", headers=admin).json()
    assert len([e for e in enr if e["status"] == "enrolled"]) == 2, "在册报名数与容量对不上"
    plans = client.get("/api/education/training-plans", headers=admin).json()
    row = [p for p in plans if p["id"] == pid][0]
    assert row["enrolled"] == 2 and row["remaining"] == 0


def test_退报名释放名额(client, admin):
    """占额与释放必须对称，否则退了报名名额也放不出来，计划永远显示满。"""
    org, toks = _mk_org_and_students(client, admin, 2)
    plan = client.post(
        "/api/education/training-plans",
        json={"title": "退报名实训", "capacity": 1, "org_id": org["id"], "plan_date": "2026-09-02"},
        headers=admin,
    ).json()
    pid = plan["id"]
    assert client.post(f"/api/education/training-plans/{pid}/enroll", headers=toks[0]).status_code == 201
    # 满了，第二个人报不上
    assert client.post(f"/api/education/training-plans/{pid}/enroll", headers=toks[1]).status_code == 409
    # 第一个人退，名额放出来
    assert client.post(f"/api/education/training-plans/{pid}/cancel-enroll", headers=toks[0]).status_code == 200
    assert client.post(f"/api/education/training-plans/{pid}/enroll", headers=toks[1]).status_code == 201
    plans = client.get("/api/education/training-plans", headers=admin).json()
    assert [p for p in plans if p["id"] == pid][0]["enrolled"] == 1
