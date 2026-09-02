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
BASELINE_COVERED_WRITE_SITES = 59
BASELINE_UNRESOLVED_WRITE_SITES = 1


def _write_sites():
    """全部 `db.add(...)` 写入点的清点。

    口径（**连口径一起留档**，否则这个数字过后连被核对的资格都没有）：
      * 文件：app/routers 与 app/spd/routers 下**递归**的全部 .py；
      * 写入点：函数体内形如 `db.add(x)` 的调用，一次调用算一个；
      * 形状可识别：能顺着 `db.add(Model(...))` 或 `obj = Model(...); db.add(obj)`
        解析出模型名的写入点；
      * 规则覆盖：模型对应的表带 DB 级唯一约束，或在 LOGICAL_UNIQUE_TABLES 里。
    """
    model_table = _model_to_table()
    model_names = set(model_table)
    guarded_tables = _tables_with_unique_constraint() | set(LOGICAL_UNIQUE_TABLES)

    total = resolved = covered = 0
    unresolved: list[str] = []
    uncovered_tables: dict[str, int] = {}

    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
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
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "add" or not node.args:
                    continue
                if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "db"):
                    continue
                total += 1
                arg = node.args[0]
                model = None
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                    model = arg.func.id if arg.func.id in model_names else None
                elif isinstance(arg, ast.Name):
                    model = assigned.get(arg.id)
                if model is None:
                    unresolved.append(f"{name}:{func.name} → db.add({ast.unparse(arg)})")
                    continue
                resolved += 1
                table = model_table[model]
                if table in guarded_tables:
                    covered += 1
                else:
                    uncovered_tables[table] = uncovered_tables.get(table, 0) + 1
    return {
        "files": len(_router_files()),
        "total": total,
        "resolved": resolved,
        "covered": covered,
        "unresolved": unresolved,
        "uncovered_tables": uncovered_tables,
    }


def test_防复发闸门自证覆盖面():
    """闸门必须自报分母：扫了多少文件、多少写入点、规则覆盖多少、剩多少没覆盖。

    这条用例本身不判"代码有没有问题"，它判的是**闸门有没有缩水**：
    路由挪进子包（少扫文件）、新写法让形状识别失效（少认写入点）、
    判据退化（覆盖数下降），三者任一发生都会在这里变红。
    """
    stats = _write_sites()
    top = sorted(stats["uncovered_tables"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]
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
        f"  未识别的写入点：{stats['unresolved'] or '无'}",
        "  说明：未覆盖 ≠ 安全，只是这条规则看不到——缺口显式化，不许再藏在绿灯后面。",
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
    if stats["covered"] > BASELINE_COVERED_WRITE_SITES:
        print(f"[提示] 覆盖已升到 {stats['covered']}，请把 BASELINE_COVERED_WRITE_SITES 上调。")


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
