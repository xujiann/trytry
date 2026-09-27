"""处方、检查检验申请单的状态：列表页、打印件、报错同一句（P2-575，第十一批「导出 / 打印 vs 清单」扫描 Y1-3）。

同一个状态码原先有三套措辞：
- 处方：列表页（core.js `RX_STATUS`）「待药师审 / 药师审通过 / 已退回」，处方笺打印件自带一份「待药师审核 / 药师审核通过」，
  报错（`prescriptions.PRESCRIPTION_STATUS_NAMES`，照抄模型列注释）说「退回」；
- 申请单：列表页「已互认」，打印件「结果互认」，报错「互认既往结果」；
- 检验样本：列表页没采样的写「未采样」、核收的写「已核收」，打印件写「—」「中心已核收」，模型列注释又是「中心核收」。

打印件自己的注释写着「打印件与列表页读起来必须是同一句话」（转诊那张早就收成了一份）。修后措辞取列表页那一套——
工作人员天天看的是它，「已退回」「已互认」读作状态，「退回」读着像按钮——模型列注释与后端文案表跟着改；打印件不再
自带，直接用后端那几张表；页面上的表与后端逐字相同，这里钉住。
"""
import ast
import re
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal
from app.models import ExamRequest, Prescription
from app.routers.exams import EXAM_REQUEST_STATUS_NAMES, EXAM_SAMPLE_STATUS_NAMES
from app.routers.prescriptions import PRESCRIPTION_STATUS_NAMES

APP = Path(__file__).resolve().parents[1] / "app"


def _page_labels(name: str) -> dict[str, str]:
    """core.js 里列表页那张表的文案（值是 `["文案", "配色"]` 或 `"文案"`；空串键写作 `""`）。"""
    match = re.search(rf"const {name} = \{{(.*?)\}};", (APP / "static" / "core.js").read_text(encoding="utf-8"))
    assert match, f"core.js 里找不到 {name}"
    return dict(re.findall(r'"?(\w*)"?: \[?"([^"]*)"', match.group(1)))


def test_列表页的状态表与后端同一份():
    assert _page_labels("RX_STATUS") == PRESCRIPTION_STATUS_NAMES      # 修前后端 rejected 是「退回」
    assert _page_labels("EXAM_STATUS") == EXAM_REQUEST_STATUS_NAMES    # 修前后端 recognized 是「互认既往结果」
    assert _page_labels("SAMPLE_STATUS") == EXAM_SAMPLE_STATUS_NAMES   # 修前后端没有这张表


def test_打印件不再自带这几张状态表():
    tree = ast.parse((APP / "routers" / "printing.py").read_text(encoding="utf-8"))
    own = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and {k.value for k in node.keys if isinstance(k, ast.Constant)} & {"pending_review", "recognized", "in_transit"}
    ]
    assert own == [], f"printing.py 第 {own} 行又抄了一份处方 / 申请单状态表——用 prescriptions / exams 那几张"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2575 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2575 患者", "id_card": "330127196001012575"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2575_doc", "password": "pass123456", "role": "doctor", "org_id": org})
    assert created.status_code == 201, created.text
    doctor = login(client, "p2575_doc", "pass123456")
    rx = client.post("/api/prescriptions", headers=doctor, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "P2575 高血压",
        "items": [{"drug_code": "P2575-D", "drug_name": "P2575 氨氯地平片", "daily_dose": 5.0, "days": 7}]})
    assert rx.status_code == 201, rx.text
    requests = {}
    for center in ("lab", "imaging"):
        req = client.post("/api/exams", headers=doctor, json={
            "patient_id": patient, "from_org_id": org, "center_type": center,
            "item_code": f"P2575-{center}", "item_name": f"P2575 {center}"})
        assert req.status_code == 201, req.text
        requests[center] = req.json()["id"]
    return {"rx": rx.json()["id"], "requests": requests}


def _set(model, row_id: int, **values) -> None:
    with SessionLocal() as db:
        row = db.get(model, row_id)
        for key, value in values.items():
            setattr(row, key, value)
        db.commit()


@pytest.mark.parametrize("status", sorted(PRESCRIPTION_STATUS_NAMES))
def test_处方笺上的状态与列表页同一句(client, admin, world, status):
    _set(Prescription, world["rx"], status=status)
    html = client.get(f"/api/print/prescriptions/{world['rx']}", headers=admin).text
    # 修前 pending_review 印「待药师审核」、approved 印「药师审核通过」
    assert f"审核意见（{_page_labels('RX_STATUS')[status]}）" in html


@pytest.mark.parametrize("status", sorted(EXAM_REQUEST_STATUS_NAMES))
def test_申请单打印件的状态与列表页同一句(client, admin, world, status):
    _set(ExamRequest, world["requests"]["lab"], status=status)
    html = client.get(f"/api/print/exam-requests/{world['requests']['lab']}", headers=admin).text
    # 修前 recognized 印「结果互认」
    assert f'<td class="k">当前状态</td><td>{_page_labels("EXAM_STATUS")[status]}</td>' in html


@pytest.mark.parametrize("sample", sorted(EXAM_SAMPLE_STATUS_NAMES))
def test_检验申请单的样本状态与列表页同一句(client, admin, world, sample):
    _set(ExamRequest, world["requests"]["lab"], status="pending", sample_status=sample)
    html = client.get(f"/api/print/exam-requests/{world['requests']['lab']}", headers=admin).text
    # 修前没采样的印「—」、核收的印「中心已核收」
    assert f"<td>{_page_labels('SAMPLE_STATUS')[sample]}</td></tr></tbody>" in html


def test_别的中心没有样本物流_这一栏照旧是横杠(client, admin, world):
    html = client.get(f"/api/print/exam-requests/{world['requests']['imaging']}", headers=admin).text
    assert "<td>—</td></tr></tbody>" in html   # 与列表页同：非检验类这一栏是「—」
