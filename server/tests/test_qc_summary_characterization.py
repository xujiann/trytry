"""`GET /api/quality/records/qc-summary` 的**特征化网**：先钉住现状，再改实现。

按 `docs/特征化测试指引.md` 的做法：重写聚合口径之前，先把**当前输出**逐字段钉死，
让"改写没改行为"变成一条可判定的检查，而不是一句自我保证。

这个端点有两个必须**原样保住**的细节，它们都不写在任何文档里，只存在于实现细节中：

1. **`by_org` / `by_doctor` 并列时的次序。** 现实现是
   `sorted(buckets.items(), key=lambda kv: -kv[1]["total"])`，Python 的 sort 是**稳定**的，
   所以 total 相同的组保持**插入序**；而插入序又来自 `records` 的迭代顺序，即 `id DESC`。
   合起来：**并列时，最大 id 更大的组排在前面**。换成 SQL `GROUP BY` 时必须写成
   `ORDER BY total DESC, MAX(id) DESC` 才等价——漏了后半句，并列组的次序就会跟着
   数据库的心情走，而那是响应字节的一部分。
2. **`grade_distribution` 对意料之外的等级是"加一个键"而不是"忽略"。**
   它从 `_grade_bucket()`（甲/乙/丙 三个 0）起步，然后 `grades.get(g, 0) + 1`——
   于是库里出现一个 `qc_grade="丁"` 时，响应里会**多出一个 `"丁"` 键**。
   而每组的 `grade_a/b/c` 只读那三个固定键，所以"丁"进了 `total` 却不进任何一个 grade_x
   ——`grade_a + grade_b + grade_c < total` 是**现状允许**的形状。

本文件在**当前实现**上写成并跑通，然后才动实现；重写后必须一条不改地继续绿。
"""
import pytest

from app.database import SessionLocal
from app.models import Encounter, MedicalRecord, Organization, Patient


def _record(db, *, org_id, patient_id, doctor, score, grade, created_at):
    enc = Encounter(patient_id=patient_id, org_id=org_id, doctor_name=doctor,
                    encounter_type="outpatient", diagnosis_code="J00",
                    diagnosis_name="感冒", summary="")
    db.add(enc)
    db.flush()
    # MedicalRecord 一次就诊一份（encounter_id 唯一），不带 patient_id——
    # 患者维度经 encounter 反查，统计口径只用冗余的 org_id / doctor_name
    rec = MedicalRecord(encounter_id=enc.id, org_id=org_id,
                        doctor_name=doctor, qc_score=score, qc_grade=grade,
                        qc_defects=[], created_by=1, created_at=created_at,
                        updated_at=created_at)
    db.add(rec)
    return rec


@pytest.fixture(scope="module")
def seeded(client, admin):
    """一份专门照出上面两个细节的数据。

    - 甲院与乙院各 2 份 → `by_org` 两组 **total 并列**，次序只能由 max(id) 决定；
    - 张医生与李医生各 2 份 → `by_doctor` 同样并列；
    - 一份 `qc_grade="丁"` → 照出 grade_distribution 会多长一个键；
    - 跨两个月 → 照出 period 过滤（也照出旧月份那条路径）。
    """
    from datetime import datetime

    a = client.post("/api/organizations", headers=admin,
                    json={"name": "特征甲院", "org_type": "lead_hospital",
                          "level": "county"}).json()
    b = client.post("/api/organizations", headers=admin,
                    json={"name": "特征乙院", "org_type": "township",
                          "level": "township"}).json()
    with SessionLocal() as db:
        p = Patient(ehc_no="EHC-QC-1", name="特征患者", id_card="330155199001011234",
                    gender="male", birth_date="1990-01-01", phone="13911204001")
        db.add(p)
        db.flush()
        old, new = datetime(2026, 1, 15, 9, 0), datetime(2026, 2, 15, 9, 0)
        _record(db, org_id=a["id"], patient_id=p.id, doctor="张医生",
                score=100, grade="甲", created_at=old)
        _record(db, org_id=a["id"], patient_id=p.id, doctor="李医生",
                score=80, grade="乙", created_at=new)
        _record(db, org_id=b["id"], patient_id=p.id, doctor="张医生",
                score=60, grade="丙", created_at=new)
        _record(db, org_id=b["id"], patient_id=p.id, doctor="李医生",
                score=90, grade="丁", created_at=new)
        db.commit()
    return {"a": a["id"], "b": b["id"]}


def _summary(client, admin, **params):
    resp = client.get("/api/quality/records/qc-summary", headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_累计口径的每个字段(client, admin, seeded):
    s = _summary(client, admin)
    assert s["period"] == "累计"
    assert s["total"] == 4
    assert s["avg_score"] == 82.5          # (100+80+60+90)/4
    assert s["grade_a_pct"] == 25.0        # 1/4
    # 意料之外的等级会**多长一个键**，不是被忽略——这是现状，重写后必须一样
    assert s["grade_distribution"] == {"甲": 1, "乙": 1, "丙": 1, "丁": 1}


def test_并列组的次序按最大id倒序(client, admin, seeded):
    """甲乙两院各 2 份，total 并列；张李两位医师同样并列。

    现实现靠「Python 稳定排序 + records 按 id DESC 迭代」定次序，
    等价于**并列时 max(id) 大的在前**。乙院的两条 id 更大（后插入），故乙院在前；
    医师同理：李医生的最大 id（"丁"那条）大于张医生的。
    """
    s = _summary(client, admin)
    assert [o["key"] for o in s["by_org"]] == [seeded["b"], seeded["a"]]
    assert [d["key"] for d in s["by_doctor"]] == ["李医生", "张医生"]


def test_分组字段逐条(client, admin, seeded):
    s = _summary(client, admin)
    by_org = {o["key"]: o for o in s["by_org"]}
    a, b = by_org[seeded["a"]], by_org[seeded["b"]]
    assert a == {"key": seeded["a"], "name": "特征甲院", "total": 2, "avg_score": 90.0,
                 "grade_a": 1, "grade_b": 1, "grade_c": 0, "grade_a_pct": 50.0}
    # 乙院那份"丁"进了 total、却不进任何 grade_x —— grade_a+b+c < total 是现状允许的
    assert b == {"key": seeded["b"], "name": "特征乙院", "total": 2, "avg_score": 75.0,
                 "grade_a": 0, "grade_b": 0, "grade_c": 1, "grade_a_pct": 0.0}
    assert b["grade_a"] + b["grade_b"] + b["grade_c"] < b["total"]


def test_按机构过滤(client, admin, seeded):
    s = _summary(client, admin, org_id=seeded["b"])
    assert s["total"] == 2 and len(s["by_org"]) == 1
    assert s["by_org"][0]["name"] == "特征乙院"
    assert s["avg_score"] == 75.0


def test_按月份过滤(client, admin, seeded):
    jan = _summary(client, admin, period="2026-01")
    assert jan["period"] == "2026-01" and jan["total"] == 1
    assert jan["avg_score"] == 100.0 and jan["grade_distribution"]["甲"] == 1
    feb = _summary(client, admin, period="2026-02")
    assert feb["total"] == 3

    empty = _summary(client, admin, period="1999-01")
    assert empty["total"] == 0 and empty["avg_score"] == 0.0
    assert empty["grade_a_pct"] == 0.0
    assert empty["grade_distribution"] == {"甲": 0, "乙": 0, "丙": 0}
    assert empty["by_org"] == [] and empty["by_doctor"] == []


def test_非法月份仍是422(client, admin, seeded):
    for bad in ("bad", "2026-13"):
        resp = client.get("/api/quality/records/qc-summary", headers=admin,
                          params={"period": bad})
        assert resp.status_code == 422, f"{bad} 应当 422：{resp.text}"
