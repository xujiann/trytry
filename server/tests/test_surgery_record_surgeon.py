"""术中记录的「术者」页面录不了，恒取手术申请单上的（P2-1307，第三十八批扫描 AB3-3）。

`SurgeryRecordIn` 一直收 `surgeon_name` 与 `assistants`；申请单的 `surgeon_name` 缺省为申请人，术中记录的缺省取申请单上的
（`surgery.create_request` / `create_record`）。可桌面术中记录弹窗与医生移动端术中记录表单都没有术者 / 助手，手术申请表单
也没有拟施术者——住院医提申请、外科医生主刀并录入，手术记录（居民端、排班表、「查看记录」都当实际术者，P2-556）署的是
住院医，助手恒空。实测：外科吴二按页面字段提交，记录 `surgeon_name = 住院医周一`、`assistants = ''`。

修法：两端术中记录表单加「术者」（缺省带出申请单上的拟施术者，可改）与「助手」（选填），桌面「查看记录」列出助手；申请
表单加「拟施术者」（选填，空着照旧由后端取申请人）。后端不改——入参早就收。录入人（`created_by`）上「查看记录」要动出参，
不在本条。端到端档经两端页面改术者、填助手并按接口读回；这里钉住三张表单的形状与接口的取值口径。
"""
import os

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def _mobile_record_form(source):
    """移动端术中记录那一段：从 cardForm 的调用切到提交成功后重载列表为止（与 test_surgery_record_forms_send_levels 同一刀）"""
    start = source.index('cardForm(e.target.closest(".m-card"), "surg-record-form"')
    return source[start:source.index("await loadSurgery();", start)]


def _mgmt_record_modal(source):
    """管理端术中记录那一段：框自己提交（P2-607），从取排班起、到框关掉后的 `if (!ok) return;` 为止。"""
    start = source.index("const slot = schedules.find((s) => s.request_id === Number(d.record));")
    return source[start:source.index("if (!ok) return;", start)]


def test_桌面术中记录弹窗录术者与助手_术者缺省带出申请单上的():
    source = _read("pages-mgmt.js")
    modal = _mgmt_record_modal(source)
    assert '{ name: "surgeon_name", label: "术者", value: req ? req.surgeon_name : "" },' in modal   # 修前没有这两项
    assert '{ name: "assistants", label: ' in modal
    # spdModal 把每个字段按名字交给 submit，submit 原样送出
    assert 'submit: (v) => api(`/api/surgery/requests/${d.record}/record`, { method: "POST", body: JSON.stringify(v) })' in modal
    view = source[source.index("const rec = await api(`/api/surgery/requests/${d.view}/record`);"):]
    view = view[:view.index("return;")]
    assert '["术者", rec.surgeon_name], ["助手", rec.assistants || "—"]' in view


def test_移动端术中记录表单录术者与助手并送出_术者缺省带出申请单上的():
    source = _read("m", "doctor.js")
    assert 'data-surgeon="${esc(r.surgeon_name)}"' in source   # 申请单上的拟施术者跟着「填写术中记录」按钮走
    form = _mobile_record_form(source)
    assert '<input name="surgeon_name"' in form and 'value="${esc(e.target.dataset.surgeon || "")}"' in form
    assert '<input name="assistants"' in form
    assert "surgeon_name: f.surgeon_name.value.trim()," in form   # 修前两项都不送
    assert "assistants: f.assistants.value.trim()," in form


def test_申请表单录拟施术者并送出():
    source = _read("pages-mgmt.js")
    start = source.index('id="surg-form"')
    form = source[start:source.index("</form>", start)]
    assert '<input name="surgeon_name"' in form   # 修前没有这一项，申请单的术者恒为申请人
    # formJson 送表单里每个非空的字段：空着不送，后端照旧取申请人
    assert 'const body = formJson(e.target, ["admission_id"]);' in source[source.index('$("#surg-form").onsubmit'):]


# ---------------------------------------------------------------- 接口：表单送什么、记录存什么


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21307 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21307 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21307-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21307 患者", "id_card": "330106197303071319"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "急性阑尾炎"})
    assert admission.status_code == 201, admission.text
    room = client.post("/api/surgery/rooms", headers=admin, json={"org_id": org, "name": "P21307 手术间"}).json()["id"]
    staff = {}
    for username, full_name in (("p21307_res", "住院医周一"), ("p21307_sur", "外科吴二")):
        assert client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": full_name, "role": "doctor",
            "org_id": org}).status_code in (200, 201)
        token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()
        staff[username] = {"Authorization": f"Bearer {token['access_token']}"}
    return {"admission": admission.json()["id"], "room": room, "slot": iter(range(7, 20)),
            "resident": staff["p21307_res"], "surgeon": staff["p21307_sur"]}


def _scheduled(client, admin, world, **extra):
    """住院医提申请（`extra` 是申请表单多送的字段），管理员审批、排班；返回申请单。"""
    req = client.post("/api/surgery/requests", headers=world["resident"], json={
        "admission_id": world["admission"], "surgery_name": "阑尾切除术", **extra})
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    assert client.post(f"/api/surgery/requests/{rid}/approve", headers=admin, json={"approved": True}).status_code == 200
    hour = next(world["slot"])
    sched = client.post(f"/api/surgery/requests/{rid}/schedule", headers=admin, json={
        "room_id": world["room"], "scheduled_date": "2031-03-01",
        "start_time": f"{hour:02d}:00", "end_time": f"{hour:02d}:50"})
    assert sched.status_code == 201, sched.text
    return req.json()


def _record(client, world, rid, **form):
    """外科吴二主刀并录入（`form` 是术中记录表单送的术者 / 助手）；返回读回的 (术者, 助手)。"""
    rec = client.post(f"/api/surgery/requests/{rid}/record", headers=world["surgeon"], json={
        "actual_surgery_name": "阑尾切除术", "outcome": "治愈", **form})
    assert rec.status_code == 201, rec.text
    got = client.get(f"/api/surgery/requests/{rid}/record", headers=world["surgeon"]).json()
    return got["surgeon_name"], got["assistants"]


def test_术中记录改了术者_存改后的值(client, admin, world):
    req = _scheduled(client, admin, world)
    assert req["surgeon_name"] == "住院医周一"   # 申请表单没填拟施术者：申请人
    assert _record(client, world, req["id"], surgeon_name="外科吴二", assistants="住院医周一") == ("外科吴二", "住院医周一")


def test_术中记录不改术者_等于申请单上的(client, admin, world):
    # 申请时填了拟施术者：表单缺省带出它、不改照送
    req = _scheduled(client, admin, world, surgeon_name="外科吴二")
    assert req["surgeon_name"] == "外科吴二"
    assert _record(client, world, req["id"], surgeon_name=req["surgeon_name"], assistants="") == ("外科吴二", "")
    # 术者一栏清空了送空串：照旧取申请单上的（空着申请的即申请人）
    req = _scheduled(client, admin, world)
    assert _record(client, world, req["id"], surgeon_name="", assistants="") == ("住院医周一", "")
