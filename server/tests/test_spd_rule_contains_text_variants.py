"""慢专病规则的「包含」认得出写法不同的同一段文字：康熙部首、零宽字符、大小写、全角字母都照样命中（P2-1147，第三十三批
扫描 A3-5）。

`spd/rules.py` 的 `contains` 原先按原样找子串（`str(expected) in str(v)`）。平台侧「关键词在不在这段文字里」早已两侧过
`texttypes.text_key`（DRG / 审方禁忌诊断 P2-792），同一子系统的随访方案关键词也在用 `has_keyword`（P2-917），只有这里没跟上。
2026-09-30 开发库实测：5 个患者（0 对照「原发性高血压」、1 康熙部首写的「原发性高血压」（「高」「血」是 U+2FBC / U+2F8E，
从 PDF 复制）、2「原发性高血压」中间夹一个 U+200B、3「（copd）」、4「（ＣＯＰＤ）」），纳入规则「诊断名称 包含 高血压」
自动识别只进了 0、3、4，「诊断名称 包含 COPD」只进了 0、1、2——漏掉的人不报错、不提示，只是不在目标池里。

修法：`contains` 两侧都过 `text_key` 再找子串；比较值归一后为空的（存量里的空串、只有空白或格式字符的）照旧按原样比。
"""
import pytest

from conftest import login

from app.spd.rules import evaluate

B = "/api/spd"
KANGXI_GAO_XUE = "\u2fbc\u2f8e"   # 康熙部首 U+2FBC「高」、U+2F8E「血」：从 PDF 复制出来的「高血」常是这两个码点
PATIENTS = [   # (高血压那条诊断, COPD 那条诊断里括号中的写法)
    ("原发性高血压", "COPD"),
    ("原发性" + KANGXI_GAO_XUE + "压", "COPD"),
    ("原发性高\u200b血压", "COPD"),
    ("原发性高血压", "copd"),
    ("原发性高血压", "ＣＯＰＤ"),
]
PATIENT_IDS = ["对照", "康熙部首", "零宽字符", "小写字母", "全角字母"]


def _hit(value, text):
    return evaluate([{"field": "diagnosis_name", "op": "contains", "value": value}], {"diagnosis_name": [text]})[0]


@pytest.mark.parametrize("htn, copd", PATIENTS, ids=PATIENT_IDS)
def test_求值器_写法不同照样命中(htn, copd):
    assert _hit("高血压", htn) is True   # 修前康熙部首、零宽字符两例 False
    assert _hit("COPD", f"慢性阻塞性肺疾病（{copd}）") is True   # 修前小写、全角两例 False


def test_求值器_比较值一侧写法不同也认_不相干的照旧不命中():
    assert _hit("ｃｏｐｄ", "慢性阻塞性肺疾病（COPD）") is True
    assert _hit("高 血压", "原发性高血压") is True
    assert _hit("高血压", "阑尾炎") is False
    assert evaluate([{"field": "surgery", "op": "contains", "value": "冠脉"}], {"surgery": ["冠脉搭桥术"]})[0] is True


def test_求值器_比较值归一后为空的照旧按原样比():
    # 存量里的空串照旧对谁都成立（配置时已拦，P2-1117；存量怎么判不在这里改）；只有格式字符的不因归一变成人人命中
    assert _hit("", "原发性高血压") is True
    assert _hit("\u200b", "原发性高血压") is False
    assert _hit("\u200b", "原发性高\u200b血压") is True


@pytest.fixture(scope="module")
def pools(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21147 规则测试卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21147_doc", "password": "passw0rd1", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    doctor = login(client, "p21147_doc", "passw0rd1")
    for code, keyword in (("P21147HTN", "高血压"), ("P21147COPD", "COPD")):
        created = client.post(f"{B}/programs", headers=admin, json={
            "code": code, "name": f"P21147 {keyword}",
            "include_rules": [{"field": "diagnosis_name", "op": "contains", "value": keyword}]})
        assert created.status_code == 201, created.text
    names = {}
    for i, (htn, copd) in enumerate(PATIENTS):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21147 患者{i}", "id_card": f"33010619700101114{i}"})
        assert patient.status_code == 201, patient.text
        names[i] = patient.json()["name"]
        for dx in (htn, f"慢性阻塞性肺疾病（{copd}）"):
            encounter = client.post("/api/encounters", headers=doctor, json={
                "patient_id": patient.json()["id"], "org_id": org, "diagnosis_name": dx})
            assert encounter.status_code in (200, 201), encounter.text
    result = {}
    for code in ("P21147HTN", "P21147COPD"):
        run = client.post(f"{B}/screenings/auto-run", headers=doctor, json={"program_code": code, "org_id": org})
        assert run.status_code == 200, run.text
        listed = client.get(f"{B}/candidates", headers=doctor, params={"program_code": code})
        assert listed.status_code == 200, listed.text
        result[code] = sorted(row["patient_name"] for row in listed.json())
    return result, sorted(names.values())


def test_自动识别_五种写法全部进目标池(pools):
    result, everyone = pools
    assert result["P21147HTN"] == everyone, result    # 修前漏了康熙部首、零宽字符两位
    assert result["P21147COPD"] == everyone, result   # 修前漏了小写、全角两位
