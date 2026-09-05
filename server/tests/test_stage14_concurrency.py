"""第八轮：新增模块的并发写入缺陷（D-5/D-6/D-7）与防复发扫描。

平台早在阶段七就踩过一次 check-then-act（D-2 全域基金池并发建出两个），
阶段九修掉并把教训写进注释。结果阶段九·五新写的三个模块又各犯一遍——
**知道这个坑并不足以不掉进去**。所以除了修三处，还立了一条扫描用例：
往带唯一约束的表里写、却没处理约束冲突的，一律红。
"""
import ast
import os
import textwrap
import threading
import warnings
from collections import Counter

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
    """全部路由文件的 (显示名, 绝对路径)。显示名带子系统前缀与子目录，报错时一眼看出出处。

    **必须递归**（os.walk 而不是 os.listdir）：`app/spd/routers/config/` 是一个
    子包，用 listdir 一层扫只看得到 87 个文件，那 8 个文件里的 18 个写入点
    从来没被任何一条防复发规则看过。"路由拆成子包"是这类扫描最常见的失效方式——
    代码结构一变，闸门自己不声不响地缩了水，而它照样报绿。
    """
    files = []
    for directory in ROUTER_DIRS:
        label = "spd/" if directory is SPD_ROUTER_DIR else ""
        root_dir = os.path.abspath(directory)
        for root, dirs, names in os.walk(root_dir):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            rel = os.path.relpath(root, root_dir)
            prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for name in sorted(names):
                if name.endswith(".py"):
                    files.append((f"{label}{prefix}{name}", os.path.join(root, name)))
    return sorted(files)



@pytest.fixture(scope="module")
def client():
    reset_database()
    # raise_server_exceptions=False：未捕获异常按 500 返回而不是把异常抛进用例。
    # 这里要断言的正是"并发下会不会出 500"，让它抛出来就看不到状态码了。
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


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


# 读-改-写的**赋值形状**存量欠账（只减不增）。
# 规则原先只认 `ast.AugAssign`（`x.c += n`）——而同一个缺陷用赋值写出来
# （`x.c = f(x.c, n)`）它一个也看不见：实测 21 处赋值形状的读-改-写全在规则之外，
# 其中 8 处是货真价实的累加/追加（金额、字符串、JSON 列）。**不删规则、不放宽断言**，逐条登记为欠账：
# 新增一条会顶破基线变红，修掉一条就把它从清单里删掉。
#
# 8 处真累加/追加已于 P1-28 清零（字符串追加 → `concurrency.append_text`；处方审核 →
# 一条带状态条件的 UPDATE；JSON 列整体覆写 → `serialized_on` 行锁临界区内重读再写）。
# 登记粒度随之改成 **函数 × 条数**（`{key: (条数, 性质)}`）而不只是函数名：清零那一轮
# 发现 `record_call_result` 一个名字下同时挂着两条真追加与两条幂等回填——只登函数名，
# 真追加修掉之后谁再往这个函数里塞一条新的读-改-写，规则照样报绿。条数**只许变小**，
# 且必须与实际相等：多了由 `test_不得再用读改写累加计数` 判红，少了由
# `test_读改写欠账清单不得腐烂` 逼着把条数调低（为 0 则删条目）。
KNOWN_READ_MODIFY_WRITE: dict[str, tuple[int, str]] = {
    # —— 幂等/取极值形状：同为读-改-写，但重复执行结果一致，丢更新后果有限（登记，暂不修）——
    "education.py:submit_exam": (1, "score = max(score, 新分)，取极值"),
    "portal.py:bind_wechat": (1, "nickname = nickname or 新值，幂等回填"),
    "spd/followup.py:record_call_result": (
        2, "started_at / operator_id = 旧值 or 新值，幂等回填（同函数的两条真追加已进 serialized_on 临界区）",
    ),
    "spd/portal.py:feedback_intervention": (1, "read_at = read_at or now，幂等回填"),
    "spd/portal.py:read_education": (1, "read_at = read_at or now，幂等回填"),
    "spd/tasks.py:escalate_task": (1, "priority = max(priority, 2)，取极值"),
    "spd/tasks.py:submit_task": (1, "assignee_id = assignee_id or 当前用户，幂等回填"),
    "spd/config/catalog.py:update_program": (1, "version = _bump_version(version)，配置版本号自增（低频、单写者）"),
}


def _serialized_nodes(func: ast.FunctionDef) -> set[int]:
    """`with serialized_on(...)` 临界区里、且 `db.refresh(...)` **之后**的全部节点 id。

    行锁到手后重读再改写，才不是丢更新（`concurrency.serialized_on` 文档约定）；
    锁内但 refresh 之前的赋值用的仍是锁外读到的旧值，不豁免。别的 with
    （`begin_nested()` 之类）不是临界区，也不豁免。
    """
    guarded: set[int] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.With):
            continue
        if not any(
            isinstance(item.context_expr, ast.Call)
            and ast.unparse(item.context_expr.func).rsplit(".", 1)[-1] == "serialized_on"
            for item in node.items
        ):
            continue
        refreshed = False
        for stmt in node.body:
            if refreshed:
                guarded.update(id(n) for n in ast.walk(stmt))
            elif any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "refresh"
                for n in ast.walk(stmt)
            ):
                refreshed = True
    return guarded


def _read_modify_writes_in(func: ast.FunctionDef) -> list[ast.stmt]:
    """一个函数里对 ORM 对象做读-改-写的语句，两种形状都要认：

        obj.col += n                     # AugAssign——第一版只认这个
        obj.col = f(obj.col, n)          # Assign 且右值里含同一个属性——**这才是多数**

    只认 AugAssign 的那一版，在同一份代码上漏掉 21 处赋值形状的读-改-写：
    一条只看得见一种写法的规则，给出的是虚假的安全感（第 17 章例三）。
    """
    local_names = ("self", "totals", "acc")
    guarded = _serialized_nodes(func)
    found: list[ast.stmt] = []
    for node in ast.walk(func):
        if id(node) in guarded:
            continue
        target = None
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Attribute):
                continue
            if not isinstance(node.op, (ast.Add, ast.Sub)):
                continue
            target = node.target
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Attribute):
                continue
            target = node.targets[0]
            src = ast.unparse(target)
            # 右值里必须出现同一个属性，才叫"读了旧值再写回去"；
            # 纯赋新值（obj.col = body.x）不是读-改-写，不报。
            if not any(
                isinstance(sub, ast.Attribute) and ast.unparse(sub) == src
                for sub in ast.walk(node.value)
            ):
                continue
        else:
            continue
        # 只管 ORM 列上的读-改-写；本地变量/累加器（sum += x）不在此列
        if isinstance(target.value, ast.Name) and target.value.id in local_names:
            continue
        found.append(node)
    return found


def _read_modify_write_offenders() -> list[str]:
    """路由层里全部读-改-写的位置（`文件:函数 → 语句`），豁免见 `_read_modify_writes_in`。"""
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
            offenders.extend(f"{key} → {ast.unparse(node)}" for node in _read_modify_writes_in(func))
    return offenders


def test_不得再用读改写累加计数(client):
    """扫描：`obj.col += x` 与 `obj.col = f(obj.col, x)` 两种读-改-写在路由层一律不允许。

    D-9 这一类的成因与 D-5～D-8 相同——正确做法（原子 UPDATE）平台早就有，
    只是没抽出来，于是每写一个新模块就再手写一遍读-改-写。
    抽出来之后要配一条规则盯住，否则下一个模块照旧。
    """
    offenders = _read_modify_write_offenders()
    by_function: dict[str, list[str]] = {}
    for offender in offenders:
        by_function.setdefault(offender.split(" → ")[0], []).append(offender)
    new = sorted(
        f"{offender}（该函数登记 {KNOWN_READ_MODIFY_WRITE.get(key, (0,))[0]} 条，实际 {len(items)} 条）"
        for key, items in by_function.items()
        if len(items) > KNOWN_READ_MODIFY_WRITE.get(key, (0, ""))[0]
        for offender in items
    )
    print(
        f"\n[读-改-写规则] 命中 {len(offenders)} 处（AugAssign + Assign 两种形状），"
        f"其中已登记欠账 {len(offenders) - len(new)} 处、新增 {len(new)} 处"
    )
    assert new == [], (
        "以下位置对数据库对象做读-改-写（并发下丢更新）：\n  "
        + "\n  ".join(new)
        + "\n请改用 app/concurrency.py 的 add_amount / take_amount / claim_quota / append_text；"
        "一条 SQL 压不进去的（JSON 列）进 serialized_on 临界区并先 db.refresh；"
        "\n确属存量欠账的，登记进 KNOWN_READ_MODIFY_WRITE 并写明条数与性质（清单只减不增）。"
    )


def test_读改写欠账清单不得腐烂():
    """清单条目登记的条数必须与该函数里实际剩下的读-改-写条数**相等**。

    只登函数名的旧清单里挂了三条早已修好的陈账（`refund_payment` / `_finish_task` /
    `dispatch_edu_push`），没有任何检查在证明它们还成立。欠账清单腐烂的方向永远是
    "欠账其实已经还了、清单还在替它占位"——占着的位以后就能被新欠账悄悄坐进去。
    修掉一条就把条数调低，调到 0 就删条目。
    """
    counts = Counter(o.split(" → ")[0] for o in _read_modify_write_offenders())
    loose = sorted(
        f"{key}：登记 {cap} 条，实际 {counts.get(key, 0)} 条"
        for key, (cap, _reason) in KNOWN_READ_MODIFY_WRITE.items()
        if counts.get(key, 0) < cap
    )
    assert loose == [], (
        "欠账清单里这些条目登记的条数高于实际，请调低（为 0 则删掉条目）：\n  " + "\n  ".join(loose)
    )


def test_读改写规则自证_临界区豁免只认refresh之后():
    """规则自己的行为用一段合成代码钉住，五种形状各得其所——规则改坏了这里先红。"""
    source = textwrap.dedent(
        '''
        def plain(db, obj, body):
            obj.note = obj.note + body.note

        def locked_after_refresh(db, obj, body):
            with serialized_on(db, Model, obj.id):
                db.refresh(obj)
                obj.log = (obj.log or []) + [body.entry]
                db.commit()

        def locked_before_refresh(db, obj, body):
            with serialized_on(db, Model, obj.id):
                obj.log = (obj.log or []) + [body.entry]
                db.refresh(obj)
                db.commit()

        def other_with(db, obj, body):
            with db.begin_nested():
                db.refresh(obj)
                obj.count += 1

        def fresh_value(db, obj, body):
            obj.note = body.note
        '''
    )
    flagged = {
        func.name: [ast.unparse(node) for node in _read_modify_writes_in(func)]
        for func in ast.parse(source).body
        if isinstance(func, ast.FunctionDef)
    }
    assert flagged == {
        "plain": ["obj.note = obj.note + body.note"],           # 裸读-改-写：报
        "locked_after_refresh": [],                              # 锁内、refresh 后：豁免
        "locked_before_refresh": ["obj.log = (obj.log or []) + [body.entry]"],  # 锁内但 refresh 前：报
        "other_with": ["obj.count += 1"],                        # 别的 with 不是临界区：报
        "fresh_value": [],                                       # 纯赋新值：不报
    }


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

# ——「逻辑唯一但库上没有约束」的表清单（只进不退）——
#
# 原判据只认"表上已有 DB 级唯一约束"：229 个写入点里只有 57 个落在这类表上（24.9%），
# 剩下四分之三**规则完全不看**。而 check-then-act 的成因恰恰是"业务上唯一、
# 库上没约束"——库上有约束的地方至少会撞 IntegrityError 报错，没约束的地方
# 是**静默写出两条**，更坏。本轮实测新发现的四处（号源批量、住院登记、结算认领、
# 退款）无一例外落在那 75% 里。
#
# 所以另立这份显式清单，把"逻辑唯一"的表也纳入判据。清单只许变长（覆盖变大），
# 不许变短；某张表补了 DB 唯一约束之后，它会自动落进 _tables_with_unique_constraint()，
# 那时才可以从这里删。
LOGICAL_UNIQUE_TABLES = {
    "appointment_slots": "同机构/医师/资源/日期/时段号源逻辑唯一（重复号源放号量翻倍）",
    "admissions": "同患者同时只能有一条在院记录（status='admitted' 上的在院唯一）",
    "settlements": "同一次结算认领只应成功一次（结算认领 check-then-act）",
    "progress_notes": "首次病程每次住院唯一（重复书写产生双份法定文书）",
}
# **`bill_details` 于 2026-09-01 从清单移出**（不是为了让规则变绿，是原判据不成立）：
# 当年写的是"同账单同收费项逻辑唯一，重复记账即多收费"，但账单明细本就带 `quantity`
# 列，且住院床位费、护理费这类**按天逐条记同一 item_code** 是正常业务——真加上唯一
# 约束，第二天的合法计费会被拒。真正该防的是"同一笔费用重复提交"，那要靠请求级幂等键
# （客户端提交号）而不是表级唯一约束，属另案。此处如实登记判据被推翻，而不是留着一条
# 永远修不掉的"欠账"让后来者去硬修。当前行为由 test_billing_duplicate_charges.py
# 的特征化用例钉住：同项目重复记账合法且金额累加。

# 加进 LOGICAL_UNIQUE_TABLES 后被规则抓出来的**存量** offender（只减不增）。
# 规则抓出存量不是删规则的理由（第 17 章：不许为了绿而放宽断言）——逐条登记，
# 新增一条会顶破基线。修复归属：本包只动脚本/测试/CI/文档，业务代码改动交由
# 后续包（见 docs/TECH_DEBT.md 的登记项）。
KNOWN_UNGUARDED_UNIQUE_WRITES: dict[str, str] = {
    # 2026-09-01 清零（P1-29）。五条各自的去向，如实记账：
    # - create_slot / create_progress_note / create_admission：三条不变式已由迁移
    #   b8e3d5f70a91 下沉为**部分唯一索引**，接口层改走 insert_or_conflict，
    #   抢输者拿到与顺序请求一致的 409（回归见 test_logical_unique_races.py）；
    # - create_settlement：**早已修好**（e5b7c9d1f3a4 的部分唯一索引 + UPDATE 认领），
    #   只是这份清单没跟上——陈账也是账，一并销掉；
    # - create_bill_detail：判据本身不成立（账单明细按天重复记账是正常业务），
    #   已连同 bill_details 一起从 LOGICAL_UNIQUE_TABLES 移出，理由见上。
}


# ——「已审计：多行合法」的表清单（P1-30，2026-09-03）——
#
# 下面覆盖面自证里的"未覆盖"从来不是"安全"，只是"这条规则看不到"。P1-30 把这些
# 只被 db.add、库上又没有唯一约束的表**逐张**审了一遍（每张：分类员按模型/写入点/
# 读方/测试/文档给判定 → 怀疑者复审 → 业务语义/代码路径/存量数据三个独立视角分别
# 试图推翻，一张表 148 个代理来回）。四种去向：
#   * 多行是设计本意，或唯一性已由别处守住（流水/台账/时序/单据/配置目录/父表约束/
#     条件 UPDATE）→ 登记在 AUDITED_MULTI_ROW_TABLES，附一句"为什么多行合法"；
#   * 业务上确实唯一 → 部分/全量唯一索引下沉到库、写入点改走 insert_or_conflict——
#     它们自动落进 _tables_with_unique_constraint()，**不**在此清单；
#   * 唯一性长在父表/状态行上、不是本表的键 → 父行条件 UPDATE，本表只在 UPDATE
#     命中时追加 → 登记在 GUARDED_BY_PARENT_UPDATE，附守在哪；
#   * 审过但定不了——键在表上表达不出来（缺身份列/编码列）或要产品决定 →
#     AUDITED_UNDECIDED_TABLES，只减不增。
#
# 这些清单的语义是"审过"，**不是豁免**：登记进来的表日后补了唯一约束，会自动被规则
# 接管，此时要把它从这里删掉（test_已审计清单不得腐烂 会提醒）。理由必须写——
# 一旦可以不写，这里就会变成第二个绕过检查的后门。理由里的行号是审计当日的，
# 定位用 grep 函数名而不是行号。
AUDITED_MULTI_ROW_TABLES: dict[str, str] = {
    "admin_projects": "项目无业务自然键（没有项目编号，名称不唯一），路由不查重、顺序请求同样允许同机构同名项目，测试就在同机构循环建 5 个同名'批量项目'当夹具——两行同名不是缺陷而是被现有用例当成合法。重复提交属请求级幂等问题，不是表级唯一",
    "adverse_events": "不良事件上报是事件登记：一条记录=一次事件，没有患者/就诊等主体外键，description 自由文本且可匿名，业务上不存在可判重的自然键；统计口径（quality.py:206-217）就是按条数与闭环率计数，多行是设计本意",
    "aefi_reports": "AEFI 是按不良事件逐条上报的流水：同一剂次可先后出现不同反应（局部红肿后又出现严重反应）而分别上报，record_id 可空、仅凭 vaccine_code 上报的记录更无从取键；模型注释里“关联到剂次”是为归因，不是为唯一",
    "archive_authorizations": "scope 是单值列，患者要授权多个范围或续期只能再发一条，同患者同机构多条 active 授权是设计本意：契约测试 test_patients_contract.py:137-148 对同一患者、同一乡镇院连发 scope=encounter 与 scope=all 两条并断言清单 2 行，check 接口用 any() 合并多条有效授…",
    "attachments": "通用附件按 owner_type+owner_id 挂接，一个业务对象挂多份附件是设计本意（契约测试同一不良事件传 png+pdf 两条、列举返回 2 行）；sha256 去重只发生在磁盘（内容寻址、幂等），DB 行是元数据，同一文件重传两条元数据行不是需要仲裁的缺陷",
    "bill_details": "已被推翻的先例：明细带 quantity，且床位费/护理费按天逐条记同一 item_code 是正常业务，(admission_id,item_code) 唯一会拒掉第二天的合法计费；test_billing_duplicate_charges 已把\"重复记账合法且金额累加\"钉成特征化网",
    "checkup_items": "分项只在 create_checkup 里随本事务刚 flush 出来的 PhysicalExam 一起写（checkup_id 取自新生成主键），仓库里没有第二个写入者（grep `CheckupItem(` 仅此一处，无“向既有体检追加分项”的端点），跨请求不可能共享同一 checkup_id——属单写者形态",
    "child_visits": "儿童访视是流水：checkup 按满月/3/6/8/12/18/24/30月/3-6岁反复发生，newborn 访视对高危新生儿按规范也要增加次数，同儿童同类型多行是设计本意；visit_date 默认空串，(child_id, visit_type, visit_date) 连做键都不成立",
    "cold_chain_records": "[业务视角复核] (org_id, device_name, recorded_at) 不是这张表的自然键，同键两行有合法并存场景，且模型自身的设计就预留了这种并存： 1. 允许区间是按行存的，不是按设备存的（app/models/publichealth.py:410-412，作者注释\"该设备的允许区间（不同疫苗要求不同，故记在记录上而不是写死常量）\"）",
    "consent_records": "[业务视角复核] consent_records 的一行不是\"开关状态\"，而是一次同意行为的举证记录：models/consent.py:9-10、62-65 明写\"谁、在什么场景、对哪版文本、经什么方式表示了同意；撤回置 revoked_at 不删——撤回本身也要可举证\"",
    "consent_templates": "模板是无自然键的配置目录：consent_type 只有 6 个粗粒度值，同一类型下多份模板（surgery 下几十种手术告知书、exam 下增强CT/胃镜）与多版本并存是设计本意；签署引用一律走 template_id 并把正文拷贝冻结，版本升级走 PATCH 原地改 body/version 而非新建行，种子也只按'列表为空'幂等建一…",
    "consultations": "远程会诊申请是按次生成的业务单据，同一患者同一对机构可多次申请（tests/test_stage95_batch2.py 对同一患者同一机构对连开两单都成功，第二单还被拿去测“未完成不可计费”），每单各自走 applied→accepted→completed 状态机并独立计费、评分",
    "correction_requests": "更正/注销申请是独立的审批流水：同一患者可先后提交不同字段、不同时间的更正或注销，每条按 request_id 单独审核，pending→approved/rejected 由 review_correction 的 status!=pending 检查守住重复审核",
    "course_materials": "课程下的课件条目，同一课程挂多条是设计本意（契约测试同课程连挂两条并断言 total_materials==2）；无业务码列，title 为自由文本，同名重复至多是内容冗余，维护者删改即可，不需要事后仲裁",
    "courses": "课程目录没有课程编码列，只有 title/course_type/category/speaker 自由文本；同名课程按期重开、同题分直播/点播各一条都是合法的，加不了唯一键。两条同名课程会让 training_records（按 course_id+user_id 唯一）分散在两个 id 下，但这是「缺课程编码」的功能项，不是并发写入缺…",
    "critical_actions": "危急值闭环留痕表（'通知→确认→处置反馈全程记录'），一份报告按流程至少三行，报告修订还会再追加'复位'/'解除'行，测试直接断言 len(actions)==3 与含'复位'/'解除'的多行。五个写入点都是随状态迁移追加一行",
    "cssd_cost_items": "成本项是灭菌批次下的成本流水，cost_stats 按 (batch_id, cost_type) 做 sum 聚合，同一批次同一成本类型分多笔登记（两班人工、两批耗材、追加能耗）合法且金额累加——与 bill_details 反例同构，加 (batch_id, cost_type) 唯一会拒掉合法的分笔登记",
    "cssd_requests": "物品申领单按次生成，同机构同物品反复申领是常态（每次一张单、各带 quantity），每单独立走 requested→fulfilled，响应侧 fulfill_cssd_request 有 status!=requested 的 409 检查防重复发放",
    "deposits": "住院押金流水，模型注释明写\"只增不改的台账\"，余额由流水现算：一次住院分多次预交、多次退费、结算冲抵各自一行都合法（test_billing_deposits 断言\"流水只增不改：两笔都在\"）。db.add 写入点是 prepay，纯追加无自然键",
    "disease_path_records": "路径节点执行记录是“某次入组走到哪一步、谁做的、结果如何”的执行日志，模型与端点都没有“每节点只记一次”的声明；完成度用 set(node_key) 去重（_completion 与 program_stats 都是），同一节点重复执行（疗效复评做两次、补做）多条并存对完成度无影响，records 列表按 id 如实列出",
    "dispense_items": "发药明细「按批次一行一扣」，一张发药单下多行（多批次、同一药跨批）是 FEFO 设计本意，表本身没有自然键；真正要防的「同一处方发两次」落在父表 dispense_records.prescription_id 唯一约束上，且每一行明细只在 _claim_batch 条件 UPDATE 抢到批次余量之后才 add",
    "drug_shortages": "缺药登记是按次需求单：同机构同药品多次报缺、同患者多次登记都是正常业务（契约测试对同 org+drug_code 连登 s2/s3 两条均 201 并计入统计；黑名单口径'两次登记未取药'也预设一人多条登记），supply-risk 按未结案登记条数计风险本就是多行语义，表带 quantity 列——与 bill_details 反例同类…",
    "duty_rosters": "[业务视角复核] duty_rosters 不应登记为\"逻辑唯一\"，提议键 (center_type, duty_date, shift, doctor_name) 下存在两行合法并存的业务场景，加唯一索引会拒掉合法排班",
    "elderly_assessments": "老年评估按次追加，所有读侧都取「每人最新一次」（失能清单/预警/统计三处都是 latest[patient_id]=a），同一人同一 assessed_date 评三次被测试明确当成合法（assessment_records==3、assessed_people==1）",
    "emergency_cases": "呼救事件流水，每次 120 调度生成一条；patient_id 可空、无事件号/呼救单号列，同一地点/同一患者多次呼救合法（测试同地点连建两条、三条）。两个调度员对同一起事故各建一单是现实里的重复，但表上没有任何列能表达「同一起事故」，加不了键，也不是并发特有的问题",
    "emergency_vitals": "院前生命体征是车载终端持续回传的时间序列，一次急救事件天然有多条（模型注释'实时回传'，路由 docstring'院内可实时调阅'），没有任何业务列能构成自然键；同文件的 EmergencyMilestone 有意加了 UniqueConstraint 而 EmergencyVital 没加，是作者的明确取舍",
    "employee_changes": "人员变动留痕表（入职/转正/调动/离职），同一员工多条变动是设计本意，每行对应一次对 employees 的状态联动；并发两笔调动会让 employees.org_id 最后写者胜出，那是 employees 的丢失更新，变动日志如实记两笔反而是对的",
    "employees": "人员主数据表但 schema 里没有工号/证件号等身份列，(org_id, name) 同名同姓合法，系统无法从列判断两行是否同一人，唯一约束无从表达；'同一人建两档'只能靠业务流程/后续加工号列解决，不属并发闸门范畴",
    "encounters": "就诊记录一次就诊一条，同患者同机构同日可多次就诊（不同科室/复诊），表上没有就诊流水号或外部单号列，不存在可加约束的自然键；create_encounter 不查重是设计本意。住院入院那条 Encounter 与 Admission 同事务提交，Admission 走 insert_or_conflict 命中部分唯一索引时整体回滚，等于…",
    "esb_flow_runs": "每次按流程消费一条消息落一行执行记录（模型 docstring'逐步结果落 step_results 便于回溯'），同一 flow+message 重跑是重试机制的一部分：test_esb.py 明确对同一消息重跑同一流程并断言 failed 运行 ≥2 条",
    "esb_messages": "消息队列表：外部接入方每次 POST 入队即一条新消息，MessageIn 只有 msg_type/payload/max_retries，没有客户端消息号或去重键，表上也没有任何可判'同一条'的业务列",
    "exam_requests": "检查申请单按次开单，同患者同项目多张是设计本意：互认流程（accept_recognition_of）就要求为同一患者同一 item_code 再开一张新单并指向源单，契约测试也为同患者同项目一次塞四张不同状态的单",
    "exam_resources": "[业务视角复核] 核验对象：`ExamResource`（/home/user/trytry/server/app/models/clinical.py:279-293）与唯一写入点 `create_exam_resource`（/home/user/trytry/server/app/routers/exams.py:814-823）",
    "exchange_logs": "交换日志，模型 docstring 即'每次入站转换落一条日志'，是监控与失败率统计的流水；三处写入点都是每次交换/每个 persist 步落一行，integration.py 甚至用独立 SessionLocal 写入以保证业务失败也留痕——典型审计流水，多行是全部价值所在",
    "fd_contract_services": "履约记录是签约协议下的服务流水（上门/咨询/随访/转诊按次记），绩效统计按时间窗 count 条数（performance.py），同一协议同一 service_type 多条正是设计本意。唯一性在上一层 fd_contracts 已由 uq_contract_patient_org + insert_or_conflict 守住，服务记…",
    "finance_entries": "收支流水台账（accounting.py 自述 FinanceEntry 是'期间 + 收/支 + 金额的流水台账'），汇总端点按 org+period+category SUM(amount)，同期间同类目多笔（item 可为空串）正是 bill_details 那种'累加合法'形状——加唯一约束会拒掉第二笔合法记账",
    "followups": "慢病随访是按次追加的随访序列（每条随访推进 chronic.next_due 到下一次），同一 chronic_id 多条是设计本意——tests/test_performance_orgs_contract.py:425-426 注释“同一人随访 3 次”、test_dataquality.py:184-185 同档案两条",
    "fund_prepayments": "[业务视角复核] (pool_id, batch_no) 不是 fund_prepayments 的自然键：这张表记录的是\"每一笔真实到账\"，batch_no 只是挂在到账上的分组标签，同一批次下多笔到账是设计本意。依据： 1. 设计文档对这张表的定义是「预付：按比例预付，记批次与到账」（docs/通用平台能力补全开发计划.md:75）——行的粒度是\"到账…",
    "health_articles": "健康宣教稿件，无编号列，title 自由文本、status 只分草稿/已发布；同题多稿（改版、换分类）合法，重复稿由编制人员删改处理，不需要仲裁。写入点不查重也无可查之键",
    "health_monitor_records": "监测记录是测量流水（营养/环境/职业/放射/学校），同机构同指标同日多次采样本就合法，record_date 缺省为空串（大量行连日期键都没有），exceeded 由每行自算；读路径只做筛选与倒序列表，不聚合、不去重，没有任何地方假设 (domain, org_id, indicator, record_date) 唯一",
    "home_visit_orders": "上门服务是按次申请的工单（申请→派单→完成），同一患者/机构/服务类型多张工单是常态，expect_date 可空且同日多次上门也合法，找不到可用的自然键；契约测试里同一患者同一机构建了两张不同类型工单并钉为合法",
    "improvement_tasks": "整改任务是自由文本的问题项，同机构同指标可以同时下达多条不同问题的任务，没有可用的自然键；测试也为同一机构批量种入 owner/due_date 完全相同的多条任务",
    "infection_reports": "院感病例登记：同一患者可多次、多部位发生感染，测试固定装置就为同一患者连报两条（不同部位）并各自核实；\"重复上报\"由业务里的核实环节（confirmed/excluded）仲裁，统计只数 confirmed（quality.py:435-448）",
    "infectious_cases": "传染病病例报告是逐例流水，表里根本没有患者身份列（只有 org/disease/onset_date），同机构同病种同发病日多例是正常疫情（预警就是按例数聚合），无法也不该构造自然键；重复上报同一例属请求级幂等问题",
    "informed_consents": "知情告知书是逐份签署的证据记录：同一就诊可合法并存多份同类型待签告知书（如两项不同检查各一份 exam），且 related_id 默认 0 的“未关联”写法是常规用法（test_outpatient_docs.py:74 就这么写），任何含 related_id 的自然键都会在 0 上塌缩",
    "insurance_settlements": "医保结算记录本身按次产生：同一患者在同一机构多次就诊各结算一次、门诊一次就诊允许多张结算单（finance.py:108-109）→ 多条 InsuranceSettlement 合法，且表上没有医保侧结算流水号可判\"同一笔\"（test_guideline 同患者两条并汇总进 fund-stats）",
    "knowledge_entries": "知识条目是内容库，同分类同标题多行是\"历史版本\"的正常形态：旧版靠 active=False/expire_date 停用，新版重发同名条目（测试里就有标题为「旧版病历书写制度」的条目）。title 索引本就非唯一，也没有机构维度",
    "live_sessions": "直播申请单，按次提交、走 pending→approved/rejected/finished 审核流；同人同题再次申请是正常的重提，「主题重复」由审核人驳回处理（契约测试即以 comment=主题重复 驳回第二条），不是数据层要拦的重复",
    "login_logs": "等保登录留痕，成功/失败/锁定每次尝试各落一行且立即 commit，同一 username 多行正是爆破画像所需（测试里同一账号连错 5 次 + 锁定命中全部入库）；纯审计流水，无自然键",
    "material_purchases": "非药品物资采购申请是按批生成的单据：同机构同品名多次申请（分批补货）是常态，表上没有申请单号/合同号唯一列，contract_no 默认空串且在后续 contract 步骤才填。并发双提只是两张待审申请，审批人驳回其一即可，无需事后仲裁",
    "maternal_visits": "产检/产后访视是按次流水，同一册子多次 prenatal 访视是设计本意（契约测试对同一册子连做 v1/v2 两次产检、再一次产后访视，均 201）；表上无任何自然键列，写入点不查重是正确的。与行数相关的唯一不变式——'高血压只标一次高危'——已由 _mark_high_risk 的条件 UPDATE 守住，不落在本表上",
    "newborn_screenings": "新筛按项目（metabolic/hearing/chd）逐次记录，而听力/代谢筛查本就有初筛→复筛→召回复查多次结果（初筛 abnormal、复筛 normal 是要同时留下的两行），列表端点语义就是'筛查史'，表上无轮次列——同一 (child_id, item) 多行是合法史料而非缺陷",
    "nursing_records": "护理记录是巡视/病情观察/医嘱执行的流水，同一住院或就诊多条是本意；文书完整性也只按 count 统计（clinical_docs.py:230、outpatient_docs.py:529、inpatient.py:604），tests/test_nursing_order_link.py:128-135 对同一住院同一医嘱循环多次写记…",
    "official_docs": "公文/通知发布表，无文号列，title 是自由文本，同标题同类型反复发（年度例行通知）合法；draft→published 是单行状态跃迁，与插入唯一性无关",
    "online_consults": "在线咨询是按次生成的工单（open→replied→closed），同一患者在同一机构同时有多条 open 咨询是正常业务（问了两个问题/一条咨询一条续方），列里没有任何可充当自然键的组合；写入点也不查重、不需要查重",
    "order_executions": "医嘱执行记录是\"逐次执行登记\"（模型 docstring 原话），长期医嘱 bid/tid 每次执行各记一行，同一医嘱多行是设计本意；测试也把同一医嘱登记两次并断言 len==2 当合法。唯一的写入点只校验医嘱存在且 active，不查重也不需要查重",
    "outbound_visits": "县外就诊登记是按次记录：同一患者多次外出就诊、同日同院门诊两次都合法（test_analytics 同患者两条即 outside_visits=2），表上没有医保结算号之类的外部键可判\"同一笔\"；双击重复登记属请求级幂等问题，与 bill_details 同形，不是表级自然键",
    "pathogen_monitors": "病原监测是按次上报的送检计数流水：同机构同病原同日可多批送检（标本类型还是自由文本），而且该资源只有 POST 与 GET、没有 PATCH，同日重报事实上就是修正路径；预警 multi_point_alerts 按行独立判定“送检≥10 且阳性率≥10%”而不跨行求和，重复行不会产生需要事后仲裁的汇总失真",
    "payment_orders": "模型注释明写\"一次结算可分多笔渠道支付\"，同一 settlement 多张单（现金+医保拆付、失败后重付、部分退款后换渠道）都是设计本意（test_payment_reconciliation 断言 2 张、失败后重试 paid），trade_no 由通道事后回填且默认空串，不能当键——所以没有\"两行同键\"的自然键",
    "ph_event_actions": "处置动作留痕（docstring 明写'处置动作留痕：应急值守、流调、资源调度等指挥记录'），同事件多条动作是设计本意，契约测试对同一事件连记两条并按 id 正序回读。唯一的前置检查是事件是否 active，那是状态门禁不是唯一性——与 close_event 之间的竞态（结案瞬间追加一条动作）属 ph_events 状态机问题，不是本表…",
    "ph_events": "突发公卫事件无业务自然键（无事件编号，title/disease_name 可重复——同一病种在不同学校可同时立多起 active 事件），路由不查重、顺序请求同样允许同名事件，诊间提醒只数 active 事件总数不关心重复",
    "physical_exams": "体检记录是患者的体检史，同一患者多次体检天然多行；tests/test_error_branches.py:274-290、test_final_gap1.py:216-247、test_checkups_characterization.py:43-53 都对同一患者连建多条并断言 ≥2 / ==2，360 档案（encounters.…",
    "prenatal_screenings": "同一册子多项筛查（唐筛/无创/超声/产前诊断）与同一类型不同孕周复查（早孕 NT 与中孕结构超声都是 ultrasound）都是常规流程；代码注释明说'同一本册子的几项筛查常常同一天出结果、同时录入'，测试断言同一 record 两条并入统计",
    "prescription_items": "明细只在 create_prescription 同一事务内随刚 flush 出来的处方头一起写入，不存在第二个写入者能对同一 prescription_id 并发追加；且同方重复 drug_code 被明确定义为合法输入（转药师审并写入 review_comment，不是拒绝），两条同 (prescription_id, drug_co…",
    "prescriptions": "处方是按次开具的单据，同患者同机构同日多张处方（不同就诊、退回后重开、分方）都是正常业务，表上没有任何业务自然键，顺序请求同样不查重；医师双击重复提交属请求级幂等键问题（与 bill_details 被推翻的判据同类），不是表级唯一",
    "project_milestones": "里程碑是项目下的多条节点，同名多条（如分期验收）没有业务禁令，路由不查重、顺序请求同样可重复，测试给每个项目建同名'节点'；重复行只影响展示计数，不驱动任何状态机（done/reopen 都按 milestone_id 单行操作）",
    "purchase_orders": "采购单是按次生成的申请单据，同机构同品种反复采购本就是多行；唯一需要防重的是验收入库那一步，已由 receive 的条件 UPDATE 状态闸门守住，与插入无关",
    "qc_measurements": "室内质控测定值是同一批号下的时间序列，Westgard 2-2s/R-4s 规则本身就依赖同批号连续多点（写入时读取上一条 prev），同分钟重复测定也是正常复测；测试每个批号连录多值。多行是本意，无自然键",
    "qc_records": "共享中心质控记录，同中心同项目同日可多次质控（失控→重新定标→复测，既有用例的 note 就是'失控，重新定标'），按次记录是本意；无样本/申请单级的唯一主体可挂",
    "record_qcs": "病历抽检评分：同一份病历可被不同质控员、不同轮次多次抽检，现有测试明确对同一 encounter 连打两次（95 分与 72 分）并断言 total==2、平均分按两条算——多行是既定合法行为，加唯一约束会把这些用例打红",
    "reconciliation_diffs": "对账差异明细：一批对账下每条差异（本地有通道无/通道有本地无/金额不一致）各一行，多行是表的全部意义；`for d in diffs: db.add(d)` 这形状在 2026-09-03 之前扫描器解析不了，是唯一的“未识别”写入点，本表此前从未进过审计分母。批次级唯一性归父表 reconciliation_batches",
    "referrals": "平台侧转诊单是按次开具的单据：同患者可先后多次上转/下转，代码与文档里都没有\"同患者同时只允许一条在途转诊\"的规则（spd 那套更完整的转诊实现也不查在途重复），居民端把它当时间线全量展示；测试固定装置更直接为同一患者/同方向/同两机构造出两条 pending 单并断言排序",
    "report_revisions": "报告修订历史表（M-6），每次 PATCH 报告都追加一行修订前值，同一 report_id 多行是设计本意——审计流水；test_stage4_m1 明确断言两次修订后 revisions 长度为 2",
    "role_change_logs": "角色变更留痕，同一用户多条是变更历史的设计本意（浙#43），查询端按 id 倒序列全部。并发下两个管理员同时改同一用户会写出两条内容相同的日志，但那是 users.role 上的 read-then-write 丢失更新（users.py:540 先比对再赋值），日志只是如实记录了两次请求，不构成要仲裁的重复实体",
    "satisfaction_surveys": "满意度评价是按次提交的评分流水，现有测试 test_extras.py:48-51 对同一 target_type/target_id/patient 连提两次并断言均分 4.5，多行被明确当成合法",
    "secondments": "[业务视角复核] 断言把 secondments 当成\"全职派驻台账\"，但这张表按设计装的不只是派驻。app/models/hr.py:91-93 与迁移 c4d5e6f7a8b9 的注释都明写 assignment_type 有 long_term=长期派驻 / support=短期支援 / rounds=巡诊 / other=其他，且\"巡诊与短期支援不…",
    "shift_handovers": "[业务视角复核] (ward_id, handover_date, shift) 下两行合法并存的场景不止一个，且代码本身处处按\"追加型记录\"设计，没有任何\"每班一条\"的前提，判 append_only 而非逻辑唯一",
    "simulation_attempts": "模型 docstring 明写“允许重复作答并全部留痕，取最高分参与考核”，测试 test_重做取最高分但全部留痕 断言同人同病例两条作答记录都保留、attempt_no 还作为教学反馈输出；同 (case_id, user_id) 多行是设计本意",
    "simulation_cases": "教学内容条目（标题 + 情境 + 决策点 JSON），没有业务自然键，标题重复只是内容重复而非需要仲裁的数据冲突；写入点只校验决策点 key 唯一与答案在选项内，不查重也无需查重",
    "sms_codes": "验证码是按次下发的一次性凭证：60 秒冷却过后同号同用途再发一条是设计本意，校验只认 id 最大且未消费未过期的那条（旧码自然作废，由 sms_code_cleanup 定时清掉），测试里同号连发 5 条视为合法",
    "spd_assessments": "评估记录是历史序列：同一患者同一量表反复评估以观察风险变化是功能本意（模型注释\"量表版本随记录固化，量表改版不回改历史评估结论\"，居民端与档案页都按 id 倒序列历史，趋势统计按时间段数评估人次）。没有可判重的自然键",
    "spd_case_reports": "异常上报明细按事件生成：同一患者可因不同触发规则、不同时间多次被上报，每条上报同时派生一条处置任务并计一次积分；唯一写入点是人工 POST，没有自动规则引擎批量生成，因此不存在\"同规则同患者同日\"这种可被并发写重的机器键",
    "spd_consult_messages": "在线咨询的聊天消息流水：同一会话、同一发送方连发多条是设计本意（列表端点按 consult 计数、按 id 顺序回放 500 条）。start_consult 里的 check-then-act 守的是 spd_consults 的\"同病种只开一条会话\"，与消息表无关",
    "spd_edu_pushes": "宣教推送记录按次生成：同一素材对同一患者可先立即推一次再定时推一次，表带 frequency 列，成效统计 push_times 就是按行数累计（契约测试里同素材同患者两次推送、push_times=3 被钉死）",
    "spd_followup_records": "[业务视角复核] (patient_id, rule_id, planned_at) 不是 spd_followup_records 的自然键——同键两行合法并存的场景不止一个，且都不是\"边角案例\"，而是接口现有契约与医共体核心流程直接产生的： 1) 同患者同方案合法重排（再入院 / 下转再出院）",
    "spd_groups": "患者分组是配置实体：name 自由文本、无唯一约束、代码里没有按 name 查找分组的地方（grep `SpdGroup.name ==` 为空），成员关系已由 spd_group_members 的 (group_id, patient_id) 唯一约束守住",
    "spd_health_prescriptions": "健康处方是医生每次开具一份的历史记录，同患者同病种随时间多份是常态（列表按 patient_id 分页、id 倒序），没有任何列能构成\"同时只能一份\"的键。写入点无查重也无需查重，双击重复提交属请求级幂等另案",
    "spd_measurements": "体征测量天然多行：同患者同指标反复测、设备一分钟回传多次都合法，复合索引 (patient_id, metric, measured_at) 本身就是非唯一的时序索引，测试里同患者同指标两条→趋势 total>=2 被钉死",
    "spd_package_usages": "服务项目扣减台账（docstring“消费台账”），同一绑定同一 item_code 按次多行是设计本意，带 qty/used_at，测试也对同一项目连扣两次并期望都 201。附注：这里真正的并发风险是 binding.items 的 JSON 读改写（deepcopy 后整体覆写、无 serialized_on）可能多扣越额，属丢失更新…",
    "spd_recalls": "[业务视角复核] spd_recalls 一行 = 一次召回\"尝试\"（reason + contacts 联系过程 + result 结果），属于\"多次尝试\"形状；而\"这个档案当前是否召回中\"的权威状态并不在这张表，而在 spd_enrollments.status=='recalled'（workbench.py:1070/1188 的\"召回中\"计数、u…",
    "spd_redeems": "兑换单按次生成：同一账户可多次兑换同一商品，每次都是一张新的待核销单，可兑换的次数由库存与余额的原子 take_amount 条件扣减限定（第二次库存不足返回 409，测试已钉住）。verify_code 是 6 位随机码、非唯一，核销按 (verify_code, status='pending') 取 first()——这是随机碰撞的…",
    "spd_referral_cases": "转诊单是按次生成的单据：同一患者同一病种可多次转诊（多次发作、上转与下转、退回后再发起），代码里没有任何“同时只允许一张在途单”的规则（_create_case 不查、check_referral_rules 把是否开单留给医生），测试也自由给同一患者种多条单",
    "spd_report_instances": "报告实例是'按次生成的快照'：手工 generate_report 不查重、period_label 可由请求体任意给，同模板同期间重新生成是正常操作，且 docstring 明说报告只是'同一批数字的另一种排版'，两份并存无需仲裁",
    "spd_report_tasks": "报告推送任务是配置实体：同一模板可配多条任务（不同订阅人/频率/机构集/有效期），name 是自由文本，模型与路由都没有任何自然键或查重，测试通过 API 为不同模板各建一条也从未断言唯一。重复配置最多导致重复推送，属配置卫生问题而非需仲裁的数据缺陷",
    "spd_revisits": "[业务视角复核] spd_revisits 是\"复诊计划\"表，一行就是一次排期；(patient_id, program_code) 在 source='high_risk' AND status='planned' 范围内并不是业务不变式，只是 _auto_intervene 自动路径的一个去重便利，业务上存在多种两行合法并存的场景： 1. 手工路径明确…",
    "spd_screenings": "筛查记录是按次留痕：同一患者同一病种可反复被机会性筛查、主动筛查、居民自查、规则导入（source 四种），每条都是独立判定，多行合法。去重发生在下游目标池 spd_candidates（唯一约束 uq_spd_candidate_patient_program + _upsert_candidate），就诊事件订阅者也是先查候选池再写筛…",
    "spd_sync_logs": "数据源同步日志：每跑一次（定时 run_source 或人工回填 record_sync）追加一行，成功率按最近 100 行统计，测试里对同一 source 连发两条日志且两条都要在、按新到旧排列。同一 source 多行是表的全部意义，没有自然键",
    "spd_teams": "配置/主数据实体：name 是自由文本、无 UniqueConstraint、无任何按 name 查找的代码（grep `SpdTeam.name ==` 为空），成员/任务/纳管都外键到 team.id",
    "stock_takes": "盘点是每次一行的账实差异流水，同机构同药品反复盘点是常态（契约测试对 PHCT-MET 连盘两次并断言 2 行）；并发下真正要防的是重复调账，已由写入前对 DrugStock.quantity == book_qty 的条件 UPDATE 守住，抢输者 409 不会落盘点行",
    "stock_transfers": "调拨流水带 quantity，同药品同两机构之间多次调拨是正常业务；行只在批次原子占用与调出侧条件扣减都成功后才写入，并发争抢的是库存而不是这张流水表",
    "surgery_requests": "同一次住院可以有多台手术申请（分期手术、计划内二次探查），模型注释专门为此说明 unplanned_return 必须由医师显式标记而不能靠“同住院第二台”推断；测试也把同一 admission 两条申请当合法数据 seed",
    "tcm_dispense_orders": "每次代煎下单都是新的一张单（herbs/doses/decoct 由本次处方决定），同患者同机构多次下单、甚至同方复购都是正常业务；表上没有业务自然键，双击产生的两单与合法复购不可区分，只能靠请求级幂等键处理，不是唯一约束场景",
    "tcm_master_cases": "名老中医医案是内容条目，同一老师同病同证可有多则验案，title/visit_date 都是自由文本，无业务自然键；重复提交只是内容重复，stats 里 total 多计 1 不构成要事后仲裁的冲突，发布/撤回按 id 单条操作",
    "training_plans": "实训计划是按期发布的班次单，同机构同技术同日开两班（不同 trainer/title/上下午）合法，表上没有能表达「同一班次」的键；名额并发已在报名侧由 enrolled_count 原子占额守住，计划本身多一条不产生需要仲裁的数据缺陷，多余的计划由发布方 status=closed 关掉即可",
    "transfusion_requests": "用血申请是按次提交的申请单，同一患者一次住院里多次申请（术中备血→追加用血）是正常业务；tests/test_final_gap4.py:64-99 对同一患者/机构/血型/成分连发两张申请并各自走审批、发血，被当成合法流程",
    "treatment_records": "门急诊处置记录一次处置一行，同一就诊多次雾化/换药本就是多行；完整性接口按 encounter 计数处置条数，没有任何“每就诊一条”的语义",
    "vaccination_records": "[业务视角复核] 这张表的行粒度是「一针/一支」，不是「接种程序里的一个剂次号」，所以「同患者、同疫苗、同剂次号、同日期两行」在业务上可以是两针合法接种，不是必然缺陷。依据：(1) app/routers/vaccination.py:103-109 每写一行恰好对批次 claim_quota 扣 1 支库存，行与\"支\"一一对应",
    "vaccine_contraindications": "同一患者同一疫苗可以同时存在多条生效禁忌（暂时+长期各一条），契约测试就是这么建的且两条都 201、都 blocking；解除走留痕不删行，历史条目继续保留。判定函数返回的是列表并取 forbidden[0]，本身就按多条设计，(patient_id, vaccine_code[, status='active']) 不是唯一键",
    "vital_sign_records": "体温单是体征时序流水；measured_at 是分钟级自由字符串，同一时刻可有互补的部分记录（模型注释“一次测量未必测全”、各项可空）或异常后复测，列表接口显式按 (measured_at, id) 排序即预期同一 measured_at 多行",
    "waste_locations": "[业务视角复核] (org_id, location_type, name) WHERE active 不是 waste_locations 的自然键，name 只是一个自由文本标签，不是点位身份；两行同键并存不构成\"要人事后仲裁的数据缺陷\"，判据证据不足，不宜登记为逻辑唯一",
    "women_health_records": "婚前/孕前/妇女保健/避孕节育是按次服务流水，同一人同一 record_type 可反复发生（年度妇检、多次避孕节育服务、再婚再做婚检），表上无任何自然键列，写入点不查重是正确的；测试只是每种类型各建一条并按类型过滤，未把同类型两行当非法",
    "workflow_instances": "[业务视角复核] (definition_key, business_type, business_id) 在 status='running' 上并不构成\"两行即缺陷\"的自然键，理由有三： 1. 锚点不是\"单据\"而是\"任何业务对象\"",
}

AUDITED_UNDECIDED_TABLES: dict[str, str] = {
    "child_records": "两难：它是儿童保健\"档案\"，同一儿童被建两份档案确实是缺陷（访视/新筛/高危标记会分裂在两行上，要人合并），但表上没有任何身份列（无 id_card/ehc_no，儿童不是 patients），guardian_patient_id 可空，(name, birth_date, guardian_patient_id) 既约束不住 guardian 为 NULL 的行、也拦不住重名/同日双胞胎等合法…",
    "emergency_resources": "两难：一方面它是“一物一行、PATCH 调数量”的台账（update_resource 只改 quantity/min_quantity/expire_date 等），暗示 (org_id, resource_type, name) 应当唯一，双击会多出一行并让 readiness 的 total/by_type 多计 1、below_min 列表出现重名",
    "followup_tasks": "派生路径 create_task 确实是 (category, source_id, status='pending') 上的 check-then-act，作者 docstring 也把重复派生视为缺陷；但人工补建路径 create_followup 自由写任意 (category, source_id)，绝大多数人工任务 source_id=0，且同一来源多时点随访（如术后第 7 天/第 30…",
    "report_templates": "共享中心报告模板是配置表，候选键只有 (center_type, name)，但代码/测试/文档没有任何地方把同名模板当缺陷（无 code 列、无查重、无删改端点、无种子），重复只是列表里多一条可选项而非要仲裁的数据；对比 spd_report_templates 是靠 code 列唯一",
    "spd_interventions": "两难：自动路径 _auto_intervene 明确按 (enrollment_id, template_id, status∈planned/doing) 先查再插（check-then-act），两次评估并发会写出两条一模一样的\"高危自动干预\"",
    "spd_lifecycle_events": "不变式成立但**故意不建索引**（P1-30 复核，2026-09-04）：\"一份档案同时只有一次待目标机构确认的跨机构迁出\"是真的，可这张表没有\"撤回待确认迁出\"的通道——只有 confirm，没有拒绝/撤回。今天迁错机构还能再发一条指向正确机构的迁出（多出的那条不确认即无副作用），加了 (enrollment_id) WHERE event='migrate' AND NOT confirmed 就变成\"发不出第二条、也撤不掉第一条\"，把一条良性多余行换成一份卡死的档案。补上撤回通道再建索引",
}

# 唯一性不在本表、而在**父表/状态行**上的表：本表是流水/日志/子行，"不得重复"的真正含义是
# "父行的那次状态跃迁只能成功一次"。守法是父行条件 UPDATE（`UPDATE … WHERE status = 期望态`，
# rowcount 为 0 即 409），本表只在 UPDATE 命中后追加——判定与写入在同一条 SQL 里，
# 与 add_amount / claim_quota 同理。值写"守在哪个函数、哪条 UPDATE"。
GUARDED_BY_PARENT_UPDATE: dict[str, str] = {
    "charge_price_changes": "调价历史一次跃迁一行：唯一性长在收费项的**价格本身**上（10→12→10 的往返调价里 old_price 会合法重复，静态部分唯一索引表达不了），由 `billing._change_price` 的 `UPDATE charge_items SET price=新价 WHERE id=:id AND price=:旧价` 守住——rowcount 0 即抢输，拿 409，历史行只在命中后追加",
    "asset_movements": "采购验收那条入库流水一张单只该有一条：表上没有任何列能表明\"这是采购单 X 的验收\"（手工出入库同表、note 自由文本），闸门在父单 `materials._mark_received` 的 `UPDATE material_purchases … WHERE status='contracted'`——加库存与写流水都只在 rowcount 命中后发生（姊妹路径 pharmacy.receive_purchase 同形）",
    "spd_referral_steps": "转诊轨迹每格只走一次：不变式长在转诊单 `spd_referral_cases` 的状态跃迁上，由 `referral._advance_case` 的 `UPDATE … WHERE id=:id AND status=期望态` 守住；轨迹行与派生的任务/积分同事务，抢输者一次 rollback 全退",
    "spd_tasks": "随访办结派出的\"异常处置\"任务只该派一次：表上没有指向随访记录的列（现有列组不成键），闸门在 `spd/service.close_followup_record` 的 `UPDATE spd_followup_records SET status=终态 WHERE id=:id AND status IN 允许态`——医护端与居民端两个入口共用它，任务只在命中后派",
    "workflow_transitions": "一个流程实例离开某个节点只该有一条流转行：唯一性长在实例行上，由 `workflows._move_instance` 的 `UPDATE workflow_instances … WHERE status='running' AND current_node=读到的节点` 守住。**刻意不建 (instance_id, from_node) 唯一索引**——节点定义不拒环（a→b→a 合法），环形流程的第二圈会撞索引，单子从此既推不动也终止不了；条件 UPDATE 认的是\"当前位置\"而非历史，对环同样成立",
    "spd_point_records": "积分流水多行是台账本义（同一账户反复签到/兑换）。带业务事件的入账走 `service.award_points` 的 (rule_code, ref_type, ref_id) 幂等判定；assess.py 的两处不带 ref_id——签到\"一天一笔\"由 spd_signins 的唯一约束守、兑换扣分由 take_amount 的条件 UPDATE 守，都在事件键之外（回归见 tests/test_spd_point_record_ledger.py）",
}


def _model_bindings(func: ast.FunctionDef, model_names: set[str]) -> dict[str, str]:
    """函数体内"变量名 → 模型名"的绑定表，供 `db.add(变量)` 反查模型。

    三种形状：
        obj = Model(...)                       # 直接赋值
        xs.append(Model(...)); for x in xs:    # 容器只装过**同一种**模型，循环变量才可解析
    容器里装过两种模型、或装过非模型的东西，一律放弃解析（记成 "?"）——宁可漏也不误报。
    """
    assigned: dict[str, str] = {}
    appended: dict[str, str] = {}
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
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.args
        ):
            arg = node.args[0]
            model = (
                arg.func.id
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id in model_names
                else "?"
            )
            prev = appended.get(node.func.value.id)
            appended[node.func.value.id] = model if prev in (None, model) else "?"
    for node in ast.walk(func):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Name):
            model = appended.get(node.iter.id)
            if model and model != "?":
                assigned.setdefault(node.target.id, model)
    return assigned


def _inserted_models(func: ast.FunctionDef, model_names: set[str]) -> set[str]:
    """函数里被 `db.add(...)` 插入的模型名。

    三种写法都要认：
        db.add(Model(...))            # 内联
        obj = Model(...); db.add(obj)  # 先赋值再插——**这才是绝大多数**
        for x in xs: db.add(x)         # xs 只 append 过同一种模型（对账差异明细就是这形状）

    第一版只认内联写法，结果 192 处插入里只看得见 22 处（11%），
    而变量写法里藏着 48 处真问题。**一条只覆盖 11% 的规则给出的是虚假的安全感**，
    比没有规则更糟——它会让人以为这一类已经被守住了。
    """
    assigned = _model_bindings(func, model_names)

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


def _unguarded_unique_writes() -> list[str]:
    """往"唯一表"（DB 级唯一约束 **或** LOGICAL_UNIQUE_TABLES）里插入却不处理冲突的位置。"""
    model_table = _model_to_table()
    unique_tables = _tables_with_unique_constraint() | set(LOGICAL_UNIQUE_TABLES)
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
            if "IntegrityError" in source or any(h in source for h in helpers):
                continue
            for model in sorted(_inserted_models(func, model_names)):
                table = model_table[model]
                if table in unique_tables:
                    offenders.append(f"{key} → {model}({table})")
                    break
    return offenders


def test_写唯一约束表的接口必须处理约束冲突():
    """防的是这一轮实测到的同一个错误再犯第四次。

    判据：函数里 `db.add` 了某个模型，而该模型的表**带 DB 级唯一约束、或列在
    LOGICAL_UNIQUE_TABLES 里**，则该函数必须出现下列之一——捕获 `IntegrityError`，
    或调用 `app/concurrency.py` 的助手。

    只认"DB 级唯一约束"的那一版，判据只覆盖 229 个写入点里的 42 个（19.9%）；
    而 check-then-act 恰恰爱长在"业务上唯一、库上没约束"的表上——那里撞不出
    IntegrityError，是**静默写出两条**。所以另立逻辑唯一清单一并纳入。

    仍会有漏网（跨函数传对象、循环里从容器取对象），但**不会误报**：
    命中的都确实是"往唯一表里插入"。宁可漏也不误报——一条经常误报的
    规则会先被加豁免、再被加得没人看，最后被删掉。覆盖面数字见
    test_防复发闸门自证覆盖面。
    """
    offenders = _unguarded_unique_writes()
    new = sorted(o for o in offenders if o.split(" → ")[0] not in KNOWN_UNGUARDED_UNIQUE_WRITES)
    print(
        f"\n[唯一表写入规则] 命中 {len(offenders)} 处，其中已登记欠账 "
        f"{len(offenders) - len(new)} 处、新增 {len(new)} 处"
    )
    assert new == [], (
        "以下接口往（DB 唯一约束或逻辑唯一）表里直接插入，却没处理约束冲突"
        "（并发下会 500 丢数据，或静默写出两条）：\n  " + "\n  ".join(new) +
        "\n请改用 app/concurrency.py 的 insert_or_conflict / insert_with_retry / "
        "upsert_unique，或显式捕获 IntegrityError；确属不会冲突的加进 CONFLICT_SAFE 并写明理由；"
        "\n确属存量欠账的登记进 KNOWN_UNGUARDED_UNIQUE_WRITES（清单只减不增）。"
    )


def test_豁免清单不得腐烂():
    """豁免条目对应的函数必须还存在——函数改名或删掉之后豁免会一直挂着。"""
    existing = set()
    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            existing.add(f"{name}:{func.name}")
    listed = set(CONFLICT_SAFE) | set(KNOWN_UNGUARDED_UNIQUE_WRITES) | set(KNOWN_READ_MODIFY_WRITE)
    stale = sorted(listed - existing)
    assert stale == [], f"豁免/欠账清单里这些函数已不存在，应删除：{stale}"


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


# ============================================ 闸门自证覆盖面（第 17 章 §17.5 第 4 条）
#
# "扫描器、闸门、校验器，凡是有上限、抽样、跳过的，必须把'看了多少、跳过多少'
# 打印出来；欠账要显式、可量化、只减不增。"
#
# 上面那两条规则一直报绿，但从没交代过自己的**分母**。实测清点（口径见
# _write_sites 的 docstring，可自行复算）：
#
#   补强前：os.listdir 只扫到 87 个路由文件 / 211 个写入点，形状识别 210 个（99.5%），
#           而"唯一表"判据只覆盖 42 个（19.9%）——**另外八成规则完全不看**。
#   补强后：os.walk 扫到 95 个文件 / 229 个写入点，形状识别 228 个（99.6%），
#           判据覆盖 63 个（27.5%）。
#
# 覆盖率从 19.9% 到 27.5% 不是"合格"，是**把缺口显式化**：剩下 72.5% 明明白白
# 写在输出里，谁都能看见它没被守住，而不是藏在一个绿灯后面。

# 覆盖面基线：这两个数字**只许变好**（覆盖数只增、未识别数只减）。
#
# 2026-09-01（P1-29）63 → 59，**唯一一次下调，且是判据被推翻而非闸门放水**：
# `bill_details` 从 LOGICAL_UNIQUE_TABLES 移出（账单明细按天重复记同一 item_code
# 是正常业务，详见该清单里的说明），它的 4 个写入点本就不该算进"唯一表判据覆盖"
# 的分子——原来的 63 是把一条不成立的不变式也算成了覆盖，属虚高。
# 同一批次里覆盖的**质量**是升的：admissions / appointment_slots / progress_notes
# 三张表从"只在清单里逻辑唯一"变成库里真有部分唯一索引（迁移 b8e3d5f70a91），
# 抢输者拿 409 而不是静默写出两条。此后仍只许变好。
# 2026-09-04（P1-30 第二步）59 → 131。**两件事叠在一起，分开记账**：
#   * 口径变了：插入助手（insert_or_conflict / insert_if_absent / insert_with_retry）
#     也算写入点，分母 226 → 284。不这么改，"把 db.add 改成 insert_or_conflict"会让
#     那处从分子分母一起消失、覆盖数不升反降——一条会因为修复而报警的度量留不住。
#   * 事情也真的变好了：13 条（部分）唯一索引下沉到库（迁移 b9c8d7e6f5a4 / f4e3d2c1b0a9），
#     原本"业务上唯一、库上无约束"的表进了 _tables_with_unique_constraint()。
# 因此 131/284（46.1%）与旧的 59/226（26.1%）**不可直接相比**；此后仍只许变好。
BASELINE_COVERED_WRITE_SITES = 131
# 2026-09-03（P1-30）1 → 0：最后一个未识别写入点（billing.run_reconciliation 的
# `for d in diffs: db.add(d)`）由 _model_bindings 认出了"容器只装同一种模型"的形状。
BASELINE_UNRESOLVED_WRITE_SITES = 0
# 2026-09-03（P1-30）新增两条棘轮：未覆盖的写入点里"既没审计过"的个数与"待决"的个数，
# 都只许变小。前者的意义是：**新表**进路由之前必须先回答"多行合法吗"（见上面三份清单）。
# 起点 26：正是审计判为"业务上确实唯一/守在父行"的 20 张表的写入点——它们的去向是
# 唯一索引下沉 + insert_or_conflict、或父行条件 UPDATE（同一工程包后续提交），落地一张
# 这个数就该降一次；待决 8 个对应 AUDITED_UNDECIDED_TABLES 的 5 张表。
# 2026-09-04（P1-30 收口）26 → **0**：284 个写入点，每一个都有了去向——要么库上真有唯一
# 约束（131），要么审过并写明"为什么多行合法/由哪条父行 UPDATE 守住"（143），要么明确待决
# 并写明"卡在什么地方"（10）。这个 0 的含义是**没有一处写入点是没人看过的**，不是"没有并发
# 问题"；新表进路由前必须先回答"多行合法吗"，答案落到某一份清单或一条唯一索引上，否则这里变红。
BASELINE_UNAUDITED_WRITE_SITES = 0
# 2026-09-04：8 → 10，**唯一一次上调，且是"从更差的那一档挪进来"而不是放水**。
# `spd_lifecycle_events` 的两个写入点原本落在"既未覆盖也未审计"（26 那一档）里；
# 审计判定它的不变式为真，却因为这张表没有"撤回待确认迁出"的通道而**故意不建索引**
# （建了就把一条良性的多余行换成一份卡死的档案，理由见 AUDITED_UNDECIDED_TABLES
# 与 TECH_DEBT P1-40）。它是从"没审过"挪进"审过但定不了"，总缺口在变小。
# P1-40 补上撤回通道并建索引后，这个数要跟着降回 8。
BASELINE_UNDECIDED_WRITE_SITES = 10


#: 走 `app/concurrency.py` 助手插入的写法（第一个模型实参就是要写的行）。
#: 这些**也是写入点**——见 `_write_sites` docstring 里那条"分母不许因为修好而缩水"。
_INSERT_HELPERS = {"insert_or_conflict", "insert_if_absent", "insert_with_retry"}


def _first_model_call(node: ast.AST, model_names: set[str]) -> str | None:
    """节点里第一处 `Model(...)` 构造的模型名（按源码顺序）。"""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id in model_names:
                return child.func.id
    return None


def _model_built_by(func: ast.FunctionDef, name: str, model_names: set[str]) -> str | None:
    """`insert_with_retry(db, _build)` 里那个 `_build` 造的是哪个模型。

    `insert_with_retry` 收的是**每次重试都重新构造一行**的工厂函数（服务端生成
    的顺序编号要重算），所以第二个实参不是行对象而是可调用对象——不顺着它走，
    这三处（医废追溯码、病理标本号、证书编号）就会一直挂在"形状识别不了"里。
    """
    for node in ast.walk(func):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return _first_model_call(node, model_names)
    return None


def _write_sites():
    """全部**插入点**的清点（`db.add(...)` 与 `app/concurrency.py` 的插入助手）。

    口径（**连口径一起留档**，否则这个数字过后连被核对的资格都没有）：
      * 文件：app/routers 与 app/spd/routers 下**递归**的全部 .py；
      * 写入点：函数体内形如 `db.add(x)` 的调用，或 `insert_or_conflict(db, x, ...)` /
        `insert_if_absent(db, x)` / `insert_with_retry(...)` 这类助手调用，一次算一个；
      * 形状可识别：能顺着 `db.add(Model(...))`、`obj = Model(...); db.add(obj)` 或
        `xs.append(Model(...)); for x in xs: db.add(x)` 解析出模型名的写入点；
      * 规则覆盖：模型对应的表带 DB 级唯一约束，或在 LOGICAL_UNIQUE_TABLES 里。

    **为什么助手调用也要算进来**（2026-09-04，P1-30 落地时发现的度量缺陷）：
    原口径只数 `db.add`，于是把一处写入点改成 `insert_or_conflict` 之后，那处就从
    分子分母里一起消失——代码变安全了，"覆盖数"反而**下降**，`covered >= 基线`
    这条棘轮会因为有人修好了东西而变红。一条会因为修复而报警的度量，早晚被当噪音
    调松或删掉。把助手调用一并计入，分母才稳定，覆盖数才随修复上升。
    """
    model_table = _model_to_table()
    model_names = set(model_table)
    guarded_tables = _tables_with_unique_constraint() | set(LOGICAL_UNIQUE_TABLES)
    audited_tables = set(AUDITED_MULTI_ROW_TABLES) | set(GUARDED_BY_PARENT_UPDATE)
    undecided_tables = set(AUDITED_UNDECIDED_TABLES)

    total = resolved = covered = audited = undecided = 0
    unresolved: list[str] = []
    uncovered_tables: dict[str, int] = {}
    unaudited_tables: dict[str, int] = {}
    written_tables: set[str] = set()

    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            assigned = _model_bindings(func, model_names)
            for node in ast.walk(func):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr != "add":
                        continue
                    if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "db"):
                        continue
                    arg, how = node.args[0], "db.add"
                elif isinstance(node.func, ast.Name) and node.func.id in _INSERT_HELPERS:
                    # 助手的第一个实参是 session，第二个才是要写的行
                    if len(node.args) < 2:
                        continue
                    arg, how = node.args[1], node.func.id
                else:
                    continue
                total += 1
                model = None
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                    model = arg.func.id if arg.func.id in model_names else None
                elif isinstance(arg, ast.Name):
                    # `insert_with_retry(db, _build)` 传的是**构造函数**而不是行对象：
                    # 顺着同名内嵌 def 的函数体去找它 return 的那个模型。
                    model = assigned.get(arg.id) or _model_built_by(func, arg.id, model_names)
                elif isinstance(arg, ast.Lambda):
                    model = _first_model_call(arg, model_names)
                if model is None:
                    unresolved.append(f"{name}:{func.name} → {how}({ast.unparse(arg)})")
                    continue
                resolved += 1
                table = model_table[model]
                written_tables.add(table)
                if table in guarded_tables:
                    covered += 1
                    continue
                uncovered_tables[table] = uncovered_tables.get(table, 0) + 1
                if table in audited_tables:
                    audited += 1
                elif table in undecided_tables:
                    undecided += 1
                else:
                    unaudited_tables[table] = unaudited_tables.get(table, 0) + 1
    return {
        "files": len(_router_files()),
        "total": total,
        "resolved": resolved,
        "covered": covered,
        "audited": audited,
        "undecided": undecided,
        "unresolved": unresolved,
        "uncovered_tables": uncovered_tables,
        "unaudited_tables": unaudited_tables,
        "written_tables": written_tables,
    }


def test_防复发闸门自证覆盖面():
    """闸门必须自报分母：扫了多少文件、多少写入点、规则覆盖多少、剩多少没覆盖。

    这条用例本身不判"代码有没有问题"，它判的是**闸门有没有缩水**：
    路由挪进子包（少扫文件）、新写法让形状识别失效（少认写入点）、
    判据退化（覆盖数下降），三者任一发生都会在这里变红。
    """
    stats = _write_sites()
    top = sorted(stats["uncovered_tables"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    unaudited = sum(stats["unaudited_tables"].values())
    summary = "\n".join([
        "",
        "[并发防复发闸门] 覆盖面自证",
        f"  扫描文件：{stats['files']} 个（app/routers + app/spd/routers，递归，无跳过）",
        f"  db.add 写入点：{stats['total']} 个",
        f"  形状可识别：{stats['resolved']} 个"
        f"（{stats['resolved'] / stats['total']:.1%}），未识别 {len(stats['unresolved'])} 个",
        f"  唯一表判据覆盖：{stats['covered']} 个"
        f"（{stats['covered'] / stats['total']:.1%}），**未覆盖 {stats['total'] - stats['covered']} 个**",
        f"  未覆盖写入点最多的表（前 10）：{top}",
        f"  未覆盖里已逐表审计——多行合法/父行守住：{stats['audited']} 个，待决：{stats['undecided']} 个，"
        f"**既未覆盖也未审计：{unaudited} 个** {sorted(stats['unaudited_tables']) or ''}",
        f"  未识别的写入点：{stats['unresolved'] or '无'}",
        "  说明：未覆盖 ≠ 安全，只是这条规则看不到；审计清单说的是“审过、多行合法”，"
        "不是豁免——缺口显式化，不许再藏在绿灯后面。",
    ])
    print(summary)
    # print 在 `-q` 下被吞掉；warning 会进 "warnings summary"，
    # 让覆盖面数字在 CI 默认输出里也看得见。
    warnings.warn(summary, UserWarning, stacklevel=2)

    assert stats["covered"] >= BASELINE_COVERED_WRITE_SITES, (
        f"规则覆盖的写入点从 {BASELINE_COVERED_WRITE_SITES} 掉到 {stats['covered']}："
        " 判据退化或路由文件没扫全（闸门缩水了）。"
    )
    assert len(stats["unresolved"]) <= BASELINE_UNRESOLVED_WRITE_SITES, (
        f"形状识别不了的写入点从 {BASELINE_UNRESOLVED_WRITE_SITES} 涨到 "
        f"{len(stats['unresolved'])}：{stats['unresolved']}"
    )
    assert unaudited <= BASELINE_UNAUDITED_WRITE_SITES, (
        f"既无唯一约束、也没审计过的写入点从 {BASELINE_UNAUDITED_WRITE_SITES} 涨到 {unaudited}："
        f"{stats['unaudited_tables']}。新表往路由里 db.add 之前先回答“多行合法吗”："
        "唯一就建唯一索引 + insert_or_conflict；多行合法就登记进 AUDITED_MULTI_ROW_TABLES "
        "并写明理由；守在父行上的登记进 GUARDED_BY_PARENT_UPDATE；定不了的进 "
        "AUDITED_UNDECIDED_TABLES（那份只减不增，进一条要有书面理由）。"
    )
    assert stats["undecided"] <= BASELINE_UNDECIDED_WRITE_SITES, (
        f"待决写入点从 {BASELINE_UNDECIDED_WRITE_SITES} 涨到 {stats['undecided']}：待决清单只减不增。"
    )
    if stats["covered"] > BASELINE_COVERED_WRITE_SITES:
        print(f"[提示] 覆盖已升到 {stats['covered']}，请把 BASELINE_COVERED_WRITE_SITES 上调。")
    if unaudited < BASELINE_UNAUDITED_WRITE_SITES or stats["undecided"] < BASELINE_UNDECIDED_WRITE_SITES:
        print("[提示] 未审计/待决数已下降，请把对应 BASELINE_* 下调，让棘轮咬住新位置。")


def test_已审计清单不得腐烂():
    """三份审计清单说的是"审过、多行合法/父行守住/待决"，条目必须还成立：

    * 表还在模型里（表删了条目还挂着，是死账）；
    * 表还**没有**唯一约束（补了约束规则就接管了，条目该删，否则"审计"与"约束"
      两本账对不上）；
    * 表还在路由里被 db.add（不再写入的表登记着没意义）；
    * 三份清单互不重叠，且每条都写了理由。
    """
    tables = set(models.Base.metadata.tables)
    unique = _tables_with_unique_constraint() | set(LOGICAL_UNIQUE_TABLES)
    stats = _write_sites()
    books = {
        "AUDITED_MULTI_ROW_TABLES": AUDITED_MULTI_ROW_TABLES,
        "GUARDED_BY_PARENT_UPDATE": GUARDED_BY_PARENT_UPDATE,
        "AUDITED_UNDECIDED_TABLES": AUDITED_UNDECIDED_TABLES,
    }
    names = list(books)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            overlap = sorted(set(books[a]) & set(books[b]))
            assert overlap == [], f"{a} 与 {b} 同时登记了 {overlap}：一张表只能有一种去向"
    listed = {t: why for book in books.values() for t, why in book.items()}
    missing = sorted(t for t in listed if t not in tables)
    assert missing == [], f"审计清单里这些表已不在模型里，应删除：{missing}"
    taken_over = sorted(t for t in listed if t in unique)
    assert taken_over == [], (
        f"这些表已有唯一约束、规则已接管，应从审计清单删除（两本账不能同时记）：{taken_over}"
    )
    not_written = sorted(t for t in listed if t not in stats["written_tables"])
    assert not_written == [], f"审计清单里这些表已不再被路由 db.add，条目是死账：{not_written}"
    blank = sorted(t for t, why in listed.items() if len(why.strip()) < 12)
    assert blank == [], f"审计清单条目必须写明理由：{blank}"


def test_路由扫描必须递归到子包():
    """防的是"路由拆进子包 → 扫描静默缩水"这一种失效。

    实测：app/spd/routers/config/ 是子包，os.listdir 一层扫漏掉它整包
    （8 个文件、18 个 db.add 写入点），而闸门照样报绿。
    """
    scanned = {name for name, _ in _router_files()}
    assert any(name.startswith("spd/config/") for name in scanned), (
        "没扫到 app/spd/routers/config/ 子包——_router_files() 又退回成不递归了"
    )
    top_level_only = {n for n in scanned if "/" not in n.replace("spd/", "", 1)}
    assert len(scanned) > len(top_level_only), "子包里的路由文件一个都没扫到"
