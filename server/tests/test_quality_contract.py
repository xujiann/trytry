"""质量安全 `/api/quality` 二十个端点的**特征化网 + 响应契约**。

套路同 `test_analytics_contract.py`：先补网钉住**当前**响应的完整 JSON（dict 相等）
与键序 → 再加 `response_model` → 加完逐字节不变（CLAUDE.md §11）。

本簇的建模判断（都以此处的精确断言为依据）：

- 所有比率/均分字段恒为 float：`round(x*100.0/n, 2)`、`round(sum/n, 1)` 的两条
  分支（有数据 / 兜底字面量 `0.0`）都是浮点，不存在 Money 那种 int/float 并存。
- `qc-summary` 分组行的 `key` 是 `int | str`：按机构分组是 org_id（int）、
  按医师分组是姓名（str），同一个形状两处复用。
- `clinical-indicators` 的 `uncollected` 是**条件键**：只有"术前术后诊断符合率"
  这一行有它（未采集数只对这条指标有意义）。逐字段声明默认值会给其余六行注入
  `"uncollected": null`——改字节，故端点用 `response_model_exclude_unset=True`，
  这里把"有它的行在、没它的行整个键不在"两个方向都钉死。
- 病历缺陷项（`defects[]`）是固定形状（`evaluate_record` 唯一产地），四个端点
  （提交回执/复评/详情/列表外的 qc 快照）共用同一个模型，此处钉住其全部字段与键序。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

GOOD_ILLNESS = (
    "患者3天前无明显诱因出现咳嗽咳痰，痰白质黏，伴发热，最高体温38.5℃，"
    "自服退热药后可暂缓，无胸痛咯血，无夜间盗汗，饮食睡眠尚可，二便正常，体重无明显变化。"
)
GOOD_BASIS = "根据患者咳嗽发热病史、肺部湿啰音体征及胸片提示右下肺斑片影，符合社区获得性肺炎诊断标准。"
GOOD_RECORD = {
    "chief_complaint": "咳嗽发热3天",
    "present_illness": GOOD_ILLNESS,
    "past_history": "否认高血压糖尿病史，否认药物过敏史",
    "physical_exam": "体温38.2℃，血压126/78mmHg，脉搏92次/分，右下肺可闻及湿啰音",
    "diagnosis_basis": GOOD_BASIS,
    "treatment_plan": "予头孢呋辛抗感染用药，止咳化痰对症处置，3天后门诊随访复查",
}


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, admin):
    """两家机构 + 各角色实名用户：出参里的经手人姓名全部可精确断言。"""
    org1 = client.post(
        "/api/organizations",
        json={"name": "质量契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "质量契约卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    users = {}
    for username, full_name, role, org in [
        ("qct_doc", "钱医生", "doctor", org1),
        ("qct_doc2", "孙医生", "doctor", org2),
        ("qct_dir", "李主任", "director", org1),
        ("qct_op", "周经办", "operator", org1),
        ("qct_ph", "吴公卫", "public_health", org1),
    ]:
        client.post(
            "/api/users",
            json={
                "username": username,
                "password": "pass123456",
                "full_name": full_name,
                "role": role,
                "org_id": org["id"],
            },
            headers=admin,
        )
        users[username] = login(client, username, "pass123456")
    patient = client.post(
        "/api/patients",
        json={"name": "契约患者", "id_card": "330281199203046014", "gender": "男", "birth_date": "1992-03-04"},
        headers=admin,
    ).json()
    enc1 = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org1["id"], "diagnosis_name": "社区获得性肺炎"},
        headers=users["qct_doc"],
    ).json()
    enc2 = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org2["id"], "diagnosis_name": "上呼吸道感染"},
        headers=users["qct_doc2"],
    ).json()
    return {
        "org1": org1,
        "org2": org2,
        "patient": patient,
        "enc1": enc1,
        "enc2": enc2,
        "doctor": users["qct_doc"],
        "doctor2": users["qct_doc2"],
        "director": users["qct_dir"],
        "operator": users["qct_op"],
        "public_health": users["qct_ph"],
    }


# ---------------------------------------------------------------- 不良事件

ADVERSE_KEY_ORDER = [
    "id", "org_id", "event_type", "level", "anonymous", "reporter_name",
    "description", "status", "review_note", "reviewed_by", "rectify_note",
    "rectified_by", "created_at",
]


@pytest.fixture(scope="module")
def adverse(client, base):
    """一条实名事件走完 上报→审核→整改 闭环 + 一条匿名事件，两条形状全钉。"""
    resp = client.post(
        "/api/quality/adverse-events",
        json={
            "org_id": base["org1"]["id"],
            "event_type": "medication",
            "level": "II",
            "description": "给药剂量录入错误，已及时纠正",
        },
        headers=base["doctor"],
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    reviewed = client.post(
        f"/api/quality/adverse-events/{created['id']}/review",
        json={"note": "情况属实，责成科室整改"},
        headers=base["director"],
    ).json()
    rectified = client.post(
        f"/api/quality/adverse-events/{created['id']}/rectify",
        json={"note": "已完成双人核对培训"},
        headers=base["operator"],
    ).json()
    anon = client.post(
        "/api/quality/adverse-events",
        json={
            "org_id": base["org1"]["id"],
            "event_type": "fall",
            "level": "III",
            "description": "患者如厕滑倒，未受伤",
            "anonymous": True,
        },
        headers=base["public_health"],
    ).json()
    return {"created": created, "reviewed": reviewed, "rectified": rectified, "anon": anon}


def test_不良事件_上报回执精确形状与键序(base, adverse):
    body = adverse["created"]
    assert list(body.keys()) == ADVERSE_KEY_ORDER
    assert body == {
        "id": body["id"],
        "org_id": base["org1"]["id"],
        "event_type": "medication",
        "level": "II",
        "anonymous": False,
        "reporter_name": "钱医生",
        "description": "给药剂量录入错误，已及时纠正",
        "status": "reported",
        "review_note": "",
        "reviewed_by": "",
        "rectify_note": "",
        "rectified_by": "",
        "created_at": body["created_at"],
    }
    assert isinstance(body["created_at"], str)


def test_不良事件_审核与整改回执精确(adverse):
    assert adverse["reviewed"] == {
        **adverse["created"],
        "status": "reviewed",
        "review_note": "情况属实，责成科室整改",
        "reviewed_by": "李主任",
    }
    assert adverse["rectified"] == {
        **adverse["reviewed"],
        "status": "rectified",
        "rectify_note": "已完成双人核对培训",
        "rectified_by": "周经办",
    }
    # 匿名上报：不落报告人
    assert adverse["anon"]["anonymous"] is True and adverse["anon"]["reporter_name"] == ""


def test_不良事件_列表与回执同形(client, admin, adverse):
    rows = client.get("/api/quality/adverse-events", headers=admin).json()
    assert rows == [adverse["anon"], adverse["rectified"]]  # id 倒序
    assert client.get(
        "/api/quality/adverse-events?status=rectified", headers=admin
    ).json() == [adverse["rectified"]]
    assert client.get(
        "/api/quality/adverse-events?event_type=fall", headers=admin
    ).json() == [adverse["anon"]]


def test_不良事件_统计精确(client, admin, adverse):
    resp = client.get("/api/quality/adverse-events-stats", headers=admin)
    assert list(resp.json().keys()) == ["total", "rectified", "closed_loop_pct", "by_type", "by_level"]
    assert resp.json() == {
        "total": 2,
        "rectified": 1,
        "closed_loop_pct": 50.0,
        "by_type": {"fall": 1, "medication": 1},
        "by_level": {"II": 1, "III": 1},
    }
    assert isinstance(resp.json()["closed_loop_pct"], float)


# ---------------------------------------------------------------- 病历抽检质控

RECORD_QC_KEY_ORDER = ["id", "target_type", "target_id", "score", "grade", "defects", "qc_by"]


@pytest.fixture(scope="module")
def record_qc(client, base):
    qc1 = client.post(
        "/api/quality/record-qc",
        json={"target_type": "encounter", "target_id": base["enc1"]["id"], "score": 95},
        headers=base["director"],
    ).json()
    qc2 = client.post(
        "/api/quality/record-qc",
        json={
            "target_type": "encounter",
            "target_id": base["enc1"]["id"],
            "score": 72,
            "defects": "主诉缺失;现病史不完整",
        },
        headers=base["doctor"],
    ).json()
    return {"qc1": qc1, "qc2": qc2}


def test_抽检_回执精确形状与键序(base, record_qc):
    assert list(record_qc["qc1"].keys()) == RECORD_QC_KEY_ORDER
    assert record_qc["qc1"] == {
        "id": record_qc["qc1"]["id"],
        "target_type": "encounter",
        "target_id": base["enc1"]["id"],
        "score": 95,
        "grade": "甲",
        "defects": "",
        "qc_by": "李主任",
    }
    assert record_qc["qc2"] == {
        "id": record_qc["qc2"]["id"],
        "target_type": "encounter",
        "target_id": base["enc1"]["id"],
        "score": 72,
        "grade": "丙",
        "defects": "主诉缺失;现病史不完整",
        "qc_by": "钱医生",
    }


def test_抽检_列表与统计精确(client, admin, record_qc):
    rows = client.get("/api/quality/record-qc", headers=admin).json()
    assert rows == [record_qc["qc2"], record_qc["qc1"]]  # id 倒序
    assert client.get("/api/quality/record-qc?grade=甲", headers=admin).json() == [record_qc["qc1"]]
    stats = client.get("/api/quality/record-qc-stats", headers=admin).json()
    assert stats == {"total": 2, "avg_score": 83.5, "grade_a_pct": 50.0, "with_defects": 1}
    assert isinstance(stats["avg_score"], float) and isinstance(stats["grade_a_pct"], float)


# ---------------------------------------------------------------- 院感上报

INFECTION_KEY_ORDER = [
    "id", "org_id", "patient_id", "infection_site", "pathogen", "note",
    "status", "reported_by", "report_date",
]


@pytest.fixture(scope="module")
def infection(client, base):
    r1 = client.post(
        "/api/quality/infection-reports",
        json={
            "org_id": base["org1"]["id"],
            "patient_id": base["patient"]["id"],
            "infection_site": "surgical_site",
            "pathogen": "金黄色葡萄球菌",
            "note": "术后第3天切口红肿",
            "report_date": "2026-08-20",
        },
        headers=base["doctor"],
    ).json()
    r2 = client.post(
        "/api/quality/infection-reports",
        json={
            "org_id": base["org1"]["id"],
            "patient_id": base["patient"]["id"],
            "infection_site": "urinary",
        },
        headers=base["public_health"],
    ).json()
    confirmed = client.post(
        f"/api/quality/infection-reports/{r1['id']}/verify?confirmed=true",
        headers=base["public_health"],
    ).json()
    excluded = client.post(
        f"/api/quality/infection-reports/{r2['id']}/verify?confirmed=false",
        headers=base["director"],
    ).json()
    return {"r1": r1, "r2": r2, "confirmed": confirmed, "excluded": excluded}


def test_院感_上报回执精确形状与键序(base, infection):
    assert list(infection["r1"].keys()) == INFECTION_KEY_ORDER
    assert infection["r1"] == {
        "id": infection["r1"]["id"],
        "org_id": base["org1"]["id"],
        "patient_id": base["patient"]["id"],
        "infection_site": "surgical_site",
        "pathogen": "金黄色葡萄球菌",
        "note": "术后第3天切口红肿",
        "status": "reported",
        "reported_by": "钱医生",
        "report_date": "2026-08-20",
    }
    # 缺省入参的零值分支：pathogen/note/report_date 都是空串（不是 null）
    assert infection["r2"] == {
        "id": infection["r2"]["id"],
        "org_id": base["org1"]["id"],
        "patient_id": base["patient"]["id"],
        "infection_site": "urinary",
        "pathogen": "",
        "note": "",
        "status": "reported",
        "reported_by": "吴公卫",
        "report_date": "",
    }


def test_院感_核实回执与列表精确(client, admin, infection):
    assert infection["confirmed"] == {**infection["r1"], "status": "confirmed"}
    assert infection["excluded"] == {**infection["r2"], "status": "excluded"}
    rows = client.get("/api/quality/infection-reports", headers=admin).json()
    assert rows == [infection["excluded"], infection["confirmed"]]  # id 倒序
    assert client.get(
        "/api/quality/infection-reports?status=confirmed", headers=admin
    ).json() == [infection["confirmed"]]


def test_院感_统计精确(client, admin, infection):
    resp = client.get("/api/quality/infection-stats", headers=admin)
    assert list(resp.json().keys()) == ["confirmed", "pending_verify", "by_site"]
    assert resp.json() == {"confirmed": 1, "pending_verify": 0, "by_site": {"surgical_site": 1}}


# ---------------------------------------------------------------- 结构化病历与环节质控

RECORD_KEY_ORDER = [
    "id", "encounter_id", "org_id", "doctor_name",
    "chief_complaint", "present_illness", "past_history", "physical_exam",
    "diagnosis_basis", "treatment_plan",
    "qc_score", "qc_grade", "created_at", "updated_at",
]
DEFECT_KEY_ORDER = [
    "rule_code", "rule_name", "field", "field_name",
    "rule_type", "rule_type_name", "message", "deduct_points",
]

#: 只填主诉的病历命中的缺陷清单（除 id/时间外全部可静态写死；
#: 规则库为种子 12 条、按 code 排序逐条判定，故顺序与内容都确定）。
EXPECTED_DEFECTS = [
    {
        "rule_code": "MRQC03", "rule_name": "现病史不少于50字",
        "field": "present_illness", "field_name": "现病史",
        "rule_type": "min_length", "rule_type_name": "字数下限",
        "message": "仅 0 字，少于要求的 50 字", "deduct_points": 10,
    },
    {
        "rule_code": "MRQC04", "rule_name": "现病史须描述起病时间与诱因",
        "field": "present_illness", "field_name": "现病史",
        "rule_type": "keyword_present", "rule_type_name": "要点关键词",
        "message": "未体现要点（应含以下之一：天、小时、月、年、诱因）", "deduct_points": 4,
    },
    {
        "rule_code": "MRQC05", "rule_name": "既往史必填",
        "field": "past_history", "field_name": "既往史",
        "rule_type": "required", "rule_type_name": "必填",
        "message": "未填写", "deduct_points": 6,
    },
    {
        "rule_code": "MRQC06", "rule_name": "体格检查必填",
        "field": "physical_exam", "field_name": "体格检查",
        "rule_type": "required", "rule_type_name": "必填",
        "message": "未填写", "deduct_points": 8,
    },
    {
        "rule_code": "MRQC07", "rule_name": "体格检查须含生命体征",
        "field": "physical_exam", "field_name": "体格检查",
        "rule_type": "keyword_present", "rule_type_name": "要点关键词",
        "message": "未体现要点（应含以下之一：体温、血压、脉搏、呼吸、心率）", "deduct_points": 4,
    },
    {
        "rule_code": "MRQC08", "rule_name": "诊断依据必填",
        "field": "diagnosis_basis", "field_name": "诊断依据",
        "rule_type": "required", "rule_type_name": "必填",
        "message": "未填写", "deduct_points": 10,
    },
    {
        "rule_code": "MRQC09", "rule_name": "诊断依据不少于30字",
        "field": "diagnosis_basis", "field_name": "诊断依据",
        "rule_type": "min_length", "rule_type_name": "字数下限",
        "message": "仅 0 字，少于要求的 30 字", "deduct_points": 5,
    },
    {
        "rule_code": "MRQC10", "rule_name": "治疗方案必填",
        "field": "treatment_plan", "field_name": "治疗方案",
        "rule_type": "required", "rule_type_name": "必填",
        "message": "未填写", "deduct_points": 10,
    },
]


@pytest.fixture(scope="module")
def records(client, base):
    full = client.post(
        "/api/quality/records",
        json={"encounter_id": base["enc1"]["id"], **GOOD_RECORD},
        headers=base["doctor"],
    ).json()
    flawed = client.post(
        "/api/quality/records",
        json={"encounter_id": base["enc2"]["id"], "chief_complaint": "咽痛1天"},
        headers=base["doctor2"],
    ).json()
    return {"full": full, "flawed": flawed}


def test_病历_提交回执精确形状与键序(base, records):
    body = records["full"]
    assert list(body.keys()) == ["created", "record", "qc"]
    assert list(body["record"].keys()) == RECORD_KEY_ORDER
    assert list(body["qc"].keys()) == ["score", "grade", "deducted", "rules_checked", "defects"]
    assert body == {
        "created": True,
        "record": {
            "id": body["record"]["id"],
            "encounter_id": base["enc1"]["id"],
            "org_id": base["org1"]["id"],
            "doctor_name": "钱医生",
            **GOOD_RECORD,
            "qc_score": 100,
            "qc_grade": "甲",
            "created_at": body["record"]["created_at"],
            "updated_at": body["record"]["updated_at"],
        },
        "qc": {"score": 100, "grade": "甲", "deducted": 0, "rules_checked": 12, "defects": []},
    }


def test_病历_缺陷清单精确形状与键序(base, records):
    body = records["flawed"]
    assert body["qc"]["defects"] and list(body["qc"]["defects"][0].keys()) == DEFECT_KEY_ORDER
    assert body == {
        "created": True,
        "record": {
            "id": body["record"]["id"],
            "encounter_id": base["enc2"]["id"],
            "org_id": base["org2"]["id"],
            "doctor_name": "孙医生",
            "chief_complaint": "咽痛1天",
            "present_illness": "",
            "past_history": "",
            "physical_exam": "",
            "diagnosis_basis": "",
            "treatment_plan": "",
            "qc_score": 43,
            "qc_grade": "丙",
            "created_at": body["record"]["created_at"],
            "updated_at": body["record"]["updated_at"],
        },
        "qc": {"score": 43, "grade": "丙", "deducted": 57, "rules_checked": 12,
               "defects": EXPECTED_DEFECTS},
    }


def test_病历_列表详情与复评精确(client, admin, base, records):
    flawed = records["flawed"]["record"]
    rows = client.get(
        f"/api/quality/records?encounter_id={base['enc2']['id']}", headers=admin
    ).json()
    assert rows == [flawed]  # 列表行与提交回执的 record 段同形
    detail = client.get(f"/api/quality/records/{flawed['id']}", headers=admin)
    assert list(detail.json().keys()) == ["record", "defects"]
    assert detail.json() == {"record": flawed, "defects": EXPECTED_DEFECTS}
    rescored = client.get(f"/api/quality/records/{flawed['id']}/qc", headers=admin)
    assert list(rescored.json().keys()) == [
        "record_id", "score", "grade", "deducted", "rules_checked", "defects"
    ]
    assert rescored.json() == {
        "record_id": flawed["id"],
        "score": 43,
        "grade": "丙",
        "deducted": 57,
        "rules_checked": 12,
        "defects": EXPECTED_DEFECTS,
    }


QC_SUMMARY_GROUP_KEY_ORDER = [
    "key", "name", "total", "avg_score", "grade_a", "grade_b", "grade_c", "grade_a_pct",
]


def test_病历_环节质控汇总精确(client, admin, base, records):
    resp = client.get("/api/quality/records/qc-summary", headers=admin)
    body = resp.json()
    assert list(body.keys()) == [
        "period", "total", "avg_score", "grade_distribution", "grade_a_pct", "by_org", "by_doctor"
    ]
    assert list(body["by_org"][0].keys()) == QC_SUMMARY_GROUP_KEY_ORDER
    # 分组行按 total 降序、并列时按病历 id 倒序的桶建立序（org2 的病历后建、先遍历到）
    assert body == {
        "period": "累计",
        "total": 2,
        "avg_score": 71.5,
        "grade_distribution": {"甲": 1, "乙": 0, "丙": 1},
        "grade_a_pct": 50.0,
        "by_org": [
            {"key": base["org2"]["id"], "name": "质量契约卫生院", "total": 1, "avg_score": 43.0,
             "grade_a": 0, "grade_b": 0, "grade_c": 1, "grade_a_pct": 0.0},
            {"key": base["org1"]["id"], "name": "质量契约医院", "total": 1, "avg_score": 100.0,
             "grade_a": 1, "grade_b": 0, "grade_c": 0, "grade_a_pct": 100.0},
        ],
        "by_doctor": [
            {"key": "孙医生", "name": "孙医生", "total": 1, "avg_score": 43.0,
             "grade_a": 0, "grade_b": 0, "grade_c": 1, "grade_a_pct": 0.0},
            {"key": "钱医生", "name": "钱医生", "total": 1, "avg_score": 100.0,
             "grade_a": 1, "grade_b": 0, "grade_c": 0, "grade_a_pct": 100.0},
        ],
    }
    # 分组 key 的两种真实类型——`int | str` 建模的**全部依据**
    assert isinstance(body["by_org"][0]["key"], int)
    assert isinstance(body["by_doctor"][0]["key"], str)


def test_病历_汇总零分支精确(client, admin, records):
    assert client.get("/api/quality/records/qc-summary?period=1999-01", headers=admin).json() == {
        "period": "1999-01",
        "total": 0,
        "avg_score": 0.0,
        "grade_distribution": {"甲": 0, "乙": 0, "丙": 0},
        "grade_a_pct": 0.0,
        "by_org": [],
        "by_doctor": [],
    }


# ---------------------------------------------------------------- 环节质控规则库

RECORD_RULE_KEY_ORDER = [
    "id", "code", "name", "check_field", "field_name", "rule", "rule_name",
    "config", "deduct_points", "active",
]


def test_规则库_列表精确形状与键序(client, admin):
    rows = client.get("/api/quality/record-qc-rules", headers=admin).json()
    assert len(rows) == 12
    for row in rows:
        assert list(row.keys()) == RECORD_RULE_KEY_ORDER
    by_code = {r["code"]: r for r in rows}
    assert by_code["MRQC02"] == {
        "id": by_code["MRQC02"]["id"],
        "code": "MRQC02",
        "name": "主诉简明（不超过20字）",
        "check_field": "chief_complaint",
        "field_name": "主诉",
        "rule": "max_length",
        "rule_name": "字数上限",
        "config": {"max": 20},
        "deduct_points": 3,
        "active": True,
    }
    # 派生字段的 field_name 走 DERIVED_FIELDS 映射
    assert by_code["MRQC11"] == {
        "id": by_code["MRQC11"]["id"],
        "code": "MRQC11",
        "name": "危急值须有处置记录",
        "check_field": "critical_disposal",
        "field_name": "危急值处置记录",
        "rule": "keyword_present",
        "rule_name": "要点关键词",
        "config": {"keywords": ["危急值", "紧急处置", "抢救"], "condition": "has_critical_report"},
        "deduct_points": 10,
        "active": True,
    }


def test_规则库_调整回执精确(client, admin):
    rows = client.get("/api/quality/record-qc-rules?active=true", headers=admin).json()
    rule = next(r for r in rows if r["code"] == "MRQC02")
    patched = client.patch(
        f"/api/quality/record-qc-rules/{rule['id']}", json={"deduct_points": 4}, headers=admin
    ).json()
    assert patched == {**rule, "deduct_points": 4}
    # 恢复原值，避免影响后续模块的评分口径
    restored = client.patch(
        f"/api/quality/record-qc-rules/{rule['id']}", json={"deduct_points": 3}, headers=admin
    ).json()
    assert restored == rule


# ---------------------------------------------------------------- 医疗质量三维指标

INDICATOR_ROW_KEY_ORDER = [
    "key", "name", "dimension", "numerator", "denominator", "rate_pct", "caliber",
]
#: 术前术后符合率这一行**多一个** `uncollected` 键（在 rate_pct 与 caliber 之间）
PREOP_ROW_KEY_ORDER = [
    "key", "name", "dimension", "numerator", "denominator", "rate_pct", "uncollected", "caliber",
]


def _zero_indicators() -> list[dict]:
    """本模块不造住院/手术/急救数据：七项指标全部为零分支，可静态写死。"""
    return [
        {
            "key": "admit_discharge_match",
            "name": "入出院诊断符合率",
            "dimension": "诊断准确",
            "numerator": 0,
            "denominator": 0,
            "rate_pct": 0.0,
            "caliber": "出院诊断与入院诊断一致的出院人次 ÷ 出院人次（诊断名归一化比对）",
        },
        {
            "key": "preop_postop_match",
            "name": "术前术后诊断符合率",
            "dimension": "诊断准确",
            "numerator": 0,
            "denominator": 0,
            "rate_pct": 0.0,
            "uncollected": 0,
            "caliber": "术后诊断与术前诊断一致的手术台次 ÷ 已填两项诊断的手术台次",
        },
        {
            "key": "cure_improve",
            "name": "治愈好转率",
            "dimension": "治疗有效",
            "numerator": 0,
            "denominator": 0,
            "rate_pct": 0.0,
            "caliber": "转归为治愈或好转的出院人次 ÷ 出院人次",
        },
        {
            "key": "mortality",
            "name": "住院死亡率",
            "dimension": "治疗有效",
            "numerator": 0,
            "denominator": 0,
            "rate_pct": 0.0,
            "caliber": "转归为死亡的出院人次 ÷ 出院人次",
        },
        {
            "key": "rescue_success",
            "name": "抢救成功率",
            "dimension": "治疗有效",
            "numerator": 0,
            "denominator": 0,
            "rate_pct": 0.0,
            "caliber": "抢救成功例数 ÷ 已判定转归的抢救例数（未判定的不计入分母）",
        },
        {
            "key": "surgery_complication",
            "name": "手术并发症发生率",
            "dimension": "手术质量",
            "numerator": 0,
            "denominator": 0,
            "rate_pct": 0.0,
            "caliber": "术中记录填写了并发症的台次 ÷ 已出术中记录台次"
                       "（并发症栏留空按无计，故本指标只会低估）",
        },
        {
            "key": "unplanned_return",
            "name": "非计划重返手术室率",
            "dimension": "手术质量",
            "numerator": 0,
            "denominator": 0,
            "rate_pct": 0.0,
            "caliber": "医师标记为非计划重返的已完成手术 ÷ 已出术中记录台次"
                       "（不做推断：分期手术与计划内二次探查不算重返）",
        },
    ]


def test_临床指标_全期精确形状与键序(client, admin, base):
    resp = client.get("/api/quality/clinical-indicators", headers=admin)
    body = resp.json()
    assert list(body.keys()) == ["period", "org_id", "group_id", "indicators"]
    assert list(body["indicators"][0].keys()) == INDICATOR_ROW_KEY_ORDER
    assert list(body["indicators"][1].keys()) == PREOP_ROW_KEY_ORDER
    assert body == {
        "period": "全期",
        "org_id": None,
        "group_id": None,
        "indicators": _zero_indicators(),
    }
    # 条件键 uncollected 只在术前术后那一行出现——其余行注入 null 即改字节
    for row in body["indicators"]:
        assert ("uncollected" in row) == (row["key"] == "preop_postop_match")


def test_临床指标_期间与机构过滤精确(client, admin, base):
    body = client.get(
        f"/api/quality/clinical-indicators?period=2026-08&org_id={base['org1']['id']}",
        headers=admin,
    ).json()
    assert body == {
        "period": "2026-08",
        "org_id": base["org1"]["id"],
        "group_id": None,
        "indicators": _zero_indicators(),
    }
    assert client.get(
        "/api/quality/clinical-indicators?period=2026", headers=admin
    ).status_code == 422
