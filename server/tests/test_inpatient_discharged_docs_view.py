"""出院后，病程、护理记录、体温单、文书完整性在任何页面都看不到（P2-1768，第五十二批扫描 AP3-4）。

住院临床文书页（`pages-mgmt.js::renderClinicalDocs`）与医生移动端查房的住院下拉只取在院的（P2-154 / P2-1333），住院页已出院的
那一行只有「病案首页」「医嘱单」与打印按钮；病程、护理、体征、完整性四个读接口对已出院的住院照常给数据（按患者可见性判、留痕），
全前端调它们的只有那两处。修前实测：出院 200 → `status=admitted` 返回 `[]`；`GET …/progress-notes` 仍是 200、`X-Total-Count =
103`——终末质控、病案归档、再入院时医生想看上次的病程 / 体温单，界面上都无处可看。

修法：文书页加「已出院的住院（只读查看）」下拉（取一页最近出院的，列不全时首项写明共几次），选了就把四块画成那一次的——
不画写入表单，也不挂提交处理（出院后病程、护理、体征都 409）；取数照旧走那四个读接口，可见性与留痕不变。选在院的、或选回首项，
回到在院患者的写入视图。放在本页而不在住院页另画一份：四块与在院的走同一段渲染，表头与体温单曲线只有一份。打印件不在本条。

页面函数原样拿到 node 里跑（`clinical_docs_page.py`），请求以同院医师的身份转给真接口。
"""
import shutil

import pytest

import clinical_docs_page
from conftest import login

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 四个读接口的留痕资源名（`clinical_docs._admission_or_404` 的 resource）
READ_RESOURCES = {"progress_note", "nursing_record", "vital_sign", "document_completeness"}


@pytest.fixture(scope="module")
def world(client, admin):
    """同一病区两位：甲在院；乙写过病程、护理、体征之后出院。页面以同院医师的身份打开。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21768 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21768_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "P21768 医师"})
    assert created.status_code in (200, 201), created.text
    doctor = login(client, "p21768_doc", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21768 内科病区"}).json()["id"]
    ids = {}
    for key, i in (("in", 0), ("out", 1)):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"D{i}"}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21768 患者{key}", "id_card": f"33010619780808{1768 + i:04d}"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=doctor, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "社区获得性肺炎"})
        assert adm.status_code == 201, adm.text
        ids[key] = adm.json()["id"]
        ids[f"{key}_patient"] = patient
    base = f"/api/inpatient/admissions/{ids['in']}"
    assert client.post(f"{base}/progress-notes", headers=doctor, json={
        "note_type": "daily", "content": "P21768 在院甲的日常病程"}).status_code == 201
    base = f"/api/inpatient/admissions/{ids['out']}"
    for note_type, content in (("first", "P21768 乙的首次病程"), ("daily", "P21768 乙的日常病程")):
        assert client.post(f"{base}/progress-notes", headers=doctor, json={
            "note_type": note_type, "content": content}).status_code == 201
    assert client.post(f"{base}/nursing-records", headers=doctor, json={
        "content": "P21768 乙的护理记录"}).status_code == 201
    for at, temp in (("2026-10-08 08:00", 38.9), ("2026-10-08 16:00", 37.4)):
        assert client.post(f"{base}/vitals", headers=doctor, json={"measured_at": at, "temperature": temp,
                                                                    "pulse": 90}).status_code == 201
    assert client.post(f"{base}/case-summary", headers=doctor, json={
        "discharge_diagnosis": "社区获得性肺炎", "outcome": "好转"}).status_code == 201
    assert client.post(f"{base}/discharge", headers=doctor).status_code == 200
    return {**ids, "doctor": doctor}


def _access_rows(patient_id: int) -> list[str]:
    from app.database import SessionLocal
    from app.models import AccessLog

    with SessionLocal() as db:
        return [r.resource for r in db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.username == "p21768_doc").all()]


def test_接口前提_出院后四个读接口照常给数据_写一律409(client, world):
    base = f"/api/inpatient/admissions/{world['out']}"
    for path in ("progress-notes", "nursing-records", "vitals", "document-completeness"):
        assert client.get(f"{base}/{path}", headers=world["doctor"]).status_code == 200, path
    assert client.post(f"{base}/progress-notes", headers=world["doctor"], json={
        "note_type": "discharge", "content": "出院记录"}).status_code == 409
    in_hospital = client.get("/api/inpatient/admissions?status=admitted", headers=world["doctor"]).json()
    assert world["out"] not in [a["id"] for a in in_hospital]   # 在院下拉里没有它：修前页面上无处可看


def test_出院后从只读入口读得到四项_没有写入表单_在院的照旧(client, world):
    steps = """
      await renderClinicalDocs();
      const before = pageHtml();
      if (!elements["#doc-discharged select"]) return { before };   // 修前页面上没有这张下拉
      elements["#doc-discharged select"].onchange({ target: { value: String(ARGS.params.out) } });
      const routed = ROUTED;
      await renderClinicalDocs();   // route() 只计次：照它重画一遍
      const readOnly = pageHtml();
      elements["#doc-pick select"].onchange({ target: { value: String(ARGS.params.in) } });
      await renderClinicalDocs();
      return { before, readOnly, back: pageHtml(), routed, posts };
    """
    logged = len(_access_rows(world["out_patient"]))
    result, requests = clinical_docs_page.run(client, world["doctor"], steps,
                                              params={"in": world["in"], "out": world["out"]},
                                              storage={"medplat_doc_adm": str(world["in"])})
    before = result["before"]
    forms = ('id="note-form"', 'id="nursing-form"', 'id="vital-form"')

    # 在院的照旧：四块画甲、三张写入表单都在；下拉里列着已出院的乙（修前没有这张下拉）
    assert all(form in before for form in forms)
    assert "P21768 在院甲的日常病程" in before
    assert f'<option value="{world["out"]}">' in before
    read_only, back = result["readOnly"], result["back"]

    # 只读查看乙：病程、护理、体温单（曲线与表）、完整性都在，写入表单与消息行一个都没有
    assert result["routed"] == 1
    for text in ("P21768 乙的首次病程", "P21768 乙的日常病程", "P21768 乙的护理记录", "2026-10-08 08:00", "38.9"):
        assert text in read_only, text
    assert "<svg" in read_only and "缺项：" not in read_only and "文书完整" in read_only
    assert not any(form in read_only for form in forms), read_only
    assert 'id="doc-msg"' not in read_only
    assert "P21768 在院甲的日常病程" not in read_only
    assert f'<option value="{world["out"]}" selected>' in read_only
    banner = read_only[read_only.index('<p class="desc" id="doc-readonly">'):]
    banner = banner[:banner.index("</p>")]
    assert "P21768 患者out" in banner and "只读" in banner and "不能再写" in banner   # 写明看的是哪一位、只读
    assert 'id="doc-readonly"' not in before and 'id="doc-readonly"' not in back
    assert result["posts"] == []

    # 取数走那四个读接口（可见性与留痕不变），不取乙的医嘱
    out = f"/api/inpatient/admissions/{world['out']}/"
    got = {path[len(out):] for method, path in requests if path.startswith(out)}
    assert got == {"progress-notes", "nursing-records", "vitals", "document-completeness"}, got
    assert not any(f"admission_id={world['out']}" in path for _, path in requests)
    assert READ_RESOURCES <= set(_access_rows(world["out_patient"])[logged:])

    # 选回在院的：写入视图回来
    assert all(form in back for form in forms) and "P21768 在院甲的日常病程" in back


def test_没有在院患者时照样能只读查看出院的(client, world):
    """在院清单为空（`current` 为 0）时，只读查看不依赖在院的那一位。"""
    steps = """
      await renderClinicalDocs();
      const empty = pageHtml();
      if (!elements["#doc-discharged select"]) return { empty, html: "" };   // 修前页面上没有这张下拉
      elements["#doc-discharged select"].onchange({ target: { value: String(ARGS.params.out) } });
      await renderClinicalDocs();
      return { empty, html: pageHtml() };
    """
    # 甲也出院之后，在院清单是空的
    base = f"/api/inpatient/admissions/{world['in']}"
    assert client.post(f"{base}/case-summary", headers=world["doctor"], json={
        "discharge_diagnosis": "社区获得性肺炎", "outcome": "治愈"}).status_code == 201
    assert client.post(f"{base}/discharge", headers=world["doctor"]).status_code == 200
    result, _ = clinical_docs_page.run(client, world["doctor"], steps, params={"out": world["out"]})
    assert "暂无在院患者" in result["empty"]
    assert "P21768 乙的首次病程" in result["html"] and 'id="note-form"' not in result["html"]
