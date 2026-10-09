"""出院确认框列了四条后果，没写「此后病程（含出院记录）、护理、体征都不能再写」（P2-1772，第五十二批扫描 AP3-10）。

住院页「出院」的确认框（P2-1334 立的规矩：写明几项后果、标「不可撤销」）列了停医嘱、释放床位、派随访与通知、慢专病派计划，
没写文书：后端出院后病程、护理、体征一律 409（`clinical_docs.py` 三个写端点的「患者已出院，不可再……」），用户手册写着
「患者出院后不可再补录」。修前实测：点确定后再写出院记录 409，完整性仍报「完整」——出院记录这时已经无处可写。

修法：确认框加上这一条（出院后的书写窗口怎么定随 P2-1321，这里只写现状）。点取消仍在院、确定才出院由端到端
`test_住院页出院先确认_取消仍在院_确定才出院` 按接口核对（它只钉「不可撤销」，没钉原文，不用跟着改）。
"""
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _discharge_intro() -> str:
    """住院页点击处理里「出院」分支开头到 POST 地址之间的那一段（确认框的原文在这里）。"""
    start = SOURCE.index("async function renderInpatient()")
    body = SOURCE[start:SOURCE.index("\nasync function ", start + 1)]
    branch = body[body.index("if (d.discharge)"):]
    return branch[:branch.index("/discharge`")]


def test_确认框写明出院后病程护理体征都不能再写():
    intro = _discharge_intro()
    assert "· 此后这次住院的病程（含出院记录）、护理记录、体征都不能再写" in intro, intro   # 修前没有这一条
    assert intro.index("不能再写") < intro.index("点「取消」不办。")   # 列在后果里，不在「取消」之后


def test_接口前提_出院后病程护理体征一律409(client, admin):
    """确认框只写现状：三个写端点出院后确实都拒（措辞与确认框同一件事）。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21772 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21772 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "C1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21772 患者", "id_card": "330106198102021772"}).json()["id"]
    aid = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"}).json()["id"]
    base = f"/api/inpatient/admissions/{aid}"
    assert client.post(f"{base}/case-summary", headers=admin, json={
        "discharge_diagnosis": "肺炎", "outcome": "好转"}).status_code == 201
    assert client.post(f"{base}/discharge", headers=admin).status_code == 200
    for path, body, detail in (
        ("progress-notes", {"note_type": "discharge", "content": "出院记录"}, "患者已出院，不可再书写病程记录"),
        ("nursing-records", {"content": "出院宣教"}, "患者已出院，不可再书写护理记录"),
        ("vitals", {"measured_at": "2026-10-09 08:00", "temperature": 36.6}, "患者已出院，不可再记录体征"),
    ):
        resp = client.post(f"{base}/{path}", headers=admin, json=body)
        assert (resp.status_code, resp.json()) == (409, {"detail": detail}), path
