"""基金结余分配：份额权重先被舍到 4 位小数再归一化，成比例的两个公式分出不同的钱（P2-1479，第四十三批扫描 AG3-2）。

口径（`fund.formula_variables` 的 note）：「公式返回份额权重（非金额），平台按各机构权重占比归一化后乘结余额」——归一化之后
与量纲无关，`score ** 2` 与 `(score / 100) ** 2`、`score` 与 `score / 10000` 该分出分毫不差的钱。修前 `distribute` 拿
`formula.evaluate` 的结果作权重，而它返回 `round(value, 4)`。扫描实测（结余 925000，甲镇 13.3 分、乙镇 6.7 分）：`score`
分 615125 / 309875，`score / 10000` 的权重舍成 0.0013 / 0.0007、分 601250 / 323750（13875 元挪到乙镇）；`score ** 2`
分 737772.79，`(score / 100) ** 2` 分 737500；`(score / 100) ** 6` 两家都舍成 0，报 422「…常见原因是本期绩效得分普遍为 0」，
实际得分是 13.3 和 6.7。

修法：`formula.py` 加不舍入的入口 `evaluate_raw`（与 `evaluate` 共用解析与有限性检查，`evaluate` 照旧取 4 位小数、别的
调用方不受影响），分配改用它，只在落库快照时照原有 6 位舍入；权重全为 0 的 422 分开说「得分全为 0」与「公式结果全部 ≤ 0
（或过小）」。
"""
import pytest

from app import formula
from app.clock import now_naive
from conftest import login

BALANCE = 925000


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21479 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    towns = [client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
        for name in ("P21479 甲镇卫生院", "P21479 乙镇卫生院")]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21479_dir", "password": "passw0rd1", "role": "director", "org_id": county})
    assert created.status_code == 201, created.text
    director = login(client, "p21479_dir", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21479 患者", "id_card": "330102197001011479"}).json()["id"]
    # 甲镇 3 张上转结案 2 张、乙镇 3 张结案 1 张 → 结案率 2/3 与 1/3，得分 13.3 与 6.7（县医院 0 分）
    for town, done in zip(towns, (2, 1)):
        for i in range(3):
            ref = client.post("/api/referrals", headers=admin, json={
                "patient_id": patient, "from_org_id": town, "to_org_id": county, "direction": "up", "reason": "P21479"})
            assert ref.status_code == 201, ref.text
            if i < done:
                for status in ("accepted", "completed"):
                    moved = client.patch(f"/api/referrals/{ref.json()['id']}/status", headers=admin,
                                         json={"status": status})
                    assert moved.status_code == 200, moved.text
    year = now_naive().year   # 分配按池子年度取分，业务数据都落在今年
    pool = client.post("/api/fund/pools", headers=director, json={
        "year": year, "insurance_type": "resident", "total_amount": 1000000, "prepay_ratio_pct": 0}).json()["id"]
    client.post(f"/api/fund/pools/{pool}/periods", headers=director, json={"period": f"{year}-06", "actual_amount": 75000})
    settled = client.post(f"/api/fund/pools/{pool}/settle", headers=director, json={})
    assert settled.status_code == 201 and settled.json()["balance"] == BALANCE, settled.text
    return {"admin": admin, "director": director, "county": county, "towns": towns, "pool": pool, "year": year}


def _distribute(client, world, expr: str) -> dict[int, dict]:
    resp = client.post(f"/api/fund/pools/{world['pool']}/distribute", headers=world["director"],
                       json={"formula_expr": expr})
    assert resp.status_code == 200, (expr, resp.status_code, resp.text)
    assert resp.json()["distributed_amount"] == BALANCE
    return {d["org_id"]: d for d in resp.json()["distributions"]}


def _amounts(rows: dict[int, dict]) -> dict[int, float]:
    return {org: d["amount"] for org, d in rows.items()}


def test_得分前提(client, world):
    rows = _distribute(client, world, "score")
    town_a, town_b = world["towns"]
    assert (rows[town_a]["score"], rows[town_b]["score"], rows[world["county"]]["score"]) == (13.3, 6.7, 0)
    assert (rows[town_a]["amount"], rows[town_b]["amount"]) == (615125, 309875)


@pytest.mark.parametrize(("expr", "same_as"), [
    ("score / 10000", "score"),                # 修前 601250 / 323750：权重舍成 0.0013 / 0.0007
    ("(score / 100) ** 2", "score ** 2"),      # 修前 737500 / 187500：权重舍成 0.0177 / 0.0045
    ("(score / 100) ** 6", "score ** 6"),      # 修前两家都舍成 0，422「…本期绩效得分普遍为 0」
])
def test_成比例的公式分出分毫不差的钱(client, world, expr, same_as):
    expected = _amounts(_distribute(client, world, same_as))
    assert _amounts(_distribute(client, world, expr)) == expected


def test_快照里的权重照原有6位小数舍入_分钱用的是不舍入的权重(client, world):
    town_a, town_b = world["towns"]
    for expr, weights in (("score / 10000", (0.00133, 0.00067)),             # 修前 0.0013 / 0.0007
                          ("(score / 100) ** 2", (0.017689, 0.004489)),      # 修前 0.0177 / 0.0045
                          ("score ** 6", (5534900.853769, 90458.382169))):   # 修前 5534900.8538 / 90458.3822
        rows = _distribute(client, world, expr)
        assert (rows[town_a]["weight"], rows[town_b]["weight"]) == weights, expr
        snapshot = client.get(f"/api/fund/pools/{world['pool']}/distributions", headers=world["director"]).json()
        assert {d["org_id"]: d["weight"] for d in snapshot} == {org: d["weight"] for org, d in rows.items()}
        assert all(d["weight"] == round(d["weight"], 6) for d in snapshot)   # 落库照旧 6 位
    rows = _distribute(client, world, "score ** 2")
    assert (rows[town_a]["share_pct"], rows[town_b]["share_pct"]) == (79.7592, 20.2408)


def test_权重全为0_得分不全为0时说是公式的毛病(client, world):
    resp = client.post(f"/api/fund/pools/{world['pool']}/distribute", headers=world["director"],
                       json={"formula_expr": "score - 50"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    # 修前一律「常见原因是本期绩效得分普遍为 0」——实际得分是 13.3 与 6.7
    assert detail == ("所有机构的份额权重都为 0，无法归一化分配：本期绩效得分并非全为 0，"
                      "是分配公式对各机构算出的结果全部 ≤ 0（或过小），请检查公式")


def test_权重全为0_得分全为0时照旧提示改用与得分无关的公式(client, world):
    admin, director = world["admin"], world["director"]
    group = client.post("/api/org-groups", headers=admin, json={"name": "P21479 只有县医院", "group_type": "zone"})
    assert group.status_code == 201, group.text
    client.post(f"/api/org-groups/{group.json()['id']}/members", headers=admin, json={"org_id": world["county"]})
    pool = client.post("/api/fund/pools", headers=director, json={
        "year": world["year"], "insurance_type": "resident", "org_group_id": group.json()["id"],
        "total_amount": 1000})
    assert pool.status_code == 201, pool.text
    client.post(f"/api/fund/pools/{pool.json()['id']}/settle", headers=director, json={"total_expense": 0})
    resp = client.post(f"/api/fund/pools/{pool.json()['id']}/distribute", headers=director,
                       json={"formula_expr": "score"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == ("所有机构的份额权重都为 0，无法归一化分配。"
                                     "常见原因是本期绩效得分普遍为 0（业务数据尚未产生），"
                                     "此时应改用与得分无关的公式（如均分写 1）")


def test_evaluate_raw不舍入_与evaluate共用解析与有限性检查_evaluate照旧取4位():
    variables = {"score": 13.3}
    assert formula.evaluate_raw("score / 10000", variables) == 13.3 / 10000
    assert formula.evaluate("score / 10000", variables) == 0.0013          # 别的调用方不受影响
    assert formula.evaluate("(score / 100) ** 6", variables) == 0.0
    assert formula.evaluate_raw("(score / 100) ** 6", variables) == pytest.approx(0.133 ** 6, rel=1e-12)
    for expr, message in (("score * 1e308 * 10", "超出数值范围"), ("score +", "语法错误"), ("nope", "未知变量")):
        with pytest.raises(formula.FormulaError, match=message):
            formula.evaluate_raw(expr, variables)
