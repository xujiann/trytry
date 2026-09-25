"""成本分摊：同一来源科室的分摊比例合计超过 100%，来源科室的总成本就成了负数（P1-116）。

`CostAllocationRule` 的 docstring 写着「同一来源科室的比例之和应为 100；不强制校验为 100，因为分期建规则时
中间态必然不足 100」——不足 100 是设计过的（汇总里单列「未分摊」），**超过 100 从来没人挡**：建规则只看单条
`0 < ratio_pct <= 100`。后勤科给内科 60%、再给外科 60%，两条都 201；科室成本汇总按
「直接成本 + 转入 − 转出」算，后勤科总成本成了 −20%，内外科合计拿走了后勤科 120% 的成本。

2026-09-25 实测（修前代码）：后勤直接成本 1000，两条 60% 规则都 201，汇总里后勤总成本 −200.0。

又因为规则只能建、不能改也不能删，建错一条（比例或目标科室填错）就永远挂在那里，每一期都照它分摊。

修法：建规则时按来源科室把现有比例加起来，合计超过 100 即 422（带「已分出多少、最多还能分多少」）；判定与
写入圈在来源科室这一行的临界区里（`serialized_on`），两路并发各建一条 60% 不会都过。补「改比例」「删规则」
两个入口（同一道合计校验，改比例时不算自己那条），页面上每条规则给「改比例」「删除」（删除先确认）。
"""
import pytest

B = "/api/cost"


@pytest.fixture(scope="module")
def depts(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P1116 医院", "org_type": "lead_hospital", "level": "county"}).json()

    def dept(code, name, category="clinical"):
        resp = client.post("/api/mgmt/departments", headers=admin,
                           json={"org_id": org["id"], "code": code, "name": name, "category": category})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    ids = {"hq": dept("P1116HQ", "P1116 后勤", "admin"), "nk": dept("P1116NK", "P1116 内科"),
           "wk": dept("P1116WK", "P1116 外科"), "ek": dept("P1116EK", "P1116 儿科")}
    cost = client.post(f"{B}/departments", headers=admin,
                       json={"dept_id": ids["hq"], "period": "2026-08", "cost_type": "overhead", "amount": 1000})
    assert cost.status_code == 201, cost.text
    return ids


def _rule(client, admin, src, dst, ratio):
    return client.post(f"{B}/allocation-rules", headers=admin,
                       json={"from_dept_id": src, "to_dept_id": dst, "ratio_pct": ratio})


def _summary(client, admin, dept_id):
    rows = client.get(f"{B}/departments", headers=admin, params={"period": "2026-08"}).json()
    return next(r for r in rows if r["dept_id"] == dept_id)


def test_同一来源科室的比例合计超过100即422_总成本不再变负(client, admin, depts):
    first = _rule(client, admin, depts["hq"], depts["nk"], 60)
    assert first.status_code == 201, first.text
    over = _rule(client, admin, depts["hq"], depts["wk"], 60)
    assert over.status_code == 422, over.text   # 修前 201，后勤总成本 −200
    assert "已分出 60%" in over.json()["detail"] and "最多还能分 40%" in over.json()["detail"]
    hq = _summary(client, admin, depts["hq"])
    assert hq["allocated_out"] == 600 and hq["total_cost"] == 400
    assert hq["unallocated_ratio_amount"] == 400   # 不足 100 仍按设计单列「未分摊」


def test_合计恰好100可建_浮点误差不误拦(client, admin, depts):
    # 33.3 + 33.3 + 33.4 的浮点和是 100.00000000000001 一类的数，不能被当成超过 100
    rest = _rule(client, admin, depts["hq"], depts["wk"], 33.3)
    assert rest.status_code == 201, rest.text
    rules = client.get(f"{B}/allocation-rules", headers=admin).json()
    nk_rule = next(r for r in rules if r["from_dept_id"] == depts["hq"] and r["to_dept_id"] == depts["nk"])
    moved = client.patch(f"{B}/allocation-rules/{nk_rule['id']}", headers=admin, json={"ratio_pct": 33.3})
    assert moved.status_code == 200, moved.text
    assert moved.json()["ratio_pct"] == 33.3
    last = _rule(client, admin, depts["hq"], depts["ek"], 33.4)
    assert last.status_code == 201, last.text
    hq = _summary(client, admin, depts["hq"])
    assert hq["total_cost"] == 0 and hq["unallocated_ratio_amount"] == 0


def test_改比例同样不许超过100_不算自己那条(client, admin, depts):
    rules = client.get(f"{B}/allocation-rules", headers=admin).json()
    nk_rule = next(r for r in rules if r["from_dept_id"] == depts["hq"] and r["to_dept_id"] == depts["nk"])
    over = client.patch(f"{B}/allocation-rules/{nk_rule['id']}", headers=admin, json={"ratio_pct": 40})
    assert over.status_code == 422, over.text   # 33.3 + 33.4 + 40 > 100
    same = client.patch(f"{B}/allocation-rules/{nk_rule['id']}", headers=admin, json={"ratio_pct": 33.3})
    assert same.status_code == 200, same.text   # 改成原值：自己那条不重复计入
    assert client.patch(f"{B}/allocation-rules/999999", headers=admin, json={"ratio_pct": 1}).status_code == 404
    bad = client.patch(f"{B}/allocation-rules/{nk_rule['id']}", headers=admin, json={"ratio_pct": 0})
    assert bad.status_code == 422, bad.text     # 与建规则同一条单条约束


def test_删规则后腾出比例_建错的规则改得掉(client, admin, depts):
    rules = client.get(f"{B}/allocation-rules", headers=admin).json()
    ek_rule = next(r for r in rules if r["from_dept_id"] == depts["hq"] and r["to_dept_id"] == depts["ek"])
    gone = client.delete(f"{B}/allocation-rules/{ek_rule['id']}", headers=admin)
    assert gone.status_code == 200, gone.text
    assert gone.json() == {"id": ek_rule["id"], "deleted": True}
    assert all(r["id"] != ek_rule["id"] for r in client.get(f"{B}/allocation-rules", headers=admin).json())
    assert client.delete(f"{B}/allocation-rules/{ek_rule['id']}", headers=admin).status_code == 404
    again = _rule(client, admin, depts["hq"], depts["ek"], 33.4)
    assert again.status_code == 201, again.text


def test_改删规则只有全域角色够得着(client, admin, depts):
    """两个新端点在横向越权闸门里判为豁免（`BYID_CROSS_ORG_OK`）的**前提**：与建规则同挂
    `require_roles("director")`，director 属 `GLOBAL_ROLES`，机构写守卫对全域角色恒放行。
    角色哪天放宽到非全域角色，这条就红，豁免要重新判。"""
    from app.visibility import GLOBAL_ROLES
    from conftest import login

    assert "director" in GLOBAL_ROLES
    rules = client.get(f"{B}/allocation-rules", headers=admin).json()
    rule_id = next(r["id"] for r in rules if r["from_dept_id"] == depts["hq"])
    for role in ("doctor", "operator", "public_health", "pharmacist"):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p1116_{role}", "password": "Passw0rd!", "full_name": f"P1116 {role}", "role": role})
        assert created.status_code == 201, created.text
        headers = login(client, f"p1116_{role}", "Passw0rd!")
        assert client.patch(f"{B}/allocation-rules/{rule_id}", headers=headers, json={"ratio_pct": 1}).status_code == 403
        assert client.delete(f"{B}/allocation-rules/{rule_id}", headers=headers).status_code == 403
