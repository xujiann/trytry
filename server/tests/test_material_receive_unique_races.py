"""真 PostgreSQL 上的物资采购验收并发（P1-30 · `asset_movements` / `materials._mark_received`）。

这张表**没有也不该有唯一索引**：`asset_movements` 是按次多行的台账（同一物资的
入库/领用/归还/报废各成一行），表上没有任何列能表达"这是采购单 X 的那次验收"。
不变式长在父行上——**一张 `material_purchases` 只能从 contracted 迁出一次**——
"一张采购单只落一条验收入库流水"是它的推论。守法因此不是 `insert_or_conflict`，
而是一条带状态条件的 UPDATE：`UPDATE material_purchases SET status='received', …
WHERE id = :id AND status = 'contracted'`，rowcount 为 0 即 409。

为什么必须在 PG 上跑：SQLite 的**库级写锁**把并发在语句层就压平了，抢输者是
"还没开始就被排到赢家之后"，而不是"真的和赢家同时判定"。PG 逐语句取快照、
并发事务互不可见——八路都能读到 `contracted`，接口层那句 Python 预检**全员放行**，
真正拦住后七路的只有 UPDATE 自己的 WHERE（赢家提交后 PG 按新版本重算条件，
EvalPlanQual → rowcount 0）。改之前同一段程序在 PG 上是八路全 200、库存加八次、
八条一模一样的 `采购验收 …` 流水。

跑法（与 `test_postgres_real.py` 同一开关）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/medplat_test
    python -m pytest tests/test_material_receive_unique_races.py -q

不变量：八路并发**恰一路 200**、其余七路拿到与顺序重复请求逐字相同的
409 `当前状态 received 不可验收`、没有异常漏给调用方、库里那张单的
`assets` 恰一行且数量只加一次、`asset_movements` 上属于它的入库流水**恰一条**、
`received_note` 是赢家那一条（不是拼接、也不是最后提交者盖上去的）。
同时钉住反面：**手工出入库仍可多行**——闸门守的是父行的那次跃迁，不是这张台账。

本档**不清库**（这个测试库与他人共用）：只按需把迁移升到 heads，前置数据一律带
随机后缀自建，断言只看自己那张单。
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

SERVER_DIR = Path(__file__).resolve().parents[1]

#: 抢输者拿到的 409：与顺序第二次验收（materials.receive_purchase 的 Python 预检）逐字相同
LOSER_DETAIL = "当前状态 received 不可验收"

#: 撞上别的进程正在升级/写同一张表时的等待与重试（测试库共用，不独占）。
_RETRY_TIMES = 5
_RETRY_WAIT_SECONDS = 60

_NEEDED_TABLES = ("organizations", "users", "material_purchases", "assets", "asset_movements")


def _has_tables(engine) -> bool:
    from sqlalchemy import inspect

    return set(_NEEDED_TABLES) <= set(inspect(engine).get_table_names())


@pytest.fixture(scope="module")
def pg_engine():
    """连上测试库，并**只在缺表时**把迁移升到 heads。

    与 `test_postgres_real.py` 的 `pg_engine` 刻意不同：那条要证明迁移链能白手起家，
    所以先 `DROP SCHEMA`；这里要证明的是并发行为，清库只会把同时在用这个库的其他
    用例（和其他人）一起掀翻。`alembic upgrade heads` 本身幂等，已升过就是空操作。
    """
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    for attempt in range(_RETRY_TIMES):
        if _has_tables(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _has_tables(engine):
            break
        assert attempt < _RETRY_TIMES - 1, (
            f"迁移在 PG 上失败，物资采购相关表仍不齐：\n{result.stderr[-2000:]}"
        )
        time.sleep(_RETRY_WAIT_SECONDS)  # 多半是别的进程正在升同一个库，等它升完
    assert _has_tables(engine), "测试库上没有物资采购相关表——迁移没跑到"
    yield engine
    engine.dispose()


def _contracted_purchase(Session, quantity=20, contract_no=None):
    """一家机构 + 一个经办 + 一张已签合同的采购单（名字全带随机后缀，不占共用键）。"""
    from app.models import MaterialPurchase, Organization, User

    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG验收并发县医院-{tag}", org_type="lead_hospital", level="county")
        operator = User(username=f"pg_mat_op_{tag}", password_hash="x", full_name="验收经办")
        db.add_all([org, operator])
        db.flush()
        purchase = MaterialPurchase(
            org_id=org.id, item_name=f"PG验收物资-{tag}", spec="标准件", unit="个",
            quantity=quantity, status="contracted",
            contract_no=contract_no or f"PG-HT-{tag}", requested_by=operator.id,
        )
        db.add(purchase)
        db.commit()
        return {"purchase_id": purchase.id, "org_id": org.id, "user_id": operator.id,
                "item_name": purchase.item_name, "contract_no": purchase.contract_no,
                "quantity": quantity}


def _race(worker, times):
    """Barrier 真并发（写法同 `test_postgres_real._race_on_pg`）。

    只起线程不够——线程创建有先后，前一个常常已提交完了后一个才开始，窗口根本
    没打开。等待点全部带 timeout：会阻塞的回归测试不是回归测试。
    """
    results: list = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(times)

    def run(index: int):
        try:
            barrier.wait(timeout=30)
            outcome = worker(index)
            with lock:
                results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - 收集断言用
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(times)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


def _receive(Session, purchase, note="验收合格"):
    """复刻 `routers/materials.receive_purchase` 的写序：条件 UPDATE 先行，随后才入库与写流水。

    返回 (status_code, detail)——与接口层给调用方的东西一一对应。
    """
    from fastapi import HTTPException

    from app.concurrency import add_amount, ensure_present, insert_if_absent
    from app.models import Asset, AssetMovement, MaterialPurchase
    from app.routers.materials import _mark_received

    pid = purchase["purchase_id"]
    quantity = purchase["quantity"]
    with Session() as db:
        row = db.get(MaterialPurchase, pid)
        if row.status != "contracted":  # 接口层的 Python 预检（快路径）
            return 409, f"当前状态 {row.status} 不可验收"
        if not _mark_received(db, pid, quantity, note):
            db.rollback()
            db.refresh(row)  # 抢输了就按真实状态措辞，别拿锁外读到的旧值
            return 409, f"当前状态 {row.status} 不可验收"
        try:
            code = f"MP{pid:06d}"
            asset = db.query(Asset).filter(Asset.code == code).first()
            if asset is None:
                insert_if_absent(db, Asset(org_id=purchase["org_id"], code=code,
                                           name=purchase["item_name"], category="office", quantity=0))
                asset = db.query(Asset).filter(Asset.code == code).first()
            asset = ensure_present(asset, "资产")
            add_amount(db, Asset, asset.id, "quantity", quantity)
            db.add(AssetMovement(
                asset_id=asset.id, movement_type="inbound", quantity=quantity,
                note=f"采购验收 {purchase['contract_no']}", created_by=purchase["user_id"],
            ))
            db.commit()
        except HTTPException as exc:
            db.rollback()
            return exc.status_code, exc.detail
        return 200, None


def _assert_received_once(Session, purchase, expect_note, expect_inbound=1):
    from app.models import Asset, AssetMovement, MaterialPurchase

    pid, quantity = purchase["purchase_id"], purchase["quantity"]
    with Session() as db:
        row = db.get(MaterialPurchase, pid)
        assert row.status == "received"
        assert row.received_quantity == quantity, f"验收量被加了两次：{row.received_quantity}"
        assert row.received_note == expect_note, f"备注只能是赢家那条：{row.received_note!r}"
        assets = db.query(Asset).filter(Asset.code == f"MP{pid:06d}").all()
        assert len(assets) == 1, f"同一张单建出了 {len(assets)} 条物资台账"
        assert assets[0].quantity == quantity, f"库存按同一张单加了多次：{assets[0].quantity}"
        movements = db.query(AssetMovement).filter(AssetMovement.asset_id == assets[0].id).all()
        inbound = [m for m in movements if m.movement_type == "inbound"]
        assert len(inbound) == expect_inbound, (
            f"验收入库流水应有 {expect_inbound} 条，实际 {len(inbound)} 条——"
            "多出来的那些与真的那条一模一样，事后分不出哪条是验收"
        )
        return assets[0].id


# 迁移可能要现升（首次跑），加上重试等待，给足预算；看门狗默认 120 秒会误判。
@pytest.mark.timeout(600)
def test_八路并发验收同一张采购单_恰一路验到其余同一句409(pg_engine):
    """接口层"读 status → 判 contracted → 加库存 → 写流水"是 check-then-act。

    PG 上八路都读到 contracted、Python 预检全员放行；拦住后七路的是
    `_mark_received` 那条 `WHERE status = 'contracted'`。抢输者回滚后按**刷新过的**
    真实状态措辞，给出与顺序第二次请求逐字相同的 409——对调用方而言"并发撞车"
    与"本来就重复"没有区别。库存与入库流水都只在 rowcount == 1 之后才发生。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import MaterialPurchase

    Session = sessionmaker(bind=pg_engine)
    purchase = _contracted_purchase(Session, quantity=20)

    results, errors = _race(lambda i: _receive(Session, purchase, note=f"第{i}路验收"), times=8)
    assert not errors, f"并发验收不该抛错给调用方：{errors}"
    assert len(results) == 8, f"八路都要有结论：{results}"

    codes = sorted(code for code, _ in results)
    assert codes == [200] + [409] * 7, f"同一张采购单只能验收一次：{results}"
    assert {detail for code, detail in results if code == 409} == {LOSER_DETAIL}, (
        f"抢输的七路必须拿到与顺序重复请求逐字相同的 409 文案：{results}"
    )

    with Session() as db:
        note = db.get(MaterialPurchase, purchase["purchase_id"]).received_note
    assert note in {f"第{i}路验收" for i in range(8)}, (
        f"备注应恰是某一路写的（不是拼接、也不是最后提交者盖上去的）：{note!r}"
    )
    _assert_received_once(Session, purchase, expect_note=note)

    # 尘埃落定后再来一次仍是同一句 409（此时走的是 Python 预检那条快路径）
    assert _receive(Session, purchase) == (409, LOSER_DETAIL)


@pytest.mark.timeout(600)
def test_闸门只按父行划界_另一张采购单同时验收互不影响(pg_engine):
    """守的是"这张单的那次跃迁"，不是"这批物资"或"这个机构"。

    键写宽了（比如误按 org_id 或按 contract_no 建约束）这条会红：两张不同的单
    同时验收，各自都该有一路成功。
    """
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=pg_engine)
    shared_no = f"PG-HT-同号-{uuid.uuid4().hex[:8]}"
    first = _contracted_purchase(Session, quantity=15, contract_no=shared_no)
    second = _contracted_purchase(Session, quantity=15, contract_no=shared_no)

    def worker(i):
        return _receive(Session, first if i % 2 == 0 else second, note="并行两单")

    results, errors = _race(worker, times=8)
    assert not errors, f"并发验收不该抛错给调用方：{errors}"
    assert sorted(code for code, _ in results) == [200, 200] + [409] * 6, (
        f"两张单各该恰一路验到：{results}"
    )
    _assert_received_once(Session, first, expect_note="并行两单")
    _assert_received_once(Session, second, expect_note="并行两单")


@pytest.mark.timeout(600)
def test_验收后手工再入库仍然合法_台账按次多行(pg_engine):
    """反面钉：`asset_movements` 上不许出现唯一索引。

    验收生成的 `MP{id:06d}` 物资在 `admin_mgmt.create_asset_movement` 眼里就是普通
    物资，事后手工补入库/领用/归还都是正常业务。若谁给这张表加了
    `(asset_id, movement_type='inbound')` 之类的唯一索引，这条会红——那是把一条
    良性的多行台账换成"再也补不了库"。
    """
    from sqlalchemy.orm import sessionmaker

    from app.concurrency import add_amount
    from app.models import Asset, AssetMovement

    Session = sessionmaker(bind=pg_engine)
    purchase = _contracted_purchase(Session, quantity=10)
    assert _receive(Session, purchase, note="首次验收") == (200, None)
    asset_id = _assert_received_once(Session, purchase, expect_note="首次验收")

    with Session() as db:  # 复刻 admin_mgmt.create_asset_movement 的手工入库
        add_amount(db, Asset, asset_id, "quantity", 5)
        db.add(AssetMovement(asset_id=asset_id, movement_type="inbound", quantity=5,
                             note="手工补入库", created_by=purchase["user_id"]))
        db.commit()

    with Session() as db:
        rows = db.query(AssetMovement).filter(AssetMovement.asset_id == asset_id).all()
        assert len(rows) == 2, f"手工出入库是按次多行的台账，不该被拦：{rows}"
        assert db.get(Asset, asset_id).quantity == 15
