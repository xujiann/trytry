"""FHIR DiagnosticReport 入站的 presentedForm 按 contentType 挑文本件、按声明的字符集解码（P2-1111，第三十二批「导出文件的格式
陷阱」扫描 B3-3）。

原先只取 presentedForm[0]、一律按 UTF-8 解：contentType 写 `text/plain; charset=GB18030`（或 GBK）的所见、先放一份 PDF 再放
文本的，整单 422，报错说成「不是合法的 base64 文本」——结论与危急值标记跟着一起被拒。修后挑第一份文本件、按它声明的字符集
解码（没写照旧按 UTF-8）；一份文本件都没有的照旧拒收，但把原因说对。
"""
import base64

import pytest

from app.database import SessionLocal
from app.models import ExamReport

URL = "/api/integration/fhir/DiagnosticReport"
FINDING = "右肺上叶见 6mm 磨玻璃结节，边界清"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21111 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21111 甲", "id_card": "330127196001031111"}).json()["id"]
    return {"org": org, "patient": patient}


def _request(client, admin, world) -> int:
    resp = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "imaging",
        "item_code": "CT01", "item_name": "胸部CT"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _form(content_type, text=FINDING, encoding="utf-8") -> dict:
    form = {"data": base64.b64encode(text.encode(encoding)).decode()}
    if content_type is not None:
        form["contentType"] = content_type
    return form


def _post(client, admin, request_id, forms):
    return client.post(URL, headers=admin, json={
        "resourceType": "DiagnosticReport", "status": "final",
        "basedOn": [{"reference": f"ServiceRequest/{request_id}"}],
        "conclusion": "右肺上叶磨玻璃结节，建议随访", "presentedForm": forms,
        "extension": [{"url": "urn:medplat:critical", "valueBoolean": True}]})


def _finding(request_id):
    with SessionLocal() as db:
        report = db.query(ExamReport).filter(ExamReport.request_id == request_id).one_or_none()
        return None if report is None else (report.finding, report.critical)


@pytest.mark.parametrize("content_type, encoding", [
    ("text/plain; charset=GB18030", "gb18030"),
    ("text/plain; charset=GBK", "gbk"),
    ('text/plain; charset="gb2312"', "gb2312"),
])
def test_声明了国标字符集的所见按声明解码_不再整单拒收(client, admin, world, content_type, encoding):
    rid = _request(client, admin, world)
    resp = _post(client, admin, rid, [_form(content_type, encoding=encoding)])
    assert resp.status_code == 201, resp.text   # 修前 422「不是合法的 base64 文本」
    assert _finding(rid) == (FINDING, True)


def test_先放PDF再放文本_取文本那一份(client, admin, world):
    rid = _request(client, admin, world)
    pdf = {"contentType": "application/pdf", "data": base64.b64encode(b"%PDF-1.4\n\xe2\xe3\xcf\xd3").decode()}
    resp = _post(client, admin, rid, [pdf, _form("text/plain; charset=utf-8")])
    assert resp.status_code == 201, resp.text   # 修前按 UTF-8 解 PDF，422
    assert _finding(rid) == (FINDING, True)


def test_没写contentType与没写charset的照旧按UTF8(client, admin, world):
    for content_type in (None, "text/plain"):
        rid = _request(client, admin, world)
        resp = _post(client, admin, rid, [_form(content_type)])
        assert resp.status_code == 201, resp.text
        assert _finding(rid) == (FINDING, True)


def test_只有PDF_照旧拒收但说对原因(client, admin, world):
    rid = _request(client, admin, world)
    pdf = {"contentType": "application/pdf", "data": base64.b64encode(b"%PDF-1.4\n\xe2\xe3\xcf\xd3").decode()}
    resp = _post(client, admin, rid, [pdf])
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "application/pdf" in detail and "base64" not in detail, detail   # 修前说成 base64 坏了
    assert _finding(rid) is None


def test_字符集与内容不符_坏base64_不认识的字符集_各报各的(client, admin, world):
    cases = [
        ([_form("text/plain; charset=utf-8", encoding="gb18030")], "解不开"),
        ([{"contentType": "text/plain", "data": "@@@不是base64@@@"}], "base64"),
        ([_form("text/plain; charset=x-no-such-charset")], "不认识"),
        ({"contentType": "text/plain"}, "必须是数组"),
    ]
    for forms, word in cases:
        rid = _request(client, admin, world)
        resp = _post(client, admin, rid, forms)
        assert resp.status_code == 422 and word in resp.json()["detail"], (forms, resp.text)
        assert _finding(rid) is None
