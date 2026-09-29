"""页面新增的复诊计划挂上在管档案的病种与主管医生（P1-224，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-1）。

个案管理师端「新增计划」的病种下拉缺省是筛选用的「全部病种」（空）、表单没有复诊医生框，`create_revisit` 原样落库：
建出来的复诊不带病种、不带医生。登记死亡 / 迁出时档案收尾（`close_open_work` 只收同病种的复诊）撤不掉它，照样被扫成
逾期、居民端照样看得到；医生移动端「今日复诊」按 `doctor_user_id == 本人` 数，数不到它。同文件的高危自动复诊带着档案
病种与主管医生，P1-139 也已让转诊、干预、手工任务没写病种时走 `enrollment_for`。修后没写病种的按在管档案推断（在管几个
病种的照旧留空，不替人猜），没写医生的取这份在管档案的主管医生（档案在别家机构的不替人挑别家的医生，跨机构的系统指派
待裁定 P1-211）；页面的病种下拉换成「只在管一个病种的按它」、加医生框。
"""
from pathlib import Path

import pytest

from app import clock

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1224 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctors = {}
    for name in ("p1224_doc", "p1224_doc2"):
        created = client.post("/api/users", headers=admin, json={
            "username": name, "password": "passw0rd1", "full_name": name, "role": "doctor", "org_id": org})
        assert created.status_code in (200, 201), created.text
        doctors[name] = created.json()["id"]
    patients = {}
    for n, (tag, programs) in enumerate((("单病种", ["hypertension"]), ("双病种", ["hypertension", "diabetes"]),
                                         ("别家", ["hypertension"]))):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P1224 {tag}", "id_card": f"33010219600101122{n}"}).json()["id"]
        enrollments = {}
        for program in programs:
            enrolled = client.post(f"{B}/enrollments", headers=admin, json={
                "patient_id": patient, "program_code": program, "org_id": org, "doctor_user_id": doctors["p1224_doc"]})
            assert enrolled.status_code == 201, enrolled.text
            enrollments[program] = enrolled.json()["id"]
        patients[tag] = {"id": patient, "enrollments": enrollments}
    other_org = client.post("/api/organizations", headers=admin, json={
        "name": "P1224 邻镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    other = client.post("/api/users", headers=admin, json={
        "username": "p1224_other", "password": "passw0rd1", "full_name": "p1224_other", "role": "doctor",
        "org_id": other_org})
    assert other.status_code in (200, 201), other.text
    other_headers = _login(client, "p1224_other")
    served = client.post("/api/encounters", headers=other_headers, json={
        "patient_id": patients["别家"]["id"], "org_id": other_org, "diagnosis_name": "高血压"})
    assert served.status_code in (200, 201), served.text   # 邻镇接诊过：看得见这位患者
    return {"doctors": doctors, "patients": patients, "doctor": _login(client, "p1224_doc"), "other": other_headers}


def _page_body(patient_id, **extra):
    """页面「新增计划」表单经 formJson 送的样子：病种、医生留空的键不送。"""
    return {"patient_id": patient_id, "plan_date": clock.today().isoformat(), **extra}


def test_没写病种和医生_挂上唯一在管档案_死亡收尾一并移除(client, admin, world):
    single = world["patients"]["单病种"]
    made = client.post(f"{B}/revisits", headers=world["doctor"], json=_page_body(single["id"], items="复查血压"))
    assert made.status_code == 201, made.text
    revisit = made.json()
    assert (revisit["program_code"], revisit["doctor_user_id"]) == (
        "hypertension", world["doctors"]["p1224_doc"]), revisit   # 修前 ("", None)
    calendar = client.get(f"{B}/workbench/doctor-mobile", headers=world["doctor"]).json()["calendar"]
    assert calendar["revisits"] == 1, calendar   # 修前 0：今日复诊按本人数，数不到不带医生的
    died = client.post(f"{B}/enrollments/{single['enrollments']['hypertension']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert died.status_code == 200, died.text
    rows = client.get(f"{B}/revisits", headers=admin, params={"patient_id": single["id"]}).json()
    assert [(r["id"], r["status"]) for r in rows] == [(revisit["id"], "removed")], rows   # 修前一直「待复诊」


def test_在管几个病种又没写的_照旧留空不替人猜(client, admin, world):
    double = world["patients"]["双病种"]
    made = client.post(f"{B}/revisits", headers=admin, json=_page_body(double["id"]))
    assert made.status_code == 201, made.text
    assert (made.json()["program_code"], made.json()["doctor_user_id"]) == ("", None), made.json()
    chosen = client.post(f"{B}/revisits", headers=admin, json=_page_body(double["id"], program_code="diabetes"))
    assert chosen.status_code == 201, chosen.text
    assert (chosen.json()["program_code"], chosen.json()["doctor_user_id"]) == (
        "diabetes", world["doctors"]["p1224_doc"]), chosen.json()   # 写了病种：医生取这个病种档案的主管医生


def test_档案在别家机构的_病种照推_不替人挑别家的医生(client, world):
    made = client.post(f"{B}/revisits", headers=world["other"], json=_page_body(world["patients"]["别家"]["id"]))
    assert made.status_code == 201, made.text
    assert (made.json()["program_code"], made.json()["doctor_user_id"]) == ("hypertension", None), made.json()


def test_写了医生的照写(client, admin, world):
    double = world["patients"]["双病种"]
    other = world["doctors"]["p1224_doc2"]
    made = client.post(f"{B}/revisits", headers=admin, json=_page_body(
        double["id"], program_code="hypertension", doctor_user_id=other))
    assert made.status_code == 201, made.text
    assert made.json()["doctor_user_id"] == other


def test_页面新增计划_病种不再缺省全部病种_有复诊医生框():
    start = PAGE.index('<form class="inline" id="spd-revisit-form">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<select name="program_code"><option value="">病种：只在管一个病种的按它</option>' in form
    assert "${programOptions}" not in form   # 修前是带「全部病种」的筛选下拉
    assert '<input name="doctor_user_id" type="number"' in form
    assert 'formJson(e.target, ["patient_id", "doctor_user_id"])' in PAGE
