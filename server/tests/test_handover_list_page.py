"""住院交接班只记得进、没有一个页面看得见：接班的人无从读起（P2-476，第八批扫描 V1-3）。

「住院临床文书」页的交接班一块只有交班表单；`GET /api/inpatient/handovers` 一直在（按本机构病区收口，P0-48），前端一个
调用都没有——交班内容写着「几床谁病危、注意什么」，写完就只躺在库里。那一块还挂在「有在院住院记录」的条件里：全院一个
在院患者都没有时，交接班连写都写不了（交接班按病区、不挂某次住院）。病区要手填编号，填错就 404。

修法：交接班一块挪出住院记录的条件；补清单（按病区 / 日期筛，条件只留在内存里；截到 50 条时明说），班次文案取自后端
（`shift_name`）；交班表单的病区改成下拉，默认当前住院记录所在病区。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _clinical_docs_page() -> str:
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderClinicalDocs()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_交接班清单有入口_按病区与日期筛():
    page = _clinical_docs_page()
    assert "api(`/api/inpatient/handovers${handoverQuery.toString() ? `?${handoverQuery}` : \"\"}`)" in page   # 修前没有
    form = page[page.index('<form class="inline" id="handover-filter">'):]
    form = form[:form.index("</form>")]
    assert 'name="ward_id"' in form and 'name="handover_date"' in form
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    assert 'const HANDOVER_FILTER = { ward_id: "", handover_date: "" };' in source   # 只留在内存里
    table = page[page.index('table(["日期", "班次", "病区", "交班 → 接班"'):]
    table = table[:table.index("</tr>`)")]
    for field in ("h.handover_date", "h.shift_name", "h.from_staff", "h.to_staff", "h.patient_count",
                  "h.critical_count", "h.content"):
        assert field in table, field
    assert "esc(h.shift)" not in table   # 班次文案取自后端
    assert "只列最新 50 条" in page


def test_交接班不挂在住院记录的条件里():
    page = _clinical_docs_page()
    body = page[page.index('$("#page-body").innerHTML = `'):]
    body = body[:body.index("`;\n")]
    # 病程 / 护理 / 体温单挂在当前住院记录上；交接班按病区，放在那段条件之后
    assert body.index('` : ""}') < body.index("${panel(`交接班（"), "交接班一块还在「有在院住院记录」的条件里"
    handlers = page[page.index('$("#doc-pick").onsubmit'):]
    assert handlers.index('$("#handover-form").onsubmit') < handlers.index("if (!current) return;")
    assert handlers.index('$("#handover-filter").onsubmit') < handlers.index("if (!current) return;")


def test_交班表单病区是下拉_不再手填编号():
    page = _clinical_docs_page()
    form = page[page.index('<form class="inline" id="handover-form">'):]
    form = form[:form.index("</form>")]
    assert re.search(r'<select name="ward_id" required>', form)
    assert 'type="number" placeholder="病区ID"' not in form


def test_清单带班次文案(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2476 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2476 内科病区"}).json()["id"]
    for shift in ("day", "evening", "night"):
        created = client.post("/api/inpatient/handovers", headers=admin, json={
            "ward_id": ward, "shift": shift, "handover_date": "2026-09-27", "from_staff": "甲", "to_staff": "乙",
            "content": f"P2476 {shift}"})
        assert created.status_code == 201, created.text
    rows = client.get("/api/inpatient/handovers", headers=admin,
                      params={"ward_id": ward, "handover_date": "2026-09-27"}).json()
    assert {r["shift"]: r["shift_name"] for r in rows} == {"day": "白班", "evening": "小夜", "night": "大夜"}
