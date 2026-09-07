"""`qc-summary` 的两个缺陷回归：截断统计输入 + 月份过滤发生在截断之后。

这两条**都不会报错**，所以只能靠造够量的数据把它们照出来。与
`test_qc_summary_characterization.py` 分工：那份钉「改写没改行为」，
这份钉「改写修掉了什么」。两份都必须绿——只绿一份的组合都是错的：

- 只有特征化网绿 → 行为没变，缺陷也没修；
- 只有本文件绿 → 缺陷修了，但顺带改了别的响应字节。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Encounter, MedicalRecord, Organization, Patient, utcnow

#: 原实现的硬编码上限。造的数据必须**超过**它，否则两条用例在修复前也会绿。
OLD_CAP = 5000


@pytest.fixture(scope="module")
def bulk(client, admin):
    """超过原上限的病历，且**跨两个月**。

    数据设计是照着两个缺陷来的：
    - 最新的 `OLD_CAP + 100` 份全是甲级 100 分、落在 2026-05；
    - 更早的 200 份全是丙级 60 分、落在 2026-03。

    于是在原实现上：
    - 累计口径按 id DESC 取前 5000 条，全是甲级 → 丙级 200 份**凭空消失**，
      `grade_distribution["丙"] == 0`、平均分虚高；
    - `period=2026-03` 先取最新 5000 条（全是 5 月的）再在 Python 里筛 3 月
      → `total: 0`，**和「3 月确实没有病历」长得一模一样**。
    """
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "截断质控院", "org_type": "lead_hospital",
                            "level": "county"}).json()
    from datetime import datetime

    with SessionLocal() as db:
        p = Patient(ehc_no="EHC-QC-BULK", name="截断患者",
                    id_card="330155199202021234", gender="female",
                    birth_date="1992-02-02", phone="13911204002")
        db.add(p)
        db.flush()
        now = utcnow()
        rows_enc, rows_rec = [], []

        def add(n, when, score, grade):
            for _ in range(n):
                rows_enc.append({"patient_id": p.id, "org_id": org["id"],
                                 "doctor_name": "批量医生", "encounter_type": "outpatient",
                                 "diagnosis_code": "J00", "diagnosis_name": "感冒",
                                 "summary": "", "created_at": when, "updated_at": when})
                rows_rec.append({"org_id": org["id"], "doctor_name": "批量医生",
                                 "qc_score": score, "qc_grade": grade, "qc_defects": [],
                                 "created_by": 1, "created_at": when, "updated_at": when})

        # 先插旧的（id 小），再插新的（id 大）——原实现按 id DESC 取，旧的被砍掉
        add(200, datetime(2026, 3, 10, 9, 0), 60, "丙")
        add(OLD_CAP + 100, datetime(2026, 5, 10, 9, 0), 100, "甲")
        db.execute(insert(Encounter), rows_enc)
        db.flush()
        enc_ids = [
            e[0] for e in db.query(Encounter.id)
            .filter(Encounter.org_id == org["id"])
            .order_by(Encounter.id).all()
        ]
        assert len(enc_ids) == len(rows_rec)
        for rec, enc_id in zip(rows_rec, enc_ids):
            rec["encounter_id"] = enc_id
        db.execute(insert(MedicalRecord), rows_rec)
        db.commit()
        _ = now
    return {"org": org["id"]}


def _summary(client, admin, **params):
    resp = client.get("/api/quality/records/qc-summary", headers=admin,
                      params={"org_id": params.pop("org", None), **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_统计口径不再被输入行上限截断(client, admin, bulk):
    """5100 份甲级 + 200 份丙级。

    原实现按 id DESC 取前 5000 条——全是甲级——于是 200 份丙级病历
    **一条不剩地消失**，接口报「零丙级」。质控看板上「零丙级」与真的零丙级
    长得一模一样，不报错、不变红、不写日志。
    """
    s = _summary(client, admin, org=bulk["org"])
    assert s["total"] == OLD_CAP + 300, f"统计总数仍被截断：{s['total']}"
    assert s["grade_distribution"]["丙"] == 200, (
        f"丙级病历被截断吞掉了（这正是修复前的症状）：{s['grade_distribution']}"
    )
    assert s["grade_distribution"]["甲"] == OLD_CAP + 100
    # 平均分随之修正：原实现会报 100.0（只看得到甲级那批）
    assert s["avg_score"] < 100.0


def test_查较早的月份不再静默返回零(client, admin, bulk):
    """`period=2026-03` 那 200 份丙级病历必须查得到。

    原实现的月份过滤在 `.limit(5000)` **之后**用 Python 做：先按 id DESC 取最新
    5000 条（全是 5 月的），再筛 3 月 → 一条不剩，返回 `total: 0`。
    **这与「3 月确实没有病历」无法区分**，而端点用 `require_month` 正是为了防
    「一份全零的质控统计」——同一个函数里却从另一头又造了一份。
    """
    march = _summary(client, admin, org=bulk["org"], period="2026-03")
    assert march["total"] == 200, f"较早月份被静默截成零（修复前的症状）：{march['total']}"
    assert march["grade_distribution"]["丙"] == 200
    assert march["avg_score"] == 60.0
    assert march["by_doctor"][0]["total"] == 200

    may = _summary(client, admin, org=bulk["org"], period="2026-05")
    assert may["total"] == OLD_CAP + 100
    # 两个月加起来正好是累计口径——没有行被吞掉
    assert march["total"] + may["total"] == _summary(client, admin, org=bulk["org"])["total"]


def test_确实没有数据的月份仍然返回零(client, admin, bulk):
    """修完之后「查得到」不能变成「凭空有」：真空月份还是零，且不是 422。"""
    s = _summary(client, admin, org=bulk["org"], period="1999-01")
    assert s["total"] == 0 and s["avg_score"] == 0.0
    assert s["grade_distribution"] == {"甲": 0, "乙": 0, "丙": 0}
    assert s["by_org"] == [] and s["by_doctor"] == []
