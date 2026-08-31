"""阶段三：会计凭证（复式记账）、成本核算、物资采购、高值耗材追溯。"""
import pytest


def _login(client, username, password="passw0rd1"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations", json={"name": "核算演示县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def roles(client, admin, org):
    """第九轮：业务账号一律挂机构。原先不挂——那时机构归属不影响任何判定；
    现在写接口按"只能以本机构名义写"校验，无归属的经办账号写任何机构都会 403，
    这正是守卫该有的行为，所以是修 fixture 而不是放宽守卫。"""
    for username, role in [("acc_dir", "director"), ("acc_dir2", "director"),
                           ("acc_op", "operator"), ("acc_doc", "doctor")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "passw0rd1", "full_name": username,
                  "role": role, "org_id": org["id"]},
            headers=admin,
        )
    return {k: _login(client, v) for k, v in
            {"director": "acc_dir", "director2": "acc_dir2", "operator": "acc_op", "doctor": "acc_doc"}.items()}


# ---------------------------------------------------------------- 会计科目与凭证


def test_subjects_seeded(client, admin):
    subjects = client.get("/api/accounting/subjects", headers=admin).json()
    codes = {s["code"] for s in subjects}
    assert {"1001", "4001", "5001"} <= codes
    income = client.get("/api/accounting/subjects?category=income", headers=admin).json()
    assert all(s["category"] == "income" for s in income)
    # 余额方向：收入类贷增
    assert next(s for s in subjects if s["code"] == "4001")["direction"] == "credit"


def test_voucher_requires_balance(client, roles, org):
    unbalanced = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-001", "voucher_date": "2026-08-01",
              "summary": "借贷不平测试",
              "entries": [{"subject_code": "1002", "debit": 1000},
                          {"subject_code": "4001", "credit": 900}]},
        headers=roles["director"],
    )
    assert unbalanced.status_code == 422
    assert "借贷不平" in unbalanced.json()["detail"]


def test_voucher_entry_must_be_single_sided(client, roles, org):
    both = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-002", "voucher_date": "2026-08-01",
              "entries": [{"subject_code": "1002", "debit": 500, "credit": 500},
                          {"subject_code": "4001", "credit": 500}]},
        headers=roles["director"],
    )
    assert both.status_code == 422
    assert "只能填列借方或贷方之一" in both.json()["detail"]

    neither = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-003", "voucher_date": "2026-08-01",
              "entries": [{"subject_code": "1002"}, {"subject_code": "4001"}]},
        headers=roles["director"],
    )
    assert neither.status_code == 422


def test_voucher_rejects_unknown_subject(client, roles, org):
    resp = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-004", "voucher_date": "2026-08-01",
              "entries": [{"subject_code": "9999", "debit": 100},
                          {"subject_code": "4001", "credit": 100}]},
        headers=roles["director"],
    )
    assert resp.status_code == 422
    assert "9999" in resp.json()["detail"]


@pytest.fixture(scope="module")
def voucher(client, roles, org):
    resp = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-100", "voucher_date": "2026-08-05",
              "summary": "收取门诊医疗款",
              "entries": [{"subject_code": "1002", "summary": "存入银行", "debit": 12000},
                          {"subject_code": "4001", "summary": "医疗收入", "credit": 12000}]},
        headers=roles["director"],
    )
    assert resp.status_code == 201
    return resp.json()


def test_voucher_created_with_totals_and_period(client, admin, voucher):
    assert voucher["total_debit"] == voucher["total_credit"] == 12000
    assert voucher["period"] == "2026-08"  # 未传时从凭证日期推导
    assert voucher["status"] == "draft"
    detail = client.get(f"/api/accounting/vouchers/{voucher['id']}", headers=admin).json()
    assert len(detail["entries"]) == 2


def test_duplicate_voucher_no_rejected(client, roles, org, voucher):
    resp = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-100", "voucher_date": "2026-08-06",
              "entries": [{"subject_code": "1002", "debit": 1}, {"subject_code": "4001", "credit": 1}]},
        headers=roles["director"],
    )
    assert resp.status_code == 409


def test_draft_excluded_from_trial_balance_until_posted(client, admin, roles, org, voucher):
    before = client.get(
        f"/api/accounting/trial-balance?period=2026-08&org_id={org['id']}", headers=admin
    ).json()
    assert before["total_debit"] == 0  # 草稿不计入

    assert client.post(
        f"/api/accounting/vouchers/{voucher['id']}/post", headers=roles["director"]
    ).status_code == 200

    after = client.get(
        f"/api/accounting/trial-balance?period=2026-08&org_id={org['id']}", headers=admin
    ).json()
    assert after["total_debit"] == after["total_credit"] == 12000
    assert after["balanced"] is True
    line = next(x for x in after["lines"] if x["subject_code"] == "4001")
    assert line["subject_name"] == "医疗收入" and line["credit"] == 12000


def test_posted_voucher_cannot_be_posted_again(client, roles, voucher):
    resp = client.post(f"/api/accounting/vouchers/{voucher['id']}/post", headers=roles["director"])
    assert resp.status_code == 409


def test_void_removes_from_trial_balance(client, admin, roles, org):
    v = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-200", "voucher_date": "2026-09-01",
              "entries": [{"subject_code": "1001", "debit": 300}, {"subject_code": "4004", "credit": 300}]},
        headers=roles["director"],
    ).json()
    client.post(f"/api/accounting/vouchers/{v['id']}/post", headers=roles["director"])
    assert client.get(
        f"/api/accounting/trial-balance?period=2026-09&org_id={org['id']}", headers=admin
    ).json()["total_debit"] == 300

    client.post(f"/api/accounting/vouchers/{v['id']}/void", headers=roles["director"])
    assert client.get(
        f"/api/accounting/trial-balance?period=2026-09&org_id={org['id']}", headers=admin
    ).json()["total_debit"] == 0


def test_voucher_requires_director(client, roles, org):
    resp = client.post(
        "/api/accounting/vouchers",
        json={"org_id": org["id"], "voucher_no": "JZ-999", "voucher_date": "2026-08-01",
              "entries": [{"subject_code": "1002", "debit": 1}, {"subject_code": "4001", "credit": 1}]},
        headers=roles["doctor"],
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- 成本核算


@pytest.fixture(scope="module")
def depts(client, admin, org):
    out = {}
    for code, name, category in [("NK", "内科", "clinical"), ("WK", "外科", "clinical"),
                                 ("XZ", "行政后勤", "admin")]:
        out[code] = client.post(
            "/api/mgmt/departments",
            json={"org_id": org["id"], "code": code, "name": name, "category": category},
            headers=admin,
        ).json()
    return out


def test_department_cost_upsert(client, roles, depts):
    first = client.post(
        "/api/cost/departments",
        json={"dept_id": depts["NK"]["id"], "period": "2026-08", "cost_type": "labor", "amount": 100000},
        headers=roles["director"],
    )
    assert first.status_code == 201 and first.json()["updated"] is False
    # 月末成本反复调整是常态，重复提交按覆盖处理
    again = client.post(
        "/api/cost/departments",
        json={"dept_id": depts["NK"]["id"], "period": "2026-08", "cost_type": "labor", "amount": 120000},
        headers=roles["director"],
    )
    assert again.json()["updated"] is True and again.json()["amount"] == 120000


def test_allocation_rejects_self_and_cross_org(client, admin, roles, depts, org):
    same = client.post(
        "/api/cost/allocation-rules",
        json={"from_dept_id": depts["XZ"]["id"], "to_dept_id": depts["XZ"]["id"], "ratio_pct": 50},
        headers=roles["director"],
    )
    assert same.status_code == 422

    other_org = client.post(
        "/api/organizations", json={"name": "另一家卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    other_dept = client.post(
        "/api/mgmt/departments",
        json={"org_id": other_org["id"], "code": "NK", "name": "内科", "category": "clinical"},
        headers=admin,
    ).json()
    cross = client.post(
        "/api/cost/allocation-rules",
        json={"from_dept_id": depts["XZ"]["id"], "to_dept_id": other_dept["id"], "ratio_pct": 50},
        headers=roles["director"],
    )
    assert cross.status_code == 422


def test_cost_allocation_math(client, admin, roles, depts, org):
    """行政科室 10000 元按 60/40 分摊到内外科，转入转出对得上。"""
    client.post(
        "/api/cost/departments",
        json={"dept_id": depts["XZ"]["id"], "period": "2026-08", "cost_type": "overhead", "amount": 10000},
        headers=roles["director"],
    )
    client.post(
        "/api/cost/departments",
        json={"dept_id": depts["WK"]["id"], "period": "2026-08", "cost_type": "labor", "amount": 80000},
        headers=roles["director"],
    )
    for target, ratio in [("NK", 60), ("WK", 40)]:
        client.post(
            "/api/cost/allocation-rules",
            json={"from_dept_id": depts["XZ"]["id"], "to_dept_id": depts[target]["id"], "ratio_pct": ratio},
            headers=roles["director"],
        )
    rows = client.get(f"/api/cost/departments?period=2026-08&org_id={org['id']}", headers=admin).json()
    by_id = {r["dept_id"]: r for r in rows}

    admin_row = by_id[depts["XZ"]["id"]]
    assert admin_row["direct_cost"] == 10000
    assert admin_row["allocated_out"] == 10000
    assert admin_row["total_cost"] == 0  # 全额分摊出去

    nk = by_id[depts["NK"]["id"]]
    assert nk["allocated_in"] == 6000
    assert nk["total_cost"] == 120000 + 6000
    assert nk["by_type"]["labor"] == 120000

    wk = by_id[depts["WK"]["id"]]
    assert wk["allocated_in"] == 4000
    assert wk["total_cost"] == 84000


def test_partial_allocation_flagged(client, admin, roles, org):
    """来源科室比例未配满 100% 时，未分摊部分要标出来而不是悄悄留下。"""
    dept_a = client.post(
        "/api/mgmt/departments",
        json={"org_id": org["id"], "code": "YJ", "name": "医技科", "category": "medtech"},
        headers=admin,
    ).json()
    dept_b = client.post(
        "/api/mgmt/departments",
        json={"org_id": org["id"], "code": "EK", "name": "儿科", "category": "clinical"},
        headers=admin,
    ).json()
    client.post(
        "/api/cost/departments",
        json={"dept_id": dept_a["id"], "period": "2026-10", "cost_type": "overhead", "amount": 1000},
        headers=roles["director"],
    )
    client.post(
        "/api/cost/allocation-rules",
        json={"from_dept_id": dept_a["id"], "to_dept_id": dept_b["id"], "ratio_pct": 30},
        headers=roles["director"],
    )
    rows = client.get(f"/api/cost/departments?period=2026-10&org_id={org['id']}", headers=admin).json()
    source = next(r for r in rows if r["dept_id"] == dept_a["id"])
    assert source["allocated_out"] == 300
    assert source["unallocated_ratio_amount"] == 700


def test_unit_cost_uses_occupied_bed_days(client, admin, roles, org):
    """床日成本分母必须是实际占用床日，不是床位数×天数。"""
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "核算病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "C01"}, headers=admin
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "核算患者", "id_card": "331282199001011234"}, headers=admin
    ).json()
    client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "医生", "diagnosis_name": "肺炎"},
        headers=admin,
    )
    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"], "doctor_name": "门诊医生",
              "diagnosis_name": "感冒"},
        headers=admin,
    )
    from datetime import date

    period = date.today().strftime("%Y-%m")
    client.post(
        "/api/cost/departments",
        json={"dept_id": client.get("/api/mgmt/departments", headers=admin).json()[0]["id"],
              "period": period, "cost_type": "overhead", "amount": 30000},
        headers=roles["director"],
    )
    result = client.get(f"/api/cost/unit-cost?period={period}&org_id={org['id']}", headers=admin).json()
    assert result["outpatient_visits"] >= 1
    assert result["occupied_bed_days"] >= 1
    assert result["total_cost"] >= 30000
    # 拆分后两块之和等于总成本
    assert round(result["outpatient_cost"] + result["inpatient_cost"], 2) == result["total_cost"]
    assert result["cost_per_visit"] > 0 and result["cost_per_bed_day"] > 0


def test_unit_cost_rejects_bad_period(client, admin, org):
    resp = client.get(f"/api/cost/unit-cost?period=2026&org_id={org['id']}", headers=admin)
    assert resp.status_code == 422


# ---------------------------------------------------------------- 物资采购


@pytest.fixture(scope="module")
def supplier(client, admin):
    return client.post(
        "/api/pharmacy/suppliers", json={"name": "核算物资供应商", "contact": "王经理"}, headers=admin
    ).json()


def test_material_purchase_full_flow(client, admin, roles, org, supplier, depts):
    req = client.post(
        "/api/materials/purchases",
        json={"org_id": org["id"], "dept_id": depts["NK"]["id"], "item_name": "输液架",
              "spec": "不锈钢", "unit": "个", "quantity": 20, "estimated_price": 180,
              "reason": "病区补充"},
        headers=roles["operator"],
    )
    assert req.status_code == 201
    pid = req.json()["id"]

    # 未审批不可签合同
    assert client.post(
        f"/api/materials/purchases/{pid}/contract",
        json={"supplier_id": supplier["id"], "contract_no": "HT-1", "contract_amount": 3600},
        headers=roles["operator"],
    ).status_code == 409

    assert client.post(
        f"/api/materials/purchases/{pid}/approve", json={"approved": True}, headers=roles["director"]
    ).status_code == 200
    assert client.post(
        f"/api/materials/purchases/{pid}/contract",
        json={"supplier_id": supplier["id"], "contract_no": "HT-1", "contract_amount": 3600},
        headers=roles["operator"],
    ).status_code == 200

    # 验收数量不得超过采购量
    assert client.post(
        f"/api/materials/purchases/{pid}/receive", json={"received_quantity": 25},
        headers=roles["operator"],
    ).status_code == 422

    received = client.post(
        f"/api/materials/purchases/{pid}/receive",
        json={"received_quantity": 20, "note": "验收合格"}, headers=roles["operator"],
    )
    assert received.status_code == 200
    body = received.json()
    assert body["status"] == "received" and body["asset_quantity"] == 20

    # 验收自动生成入库流水
    movements = client.get(f"/api/mgmt/assets/{body['asset_id']}/movements", headers=admin).json()
    assert any(m["movement_type"] == "inbound" and m["quantity"] == 20 for m in movements)


def test_requester_cannot_approve_own_purchase(client, roles, org):
    req = client.post(
        "/api/materials/purchases",
        json={"org_id": org["id"], "item_name": "自批物资", "quantity": 1},
        headers=roles["director"],
    ).json()
    denied = client.post(
        f"/api/materials/purchases/{req['id']}/approve", json={"approved": True},
        headers=roles["director"],
    )
    assert denied.status_code == 403


# ---------------------------------------------------------------- 高值耗材追溯


def test_consumable_trace_chain(client, admin, roles, org, supplier):
    """入库 → 使用（绑患者+手术）→ 正向按码追溯、反向按批号召回。"""
    item = client.post(
        "/api/materials/consumables",
        json={"barcode": "HV-0001", "name": "冠脉支架", "spec": "3.0x18mm", "org_id": org["id"],
              "supplier_id": supplier["id"], "batch_no": "B2026A", "expire_date": "2028-01-01",
              "unit_price": 5800},
        headers=roles["operator"],
    )
    assert item.status_code == 201
    assert client.post(
        "/api/materials/consumables",
        json={"barcode": "HV-0001", "name": "重复条码", "org_id": org["id"]},
        headers=roles["operator"],
    ).status_code == 409

    patient = client.post(
        "/api/patients", json={"name": "耗材患者", "id_card": "331282199202022345"}, headers=admin
    ).json()
    used = client.post(
        "/api/materials/consumables/HV-0001/use",
        json={"patient_id": patient["id"]}, headers=roles["doctor"],
    )
    assert used.status_code == 200 and used.json()["status"] == "used"

    # 已使用不可再次使用
    assert client.post(
        "/api/materials/consumables/HV-0001/use",
        json={"patient_id": patient["id"]}, headers=roles["doctor"],
    ).status_code == 409

    trace = client.get("/api/materials/consumables/trace/HV-0001", headers=admin).json()
    assert trace["used_patient_name"] == "耗材患者"
    assert trace["supplier_name"] == "核算物资供应商"
    assert trace["batch_no"] == "B2026A"

    recall = client.get("/api/materials/consumables?batch_no=B2026A", headers=admin).json()
    assert [c["barcode"] for c in recall] == ["HV-0001"]

    by_patient = client.get(
        f"/api/materials/consumables?patient_id={patient['id']}", headers=admin
    ).json()
    assert len(by_patient) == 1


def test_consumable_surgery_must_belong_to_patient(client, admin, roles, org):
    """耗材记到别人的手术上，追溯链就断了，必须拦。"""
    client.post(
        "/api/materials/consumables",
        json={"barcode": "HV-0002", "name": "接骨板", "org_id": org["id"]},
        headers=roles["operator"],
    )
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "耗材病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "D01"}, headers=admin
    ).json()
    surgery_patient = client.post(
        "/api/patients", json={"name": "手术患者甲", "id_card": "331282199303033456"}, headers=admin
    ).json()
    other_patient = client.post(
        "/api/patients", json={"name": "无关患者乙", "id_card": "331282199404044567"}, headers=admin
    ).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": surgery_patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "外科医生", "diagnosis_name": "骨折"},
        headers=admin,
    ).json()
    surgery = client.post(
        "/api/surgery/requests",
        json={"admission_id": adm["id"], "surgery_name": "切开复位内固定术"},
        headers=roles["doctor"],
    ).json()

    mismatch = client.post(
        "/api/materials/consumables/HV-0002/use",
        json={"patient_id": other_patient["id"], "surgery_id": surgery["id"]},
        headers=roles["doctor"],
    )
    assert mismatch.status_code == 422

    ok = client.post(
        "/api/materials/consumables/HV-0002/use",
        json={"patient_id": surgery_patient["id"], "surgery_id": surgery["id"]},
        headers=roles["doctor"],
    )
    assert ok.status_code == 200
    assert ok.json()["used_surgery_name"] == "切开复位内固定术"


def test_consumable_unknown_barcode_404(client, admin):
    assert client.get("/api/materials/consumables/trace/NOPE", headers=admin).status_code == 404
