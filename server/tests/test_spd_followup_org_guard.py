"""智能随访的自动匹配与报告生成不得以别家机构名义进行（P0-35 第四批）。

两个端点都收一个可选的 `org_id`，不给就用调用方自己的机构，给了就**照单全收**：

- 自动匹配：按这家机构的出院 / 门诊患者匹配随访方案，**以这家机构的名义**批量生成计划随访
  （`SpdFollowupRecord.org_id` 就是它）——乙院医生能往甲院的随访队列里灌任务，回执还告诉他
  甲院有多少患者命中；
- 报告生成：按这家机构的数据聚合出一份报告实例并存档，挂在这家机构名下。

2026-09-24 实测：乙院医生带甲院的 `org_id` 调两个端点，自动匹配给甲院患者生成了计划随访（200），
报告以甲院名义落档（201）。照 `assert_org_writable` 的既定口径：只能以本机构名义做。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.spd.models import SpdFollowupRecord, SpdFollowupRule, SpdReportInstance, SpdReportTemplate


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


_seq = itertools.count(1)


@pytest.fixture(scope="module")
def fu_world(client):
    """甲院有一位门诊患者命中本用例专用的随访方案；乙院医生与之毫无关系。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "智能随访甲院"), ("b", "智能随访乙院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p035e_doc_{key}", "password": "pw123456", "full_name": f"{name}医生",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    heads = {k: _login(client, f"p035e_doc_{k}") for k in orgs}
    patient = client.post("/api/patients", json={"name": "智能随访患者", "id_card": "320000199103036671"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": orgs["a"], "encounter_type": "outpatient",
                            "diagnosis_name": "P035专用随访病"},
                      headers=heads["a"])
    assert enc.status_code == 201, enc.text
    db = SessionLocal()
    try:
        db.add(SpdFollowupRule(code="p035e_rule", name="P035 专用方案", scene="outpatient",
                               diagnosis_keywords=["P035专用随访病"], points=[7], active=True))
        template = db.query(SpdReportTemplate).first()
        assert template is not None, "启动种子里没有报告模板，前提变了"
        db.commit()
        template_code = template.code
    finally:
        db.close()
    return {"orgs": orgs, "h": heads, "patient_id": patient["id"], "template_code": template_code}


def _planned(org_id: int, patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(SpdFollowupRecord).filter(
            SpdFollowupRecord.org_id == org_id, SpdFollowupRecord.patient_id == patient_id
        ).count()
    finally:
        db.close()


def _reports(org_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(SpdReportInstance).filter(SpdReportInstance.org_id == org_id).count()
    finally:
        db.close()


def test_自动匹配不得以别家机构名义生成随访(client, fu_world):
    a, pid = fu_world["orgs"]["a"], fu_world["patient_id"]
    before = _planned(a, pid)
    r = client.post("/api/spd/followup-plans/auto-match", json={"scene": "outpatient", "org_id": a},
                    headers=fu_world["h"]["b"])
    assert r.status_code == 403, r.text
    assert "机构名义" in r.json()["detail"], r.text
    assert _planned(a, pid) == before, "被拒却给甲院患者生成了随访"


def test_自动匹配本机构照常(client, fu_world):
    """不给 org_id 就是本机构；给本机构的 id 也照常。"""
    a, pid = fu_world["orgs"]["a"], fu_world["patient_id"]
    r = client.post("/api/spd/followup-plans/auto-match", json={"scene": "outpatient", "org_id": a},
                    headers=fu_world["h"]["a"])
    assert r.status_code == 200, r.text
    assert r.json()["matched"] >= 1
    assert _planned(a, pid) >= 1
    again = client.post("/api/spd/followup-plans/auto-match", json={"scene": "outpatient"},
                        headers=fu_world["h"]["a"])
    assert again.status_code == 200, again.text


def test_报告不得以别家机构名义生成(client, fu_world):
    a = fu_world["orgs"]["a"]
    before = _reports(a)
    r = client.post("/api/spd/report-instances",
                    json={"template_code": fu_world["template_code"], "org_id": a, "period_label": f"P035-{next(_seq)}"},
                    headers=fu_world["h"]["b"])
    assert r.status_code == 403, r.text
    assert "机构名义" in r.json()["detail"], r.text
    assert _reports(a) == before


def test_报告本机构照常(client, fu_world):
    a = fu_world["orgs"]["a"]
    before = _reports(a)
    r = client.post("/api/spd/report-instances",
                    json={"template_code": fu_world["template_code"], "org_id": a, "period_label": f"P035-{next(_seq)}"},
                    headers=fu_world["h"]["a"])
    assert r.status_code == 201, r.text
    assert _reports(a) == before + 1


# ---------------------------------------------------------------- 按方案生成随访（P0-43）
def _rule_id(code: str) -> int:
    db = SessionLocal()
    try:
        return db.query(SpdFollowupRule).filter(SpdFollowupRule.code == code).one().id
    finally:
        db.close()


def test_按方案生成随访不得以别家机构名义进行(client, fu_world):
    """P0-43（P0-35 判据第二层）：按方案生成随访照单全收请求里的 `org_id`——守着它的只有「患者看不看得见」。

    乙院医生只要接诊过这个患者，就能把随访任务派进甲院的随访队列：实测 201，两条计划随访都落在甲院名下。
    同文件的自动匹配（P0-35 第四批）早已收口，这一条当时漏掉，是因为判据「函数里出现任一守卫名即算守住」
    把 `assert_patient_visible` 当成了机构守卫。
    """
    a, b, pid = fu_world["orgs"]["a"], fu_world["orgs"]["b"], fu_world["patient_id"]
    enc = client.post("/api/encounters", json={"patient_id": pid, "org_id": b, "encounter_type": "outpatient"},
                      headers=fu_world["h"]["b"])
    assert enc.status_code == 201, enc.text   # 乙院接诊过：患者对乙院可见，越权的前提成立
    before = _planned(a, pid)
    r = client.post("/api/spd/followup-plans", json={"patient_id": pid, "rule_id": _rule_id("p035e_rule"),
                                                     "org_id": a}, headers=fu_world["h"]["b"])
    assert r.status_code == 403, r.text
    assert "机构名义" in r.json()["detail"], r.text
    assert _planned(a, pid) == before, "被拒却给甲院生成了随访"


def test_按方案生成随访不给机构落本机构(client, fu_world):
    b, pid = fu_world["orgs"]["b"], fu_world["patient_id"]
    r = client.post("/api/spd/followup-plans", json={"patient_id": pid, "rule_id": _rule_id("p035e_rule")},
                    headers=fu_world["h"]["b"])
    assert r.status_code == 201, r.text
    assert {item["org_id"] for item in r.json()["items"]} == {b}
