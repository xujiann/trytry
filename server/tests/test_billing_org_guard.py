"""钱那一族的机构归属校验：五条 billing 写端点（2026-09-18 实测取证后补，P1-60 第一批）。

## 实测取证（修之前，乙院 operator 对甲院的一次住院）

    POST /api/billing/details          → 201  往甲院这次住院里记一笔计费明细
    POST /api/billing/deposits         → 201  收了 5000 押金（记在甲院账上）
    POST /api/billing/deposits/refund  → 201  又把其中 4000 退走
    POST /api/billing/settlements      → 201  出院结算单，单子的 org_id 是**甲院**
    POST /api/billing/payments         → 201  对甲院的结算单收款

**一条完整的跨机构资金链**：进账、出账、结算、收款，全程由别家机构的经办完成。
`visibility.assert_org_writable` 的 docstring 早就写着"集中核算的数字要是能被任何
成员机构写进别家账本，汇总就没有意义了"——这里比记一笔支出更进一步，是真金白银。

## 为什么五条都够得着

五条的角色门都是 `require_roles("operator")`（计费那条另加 doctor），
而 `operator` **不在** `visibility.GLOBAL_ROLES` 里。所以不同于
`admin_mgmt.create_payroll`（只有全域角色够得着，那条补的是纵深防御），
这五条是实打实可越权的。

## 归属从哪里取

- 计费明细：住院走 `Admission.org_id`、门诊走 `Encounter.org_id`（费用记在哪家的账上由它定）
- 押金收 / 退：`Admission.org_id`
- 结算：函数上方两支已算出的 `org_id`（住院/门诊各自带出），守在**进临界区之前**
- 收款：`Settlement.org_id`

## 为什么没有任何闸门看见它们

五条的 id 都从 body 收（`admission_id` / `settlement_id`），而写侧越权判据只看
路径带 `{}` 的端点——见 `tests/test_body_id_org_write_guard.py` 的模块 docstring。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(client, username, password="pw123456"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def money(client, admin):
    a = client.post("/api/organizations",
                    json={"name": "钱甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "钱乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("money_op_a", a), ("money_op_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "operator", "org_id": org["id"]},
                    headers=admin)
    ward = client.post("/api/inpatient/wards",
                       json={"org_id": a["id"], "name": "钱甲院一病区"}, headers=admin).json()
    client.post("/api/billing/charge-items",
                json={"code": "MG-01", "name": "床位费", "category": "treatment", "price": 100.0},
                headers=admin)
    return {"admin": admin, "a": a, "b": b, "ward": ward,
            "op_a": _login(client, "money_op_a"), "op_b": _login(client, "money_op_b")}


_seq = [0]


def _admit(client, money):
    """每个用例自己造一次住院——钱的用例之间绝不能共用同一条住院记录。"""
    k = _seq[0]
    _seq[0] += 1
    patient = client.post("/api/patients",
                          json={"name": f"钱患者{k}", "id_card": f"3300001990010188{k:02d}"},
                          headers=money["admin"]).json()
    bed = client.post("/api/inpatient/beds",
                      json={"ward_id": money["ward"]["id"], "bed_no": f"M-{k:02d}"},
                      headers=money["admin"]).json()
    resp = client.post("/api/inpatient/admissions",
                       json={"patient_id": patient["id"], "ward_id": money["ward"]["id"],
                             "bed_id": bed["id"], "doctor_name": "王医师",
                             "diagnosis_name": "肺炎"},
                       headers=money["admin"])
    assert resp.status_code == 201, resp.text
    return patient, resp.json()


DENIED = {"detail": "无权以该机构名义写入数据"}


# ---------------------------------------------------------------- 越权反例（五条）


def test_别家operator不能往本院住院记一笔计费(client, money):
    patient, adm = _admit(client, money)
    resp = client.post("/api/billing/details", headers=money["op_b"],
                       json={"patient_id": patient["id"], "admission_id": adm["id"],
                             "item_code": "MG-01", "quantity": 3})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED
    # 没有半途落库
    got = client.get(f"/api/billing/details?patient_id={patient['id']}",
                     headers=money["admin"]).json()
    assert got == [] or all(g.get("admission_id") != adm["id"] for g in got)


def test_别家operator不能收本院住院的押金(client, money):
    _patient, adm = _admit(client, money)
    resp = client.post("/api/billing/deposits", headers=money["op_b"],
                       json={"admission_id": adm["id"], "amount": 5000, "method": "cash"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


def test_别家operator不能退本院住院的押金(client, money):
    """钱出去的那一头：先由本院正常收一笔，再由别家去退。"""
    _patient, adm = _admit(client, money)
    paid = client.post("/api/billing/deposits", headers=money["op_a"],
                       json={"admission_id": adm["id"], "amount": 5000, "method": "cash"})
    assert paid.status_code == 201, paid.text

    resp = client.post("/api/billing/deposits/refund", headers=money["op_b"],
                       json={"admission_id": adm["id"], "amount": 4000, "method": "cash"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED
    # 余额没被动过
    balance = client.get(f"/api/billing/deposits?admission_id={adm['id']}",
                         headers=money["admin"]).json()
    assert sum(1 for d in balance if d["deposit_type"] == "refund") == 0


def test_别家operator不能给本院住院出结算单(client, money):
    patient, adm = _admit(client, money)
    client.post("/api/billing/details", headers=money["op_a"],
                json={"patient_id": patient["id"], "admission_id": adm["id"],
                      "item_code": "MG-01", "quantity": 3})
    resp = client.post("/api/billing/settlements", headers=money["op_b"],
                       json={"bill_type": "inpatient", "admission_id": adm["id"]})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


def test_别家operator不能对本院的结算单收款(client, money):
    patient, adm = _admit(client, money)
    client.post("/api/billing/details", headers=money["op_a"],
                json={"patient_id": patient["id"], "admission_id": adm["id"],
                      "item_code": "MG-01", "quantity": 3})
    st = client.post("/api/billing/settlements", headers=money["op_a"],
                     json={"bill_type": "inpatient", "admission_id": adm["id"]})
    assert st.status_code == 201, st.text
    assert st.json()["org_id"] == money["a"]["id"]

    resp = client.post("/api/billing/payments", headers=money["op_b"],
                       json={"settlement_id": st.json()["id"], "channel": "cash"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


# ---------------------------------------------------------------- 正路没被堵死


def test_本院operator走完整条资金链(client, money):
    """反向证据：补守卫不得把合法路径挡掉。计费 → 收押金 → 退一部分 → 结算 → 收款。"""
    patient, adm = _admit(client, money)

    detail = client.post("/api/billing/details", headers=money["op_a"],
                         json={"patient_id": patient["id"], "admission_id": adm["id"],
                               "item_code": "MG-01", "quantity": 3})
    assert detail.status_code == 201, detail.text

    dep = client.post("/api/billing/deposits", headers=money["op_a"],
                      json={"admission_id": adm["id"], "amount": 5000, "method": "cash"})
    assert dep.status_code == 201, dep.text

    # 退到只剩 100：结算时押金冲抵 100、还要补缴 200，收款这一步才有钱可收。原先退 1000 剩 4000，自付 300 被押金
    # 全额冲抵，下面的收款照默认额又收了一遍 300——那正是 P1-142 修掉的「一笔自付收两次」
    ref = client.post("/api/billing/deposits/refund", headers=money["op_a"],
                      json={"admission_id": adm["id"], "amount": 4900, "method": "cash"})
    assert ref.status_code == 201, ref.text

    st = client.post("/api/billing/settlements", headers=money["op_a"],
                     json={"bill_type": "inpatient", "admission_id": adm["id"]})
    assert st.status_code == 201, st.text
    assert (st.json()["deposit_offset"], st.json()["payable_after_offset"]) == (100, 200)

    pay = client.post("/api/billing/payments", headers=money["op_a"],
                      json={"settlement_id": st.json()["id"], "channel": "cash"})
    assert pay.status_code == 201, pay.text
    assert pay.json()["amount"] == 200


def test_全域角色跨机构照旧放行_这是设计不是洞(client, money):
    """`GLOBAL_ROLES = {"admin", "director"}`：县级统筹本就跨机构，不该一起关掉。"""
    _patient, adm = _admit(client, money)
    resp = client.post("/api/billing/deposits", headers=money["admin"],
                       json={"admission_id": adm["id"], "amount": 100, "method": "cash"})
    assert resp.status_code == 201, resp.text
