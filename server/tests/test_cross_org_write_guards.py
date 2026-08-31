"""三个跨机构写接口的归属校验（上线前审计）。

三处共同的形状：`require_roles(...)` 是**认证**不是**授权**，而它们又都是
`/{id}` 型写接口——从 id 直取对象、跳过清单，于是清单侧的机构过滤形同虚设。
`visibility.assert_obj_org_writable` 的 docstring 早就把这类洞叫作
「按 id 绕过清单」，只是这三处没接上。

| 接口 | 加载的对象 | 归属在哪 | 原有守卫 |
|---|---|---|---|
| 停医嘱 | `InpatientOrder` | `admissions.org_id`（隔一跳） | 仅 `require_roles("doctor")` |
| 修订报告 | `ExamReport` | `exam_requests.patient_id`（隔一跳） | 仅 `require_roles("doctor")` |
| 支付退款 | `PaymentOrder` | `settlements.org_id`（隔一跳） | 仅 `require_roles("operator")` |

**为什么既有的横向越权闸门没报出它们**：`test_stage15_horizontal.py` 的判据只认
「被 `db.get(M, ...)` 直取、且 `M` 自己带 `org_id` 列」这一种形状——**归属隔一跳
外键的对象，它一个都看不见**。那份自证输出报的 95.5% 是真的，只是分母漏了一族。
所以这三条用例不是"补个测试"，是补上闸门结构上看不到的那部分。

后果各不相同，都不轻：任一成员单位的医师顺序遍历 `order_id` 就能停掉别家医院
任何一条**在用医嘱**（临床安全）；遍历 `report_id` 就能改别家的报告结论并把
**危急值闭环状态复位**；任一 `operator` 遍历 `order_id` 就能对别家的支付单
**发起退款**（钱）。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client):
    """甲院有业务、乙院与这些业务**永远无关**——拒绝断言必须找状态不会变的人。

    这条纪律抄自 `test_stage15_horizontal.py` 的 `stranger` fixture：它踩过的坑是
    "乙院在别的用例里拿到了转诊关系，后面的拒绝断言跟着变成通过"。
    """
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name, otype, level in (
        ("a", "跨机构甲县医院", "lead_hospital", "county"),
        ("b", "跨机构乙卫生院", "township", "township"),
    ):
        orgs[key] = client.post(
            "/api/organizations",
            json={"name": name, "org_type": otype, "level": level},
            headers=admin,
        ).json()
    for username, role, org in (
        ("xo_doc_a", "doctor", orgs["a"]), ("xo_op_a", "operator", orgs["a"]),
        ("xo_doc_b", "doctor", orgs["b"]), ("xo_op_b", "operator", orgs["b"]),
    ):
        client.post(
            "/api/users",
            json={"username": username, "password": "pw123456", "full_name": username,
                  "role": role, "org_id": org["id"]},
            headers=admin,
        )
    patient = client.post(
        "/api/patients",
        json={"name": "跨机构患者", "id_card": "330400199001011234"},
        headers=admin,
    ).json()
    return {
        "admin": admin, "orgs": orgs, "patient": patient,
        "doc_a": _login(client, "xo_doc_a"), "op_a": _login(client, "xo_op_a"),
        "doc_b": _login(client, "xo_doc_b"), "op_b": _login(client, "xo_op_b"),
    }


# ---------------------------------------------------------------- 停医嘱


@pytest.fixture(scope="module")
def order_in_org_a(client, world):
    """甲院的一条在用医嘱。"""
    ward = client.post(
        "/api/inpatient/wards",
        json={"org_id": world["orgs"]["a"]["id"], "name": "跨机构甲院内科病区"},
        headers=world["admin"],
    ).json()
    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": ward["id"], "bed_no": "XO-01"},
        headers=world["admin"],
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": world["patient"]["id"], "org_id": world["orgs"]["a"]["id"],
              "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "肺炎"},
        headers=world["doc_a"],
    )
    assert admission.status_code in (200, 201), admission.text
    order = client.post(
        "/api/inpatient/orders",
        json={"admission_id": admission.json()["id"], "order_type": "long",
              "content": "头孢 1g ivgtt qd"},
        headers=world["doc_a"],
    )
    assert order.status_code in (200, 201), order.text
    return order.json()


def test_别家医师不得停用本院医嘱(client, world, order_in_org_a):
    """遍历 order_id 就能停掉别家医院任何一条在用医嘱——这是临床安全问题。"""
    resp = client.post(
        f"/api/inpatient/orders/{order_in_org_a['id']}/stop", headers=world["doc_b"]
    )
    assert resp.status_code == 403, resp.text


def test_本院医师照常停用本院医嘱(client, world, order_in_org_a):
    """防误伤：守卫收得太紧会把正常临床操作拦住，那比不管更糟。

    放在拒绝用例之后：先证明别家停不掉，再证明自家停得掉——顺序反过来的话，
    医嘱已经是 stopped，拒绝那条会分不清是 403 还是 409。
    """
    resp = client.post(
        f"/api/inpatient/orders/{order_in_org_a['id']}/stop", headers=world["doc_a"]
    )
    assert resp.status_code == 200, resp.text


def test_归属判定排在状态机之前(client, world, order_in_org_a):
    """医嘱此刻已是 stopped。别家再来停，必须仍拿 403 而不是 409。

    否则 409 与 403 的差别就成了一个**旁路**：外人能靠状态码探出别家医嘱
    现在是不是在用。
    """
    resp = client.post(
        f"/api/inpatient/orders/{order_in_org_a['id']}/stop", headers=world["doc_b"]
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------- 修订报告


@pytest.fixture(scope="module")
def report_in_org_a(client, world):
    req = client.post(
        "/api/exams",
        json={"patient_id": world["patient"]["id"], "from_org_id": world["orgs"]["a"]["id"],
              "center_type": "lab", "item_code": "LAB001", "item_name": "血常规"},
        headers=world["doc_a"],
    )
    assert req.status_code in (200, 201), req.text
    rep = client.post(
        f"/api/exams/{req.json()['id']}/report",
        json={"findings": "白细胞升高", "conclusion": "细菌感染可能", "critical": False},
        headers=world["doc_a"],
    )
    assert rep.status_code in (200, 201), rep.text
    return rep.json()


def test_别家医师不得修订本院报告(client, world, report_in_org_a):
    """遍历 report_id 就能改别家的报告结论，并把危急值闭环状态复位。"""
    resp = client.patch(
        f"/api/exams/reports/{report_in_org_a['id']}",
        json={"conclusion": "被别家改写的结论"},
        headers=world["doc_b"],
    )
    assert resp.status_code == 403, resp.text


def test_本院医师照常修订本院报告(client, world, report_in_org_a):
    resp = client.patch(
        f"/api/exams/reports/{report_in_org_a['id']}",
        json={"conclusion": "复核后：细菌感染"},
        headers=world["doc_a"],
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 支付退款


@pytest.fixture(scope="module")
def paid_order_in_org_a(client, world):
    client.post(
        "/api/billing/charge-items",
        json={"code": "XO-REG", "name": "跨机构诊查费", "category": "treatment", "price": 100},
        headers=world["admin"],
    )
    encounter = client.post(
        "/api/encounters",
        json={"patient_id": world["patient"]["id"], "org_id": world["orgs"]["a"]["id"],
              "diagnosis_name": "门诊"},
        headers=world["doc_a"],
    ).json()
    client.post(
        "/api/billing/details",
        json={"patient_id": world["patient"]["id"], "encounter_id": encounter["id"],
              "item_code": "XO-REG", "quantity": 1},
        headers=world["op_a"],
    )
    settlement = client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": encounter["id"], "insurance_pay": 0},
        headers=world["op_a"],
    )
    assert settlement.status_code in (200, 201), settlement.text
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement.json()["id"], "channel": "cash"},
        headers=world["op_a"],
    )
    assert order.status_code in (200, 201), order.text
    assert order.json()["status"] == "paid"
    return order.json()


def test_别家经办不得对本院支付单退款(client, world, paid_order_in_org_a):
    """这条动的是钱：遍历 order_id 就能对别家机构的支付单发起退款。

    原实现连 `user` 参数都没有——没有任何一处能知道"谁在退"。
    """
    resp = client.post(
        f"/api/billing/payments/{paid_order_in_org_a['id']}/refund",
        json={"amount": 10, "reason": "越权尝试"},
        headers=world["op_b"],
    )
    assert resp.status_code == 403, resp.text


def test_本院经办照常退款(client, world, paid_order_in_org_a):
    resp = client.post(
        f"/api/billing/payments/{paid_order_in_org_a['id']}/refund",
        json={"amount": 10, "reason": "患者取消"},
        headers=world["op_a"],
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 防空转


def test_守卫本身没瞎(client, world, order_in_org_a, report_in_org_a, paid_order_in_org_a):
    """三个夹具必须真的造出了甲院的对象，否则上面的 403 可能只是 404 的假象。

    这条是必要的：`assert_obj_org_writable` 对 `obj is None` **直接放行**
    （注释写着"那是 404，各接口自己先判"）。夹具要是没造出对象，
    拒绝断言就会在一堆 404 上"通过"，而 404 != 403。
    """
    assert order_in_org_a["id"] and report_in_org_a["id"] and paid_order_in_org_a["id"]
    # 乙院医师拿一个**不存在**的 id：应当是 404，不是 403——
    # 说明 403 确实来自归属判定，而不是"什么都拒绝"。
    resp = client.post("/api/inpatient/orders/999999/stop", headers=world["doc_b"])
    assert resp.status_code == 404, resp.text
