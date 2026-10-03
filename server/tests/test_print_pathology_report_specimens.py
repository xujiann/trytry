"""病理报告单印标本：标本号（病理号）、送检部位、离体 / 固定时间、固定液，多标本逐行，拒收的标明原因（P2-1239，
第三十六批「打印件与对外文书的必备要素」扫描 U4-3）。

标本号由平台生成，注释自己写着「唯一性是全部价值」——切片、蜡块按它归档，多部位取材时结论要对得上是哪个标本；送检
部位、离体与固定时间、固定液都在 `pathology_specimens` 里（一张申请可以有多个标本）。报告打印件原先不读标本表：登记
标本 P000001「左乳外上象限肿物」、09:00 离体、09:20 固定，核收、阅片后出报告「（左乳）导管内乳头状瘤」，打印件上没有
P000001、部位、时间、固定液，连「标本」两个字都没有（修前实测五项都是 False）。

修法：病理中心的报告打印件在「检查所见」前加一张「标本」表，按登记先后逐行列出，被拒收的在标本号后标「拒收：原因」，
没登记标本的写「未登记标本」（与处方笺「无药品明细」同一写法）。影像 / 心电 / 检验的报告字节不变。检验类的样本接收
时间库里没有（样本流转只改状态、不记时刻），要加列，不在本条。
"""
import re

import pytest

from app.clock import to_local
from app.database import SessionLocal
from app.models import ExamReport
from conftest import login

ORG = "P21239 县人民医院"
ITEMS = {"imaging": ("CT-CHEST", "胸部CT平扫"), "ecg": ("ECG-12", "十二导联心电图"), "lab": ("K-POTASSIUM", "血清钾测定"),
         "pathology": ("PATH-BX", "组织病理学检查")}
CENTER_LABELS = {"imaging": "影像", "ecg": "心电", "lab": "检验"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": ORG, "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21239_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "病理王五"})
    assert created.status_code in (200, 201), created.text
    doc = login(client, "p21239_doc", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21239 患者", "id_card": "330281198002021239", "gender": "女", "birth_date": "1980-02-02",
        "phone": "13800001239"}).json()

    def request(center):
        code, name = ITEMS[center]
        req = client.post("/api/exams", headers=doc, json={
            "patient_id": patient["id"], "from_org_id": org, "center_type": center, "item_code": code,
            "item_name": name, "clinical_info": "乳腺肿物"})
        assert req.status_code == 201, req.text
        return req.json()["id"]

    def report(request_id):
        assert client.post(f"/api/exams/{request_id}/claim", headers=doc).status_code == 200
        rep = client.post(f"/api/exams/{request_id}/report", headers=doc, json={
            "finding": "镜下见导管内乳头状增生", "conclusion": "（左乳）导管内乳头状瘤", "critical": False,
            "reported_by": "病理王五"})
        assert rep.status_code == 201, rep.text
        return rep.json()["id"]

    def specimen(request_id, **fields):
        resp = client.post("/api/pathology/specimens", headers=doc, json={"request_id": request_id, **fields})
        assert resp.status_code == 201, resp.text
        return resp.json()

    two = request("pathology")
    kept = specimen(two, site="左乳外上象限肿物", excised_at="2026-09-20 09:00", fixed_at="2026-09-20 09:20",
                    fixative="10%中性福尔马林")
    assert client.post(f"/api/pathology/specimens/{kept['id']}/receive", headers=doc,
                       json={"received_by": "病理王五"}).status_code == 200
    # 登记页的时刻控件是 datetime-local，送来的是 `T` 分隔
    dropped = specimen(two, site="右乳肿物", excised_at="2026-09-20T09:05")
    assert client.post(f"/api/pathology/specimens/{dropped['id']}/reject", headers=doc,
                       json={"reject_reason": "未加固定液"}).status_code == 200
    return {
        "patient": patient, "doc": doc, "kept": kept["specimen_no"], "dropped": dropped["specimen_no"],
        "two": report(two),
        "none": report(request("pathology")),            # 没登记标本就出了报告（出报告不等标本，P2-482）
        **{center: report(request(center)) for center in CENTER_LABELS},
    }


def _print(client, headers, report_id):
    resp = client.get(f"/api/print/exam-reports/{report_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


def test_病理报告印标本表_多标本逐行_拒收的标明原因(client, world):
    html = _print(client, world["doc"], world["two"])
    assert "<h3>标本</h3>" in html                               # 修前连「标本」两个字都没有
    table = html[html.index("<h3>标本</h3>"):html.index("<h3>检查所见</h3>")]   # 在检查所见之前
    assert re.findall(r"<th>([^<]*)</th>", table) == ["标本号", "送检部位", "离体时间", "固定时间", "固定液"]
    assert re.findall(r"<tr><td>.*?</tr>", table) == [
        f"<tr><td>{world['kept']}</td><td>左乳外上象限肿物</td><td>2026-09-20 09:00</td><td>2026-09-20 09:20</td>"
        "<td>10%中性福尔马林</td></tr>",
        f"<tr><td>{world['dropped']}（拒收：未加固定液）</td><td>右乳肿物</td><td>2026-09-20 09:05</td><td>—</td>"
        "<td>—</td></tr>",
    ]


def test_没登记标本的病理报告写明未登记(client, world):
    html = _print(client, world["doc"], world["none"])
    table = html[html.index("<h3>标本</h3>"):html.index("<h3>检查所见</h3>")]
    assert re.findall(r"<tr><td.*?</tr>", table) == ['<tr><td colspan="5">未登记标本</td></tr>']


@pytest.mark.parametrize("center", list(CENTER_LABELS))
def test_其他中心的报告字节不变(client, world, center):
    html = _print(client, world["doc"], world[center])
    p = world["patient"]
    code, name = ITEMS[center]
    with SessionLocal() as db:
        reported_at = to_local(db.get(ExamReport, world[center]).reported_at).strftime("%Y-%m-%d %H:%M")
    sheet = html[html.index('<table class="meta">'):html.index('<div class="qr">')]
    assert sheet == (
        '<table class="meta">'
        '<tr><td class="k">姓名</td><td>P21239 患者</td><td class="k">性别</td><td>女</td></tr>'
        f'<tr><td class="k">出生日期</td><td>1980-02-02</td><td class="k">健康卡号</td><td>{p["ehc_no"]}</td></tr>'
        '<tr><td class="k">身份证号</td><td>3302**********1239</td><td class="k">联系电话</td><td>138******39</td></tr>'
        f'<tr><td class="k">申请机构</td><td>{ORG}</td><td class="k">检查类别</td><td>{CENTER_LABELS[center]}</td></tr>'
        f'<tr><td class="k">检查项目</td><td>{name}</td><td class="k">项目编码</td><td>{code}</td></tr>'
        '<tr><td class="k">临床资料</td><td colspan="3">乳腺肿物</td></tr></table>\n'
        '  \n'
        '  <div class="section"><h3>检查所见</h3><div class="body">镜下见导管内乳头状增生</div></div>\n'
        '  <div class="section"><h3>诊断结论</h3><div class="body">（左乳）导管内乳头状瘤</div></div>\n'
        '  \n'
        '  \n'
        '  <div class="sign"><span>报告医师：病理王五</span>\n'
        f'    <span>报告时间：{reported_at}</span></div>\n'
        '  '
    )
