"""阶段五：统一规则引擎、业务流程引擎、统一申请单中心。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.rules import RuleError, evaluate_condition


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _login(client, username, password="passw0rd1"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def roles(client, admin):
    for username, role in [("wf_doc", "doctor"), ("wf_dir", "director"),
                           ("wf_pha", "pharmacist"), ("wf_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "passw0rd1", "full_name": username, "role": role},
            headers=admin,
        )
    return {k: _login(client, v) for k, v in
            {"doctor": "wf_doc", "director": "wf_dir", "pharmacist": "wf_pha", "operator": "wf_op"}.items()}


# ============================================================================
# T5.1 条件 DSL
# ============================================================================


def test_condition_comparisons_and_boolean():
    v = {"daily_dose": 1500.0, "max_daily_dose": 1000.0, "age": 80.0}
    assert evaluate_condition("daily_dose > max_daily_dose", v) is True
    assert evaluate_condition("daily_dose > max_daily_dose and age >= 65", v) is True
    assert evaluate_condition("daily_dose < max_daily_dose or age >= 65", v) is True
    assert evaluate_condition("not (daily_dose > max_daily_dose)", v) is False


def test_condition_chained_comparison():
    assert evaluate_condition("0 < age < 18", {"age": 10.0}) is True
    assert evaluate_condition("0 < age < 18", {"age": 30.0}) is False


def test_condition_string_and_membership():
    v = {"drug_code": "WARFARIN", "diagnosis_name": "房颤"}
    assert evaluate_condition('drug_code == "WARFARIN"', v) is True
    assert evaluate_condition('drug_code in ("WARFARIN", "HEPARIN")', v) is True
    assert evaluate_condition('drug_code not in ("ASPIRIN",)', v) is True


def test_condition_len_function():
    """质控里判"字数少于 N"要用 len()。"""
    assert evaluate_condition("len(chief_complaint) < 5", {"chief_complaint": "咳嗽"}) is True
    assert evaluate_condition("len(chief_complaint) < 5", {"chief_complaint": "咳嗽咳痰三天"}) is False


def test_condition_boolean_variable():
    assert evaluate_condition("is_pregnant", {"is_pregnant": True}) is True
    assert evaluate_condition("is_pregnant and age > 30", {"is_pregnant": False, "age": 40.0}) is False


@pytest.mark.parametrize(
    "condition",
    ["__import__('os')", "a.__class__ == 1", "[x for x in (1,2)]", "lambda: True", "open('/x')"],
)
def test_condition_rejects_dangerous_syntax(condition):
    with pytest.raises(RuleError):
        evaluate_condition(condition, {"a": 1})


def test_condition_rejects_unknown_variable():
    with pytest.raises(RuleError, match="未知变量"):
        evaluate_condition("nope > 1", {"a": 1})


def test_condition_rejects_incomparable_types():
    with pytest.raises(RuleError, match="不可比较"):
        evaluate_condition("drug_code > 5", {"drug_code": "ABC"})


# ============================================================================
# T5.1 规则接口
# ============================================================================


def test_domains_listed_with_samples(client, admin):
    domains = client.get("/api/rules/domains", headers=admin).json()
    names = {d["domain"] for d in domains}
    assert {"prescription", "medical_record", "exam", "org_performance"} <= names
    rx = next(d for d in domains if d["domain"] == "prescription")
    types = {v["type"] for v in rx["variables"]}
    # 样例值必须覆盖真实类型，否则校验放行的条件运行时才炸
    assert {"float", "str", "bool"} <= types


def test_create_rule_validates_condition(client, admin):
    bad = client.post(
        "/api/rules",
        json={"key": "bad", "name": "非法", "domain": "prescription", "condition": "nope > 1"},
        headers=admin,
    )
    assert bad.status_code == 422
    unknown_domain = client.post(
        "/api/rules",
        json={"key": "d", "name": "域错", "domain": "nope", "condition": "1 > 0"},
        headers=admin,
    )
    assert unknown_domain.status_code == 422


def test_rule_evaluate_hits_and_blocking(client, admin):
    client.post(
        "/api/rules",
        json={"key": "rx_overdose", "name": "超日剂量", "domain": "prescription",
              "condition": "daily_dose > max_daily_dose", "message": "日剂量超过上限",
              "severity": "error", "deduct_points": 10},
        headers=admin,
    )
    client.post(
        "/api/rules",
        json={"key": "rx_elderly", "name": "老年人用药提示", "domain": "prescription",
              "condition": "age >= 65", "severity": "info", "deduct_points": 2},
        headers=admin,
    )
    result = client.post(
        "/api/rules/evaluate",
        json={"domain": "prescription",
              "variables": {"daily_dose": 3000, "max_daily_dose": 2000, "age": 78}},
        headers=admin,
    ).json()
    assert {h["key"] for h in result["hits"]} == {"rx_overdose", "rx_elderly"}
    assert result["total_deduction"] == 12
    assert result["blocked"] is True
    assert result["errors"] == []


def test_rule_evaluate_fills_missing_variables(client, admin):
    """业务侧只传部分变量时用域样例补齐，旧规则不该因少一个变量就报错。"""
    result = client.post(
        "/api/rules/evaluate",
        json={"domain": "prescription", "variables": {"age": 70}},
        headers=admin,
    ).json()
    assert result["errors"] == []
    assert any(h["key"] == "rx_elderly" for h in result["hits"])


def test_broken_rule_does_not_break_others(client, admin):
    """一条写坏的规则不能让整个审方停摆。"""
    from app.database import SessionLocal
    from app.models import RuleDefinition

    with SessionLocal() as db:
        db.add(RuleDefinition(key="rx_broken", name="坏规则", domain="prescription",
                              condition="unknown_var > 1"))
        db.commit()
    result = client.post(
        "/api/rules/evaluate",
        json={"domain": "prescription", "variables": {"age": 70}},
        headers=admin,
    ).json()
    assert [e["key"] for e in result["errors"]] == ["rx_broken"]
    assert any(h["key"] == "rx_elderly" for h in result["hits"])  # 其余规则照常命中


def test_deactivated_rule_not_evaluated(client, admin):
    assert client.delete("/api/rules/rx_elderly", headers=admin).status_code == 200
    result = client.post(
        "/api/rules/evaluate",
        json={"domain": "prescription", "variables": {"age": 70}},
        headers=admin,
    ).json()
    assert all(h["key"] != "rx_elderly" for h in result["hits"])


def test_rule_catalog_merges_legacy_sources(client, admin):
    """统一目录必须把四套既有规则并进来，并标明执行引擎。"""
    catalog = client.get("/api/rules/catalog", headers=admin).json()
    assert catalog["total"] == len(catalog["entries"])
    assert {"unified", "prescription", "data_quality", "record_quality", "performance"} <= set(
        catalog["by_source"]
    )
    # 目录统一但执行路径未统一，这一点在数据里如实标出
    engines = {e["engine"] for e in catalog["entries"]}
    assert engines == {"unified", "legacy"}


def test_rule_write_requires_admin(client, roles):
    resp = client.post(
        "/api/rules",
        json={"key": "x", "name": "x", "domain": "prescription", "condition": "age > 1"},
        headers=roles["director"],
    )
    assert resp.status_code == 403


# ============================================================================
# T5.2 流程引擎
# ============================================================================


def test_definition_validation(client, admin):
    dup = client.post(
        "/api/workflows/definitions",
        json={"key": "dup", "name": "重复节点",
              "nodes": [{"key": "a", "name": "A"}, {"key": "a", "name": "A2"}]},
        headers=admin,
    )
    assert dup.status_code == 422

    dangling = client.post(
        "/api/workflows/definitions",
        json={"key": "dangling", "name": "悬空指向",
              "nodes": [{"key": "a", "name": "A", "next": "ghost"}]},
        headers=admin,
    )
    assert dangling.status_code == 422
    assert "不存在的节点" in dangling.json()["detail"]

    no_end = client.post(
        "/api/workflows/definitions",
        json={"key": "loop", "name": "无终态",
              "nodes": [{"key": "a", "name": "A", "next": "b"}, {"key": "b", "name": "B", "next": "a"}]},
        headers=admin,
    )
    assert no_end.status_code == 422


@pytest.fixture(scope="module")
def definition(client, admin):
    resp = client.post(
        "/api/workflows/definitions",
        json={"key": "drug_intro", "name": "新药引进审批",
              "nodes": [
                  {"key": "apply", "name": "科室申请", "role": "doctor", "next": "pharmacy"},
                  {"key": "pharmacy", "name": "药学审核", "role": "pharmacist", "next": "approve"},
                  {"key": "approve", "name": "院长审批", "role": "director", "next": ""},
              ]},
        headers=admin,
    )
    assert resp.status_code == 201
    return resp.json()


def test_workflow_full_flow_with_role_gates(client, admin, roles, definition):
    instance = client.post(
        "/api/workflows/instances",
        json={"definition_key": "drug_intro", "business_type": "drug_intro",
              "business_id": 1, "title": "引进某降压新药"},
        headers=roles["doctor"],
    )
    assert instance.status_code == 201
    body = instance.json()
    assert body["current_node"] == "apply" and body["current_node_role"] == "doctor"
    iid = body["id"]

    # 药师推不动"科室申请"节点
    wrong = client.post(f"/api/workflows/instances/{iid}/advance", json={}, headers=roles["pharmacist"])
    assert wrong.status_code == 403
    assert "doctor" in wrong.json()["detail"]

    step1 = client.post(
        f"/api/workflows/instances/{iid}/advance", json={"comment": "科室提出"}, headers=roles["doctor"]
    ).json()
    assert step1["current_node"] == "pharmacy"

    step2 = client.post(
        f"/api/workflows/instances/{iid}/advance", json={"comment": "药学同意"},
        headers=roles["pharmacist"],
    ).json()
    assert step2["current_node"] == "approve"

    done = client.post(
        f"/api/workflows/instances/{iid}/advance", json={"comment": "批准"}, headers=roles["director"]
    ).json()
    assert done["status"] == "completed"

    assert client.post(
        f"/api/workflows/instances/{iid}/advance", json={}, headers=roles["director"]
    ).status_code == 409

    history = client.get(f"/api/workflows/instances/{iid}/history", headers=admin).json()
    assert [h["from_node"] for h in history] == ["apply", "pharmacy", "approve"]
    assert history[-1]["to_node"] == ""  # 终态
    assert history[0]["actor"] == "wf_doc"


def test_my_tasks_filtered_by_role(client, roles, definition):
    client.post(
        "/api/workflows/instances",
        json={"definition_key": "drug_intro", "business_type": "drug_intro", "business_id": 2,
              "title": "待科室申请的单子"},
        headers=roles["doctor"],
    )
    doctor_tasks = client.get("/api/workflows/my-tasks", headers=roles["doctor"]).json()
    assert doctor_tasks["count"] >= 1
    assert all(t["current_node_role"] in ("", "doctor") for t in doctor_tasks["tasks"])

    pharmacist_tasks = client.get("/api/workflows/my-tasks", headers=roles["pharmacist"]).json()
    assert all(t["current_node_role"] in ("", "pharmacist") for t in pharmacist_tasks["tasks"])


def test_admin_sees_all_tasks(client, admin, roles):
    admin_tasks = client.get("/api/workflows/my-tasks", headers=admin).json()
    doctor_tasks = client.get("/api/workflows/my-tasks", headers=roles["doctor"]).json()
    assert admin_tasks["count"] >= doctor_tasks["count"]


def test_cancel_instance(client, roles, definition):
    instance = client.post(
        "/api/workflows/instances",
        json={"definition_key": "drug_intro", "business_type": "drug_intro", "business_id": 3},
        headers=roles["doctor"],
    ).json()
    assert client.post(
        f"/api/workflows/instances/{instance['id']}/cancel", json={"comment": "科室撤回"},
        headers=roles["doctor"],
    ).json()["status"] == "cancelled"
    assert client.post(
        f"/api/workflows/instances/{instance['id']}/advance", json={}, headers=roles["doctor"]
    ).status_code == 409


def test_start_with_unknown_definition_404(client, roles):
    resp = client.post(
        "/api/workflows/instances",
        json={"definition_key": "ghost", "business_type": "x"},
        headers=roles["doctor"],
    )
    assert resp.status_code == 404


# ============================================================================
# T5.3 统一申请单中心
# ============================================================================


@pytest.fixture(scope="module")
def request_data(client, admin, roles):
    org = client.post(
        "/api/organizations", json={"name": "申请单演示院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    upper = client.post(
        "/api/organizations", json={"name": "申请单上级院", "org_type": "lead_hospital", "level": "city"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "申请单患者", "id_card": "331482199001011234"}, headers=admin
    ).json()
    slot = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient", "resource_name": "全科门诊",
              "slot_date": "2026-10-01", "slot_time": "09:00-10:00", "capacity": 2},
        headers=admin,
    ).json()
    client.post(
        "/api/appointments", json={"slot_id": slot["id"], "patient_id": patient["id"]}, headers=admin
    )
    client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "imaging",
              "item_code": "DR-CHEST", "item_name": "胸部DR"},
        headers=admin,
    )
    client.post(
        "/api/consultations",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "to_org_id": upper["id"],
              "question": "请求会诊"},
        headers=admin,
    )
    client.post(
        "/api/blood/requests",
        json={"patient_id": patient["id"], "org_id": org["id"], "blood_type": "A",
              "component": "rbc", "quantity_ml": 400, "reason": "术中备血"},
        headers=admin,
    )
    return {"org": org, "patient": patient}


def test_unified_requests_aggregates_five_types(client, admin, request_data):
    result = client.get(
        f"/api/service-requests?patient_id={request_data['patient']['id']}", headers=admin
    ).json()
    types = set(result["by_type"])
    assert {"appointment", "exam", "consultation", "blood"} <= types
    assert result["total"] == len(result["items"])
    assert all(i["patient_name"] == "申请单患者" for i in result["items"])
    assert all(i["org_name"] == "申请单演示院" for i in result["items"])


def test_unified_status_mapping(client, admin, request_data):
    """各单据的原生状态映射到统一口径，并保留 raw_status 供排查。"""
    result = client.get(
        f"/api/service-requests?patient_id={request_data['patient']['id']}", headers=admin
    ).json()
    appt = next(i for i in result["items"] if i["request_type"] == "appointment")
    assert appt["raw_status"] == "booked" and appt["status"] == "pending"
    cons = next(i for i in result["items"] if i["request_type"] == "consultation")
    assert cons["raw_status"] == "applied" and cons["status"] == "pending"
    assert set(result["by_status"]) <= {"pending", "processing", "done", "cancelled"}


def test_unified_requests_filters(client, admin, request_data):
    pid = request_data["patient"]["id"]
    only_exam = client.get(f"/api/service-requests?patient_id={pid}&request_type=exam", headers=admin).json()
    assert only_exam["total"] >= 1
    assert all(i["request_type"] == "exam" for i in only_exam["items"])

    pending = client.get(f"/api/service-requests?patient_id={pid}&status=pending", headers=admin).json()
    assert all(i["status"] == "pending" for i in pending["items"])


def test_unified_requests_sorted_desc(client, admin, request_data):
    result = client.get(
        f"/api/service-requests?patient_id={request_data['patient']['id']}", headers=admin
    ).json()
    stamps = [i["created_at"] for i in result["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_engines_require_login(client):
    assert client.get("/api/rules/catalog").status_code == 401
    assert client.get("/api/workflows/my-tasks").status_code == 401
    assert client.get("/api/service-requests").status_code == 401
