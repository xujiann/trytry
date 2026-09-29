"""HL7 v2 入站按编码字符拆字段、还原转义（P2-724，第十九批「导入 / 入站对接 vs 界面录入」扫描 K3-5）。

HL7 v2 的分隔符写进数据时要转义（`\\S\\` 是 `^`、`\\T\\` 是 `&`……），可重复字段的各项用 `~` 隔开。原先：PID-5 把全部 `^`
删掉拼起来——`张^三^^^^^L`（第 7 组件是名称类型码 L）成了「张三L」，带拼音重复的 `张三~ZHANG^SAN` 成了「张三~ZHANGSAN」，
A08 照此覆盖主索引姓名；PID-13「座机~手机」整串落库；ORU 的 `10\\S\\9/L`、`\\T\\` 原样印进报告所见。

修法：PID-5 取第一个重复的姓、名、其余名三个组件；PID-13 优先取像手机号的那一项；各文本字段先按分隔符拆、再还原转义。
PV1-7 医师同是可重复字段（XCN），取第一个重复；PV1-3 病区名、DG1 诊断同样还原转义。
"""
import pytest

from app.database import SessionLocal
from app.models import ExamReport, Patient

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_card(body17: str) -> str:
    return body17 + "10X98765432"[sum(int(c) * w for c, w in zip(body17, _WEIGHTS)) % 11]


ID_CARD = _id_card("33010219700101724")
WARD = "P2724 心内&老年病区"


def _adt(client, admin, event, control_id, pid5, pid13="", pid3=ID_CARD, extra=()):
    msg = "\r".join([f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260929100000||ADT^{event}|{control_id}|P|2.4",
                     f"PID|1||{pid3}^^^CN^ID||{pid5}||19700101|M|||||{pid13}", *extra])
    return client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": msg})


def _patient(id_card=ID_CARD):
    with SessionLocal() as db:
        return next(p for p in db.query(Patient).filter(Patient.name.like("P2724%")).all() if p.id_card == id_card)


def test_unescape_各转义序列还原_认不得的原样留着():
    from app.routers.integration import _hl7_unescape

    assert _hl7_unescape(r"a\F\b\S\c\T\d\R\e\E\f") == "a|b^c&d~e\\f"
    assert _hl7_unescape(r"第一行\.br\第二行") == "第一行\n第二行"
    assert _hl7_unescape(r"\H\高亮\N\正常") == "高亮正常"
    assert _hl7_unescape(r"\X0D\ 与 \Zabc\ 原样") == r"\X0D\ 与 \Zabc\ 原样"
    assert _hl7_unescape("") == ""


def test_A04的姓名按姓名组件取_不带名称类型码(client, admin):
    resp = _adt(client, admin, "A04", "P2724A", "P2724张^三^^^^^L", "13800002724")
    assert resp.status_code == 201, resp.text
    assert _patient().name == "P2724张三"   # 修前「P2724张三L」


@pytest.mark.parametrize("pid5", ["P2724张三~ZHANG^SAN", "P2724张^三^^^^^L~ZHANG^SAN^^^^^A"],
                         ids=["带拼音重复", "姓名组件带重复"])
def test_A08的姓名只取第一个重复_不把拼音拼进主索引(client, admin, pid5):
    resp = _adt(client, admin, "A08", "P2724B", pid5)
    assert resp.status_code == 201, resp.text
    assert _patient().name == "P2724张三"   # 修前「P2724张三~ZHANGSAN」


@pytest.mark.parametrize(("pid13", "phone"), [
    ("0571-88888888~13912345678", "13912345678"),                 # 修前整串落库
    ("^NET^Internet^p2724@example.com~13912345679", "13912345679"),  # 修前第一项没有号码，手机号丢了
    ("0571-88888888", "0571-88888888"),                           # 只有座机照旧取座机
], ids=["座机在前", "邮箱在前", "只有座机"])
def test_A08的电话优先取手机号(client, admin, pid13, phone):
    resp = _adt(client, admin, "A08", "P2724C", "P2724张三", pid13)
    assert resp.status_code == 201, resp.text
    assert _patient().phone == phone


def test_ORU结果项的单位与参考范围还原转义(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2724 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    request = client.post("/api/exams", headers=admin, json={
        "patient_id": _patient().id, "from_org_id": org, "center_type": "lab",
        "item_code": "CBC", "item_name": "血常规"}).json()
    msg = "\r".join(["MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260929100000||ORU^R01|P2724O|P|2.4",
                     f"PID|1||{ID_CARD}^^^CN^ID||P2724张三",
                     f"OBR|1|{request['id']}||CBC^血常规",
                     r"OBX|1|NM|WBC^白细胞||6.2|10\S\9/L|3.5-9.5|N",
                     r"OBX|2|NM|HGB^血红蛋白||132|g/L|男 130-175 \T\ 女 115-150|N"])
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": msg})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        finding = db.get(ExamReport, resp.json()["report_id"]).finding
    assert "10^9/L" in finding and "男 130-175 & 女 115-150" in finding, finding   # 修前原样印出 \S\、\T\
    assert "\\" not in finding, finding


def test_A01的病区名与诊断还原转义_医师只取第一个重复(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2724 住院县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": WARD}).json()
    assert client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": "1"}).status_code == 201
    id_card = _id_card("33010219700102724")
    resp = _adt(client, admin, "A01", "P2724D", "P2724住院^乙", pid3=id_card, extra=(
        r"PV1|1|I|P2724 心内\T\老年病区^1^1||||1001^王^主任~1002^李^主任",   # 修前病区名带着 \T\，404 病区不存在
        r"DG1|1||I10^高血压 \T\ 冠心病"))
    assert resp.status_code == 201, resp.text
    patient_id = resp.json()["patient"]["id"]
    (admission,) = client.get(f"/api/inpatient/admissions?patient_id={patient_id}", headers=admin).json()
    assert (admission["doctor_name"], admission["diagnosis_name"]) == ("王主任", "高血压 & 冠心病")   # 修前「王主任~1002」
