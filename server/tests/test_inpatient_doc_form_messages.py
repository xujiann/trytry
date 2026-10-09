"""桌面住院临床文书页护理、体征的报错写进「病程记录」面板的消息行（P2-1770，第五十二批扫描 AP3-8）。

`pages-mgmt.js::renderClinicalDocs` 只有病程面板里有一行消息（`#doc-msg`），护理、体征两张表单提交失败也写它：体温填 365、
血压录反、出院后 409 这类报错都落在页面上方的病程面板，中间隔着最多 100 行病程表和 100 行护理表，在体征面板操作的人看到的是
「点了没反应」。医生移动端同形已修（P2-1093，`m/doctor.html` 注释「体温敲成 365 的报错不在体征表单旁边」），体检同形 P2-1404。

修法：护理、体征面板各带自己的消息行（`#nursing-msg` / `#vital-msg`），三张表单各写各的。

页面函数原样拿到 node 里跑（`clinical_docs_page.py`），提交转给真接口。
"""
import re
import shutil
from pathlib import Path

import pytest

import clinical_docs_page

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
MGMT = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")

#: 表单 → 它的消息行（各在自己那块面板里）
FORM_MSG = {"note-form": "doc-msg", "nursing-form": "nursing-msg", "vital-form": "vital-msg"}
#: 面板标题打头的字 → 面板里的表单
PANEL_FORM = {"病程记录": "note-form", "护理记录": "nursing-form", "体温单": "vital-form"}


def _page() -> str:
    start = MGMT.index("async function renderClinicalDocs()")
    return MGMT[start:MGMT.index("\nasync function ", start + 1)]


def test_三张表单各写各的消息行():
    page = _page()
    for form, msg in FORM_MSG.items():
        handler = page[page.index(f'$("#{form}").onsubmit'):]
        handler = handler[:handler.index(" };")]
        assert f'"#{msg}"' in handler, (form, handler)   # 修前护理、体征都写 #doc-msg
        assert page.count(f'id="{msg}"') == 1, msg


@pytest.fixture(scope="module")
def admission(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21770 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21770 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "M1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21770 患者", "id_card": "330106197909091770"}).json()["id"]
    created = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _panels(html: str) -> dict:
    """渲染出来的页面按面板切开：{标题: 面板 HTML}。"""
    parts = re.split(r'(?=<div class="panel">)', html)
    return {re.search(r"<h3>([^<（]*)", p).group(1): p for p in parts if p.startswith('<div class="panel"><h3>')}


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_体温填365_报错出现在体温单面板里(client, admin, admission):
    steps = """
      await renderClinicalDocs();
      await submitForm("vital-form", { measured_at: "2026-10-09T08:00", temperature: "365" });
      await submitForm("nursing-form", { content: "   " , inpatient_order_id: "" });
      const cls = (sel) => (elements[sel] ? elements[sel].className : "");
      return { html: pageHtml(), msgs: { doc: msgOf("#doc-msg"), nursing: msgOf("#nursing-msg"), vital: msgOf("#vital-msg") },
               classes: { nursing: cls("#nursing-msg"), vital: cls("#vital-msg") } };
    """
    result, _ = clinical_docs_page.run(client, admin, steps, storage={"medplat_doc_adm": str(admission)})
    msgs = result["msgs"]
    assert "temperature" in msgs["vital"] and "不能大于 45" in msgs["vital"], msgs   # 修前写进病程面板的 #doc-msg
    assert "护理记录要写内容" in msgs["nursing"], msgs
    assert msgs["doc"] == "", msgs
    assert result["classes"] == {"nursing": "msg err", "vital": "msg err"}
    panels = _panels(result["html"])
    for title, form in PANEL_FORM.items():
        assert f'id="{form}"' in panels[title] and f'id="{FORM_MSG[form]}"' in panels[title], title   # 消息行就在表单那块面板里
