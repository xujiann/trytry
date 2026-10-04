"""住院行带出病区名、床号、患者姓名，三处页面按「病区 床号 姓名」认人（P2-1335，第三十九批扫描 AC4-4）。

修前：住院页「病区/床位」列印的是床位主键（`${wardName} / ${a.bed_id}`）——床位主键全县连续编号，床号按病区从头编，
「外科病区 / 7」其实是外科 02 床，病区里真有 07 床时指的就是另一位病人；「患者」列只有患者 ID。住院临床文书的选择框是
「#住院号 患者ID 诊断」，医生移动端查房的是「诊断（住院号 N）」——同诊断的几位分不开，病历容易写错人。`AdmissionOut`
本来就不给床号和姓名；居民端对同一次住院早就按床号显示（`portal.py::portal_my_admissions`）。

修法：`AdmissionOut` 末尾追加 `ward_name` / `bed_no` / `patient_name`（只增键，原有键与次序不动，契约网
`test_inpatient_contract.py` 同步），一批住院按 id 各取一次、不逐行查库；三处页面改按「病区 床号 姓名」显示。
"""
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 显示住院行的三处：住院管理的「住院记录」表、住院临床文书与医生移动端查房的选择框
SITES = (
    ("pages-clinical.js", "async function renderInpatient()"),
    ("pages-mgmt.js", "async function renderClinicalDocs()"),
    ("m/doctor.js", "async function loadRound()"),
)


def _ward_with_beds(client, admin, org, name):
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": name}).json()["id"]
    beds = {no: client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": no}).json()["id"]
            for no in ("01", "02", "03", "04", "05")}
    return ward, beds


def test_入院_转科_出院回执与住院清单都带病区名床号姓名(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21335 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    medical, medical_beds = _ward_with_beds(client, admin, org, "P21335 内科病区")
    surgical, surgical_beds = _ward_with_beds(client, admin, org, "P21335 外科病区")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21335 王五", "id_card": "330106197001010011"}).json()["id"]

    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": medical, "bed_id": medical_beds["01"], "diagnosis_name": "腹痛待查"})
    assert admitted.status_code == 201, admitted.text
    body = admitted.json()
    assert (body["ward_name"], body["bed_no"], body["patient_name"]) == ("P21335 内科病区", "01", "P21335 王五")

    moved = client.post(f"/api/inpatient/admissions/{body['id']}/transfer", headers=admin, json={
        "ward_id": surgical, "bed_id": surgical_beds["02"]})
    assert moved.status_code == 200, moved.text
    moved = moved.json()
    assert (moved["ward_name"], moved["bed_no"], moved["patient_name"]) == ("P21335 外科病区", "02", "P21335 王五")
    # 床位主键全县连续编号（外科 02 床排在内科五张床之后），床号按病区从头编：修前页面印的是前者「外科病区 / 7」
    assert moved["bed_id"] == surgical_beds["02"] and str(moved["bed_id"]) != "2"
    assert client.get(f"/api/inpatient/admissions?patient_id={patient}", headers=admin).json() == [moved]

    client.post(f"/api/inpatient/admissions/{body['id']}/case-summary", headers=admin, json={
        "discharge_diagnosis": "急性阑尾炎", "outcome": "治愈"})
    out = client.post(f"/api/inpatient/admissions/{body['id']}/discharge", headers=admin)
    assert out.status_code == 200, out.text
    assert out.json() == {**moved, "status": "discharged", "discharged_at": out.json()["discharged_at"]}


@contextmanager
def _count_sql():
    from app.database import engine

    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


def test_住院清单带名字_查询数不随行数涨(client, admin):
    """病区名、床号、姓名按这一页各取一次：一页多出 10 位在院、各在不同病区，SQL 条数不变（逐行查就是一页 1500 次往返）。"""
    from app.database import SessionLocal
    from app.models import Admission, Bed, Patient, User, Ward
    from app.visibility import clear_visibility_cache

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21335 查询数县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]

    def admit(count, tag):
        with SessionLocal() as db:
            operator = db.query(User).filter(User.username == "admin").one().id
            for i in range(count):
                ward = Ward(org_id=org, name=f"P21335 {tag}病区{i}")
                patient = Patient(ehc_no=f"P21335-{tag}-{i}", name=f"P21335 {tag}患者{i}",
                                  id_card=f"3301061980{tag}{i:06d}")
                db.add_all([ward, patient])
                db.flush()
                bed = Bed(ward_id=ward.id, bed_no=f"{i + 1:02d}", status="occupied")
                db.add(bed)
                db.flush()
                db.add(Admission(patient_id=patient.id, org_id=org, ward_id=ward.id, bed_id=bed.id,
                                 diagnosis_name="肺炎", created_by=operator))
            db.commit()

    def listed():
        clear_visibility_cache()
        with _count_sql() as counter:
            resp = client.get("/api/inpatient/admissions?status=admitted&limit=500", headers=admin)
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert all(r["patient_name"] and r["bed_no"] and r["ward_name"] for r in rows), rows   # 修前没有这三键
        return counter["n"], len(rows)

    admit(2, "10")
    listed()   # 预热：首个请求可能多几条一次性的查询
    before, rows_before = listed()
    admit(10, "20")
    after, rows_after = listed()
    assert rows_after == rows_before + 10
    assert after == before, f"查询数随行数增长：{before} -> {after}"


def _function_body(path, header):
    source = (STATIC / path).read_text(encoding="utf-8")
    start = source.index(header)
    return source[start:source.index("\n}\n", start)]


def test_三处按病区床号姓名显示_不再印床位主键():
    for path, header in SITES:
        body = _function_body(path, header)
        assert "a.bed_id" not in body, path   # 修前住院页印 `${a.bed_id}`：床位主键当床号
        assert "${esc(a.ward_name)} ${esc(a.bed_no)}" in body, path   # 与居民端「科室床位」同一写法
        assert "esc(a.patient_name)" in body, path
    docs = _function_body("pages-mgmt.js", "async function renderClinicalDocs()")
    assert "患者${a.patient_id}" not in docs   # 修前「#住院号 患者ID 诊断」
