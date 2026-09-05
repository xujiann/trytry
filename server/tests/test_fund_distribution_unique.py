"""结余分配明细的唯一不变式（P1-30）：一次清算里同一家机构只分一条钱。

`POST /api/fund/pools/{id}/distribute` 是典型的 check-then-act：先把本次清算的
旧明细整批删掉，再按机构逐条插一整套新明细。顺序请求下这就是"覆盖"，
返回 200，**洞完全看不见**；并发下两路各自只删得掉自己快照里看得见的行
（PG 读已提交下输家的 DELETE 阻塞在赢家的行锁上、解锁后那些行已不存在，
而赢家新插的行它又看不见），于是两套明细各插一遍——库里 2n 行、
`distributed_amount` 直接翻倍。分的是医共体真金白银的结余，账当场对不上。

兜底是唯一索引 `uq_fund_distribution_settlement_org(settlement_id, org_id)`：
输家的 INSERT 撞索引 → IntegrityError → 回滚**整个事务连同它那条 DELETE** →
409。赢家那套明细完好无损，输家重试一次即正常覆盖。

顺序路径造不出这个 409（重复分配本就合法），所以行为用例把"输家实际到达的
位置"直接造出来：让 `org_scorecards` 吐出重复的 org_id，与并发时两套明细撞在
一起是同一个落点。真并发的验证在 `tests/test_fund_distribution_unique_races.py`
（真 PG，默认跳过）。
"""
import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from app.database import Base, engine

INDEX_NAME = "uq_fund_distribution_settlement_org"
CONFLICT_DETAIL = "本池结余分配正被另一请求处理，请刷新后重试"


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": org_type, "level": level},
            headers=admin,
        ).json()
        for name, org_type, level in [
            ("唯一索引县医院", "lead_hospital", "county"),
            ("唯一索引东镇卫生院", "township", "township"),
            ("唯一索引西镇卫生院", "township", "township"),
        ]
    ]


@pytest.fixture(scope="module")
def director(client, admin, orgs):
    client.post(
        "/api/users",
        json={"username": "fdu_dir", "password": "passw0rd1", "role": "director",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login",
                       json={"username": "fdu_dir", "password": "passw0rd1"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def group(client, admin, orgs):
    """把三家机构圈进一个分组：分配范围跟着分组走，行数才与种子数据无关。"""
    grp = client.post("/api/org-groups",
                      json={"name": "唯一索引片区", "group_type": "zone"},
                      headers=admin).json()
    for org in orgs:
        client.post(f"/api/org-groups/{grp['id']}/members",
                    json={"org_id": org["id"]}, headers=admin)
    return grp


def _settled_pool(client, director, group, year, total=90000.0, expense=0.0):
    pool = client.post(
        "/api/fund/pools",
        json={"year": year, "insurance_type": "resident",
              "org_group_id": group["id"], "total_amount": total},
        headers=director,
    ).json()
    resp = client.post(f"/api/fund/pools/{pool['id']}/settle",
                       json={"total_expense": expense, "overrun_action": "none"},
                       headers=director)
    assert resp.status_code == 201, resp.text
    return pool


@pytest.fixture(scope="module")
def pool_a(client, director, group):
    return _settled_pool(client, director, group, 2041)


@pytest.fixture(scope="module")
def pool_b(client, director, group):
    return _settled_pool(client, director, group, 2042, total=60000.0)


# ---------------- 行为：撞键给同一句 409，合法的多条照常接受 ----------------


def _duplicate_one_scorecard(monkeypatch):
    """让 `org_scorecards` 多吐一张同 org_id 的卡，把撞键那一刻造出来。

    并发的输家实际到达的位置就是这里：它插的那套明细与赢家已提交的那套
    键完全相同。顺序请求造不出这个落点（重复分配本就是覆盖），
    所以只能从计分卡这一侧注入。
    """
    from app.routers import fund

    real = fund.org_scorecards

    def duplicated(**kwargs):
        result = real(**kwargs)
        cards = result["scorecards"]
        return {**result, "scorecards": [*cards, dict(cards[0])]}

    monkeypatch.setattr(fund, "org_scorecards", duplicated)


def test_同一次清算里同一家机构分到两条明细时给出409(client, director, pool_a, monkeypatch):
    """把"输家两套明细撞在一起"造出来：同一个 org_id 出现两张计分卡。

    断言状态码**与文案**一起钉——只钉状态码分辨不出它是撞了唯一索引，
    还是被别的前置校验（超支/公式/无机构）挡在了写库之前。
    """
    first = client.post(f"/api/fund/pools/{pool_a['id']}/distribute",
                        json={"formula_expr": "1"}, headers=director)
    assert first.status_code == 200, first.text
    before = client.get(f"/api/fund/pools/{pool_a['id']}/distributions",
                        headers=director).json()
    assert len(before) == 3

    _duplicate_one_scorecard(monkeypatch)
    resp = client.post(f"/api/fund/pools/{pool_a['id']}/distribute",
                       json={"formula_expr": "1"}, headers=director)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == CONFLICT_DETAIL

    monkeypatch.undo()
    after = client.get(f"/api/fund/pools/{pool_a['id']}/distributions",
                       headers=director).json()
    assert after == before, (
        "撞键回滚必须连同那条 DELETE 一起回退——否则赢家的整套明细被输家删空了"
    )


def test_撞键回滚后清算单的公式与依据也不变(client, director, pool_a, monkeypatch):
    """回滚的是整个事务：明细、`formula_expr`、`score_basis` 必须一起退回去。

    只回滚明细而留下改过的公式，等于"这次分配按 score 分的"写进了账，
    而库里躺着的是上一次按均分算出来的钱——事后复核会得出错误结论。
    """
    ok = client.post(f"/api/fund/pools/{pool_a['id']}/distribute",
                     json={"formula_expr": "1"}, headers=director)
    assert ok.status_code == 200, ok.text
    before = ok.json()

    _duplicate_one_scorecard(monkeypatch)
    resp = client.post(f"/api/fund/pools/{pool_a['id']}/distribute",
                       json={"formula_expr": "score ** 2 + 1"}, headers=director)
    assert resp.status_code == 409 and resp.json()["detail"] == CONFLICT_DETAIL
    monkeypatch.undo()

    after = client.get(f"/api/fund/pools/{pool_a['id']}/settlement",
                       headers=director).json()
    assert after["formula_expr"] == before["formula_expr"] == "1"
    assert after["score_basis"] == before["score_basis"]
    assert after["distributed_amount"] == before["distributed_amount"]


def test_一次清算里多家机构各一条是合法的(client, director, pool_a):
    """唯一的是 (清算, 机构) 这一对，不是"一次清算只许一条明细"。

    写成 settlement_id 单列唯一会把"分给三家机构"拒掉——那是另一种坏。
    """
    resp = client.post(f"/api/fund/pools/{pool_a['id']}/distribute",
                       json={"formula_expr": "1"}, headers=director)
    assert resp.status_code == 200, resp.text
    rows = resp.json()["distributions"]
    assert len(rows) == 3 and len({r["org_id"] for r in rows}) == 3
    assert round(sum(r["amount"] for r in rows), 2) == 90000.00


def test_同一家机构在另一次清算里照样分到钱(client, director, pool_a, pool_b):
    """跨清算的同机构明细是常态：每年都要分一次钱。

    写成 org_id 单列唯一，第二个池子就分不出去了。
    """
    resp = client.post(f"/api/fund/pools/{pool_b['id']}/distribute",
                       json={"formula_expr": "1"}, headers=director)
    assert resp.status_code == 200, resp.text
    a_rows = client.get(f"/api/fund/pools/{pool_a['id']}/distributions",
                        headers=director).json()
    b_rows = client.get(f"/api/fund/pools/{pool_b['id']}/distributions",
                        headers=director).json()
    assert {r["org_id"] for r in a_rows} == {r["org_id"] for r in b_rows}
    assert round(sum(r["amount"] for r in b_rows), 2) == 60000.00


def test_重新分配仍是覆盖而非报冲突(client, director, pool_b):
    """同一事务里"先删后插同一批键"不撞索引——重来的能力不能被兜底顺手掐掉。"""
    for expr in ("1", "score + 1", "1"):
        resp = client.post(f"/api/fund/pools/{pool_b['id']}/distribute",
                           json={"formula_expr": expr}, headers=director)
        assert resp.status_code == 200, resp.text
    rows = client.get(f"/api/fund/pools/{pool_b['id']}/distributions",
                      headers=director).json()
    org_ids = [r["org_id"] for r in rows]
    assert len(org_ids) == len(set(org_ids)) == 3, "重复分配叠加出了重复明细"


# ---------------- 防拆卸静态钉 ----------------


def test_分配明细唯一索引不许消失():
    """模型侧的声明就是这条不变式的落点，删掉就等于把翻倍分钱的洞放回去。

    同时钉住它是**全量**唯一（无 where）：两个键列都 NOT NULL，
    不存在 SQL `NULL != NULL` 的逃逸；哪天 org_id 改成可空，
    全量索引就挡不住 NULL 行了，得改成部分索引（见 uq_slot_with/without_employee）。
    """
    table = Base.metadata.tables["fund_distributions"]
    index = next((i for i in table.indexes if i.name == INDEX_NAME), None)
    assert index is not None, f"fund_distributions 的 {INDEX_NAME} 没了——翻倍分钱的洞回来了"
    assert index.unique, f"{INDEX_NAME} 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == ["settlement_id", "org_id"], (
        f"{INDEX_NAME} 的键变了"
    )
    for dialect in ("sqlite", "postgresql"):
        assert not index.dialect_options[dialect].get("where"), (
            f"{INDEX_NAME} 变成了部分索引：被 where 排除掉的行会重新逃逸"
        )
    for col in ("settlement_id", "org_id"):
        assert not table.c[col].nullable, (
            f"{col} 变成可空了：NULL 行不受唯一索引约束，索引会被静默架空"
        )


def test_分配明细唯一索引真的建在库上(client):
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    names = {i["name"] for i in sa_inspect(engine).get_indexes("fund_distributions")}
    assert INDEX_NAME in names, f"fund_distributions 上没有 {INDEX_NAME}（库与模型对不上）"


def test_绕开接口层直插重复明细时库里真的拦得住(client, director, pool_a, pool_b):
    """索引"在不在"与"拦不拦得住"是两回事。

    接口层顺序请求下走的是覆盖分支，行为用例因此**分辨不出**兜底是否真的生效
    （SQLite 的库级写锁又让线程探针对拆卸不敏感）。这里绕开接口层直接写库
    ——那正是并发抢输者实际到达的位置——看数据库自己是否抬手。
    """
    from app.database import SessionLocal
    from app.models import FundDistribution, FundSettlement

    for pool in (pool_a, pool_b):
        resp = client.post(f"/api/fund/pools/{pool['id']}/distribute",
                           json={"formula_expr": "1"}, headers=director)
        assert resp.status_code == 200, resp.text
    db = SessionLocal()
    try:
        settlement = (
            db.query(FundSettlement)
            .filter(FundSettlement.pool_id == pool_b["id"]).first()
        )
        assert settlement is not None
        existing = (
            db.query(FundDistribution)
            .filter(FundDistribution.settlement_id == settlement.id).first()
        )
        assert existing is not None, "前置：这次清算应当已经分出明细"

        db.add(FundDistribution(
            settlement_id=settlement.id, org_id=existing.org_id,
            score=0.0, score_detail={}, weight=1.0, share_pct=100.0, amount=1.0,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 换一家机构（同清算）与换一次清算（同机构）都必须放行
        other = (
            db.query(FundDistribution)
            .filter(FundDistribution.settlement_id == settlement.id,
                    FundDistribution.org_id != existing.org_id).first()
        )
        assert other is not None
        assert (
            db.query(FundDistribution)
            .filter(FundDistribution.org_id == existing.org_id).count() >= 2
        ), "同一家机构在多次清算里各有一条明细才是常态"
    finally:
        db.close()
