"""危急值「处置反馈」的说明可以留空、可以只填空格，照样闭环（P2-1710，第五十批扫描 AN2-2）。

`CriticalResolveBody.note` 原先是 `Field(default="", max_length=512)`：空 body、`{"note": ""}`、一串空格都 200，危急值就此
`resolved`；处置轨迹只记一句「处置反馈完成」或「处置反馈：   」，处置了什么无从查起。数据质控的「危急值闭环」规则只认 `resolved`，
查不出这一类。两个页面（管理端危急值操作台、医生移动端）的反馈框都非必填，而它们的注释都把「危急值就此闭环、处置说明一个字
没有」写成 P2-38 要消除的害处；用户手册写「处置后反馈处置结果（附说明）」。修前实测：空 body 200、`critical_status: resolved`，
轨迹第 3 条 `处置反馈完成`；纯空格 200，轨迹 `处置反馈：   `。

修法：`note` 改成必填文本（`Field(min_length=1, max_length=512, pattern=NON_BLANK)`，照 `texttypes` 的约定，至少一个看得见的
字）；两个页面的反馈框设必填（管理端的模态框字段写 `required: true`，`spdModal` 的多行框原先不认它，一并补上）。仓内调用点只有
这两页，现有用例都带说明。
"""
import itertools
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import CriticalAction, ExamReport, ExamRequest, User

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def new_report(client, admin):
    """造一份已确认接收、待处置反馈的危急值报告。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21710 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21710 患者", "id_card": "330127197001011710"}).json()["id"]
    seq = itertools.count(1)

    def make():
        with SessionLocal() as db:
            creator = db.query(User).filter_by(username="admin").one().id
            request = ExamRequest(patient_id=patient, from_org_id=org, center_type="lab", item_code=f"P21710-{next(seq)}",
                                  item_name="血钾", status="reported", created_by=creator)
            db.add(request)
            db.flush()
            report = ExamReport(request_id=request.id, conclusion="血钾 2.6 mmol/L", critical=True,
                                critical_status="acknowledged")
            db.add(report)
            db.commit()
            return report.id

    return make


def _state(report_id):
    with SessionLocal() as db:
        return (db.get(ExamReport, report_id).critical_status,
                [a.action for a in db.query(CriticalAction).filter_by(report_id=report_id).order_by(CriticalAction.id)])


@pytest.mark.parametrize("body", [
    {},                      # 不带说明：修前缺省空串，轨迹记「处置反馈完成」
    {"note": ""},
    {"note": "   "},         # 修前轨迹「处置反馈：   」
    {"note": "　\t\n"},  # 全角空格、制表、换行
    {"note": "​﻿"},  # 零宽空格、BOM：看不见的格式字符
], ids=["缺省", "空串", "半角空格", "全角空白", "零宽字符"])
def test_说明留空或看不见_422_仍是已确认_轨迹不增加(client, admin, new_report, body):
    report_id = new_report()
    before = _state(report_id)
    resp = client.post(f"/api/exams/reports/{report_id}/resolve", headers=admin, json=body)
    assert resp.status_code == 422, resp.text   # 修前 200：危急值就此闭环
    assert _state(report_id) == before == ("acknowledged", [])


def test_带说明照常闭环_轨迹记下说明(client, admin, new_report):
    report_id = new_report()
    resp = client.post(f"/api/exams/reports/{report_id}/resolve", headers=admin, json={"note": " 已补钾并复查 "})
    assert resp.status_code == 200 and resp.json()["critical_status"] == "resolved", resp.text
    assert _state(report_id) == ("resolved", ["处置反馈： 已补钾并复查 "])   # 合法值原样收下，不替人 strip


def test_两个页面的反馈框必填():
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = clinical.index('const done = await spdModal("处置反馈", [')
    field = clinical[start:clinical.index("]", start)]
    assert 'name: "note"' in field and "required: true" in field, field   # 修前不带 required
    # spdModal 的多行框认 required：修前只有单行框带上，字段表写了 `required: true` 的多行框照样空着就能交
    modal = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    control = modal[modal.index('if (f.type === "textarea") {'):]
    control = control[:control.index("</textarea>`;")]
    assert '${f.required ? " required" : ""}' in control, control
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    start = doctor.index('"crit-resolve-form",')
    textarea = doctor[start:doctor.index("</textarea>", start)]
    assert '<textarea name="note"' in textarea and " required>" in textarea, textarea   # 修前不带 required
