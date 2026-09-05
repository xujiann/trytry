"""真 PostgreSQL 并发验证（P1-30）：结余分配的"删完再插"到底会不会分两遍钱。

默认跳过——CI/开发机不一定有 PG。开启方式（与 `test_postgres_real.py` 同约定）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_fund_distribution_unique_races.py -q

**这条只能在真 PG 上做**。SQLite 是库级写锁，两路请求被压成一前一后的普通覆盖，
竞态窗口根本没打开；PG 逐语句取快照、并发事务互不可见，
`DELETE 本次清算的旧明细 → 逐户 INSERT 新明细 → 一次 commit` 中间那段窗口是
真实敞开的：

- 首次分配：两路的 DELETE 都删到 0 行，然后各插一整套 → 库里 2n 行，
  `distributed_amount` 直接翻倍；
- 重新分配：输家的 DELETE 阻塞在赢家的行锁上，解锁后那些行已经不存在、
  而赢家新插的行又不在它的语句快照里 → 它同样删到 0 行再插一整套。

兜底是唯一索引 `uq_fund_distribution_settlement_org(settlement_id, org_id)`：
输家的 INSERT 撞索引 → IntegrityError → 回滚**整个事务连同它那条 DELETE** → 409。
不变量：无论几路并发，一次清算恰好 n 条明细、机构各一条、金额合计分毫等于结余。

注意：本文件用的 PG 库是**多方共用**的，所以绝不 DROP SCHEMA，
只按 uuid 造自己的机构/分组/池子，跑完留在库里不清理（清理会踩到别人）。
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
INDEX_NAME = "uq_fund_distribution_settlement_org"
CONFLICT_DETAIL = "本池结余分配正被另一请求处理，请刷新后重试"
THREADS = 8
BALANCE = 200000.0


def _retrying(what: str, action, attempts: int = 5, wait: float = 60.0):
    """共用库上撞锁/撞冲突就等一会儿再来，而不是把用例跳过去。

    跳过等于"这条不变式今天没人验证"，而它守的正是分两遍钱这种事故。
    """
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - 共用库上的锁等待属预期
            last = exc
            if i < attempts - 1:
                time.sleep(wait)
    raise AssertionError(f"{what} 重试 {attempts} 次仍未成功：{last}")


def _has_index(engine) -> bool:
    from sqlalchemy import inspect

    try:
        return INDEX_NAME in {
            i["name"] for i in inspect(engine).get_indexes("fund_distributions")
        }
    except Exception:  # noqa: BLE001 - 表还不存在时按"没建过"处理
        return False


@pytest.fixture(scope="module")
def pg_engine():
    """共用库：只在缺索引时把迁移链推到 heads，**绝不** DROP SCHEMA。"""
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    if not _has_index(engine):
        def upgrade():
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "heads"],
                cwd=SERVER_DIR,
                env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"

        _retrying("alembic upgrade heads", upgrade)
    yield engine
    engine.dispose()


def test_分配明细唯一索引由迁移真的建在PG上(pg_engine):
    """模型声明了、迁移漏了，单元档（`create_all` 建库）照样绿——只有真 PG 看得见。

    所以这条放在并发用例前面：底下几条的红绿都以它为前提。
    """
    from sqlalchemy import inspect

    indexes = {i["name"]: i for i in inspect(pg_engine).get_indexes("fund_distributions")}
    assert INDEX_NAME in indexes, f"PG 上没有 {INDEX_NAME}——迁移漏了，兜底根本不存在"
    index = indexes[INDEX_NAME]
    assert index["unique"], f"{INDEX_NAME} 不是唯一索引，等于没有约束"
    assert list(index["column_names"]) == ["settlement_id", "org_id"], (
        f"{INDEX_NAME} 的键变了：{index['column_names']}"
    )


@pytest.fixture(scope="module")
def scene(pg_engine):
    """自建 3 家机构 + 分组 + 已清算的池子（结余 20 万）。

    池子绑分组：共用库里别人造的机构随时在变，不圈范围的话
    "恰好 n 条明细"这个断言会被别人的数据搅黄。
    """
    from sqlalchemy.orm import sessionmaker

    from app.clock import now_naive
    from app.models import (
        FundPool,
        FundSettlement,
        OrgGroup,
        OrgGroupMember,
        Organization,
    )

    tag = uuid.uuid4().hex[:8]
    Session = sessionmaker(bind=pg_engine)

    def build():
        with Session() as db:
            orgs = [
                Organization(name=f"分配并发{tag}第{i}院", org_type="township",
                             level="township")
                for i in range(3)
            ]
            db.add_all(orgs)
            db.flush()
            group = OrgGroup(name=f"分配并发片区{tag}", group_type="zone")
            db.add(group)
            db.flush()
            db.add_all([
                OrgGroupMember(group_id=group.id, org_id=o.id) for o in orgs
            ])
            pool = FundPool(
                year=now_naive().year, insurance_type="resident",
                org_group_id=group.id, total_amount=1000000.0,
                prepay_ratio_pct=0.0, status="settled",
            )
            db.add(pool)
            db.flush()
            settlement = FundSettlement(
                pool_id=pool.id, total_income=1000000.0, total_expense=800000.0,
                balance=BALANCE, overrun_action="none",
            )
            db.add(settlement)
            db.commit()
            return {
                "pool_id": pool.id,
                "settlement_id": settlement.id,
                "org_ids": sorted(o.id for o in orgs),
            }

    return _retrying("建并发用例的机构/分组/池子", build)


def _race(worker, times: int = THREADS):
    """Barrier 真并发（写法同 test_postgres_real._race_on_pg）。

    只起线程不够——线程创建有先后，前一个常常已提交完了后一个才开始读，
    窗口根本没打开。等待点全部带 timeout：会阻塞的回归测试不是回归测试。
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
        t.join(timeout=120)
    return results, errors


def _distribute_racer(pg_engine, pool_id, monkeypatch):
    """造一个"八路同时进入写库段"的 worker。

    第一道 barrier 只对齐线程启动，八路随后各自去算绩效快照（十来条聚合查询），
    快的那路很可能算完、提交完，慢的那路才刚开始——窗口又被时间差抹平了。
    所以在 `org_scorecards` 返回处再对齐一次：DELETE→INSERT→commit
    这段才是竞态本体，必须让八路真正压在一起。**只动测试侧的对齐，
    不给生产代码加任何钩子**。
    """
    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.routers import fund

    Session = sessionmaker(bind=pg_engine)
    real = fund.org_scorecards
    gate = threading.Barrier(THREADS)

    def aligned(**kwargs):
        result = real(**kwargs)
        gate.wait(timeout=60)
        return result

    monkeypatch.setattr(fund, "org_scorecards", aligned)

    def worker(_i):
        with Session() as db:
            try:
                out = fund.distribute(pool_id, fund.DistributeIn(formula_expr="1"), db)
                return ("ok", round(out["distributed_amount"], 2))
            except HTTPException as exc:
                return (str(exc.status_code), exc.detail)

    return worker


def _snapshot(pg_engine, settlement_id):
    from sqlalchemy.orm import sessionmaker

    from app.models import FundDistribution

    with sessionmaker(bind=pg_engine)() as db:
        rows = (
            db.query(FundDistribution)
            .filter(FundDistribution.settlement_id == settlement_id).all()
        )
        return [(r.org_id, round(r.amount, 2)) for r in rows]


def test_八路并发分配_库里始终只有一整套明细(pg_engine, scene, monkeypatch):
    """修复前的形状：八路的 DELETE 都删到 0 行，然后各插一整套 —— 24 条明细、
    `distributed_amount` 160 万（结余的八倍），而且**一个错都不报**。

    修复后：与赢家真正重叠的那几路 INSERT 撞唯一索引，各自回滚整个事务
    （连同自己那条 DELETE）并拿到同一句 409；库里恰好 3 条明细、机构各一条、
    合计分毫等于结余。

    **不能断言"只有一路成功"**：重新分配本就是合法操作（端点注释原话
    「重新分配即覆盖上一次结果」），排在赢家提交**之后**才开始的那一路会正常地
    删旧插新并返回 200——那不是缺陷，是这个接口的既定语义。八路并发里到底有几路
    真正重叠取决于调度，断言"恰一路成功"就是把调度当成不变式，实测会在别的机器上
    随机变红。真正的不变式是下面三条：没人拿到 500、成功的每一路分出的总额都等于
    结余、尘埃落定后库里是**恰好一整套**明细（而不是两套叠加）。
    """
    worker = _distribute_racer(pg_engine, scene["pool_id"], monkeypatch)
    results, errors = _race(worker)

    assert not errors, f"并发分配不该把异常漏给调用方（500 才是真事故）：{errors}"
    assert len(results) == THREADS
    wins = [r for r in results if r[0] == "ok"]
    losses = [r for r in results if r[0] != "ok"]
    assert wins, f"八路全被拒等于这个接口在并发下不可用：{results}"
    assert all(status == "409" for status, _ in losses), f"输家应当拿 409：{losses}"
    assert {detail for _, detail in losses} <= {CONFLICT_DETAIL}, (
        f"输家拿到的文案不是约定的那句：{losses}"
    )
    assert {amount for _, amount in wins} == {round(BALANCE, 2)}, (
        f"成功的每一路分出的总额都该等于结余：{wins}"
    )

    rows = _snapshot(pg_engine, scene["settlement_id"])
    assert len(rows) == 3, f"一次清算分出了 {len(rows)} 条明细——钱分了不止一遍：{rows}"
    assert sorted(org_id for org_id, _ in rows) == scene["org_ids"]
    assert round(sum(amount for _, amount in rows), 2) == round(BALANCE, 2)


def test_八路并发重新分配_不会把上一次的明细删空也不会叠加(pg_engine, scene, monkeypatch):
    """重新分配是**合法**操作（换个公式重来），并发下它的输家更危险：

    输家的 DELETE 先阻塞在赢家的行锁上，放行后删到 0 行，接着插一整套 ——
    没有兜底就是 6 条明细、金额翻倍。有兜底则它撞索引回滚，
    **连同那条 DELETE 一起退回去**，赢家那套明细毫发无损。
    这条同时证明"回滚不会把已分好的钱抹掉"。
    """
    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.routers import fund

    # 先顺序分一次，确保库里已经有一整套明细（上一条用例已分过，这里再确认一次）
    with sessionmaker(bind=pg_engine)() as db:
        try:
            fund.distribute(scene["pool_id"], fund.DistributeIn(formula_expr="1"), db)
        except HTTPException as exc:  # pragma: no cover - 顺序调用不该冲突
            pytest.fail(f"顺序重新分配被拒了：{exc.status_code} {exc.detail}")
    before = sorted(_snapshot(pg_engine, scene["settlement_id"]))
    assert len(before) == 3

    worker = _distribute_racer(pg_engine, scene["pool_id"], monkeypatch)
    results, errors = _race(worker)

    assert not errors, f"并发重新分配不该把异常漏给调用方：{errors}"
    wins = [r for r in results if r[0] == "ok"]
    losses = [r for r in results if r[0] != "ok"]
    assert len(wins) == 1, f"重新分配也只该成一路，实际 {len(wins)} 路：{results}"
    assert {detail for _, detail in losses} == {CONFLICT_DETAIL}, f"输家文案不对：{losses}"

    after = sorted(_snapshot(pg_engine, scene["settlement_id"]))
    assert len(after) == 3, f"重新分配后剩 {len(after)} 条明细：{after}"
    assert after == before, "同样的公式重分一次，明细应当逐条相同（覆盖，不是叠加也不是删空）"
    assert round(sum(amount for _, amount in after), 2) == round(BALANCE, 2)
