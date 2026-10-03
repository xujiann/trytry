"""检查报告打印件写明出具方：别家诊断中心出的报告印报告机构与诊断医师，验真「签发机构」是诊断中心（P2-1238，
第三十六批「打印件与对外文书的必备要素」扫描 U4-1）。

申请单模型自己写的是「基层检查、上级诊断」，报告由领取申请单的那家中心出（`claimed_org_id`）。打印件原先抬头与
meta 只有申请机构，验真令牌签的也是申请机构：甲乡卫生院申请、县人民医院中心的影像李四领取并出 CT 报告（报告医师
没自填），打印件上「县人民医院」「影像李四」一处都没有，报告医师栏是「—」，扫码验真显示「签发机构 甲乡卫生院 · 有效」
（修前实测：纸面含『县人民医院』False、含『影像李四』False，令牌 org_name=甲乡卫生院）。

修法：跨机构出的报告在 meta 加「报告机构 / 诊断医师」一行（诊断医师取报告医师自填的，没填取领取人），令牌签诊断中心；
同机构出的、没领取就直接出的（`claimed_org_id` 为空，随 P2-257）字节不变；申请单等其他单据的令牌不变；抬头印谁随
P2-802 待裁定，不动。旧纸的令牌原样验得过（核验只按令牌里签的字段回显、按 id 查存在性，不按现在的出具方重算）。
"""
import pytest

from app.clock import to_local
from app.database import SessionLocal
from app.models import ExamReport
from app.printverify import make_verify_token
from app.routers import printing
from conftest import login

CENTER, TOWN = "P21238 县人民医院", "P21238 甲乡卫生院"


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": CENTER, "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": TOWN, "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    for username, org_id, full_name in (("p21238_town", town, "乡医张三"), ("p21238_center", county, "影像李四")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "doctor", "org_id": org_id,
            "full_name": full_name})
        assert created.status_code in (200, 201), created.text
    town_doc, center_doc = login(client, "p21238_town", "passw0rd1"), login(client, "p21238_center", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21238 患者", "id_card": "330281197001011238", "gender": "男", "birth_date": "1970-01-01",
        "phone": "13800001238"}).json()

    def report(applicant, from_org, *, claim=True, reported_by=""):
        req = client.post("/api/exams", headers=applicant, json={
            "patient_id": patient["id"], "from_org_id": from_org, "center_type": "imaging",
            "item_code": "CT-CHEST", "item_name": "胸部CT平扫", "clinical_info": "咳嗽2周"})
        assert req.status_code == 201, req.text
        if claim:
            assert client.post(f"/api/exams/{req.json()['id']}/claim", headers=center_doc).status_code == 200
        rep = client.post(f"/api/exams/{req.json()['id']}/report", headers=center_doc, json={
            "finding": "右肺上叶见 8mm 结节", "conclusion": "右肺上叶结节", "critical": False,
            "reported_by": reported_by})
        assert rep.status_code == 201, rep.text
        return {"request": req.json()["id"], "report": rep.json()["id"]}

    return {
        "patient": patient, "center_doc": center_doc,
        "cross": report(town_doc, town),                                   # 乡镇申请、县中心领取出，报告医师没自填
        "cross_signed": report(town_doc, town, reported_by="王主任"),       # 同上，报告医师自填了
        "same": report(center_doc, county, reported_by="影像李四"),          # 县医院自己申请、自己的中心出
        "unclaimed": report(town_doc, town, claim=False),                   # 没领取就直接出（P2-257）
    }


@pytest.fixture
def tokens(monkeypatch):
    """记下 `_render` 签进验真令牌的字段（打印件上的二维码编的就是它）。"""
    signed = []
    real = printing.make_verify_token

    def spy(**kwargs):
        signed.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(printing, "make_verify_token", spy)
    return signed


def _print(client, headers, path):
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


def _sheet(html: str) -> str:
    """单据正文：meta 表起、验真码前止（打印时间与二维码每打一次都变，不在比对之列）。"""
    return html[html.index('<table class="meta">'):html.index('<div class="qr">')]


def test_跨机构出的报告写明报告机构与诊断医师_验真签发机构是诊断中心(client, world, tokens):
    html = _print(client, world["center_doc"], f"/api/print/exam-reports/{world['cross']['report']}")
    # 修前纸面没有「县人民医院」「影像李四」；报告医师没自填，诊断医师取领取人
    assert f'<tr><td class="k">报告机构</td><td>{CENTER}</td><td class="k">诊断医师</td><td>影像李四</td></tr>' in html
    assert f'<div class="org">{TOWN}</div>' in html          # 抬头仍是申请机构（随 P2-802 待裁定，不在本条）
    assert "报告医师：—" in html                              # 报告医师栏照旧只印自填的那个
    (signed,) = tokens
    assert signed["org_name"] == CENTER                        # 修前签的是申请机构
    token = make_verify_token(**signed)
    verified = client.get("/api/print/verify", params={"token": token}).json()
    assert (verified["valid"], verified["org_name"], verified["doc_no"]) == (
        True, CENTER, f"BG{world['cross']['report']:08d}")


def test_诊断医师取报告医师自填的(client, admin, world, tokens):
    html = _print(client, admin, f"/api/print/exam-reports/{world['cross_signed']['report']}")
    assert f'<tr><td class="k">报告机构</td><td>{CENTER}</td><td class="k">诊断医师</td><td>王主任</td></tr>' in html
    assert tokens[-1]["org_name"] == CENTER


def test_同机构出的报告字节不变(client, admin, world, tokens):
    html = _print(client, admin, f"/api/print/exam-reports/{world['same']['report']}")
    p = world["patient"]
    with SessionLocal() as db:
        reported_at = to_local(db.get(ExamReport, world["same"]["report"]).reported_at).strftime("%Y-%m-%d %H:%M")
    assert _sheet(html) == (
        '<table class="meta">'
        f'<tr><td class="k">姓名</td><td>P21238 患者</td><td class="k">性别</td><td>男</td></tr>'
        f'<tr><td class="k">出生日期</td><td>1970-01-01</td><td class="k">健康卡号</td><td>{p["ehc_no"]}</td></tr>'
        '<tr><td class="k">身份证号</td><td>330281197001011238</td><td class="k">联系电话</td><td>13800001238</td></tr>'
        f'<tr><td class="k">申请机构</td><td>{CENTER}</td><td class="k">检查类别</td><td>影像</td></tr>'
        '<tr><td class="k">检查项目</td><td>胸部CT平扫</td><td class="k">项目编码</td><td>CT-CHEST</td></tr>'
        '<tr><td class="k">临床资料</td><td colspan="3">咳嗽2周</td></tr></table>\n'
        '  \n'
        '  <div class="section"><h3>检查所见</h3><div class="body">右肺上叶见 8mm 结节</div></div>\n'
        '  <div class="section"><h3>诊断结论</h3><div class="body">右肺上叶结节</div></div>\n'
        '  \n'
        '  \n'
        '  <div class="sign"><span>报告医师：影像李四</span>\n'
        f'    <span>报告时间：{reported_at}</span></div>\n'
        '  '
    )
    assert tokens[-1]["org_name"] == CENTER


def test_没领取就直接出的报告照旧取申请机构(client, admin, world, tokens):
    html = _print(client, admin, f"/api/print/exam-reports/{world['unclaimed']['report']}")
    assert "报告机构" not in html and "诊断医师" not in html
    assert f'<tr><td class="k">申请机构</td><td>{TOWN}</td><td class="k">检查类别</td><td>影像</td></tr>' \
           '<tr><td class="k">检查项目</td>' in html
    assert tokens[-1]["org_name"] == TOWN


def test_同一张申请单的申请单打印件不受影响(client, admin, world, tokens):
    html = _print(client, admin, f"/api/print/exam-requests/{world['cross']['request']}")
    assert "报告机构" not in html and "诊断医师" not in html
    assert tokens[-1]["org_name"] == TOWN                      # 申请单是申请方开的，令牌照旧签申请机构


def test_修前打出的旧纸照旧验得过(client, world):
    """旧纸的令牌签的是申请机构：核验只按令牌里签的字段回显、按 id 查存在性，换了出具方也照旧「有效」。"""
    old = make_verify_token(doc_type="exam_report", doc_id=world["cross"]["report"],
                            doc_no=f"BG{world['cross']['report']:08d}", org_name=TOWN, issued_date="2026-10-01")
    verified = client.get("/api/print/verify", params={"token": old}).json()
    assert (verified["valid"], verified["status"], verified["org_name"]) == (True, "有效", TOWN)
