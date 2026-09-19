"""以别家机构的名义写：十条实测行为回归（P1-39 的现场）。

`tests/test_body_org_write_guard.py` 是静态闸门，守的是「每个入参带机构标识的
写接口都调了 `assert_org_writable`」。本文件守的是**它真的拦得住**——静态守卫
只能证明那行代码在，证明不了它挡住了什么。

这十条都是**先复现、后修复**的：补校验之前逐条实打过，12 个探针里 9 个返回
201/200，乙卫生院的医师以甲县医院的名义开了处方、报了传染病卡、开了检查单、
上转了患者；乙院的经办从甲院药房调出了药品、把甲院的职工派驻了出去。

后果分三档，都不轻：
* **记到别家账上**——就诊、家医签约、传染病报卡：监管报数按机构统计，记错家
  等于同时虚增一家、虚减一家，而且事后看不出来；
* **以别家名义对外发起**——转诊、会诊、检查申请：接收方看到的是甲院开的单；
* **动别家的实物与人**——药品调拨、人员派驻：这两条动的是真东西。
"""
import pytest
from conftest import reset_database
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def env(client):
    adm = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]}
    a = client.post("/api/organizations", json={
        "name": "越权甲县医院", "org_type": "lead_hospital", "level": "county"}, headers=adm).json()
    b = client.post("/api/organizations", json={
        "name": "越权乙卫生院", "org_type": "township", "level": "township"}, headers=adm).json()
    pt = client.post("/api/patients", json={
        "name": "越权探针患者", "id_card": "330102199202021234"}, headers=adm).json()
    client.post("/api/dictionaries", json={
        "category": "drug", "code": "XQ001", "name": "越权探针药"}, headers=adm)
    client.post("/api/pharmacy/stocks", json={
        "org_id": a["id"], "drug_code": "XQ001", "drug_name": "越权探针药",
        "quantity": 500}, headers=adm)
    emp = client.post("/api/mgmt/employees", json={
        "org_id": a["id"], "name": "甲院职工", "title": "主治医师"}, headers=adm)

    def user(name, role, org):
        client.post("/api/users", json={
            "username": name, "password": "Xquan#2026x", "role": role,
            "org_id": org["id"]}, headers=adm)
        return {"Authorization": "Bearer " + client.post(
            "/api/auth/login", json={"username": name, "password": "Xquan#2026x"}
        ).json()["access_token"]}

    return {
        "adm": adm, "A": a, "B": b, "pt": pt,
        "emp": emp.json() if emp.status_code in (200, 201) else {},
        "doctor_b": user("xq_doc_b", "doctor", b),
        "operator_b": user("xq_op_b", "operator", b),
    }


def _cases(env):
    A, pt = env["A"], env["pt"]
    rows = [
        ("就诊：在甲院开就诊", "/api/encounters", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"], "visit_type": "outpatient",
          "visit_date": "2026-09-19"}),
        ("处方：以甲院名义开方", "/api/prescriptions", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"],
          "items": [{"drug_code": "XQ001", "drug_name": "越权探针药", "qty": 1,
                     "usage": "口服", "daily_dose": 1.0}]}),
        ("传染病：报卡记到甲院头上", "/api/infectious/cases", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"], "disease_code": "A01",
          "disease_name": "霍乱", "report_date": "2026-09-19", "onset_date": "2026-09-18"}),
        ("家医签约：替甲院签", "/api/contracts", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"], "doctor_name": "探针医生",
          "start_date": "2026-09-19"}),
        ("检查申请：以甲院名义开单", "/api/exams", "doctor_b",
         {"patient_id": pt["id"], "from_org_id": A["id"], "center_type": "imaging",
          "item_code": "E1", "item_name": "胸片"}),
        ("转诊：以甲院名义上转", "/api/referrals", "doctor_b",
         {"patient_id": pt["id"], "from_org_id": A["id"], "to_org_id": env["B"]["id"],
          "reason": "探针", "direction": "up"}),
        ("会诊：以甲院名义申请", "/api/consultations", "doctor_b",
         {"patient_id": pt["id"], "from_org_id": A["id"], "to_org_id": env["B"]["id"],
          "purpose": "探针", "question": "探针问题"}),
        ("调拨：从甲院药房调出", "/api/pharmacy/transfers", "operator_b",
         {"from_org_id": A["id"], "to_org_id": env["B"]["id"],
          "drug_code": "XQ001", "quantity": 1}),
    ]
    if env["emp"].get("id"):
        rows.append(("派驻：把甲院的职工派出去", "/api/staffing/secondments", "operator_b",
                     {"employee_id": env["emp"]["id"], "from_org_id": A["id"],
                      "to_org_id": env["B"]["id"], "start_date": "2026-09-19"}))
    return rows


def test_乙院用户不得以甲院名义写(client, env):
    """把各自的 `assert_org_writable` 去掉，对应那条必红——补校验之前实测全是 201。"""
    passed_through = []
    for label, path, role, body in _cases(env):
        r = client.post(path, json=body, headers=env[role])
        if r.status_code in (200, 201):
            passed_through.append(f"{label} → {r.status_code}")
        else:
            assert r.status_code == 403, (
                f"{label} 期望 403（无权以该机构名义写入），实际 {r.status_code}：{r.text[:160]}"
            )
            assert "无权以该机构名义写入" in r.json().get("detail", ""), (
                f"{label} 被拒了，但不是因为归属校验：{r.text[:160]}"
            )
    assert passed_through == [], (
        "以下写接口放行了「以别家机构名义写」：\n  " + "\n  ".join(passed_through)
    )


def test_本机构照常写得进(client, env):
    """守卫不能误伤本职工作：同一个用户往自己机构写必须照常成功。"""
    B = env["B"]
    ok = client.post("/api/encounters", json={
        "patient_id": env["pt"]["id"], "org_id": B["id"],
        "visit_type": "outpatient", "visit_date": "2026-09-19"},
        headers=env["doctor_b"])
    assert ok.status_code == 201, ok.text
    assert ok.json()["org_id"] == B["id"]


def test_全域角色跨机构照常(client, env):
    """admin/director 是全域角色，跨机构写是它们的正常职权，不该被这道守卫挡住。"""
    ok = client.post("/api/encounters", json={
        "patient_id": env["pt"]["id"], "org_id": env["A"]["id"],
        "visit_type": "outpatient", "visit_date": "2026-09-19"},
        headers=env["adm"])
    assert ok.status_code == 201, ok.text
