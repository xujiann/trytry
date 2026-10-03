"""押金预交与出院同时到：已出院的住院照样收进押金（P2-1187，第三十四批扫描 L1-8 押金那一半）。

`create_deposit` 原先只在锁外判「在院」，住院登记行锁（`serialized_on(Admission)`，P2-912）里只查结算单、不再判在院；
零费用的住院不结算也能出院（`_assert_billing_settled` 只拦未结清的）。押金判完「在院」、还没进锁，出院先提交了，押金照收：
住院已出院、押金余额 3000——违背「仅在院患者可预交」；顺序发生时出院之后再收是 409。兄弟路径计费早在同一把锁里重判了
在院（P2-274）。

修法同计费：锁里按列再判一次「在院」，不在院就回滚、409，文案与顺序请求同一句。这里把「判过了、还没写」钉成确定的
时序：押金一路过了锁外的「在院」与归属校验（`assert_obj_org_writable`，恰在判定与进锁之间）时，经真实接口插一路出院
并提交。
"""
import pytest

B = "/api/inpatient"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21187 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post(f"{B}/wards", headers=admin, json={"org_id": org, "name": "P21187 内科病区"}).json()["id"]
    return {"ward": ward, "n": 0}


def _admission(client, admin, world):
    """一次在院、病案首页已填、没有任何费用的住院（不结算也能出院）。"""
    world["n"] += 1
    bed = client.post(f"{B}/beds", headers=admin, json={"ward_id": world["ward"], "bed_no": f"P21187-{world['n']}"})
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21187 患者{world['n']}", "id_card": f"33012719650{world['n']}021187"}).json()["id"]
    admission = client.post(f"{B}/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed.json()["id"], "diagnosis_name": "腹痛待查"})
    assert admission.status_code == 201, admission.text
    admission_id = admission.json()["id"]
    summary = client.post(f"{B}/admissions/{admission_id}/case-summary", headers=admin, json={
        "discharge_diagnosis": "观察后排除急腹症"})
    assert summary.status_code == 201, summary.text
    return admission_id


def _deposit(client, admin, admission_id, amount=3000):
    return client.post("/api/billing/deposits", headers=admin, json={
        "admission_id": admission_id, "amount": amount, "method": "card"})


def _balance(client, admin, admission_id):
    got = client.get("/api/billing/deposits/balance", headers=admin, params={"admission_id": admission_id})
    assert got.status_code == 200, got.text
    return got.json()


def test_押金判完在院还没进锁时出院先提交_押金409_余额不变(client, admin, world, monkeypatch):
    from app.routers import billing

    admission_id = _admission(client, admin, world)
    real, fired = billing.assert_obj_org_writable, []

    def racing(db, user, obj, *args, **kwargs):
        result = real(db, user, obj, *args, **kwargs)
        if not fired:
            fired.append(client.post(f"{B}/admissions/{admission_id}/discharge", headers=admin))
        return result

    monkeypatch.setattr(billing, "assert_obj_org_writable", racing)
    got = _deposit(client, admin, admission_id)
    monkeypatch.undo()
    assert fired, "插桩没有触发：押金不再在判定与进锁之间做归属校验了，换一个插点"
    discharged = fired[0]
    assert discharged.status_code == 200 and discharged.json()["status"] == "discharged", discharged.text
    assert got.status_code == 409, got.text   # 修前 201 {'deposit_type': 'prepay', 'balance': 3000.0}
    assert got.json() == {"detail": "患者已出院，不可预交押金"}   # 与顺序请求同一句
    assert _balance(client, admin, admission_id) == {
        "admission_id": admission_id, "prepaid": 0.0, "refunded": 0.0, "offset": 0.0, "balance": 0.0}   # 修前预交 3000


def test_不并发时在院照收_出院之后409(client, admin, world):
    admission_id = _admission(client, admin, world)
    got = _deposit(client, admin, admission_id, 2000)
    assert got.status_code == 201, got.text
    assert (got.json()["deposit_type"], got.json()["amount"], got.json()["balance"]) == ("prepay", 2000.0, 2000.0)
    assert client.post(f"{B}/admissions/{admission_id}/discharge", headers=admin).status_code == 200
    late = _deposit(client, admin, admission_id)
    assert late.status_code == 409 and late.json() == {"detail": "患者已出院，不可预交押金"}, late.text
    assert _balance(client, admin, admission_id)["balance"] == 2000.0
