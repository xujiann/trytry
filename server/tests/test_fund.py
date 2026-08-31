"""阶段七 医保基金总额付费：池子—预付—预结—清算—分配。

这块直接管钱，所以用例几乎全在守四条口径：分配依据会不会被事后改掉、
超支会不会被自动扣、账面数会不会被当成资金流、分配办法有没有被写死。
"""
import pytest

from app.clock import now_naive

# 按绩效分配的池子必须建在**当年**：绩效评分自 2026-08 起是周期口径
# （见 docs/统计口径对照表.md 第 3 条），`distribute` 用 `pool.year` 取分。
# 本文件的 business fixture 造的业务数据 `created_at` 都是"现在"，
# 池子若沿用别处那种随手挑的未来年份（2036），当年没有任何业务落在窗口内，
# 全员 0 分 → 权重和为 0 → 422。与得分无关的公式（`formula_expr="1"`）不受影响，
# 所以只有这一个池子需要跟着当年走。
SCORED_POOL_YEAR = now_naive().year


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": org_type, "level": level},
            headers=admin,
        ).json()
        for name, org_type, level in [
            ("基金县医院", "lead_hospital", "county"),
            ("基金东镇卫生院", "township", "township"),
            ("基金西镇卫生院", "township", "township"),
        ]
    ]


@pytest.fixture(scope="module")
def director(client, admin, orgs):
    client.post(
        "/api/users",
        json={"username": "fd_dir", "password": "passw0rd1", "role": "director",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "fd_dir", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def operator(client, admin, orgs):
    client.post(
        "/api/users",
        json={"username": "fd_op", "password": "passw0rd1", "role": "operator",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "fd_op", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _pool(client, director, year, total=1000000.0, ratio=70.0, group_id=None):
    body = {"year": year, "insurance_type": "resident", "total_amount": total,
            "prepay_ratio_pct": ratio}
    if group_id is not None:
        body["org_group_id"] = group_id
    return client.post("/api/fund/pools", json=body, headers=director).json()


# ---------------- 池子与预付 ----------------


def test_同年度同险种同范围只能建一个池(client, director):
    _pool(client, director, 2030)
    resp = client.post("/api/fund/pools",
                       json={"year": 2030, "insurance_type": "resident", "total_amount": 1},
                       headers=director)
    assert resp.status_code == 409


def test_预付超计划只警告不拦截(client, director):
    """现实中确有追加预拨，不该因为一个比例参数就挡住真实发生的资金流。"""
    pool = _pool(client, director, 2031, total=1000.0, ratio=50.0)   # 计划预付 500
    ok = client.post(f"/api/fund/pools/{pool['id']}/prepayments",
                     json={"amount": 400, "batch_no": "P1"}, headers=director)
    assert ok.status_code == 201 and "warning" not in ok.json()
    over = client.post(f"/api/fund/pools/{pool['id']}/prepayments",
                       json={"amount": 300, "batch_no": "P2"}, headers=director)
    assert over.status_code == 201
    assert "追加预拨" in over.json()["warning"]
    assert over.json()["prepaid_amount"] == 700


def test_基金管理限管理层(client, operator):
    resp = client.post("/api/fund/pools",
                       json={"year": 2032, "total_amount": 1}, headers=operator)
    assert resp.status_code == 403


# ---------------- 预结不产生资金流 ----------------


def test_预结明说不产生资金流(client, director):
    """账面对冲与真实拨付必须分得清，否则年终对不上账时无从倒查。"""
    pool = _pool(client, director, 2033)
    resp = client.post(f"/api/fund/pools/{pool['id']}/periods",
                       json={"period": "2033-01", "actual_amount": 12345}, headers=director)
    assert resp.status_code == 201
    body = resp.json()
    assert body["cash_flow"] is False
    assert "不产生资金流" in body["note"]
    # 预结不该影响已预付金额
    assert body["pool"]["prepaid_amount"] == 0
    assert body["pool"]["accrued_expense"] == 12345


def test_重复预结同期为覆盖并标明来源(client, director):
    """月中反复核对是常态，重跑应当覆盖而不是累加出一个虚高的发生额。"""
    pool = _pool(client, director, 2034)
    for amount in (100, 200):
        client.post(f"/api/fund/pools/{pool['id']}/periods",
                    json={"period": "2034-03", "actual_amount": amount}, headers=director)
    rows = client.get(f"/api/fund/pools/{pool['id']}/periods", headers=director).json()
    assert len(rows) == 1 and rows[0]["actual_amount"] == 200
    assert rows[0]["source"] == "manual"


def test_不给金额时按结算单自动归集(client, director):
    pool = _pool(client, director, 2035)
    resp = client.post(f"/api/fund/pools/{pool['id']}/periods",
                       json={"period": "2035-01"}, headers=director)
    assert resp.json()["source"] == "auto"


# ---------------- 清算与分配 ----------------


@pytest.fixture(scope="module")
def business(client, admin, orgs):
    """造出有差异的绩效得分：两家机构各做几笔转诊，结案率不同。

    没有业务数据时全员得分为 0，按得分分配会退化成"权重全零"，
    验证不出归一化是否正确。
    """
    patient = client.post(
        "/api/patients", json={"name": "基金患者", "id_card": "331782198802021234"},
        headers=admin,
    ).json()
    # orgs[1] 两笔全部结案；orgs[2] 两笔只结案一笔 → 结案率 100% vs 50%
    for from_org, completed in [(orgs[1], 2), (orgs[2], 1)]:
        for i in range(2):
            ref = client.post(
                "/api/referrals",
                json={"patient_id": patient["id"], "from_org_id": from_org["id"],
                      "to_org_id": orgs[0]["id"], "direction": "up", "reason": "上转"},
                headers=admin,
            ).json()
            if i < completed:
                client.patch(f"/api/referrals/{ref['id']}/status",
                             json={"status": "accepted"}, headers=admin)
                client.patch(f"/api/referrals/{ref['id']}/status",
                             json={"status": "completed"}, headers=admin)
    return patient


@pytest.fixture(scope="module")
def settled(client, director, business):
    """筹资 100 万、实际发生 80 万 → 结余 20 万。

    年份取 `SCORED_POOL_YEAR`（当年）而非固定未来年——本池的用例要按绩效分配，
    见文件头注释。
    """
    pool = _pool(client, director, SCORED_POOL_YEAR, total=1000000.0)
    client.post(f"/api/fund/pools/{pool['id']}/periods",
                json={"period": f"{SCORED_POOL_YEAR}-06", "actual_amount": 800000},
                headers=director)
    settlement = client.post(f"/api/fund/pools/{pool['id']}/settle",
                             json={}, headers=director).json()
    return {"pool": pool, "settlement": settlement}


def test_清算结出结余并给出口径(client, settled):
    s = settled["settlement"]
    assert s["total_income"] == 1000000 and s["total_expense"] == 800000
    assert s["balance"] == 200000 and s["is_overrun"] is False
    assert "筹资总额 − 全年实际发生额" in s["caliber"]["balance"]


def test_一个池只能清算一次(client, director, settled):
    resp = client.post(f"/api/fund/pools/{settled['pool']['id']}/settle",
                       json={}, headers=director)
    assert resp.status_code == 409


def test_分配按公式归一化且金额加总等于结余(client, director, settled, orgs):
    pool_id = settled["pool"]["id"]
    resp = client.post(f"/api/fund/pools/{pool_id}/distribute",
                       json={"formula_expr": "score"}, headers=director)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    rows = body["distributions"]
    assert rows, "应当分出明细"
    assert abs(sum(r["share_pct"] for r in rows) - 100) < 0.01
    # 分出去的总额必须**分毫等于**结余——不是"约等于"。原先这里写 <1.0 元容差，
    # 那 1 元的松动正好把逐户各自 round 丢分的 bug 盖住了。改成到分精确。
    assert round(sum(r["amount"] for r in rows), 2) == round(body["distributed_amount"], 2)
    assert round(body["distributed_amount"], 2) == 200000.00


def test_均分公式与按绩效公式给出不同结果(client, director, settled):
    """分配办法必须是可换的——写死进代码就做不到这一点。"""
    pool_id = settled["pool"]["id"]
    equal = client.post(f"/api/fund/pools/{pool_id}/distribute",
                        json={"formula_expr": "1"}, headers=director).json()["distributions"]
    assert len({round(r["share_pct"], 2) for r in equal}) == 1, "均分应当人人相同"
    by_score = client.post(f"/api/fund/pools/{pool_id}/distribute",
                           json={"formula_expr": "score"},
                           headers=director).json()["distributions"]
    assert len(by_score) == len(equal)
    # 得分本就不同（见 business fixture），按绩效分的占比必须随之拉开
    assert len({round(r["share_pct"], 2) for r in by_score}) > 1, "按绩效分配却人人相同"
    # 得分高的分得多——这是"钱跟着考核走"的最低要求
    ranked = sorted(by_score, key=lambda r: -r["score"])
    assert ranked[0]["amount"] >= ranked[-1]["amount"]


def test_重新分配覆盖而非叠加(client, director, settled):
    pool_id = settled["pool"]["id"]
    for _ in range(3):
        client.post(f"/api/fund/pools/{pool_id}/distribute",
                    json={"formula_expr": "1"}, headers=director)
    rows = client.get(f"/api/fund/pools/{pool_id}/distributions", headers=director).json()
    org_ids = [r["org_id"] for r in rows]
    assert len(org_ids) == len(set(org_ids)), "重复分配叠加出了重复明细"


def test_分配依据是冻结快照_事后调权不影响已分结果(client, admin, director, settled):
    """这是本模块最要紧的一条：分完钱不能还能改分。"""
    pool_id = settled["pool"]["id"]
    client.post(f"/api/fund/pools/{pool_id}/distribute",
                json={"formula_expr": "score"}, headers=director)
    before = client.get(f"/api/fund/pools/{pool_id}/distributions", headers=director).json()
    frozen = {r["org_id"]: r["score"] for r in before}

    # 事后把某个绩效指标权重改掉——现算的得分会变
    indicators = client.get("/api/performance/indicators", headers=admin).json()
    key = indicators[0]["key"]
    client.patch(f"/api/performance/indicators/{key}",
                 json={"weight": indicators[0]["weight"] + 20}, headers=admin)

    after = client.get(f"/api/fund/pools/{pool_id}/distributions", headers=director).json()
    assert {r["org_id"]: r["score"] for r in after} == frozen, "调权回溯改写了已分配的得分快照"

    # 反证：重新分配一次，得分应当变成调权后的新值——说明冻结的是"那一次"，
    # 而不是"得分永远不变"。否则上面的断言可能只是因为得分根本没动。
    client.post(f"/api/fund/pools/{pool_id}/distribute",
                json={"formula_expr": "score"}, headers=director)
    redone = client.get(f"/api/fund/pools/{pool_id}/distributions", headers=director).json()
    assert {r["org_id"]: r["score"] for r in redone} != frozen, (
        "调权后重新分配得分仍未变化，本用例无法证明快照机制有效"
    )


def test_超支不自动扣减且拒绝分配(client, director):
    """没有结余可分时该做的是处置超支，而不是分一笔不存在的钱。"""
    pool = _pool(client, director, 2037, total=100000.0)
    client.post(f"/api/fund/pools/{pool['id']}/periods",
                json={"period": "2037-06", "actual_amount": 150000}, headers=director)
    s = client.post(f"/api/fund/pools/{pool['id']}/settle",
                    json={"overrun_action": "carry"}, headers=director).json()
    assert s["balance"] == -50000 and s["is_overrun"] is True
    assert s["overrun_action_name"] == "挂账结转"
    assert "不自动扣减" in s["caliber"]["overrun"]

    resp = client.post(f"/api/fund/pools/{pool['id']}/distribute",
                       json={"formula_expr": "score"}, headers=director)
    assert resp.status_code == 409
    assert "挂账结转" in resp.json()["detail"]
    # 超支不得产生任何分配明细（即"自动扣减"）
    assert client.get(f"/api/fund/pools/{pool['id']}/distributions",
                      headers=director).json() == []


def test_未清算不可分配(client, director):
    pool = _pool(client, director, 2038)
    resp = client.post(f"/api/fund/pools/{pool['id']}/distribute",
                       json={"formula_expr": "score"}, headers=director)
    assert resp.status_code == 409


def test_非法公式与未知变量被拒(client, director, settled):
    pool_id = settled["pool"]["id"]
    for expr in ("__import__('os')", "score + unknown_var", "score +"):
        resp = client.post(f"/api/fund/pools/{pool_id}/distribute",
                           json={"formula_expr": expr}, headers=director)
        assert resp.status_code == 422, expr


def test_公式变量清单与示例可查(client, director):
    data = client.get("/api/fund/formula-variables", headers=director).json()
    names = {v["name"] for v in data["variables"]}
    assert names == {"score", "rank", "org_count"}
    assert "份额权重" in data["note"]


def test_池子绑定分组后只在组内分配(client, admin, director, orgs):
    """池子绑到分组，分配范围就跟着分组走——按片区分池的县据此工作。"""
    group = client.post("/api/org-groups",
                        json={"name": "基金片区", "group_type": "zone"}, headers=admin).json()
    for org in orgs[:2]:
        client.post(f"/api/org-groups/{group['id']}/members",
                    json={"org_id": org["id"]}, headers=admin)
    pool = _pool(client, director, 2039, total=90000.0, group_id=group["id"])
    client.post(f"/api/fund/pools/{pool['id']}/periods",
                json={"period": "2039-06", "actual_amount": 60000}, headers=director)
    client.post(f"/api/fund/pools/{pool['id']}/settle", json={}, headers=director)
    rows = client.post(f"/api/fund/pools/{pool['id']}/distribute",
                       json={"formula_expr": "1"}, headers=director).json()["distributions"]
    assert {r["org_id"] for r in rows} == {orgs[0]["id"], orgs[1]["id"]}


def test_已清算的池不可再改(client, director, settled):
    resp = client.patch(f"/api/fund/pools/{settled['pool']['id']}",
                        json={"total_amount": 999}, headers=director)
    assert resp.status_code == 409


def test_结余无法整除时分配额仍分毫等于结余(client, admin, director, orgs):
    """最大余数法（Hamilton）：逐户各自 round 会丢/多几分，合计对不上结余。
    实测 100 元 3 户均分分出 99.99——分的是医共体真金白银的结余，
    账对不上就是事故。这里用故意除不尽的结余把它钉死。"""
    grp = client.post("/api/org-groups", json={"name": "整除测试片区", "group_type": "alliance"},
                      headers=admin).json()
    # 用既有三家机构入组（建组/加成员是 require_admin）
    for o in orgs:
        client.post(f"/api/org-groups/{grp['id']}/members", json={"org_id": o["id"]},
                    headers=admin)
    for yr, balance in enumerate((100.00, 100.01, 0.05, 1000.00), start=2030):
        pool = client.post(
            "/api/fund/pools",
            json={"year": yr, "insurance_type": "resident",
                  "org_group_id": grp["id"], "total_amount": balance},
            headers=director,
        ).json()
        pid = pool["id"]
        client.post(f"/api/fund/pools/{pid}/settle",
                    json={"total_expense": 0.0, "overrun_action": "none"}, headers=director)
        d = client.post(f"/api/fund/pools/{pid}/distribute",
                        json={"formula_expr": "1"}, headers=director)
        assert d.status_code == 200, d.text
        rows = client.get(f"/api/fund/pools/{pid}/distributions", headers=director).json()
        got = round(sum(r["amount"] for r in rows), 2)
        assert got == round(balance, 2), f"结余 {balance} 分出 {got}，差 {round(balance-got,2)}"
