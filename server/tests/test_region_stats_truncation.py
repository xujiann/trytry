"""卫健端区域结构分析：总数与人口结构不再算在被截断的样本上，体征计数按统计范围收口（P1-51）。

`GET /api/spd/stats/region` 先 `enroll_query.limit(20000).all()` 取纳管记录，再在 Python 里数
`total`、年龄段、性别——县域纳管量过 20000 之后，卫健端看到的患者总数与人口结构都是错的，
而「就这么多」与真的就这么多长得一模一样（P2-8 那一族的第九例）。同一个函数里体征计数
`db.query(SpdMeasurement).count()` 不带任何范围：在一个已按调用方统计范围收口的端点里返回全县计数，
乡镇看到的「体征数据」是全县的，与同一张表上的其余数字口径对不上。

修法：`total` 走 `count()`；年龄 / 性别按纳管与患者的连接**流式**逐行数（分桶与「未知」的判法一字不改，
也不再有 `Patient.id.in_([...])` 那两万个参数）；体征计数在收了范围（非全域或指定病种）时只数
在管患者的——全域且不指定病种时照旧数全表。

两段分工（与 `test_spd_stats_truncation.py` 同一套做法）：特征化用例在改前改后都绿，钉「没改行为」；
灌量用例造超过上限的数据，钉「修了什么」。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Organization, Patient
from app.spd.models import SpdEnrollment, SpdMeasurement

CAP = 20000  # region_stats 原硬编码上限


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def small(client, admin):
    """甲、乙两家卫生院（不同片区），各有在管患者与体征数据。"""
    with SessionLocal() as db:
        a = Organization(name="区域结构甲卫生院", org_type="township", level="township")
        b = Organization(name="区域结构乙卫生院", org_type="township", level="township")
        db.add_all([a, b])
        db.flush()
        people = [
            Patient(ehc_no="EHC-RS-1", name="区域甲一", id_card="330166195001011111", gender="男", birth_date="1950-01-01"),
            Patient(ehc_no="EHC-RS-2", name="区域甲二", id_card="330166198502021112", gender="女", birth_date="1985-02-02"),
            Patient(ehc_no="EHC-RS-3", name="区域甲三", id_card="330166200003031113", gender="", birth_date="坏日期"),
            Patient(ehc_no="EHC-RS-4", name="区域乙一", id_card="330166197004041114", gender="女", birth_date="1970-04-04"),
        ]
        db.add_all(people)
        db.flush()
        for p, org in zip(people, (a, a, a, b)):
            db.add(SpdEnrollment(patient_id=p.id, program_code="rs_small", org_id=org.id, status="active"))
        # 体征：甲院患者 2 条（1 正常），乙院患者 3 条（全正常）
        for p, level in ((people[0], "normal"), (people[1], "high"), (people[3], "normal"),
                         (people[3], "normal"), (people[3], "normal")):
            db.add(SpdMeasurement(patient_id=p.id, program_code="rs_small", metric="sbp", value=120, level=level))
        db.commit()
        orgs = {"a": a.id, "b": b.id}
    r = client.post("/api/users", json={"username": "p151_doc_a", "password": "pw123456", "full_name": "p151_doc_a",
                                        "role": "doctor", "org_id": orgs["a"]}, headers=admin)
    assert r.status_code == 201, r.text
    return {"orgs": orgs, "doc_a": _login(client, "p151_doc_a")}


def _region(client, headers, **params):
    r = client.get("/api/spd/stats/region", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 特征化（灌量前）
def test_特征化_全域看得到两家的结构(client, admin, small):
    s = _region(client, admin, program_code="rs_small")
    assert s["total"] == 4
    assert s["by_org"] == {str(small["orgs"]["a"]): 3, str(small["orgs"]["b"]): 1}
    # 分桶与「未知」的判法：性别不在男女里记性别未知；生日解析不了记年龄未知（性别照样计）
    assert s["gender_distribution"] == {"男": 1, "女": 2, "未知": 1}
    assert s["age_distribution"] == {"0-17": 0, "18-44": 1, "45-59": 1, "60-74": 0, "75+": 1, "未知": 1}


def test_特征化_本机构只看本机构的结构(client, small):
    s = _region(client, small["doc_a"], program_code="rs_small")
    assert s["total"] == 3
    assert s["gender_distribution"] == {"男": 1, "女": 1, "未知": 1}


# ---------------------------------------------------------------- 缺陷回归
def test_体征计数按统计范围收口(client, small):
    """甲院医生看到的体征数只含甲院在管患者的（2 条、1 条正常），而不是全表。"""
    s = _region(client, small["doc_a"], program_code="rs_small")
    assert s["measurements"] == {"total": 2, "normal": 1}


def test_全域且不指定病种时体征照旧数全表(client, admin, small):
    with SessionLocal() as db:
        total = db.query(SpdMeasurement).count()
        normal = db.query(SpdMeasurement).filter(SpdMeasurement.level == "normal").count()
    assert _region(client, admin)["measurements"] == {"total": total, "normal": normal}


@pytest.fixture(scope="module")
def bulk(small):
    """在乙院再灌 `CAP + 100` 位 80 岁的女性在管患者（另一个病种，不影响上面的小样本）。"""
    with SessionLocal() as db:
        start = db.query(Patient).count()
        db.execute(insert(Patient), [
            {"ehc_no": f"EHC-RSB-{i}", "name": f"灌量{i}", "id_card": f"RSB{i:015d}",
             "gender": "女", "birth_date": "1940-01-01"}
            for i in range(CAP + 100)
        ])
        ids = [pid for (pid,) in db.query(Patient.id).filter(Patient.ehc_no.like("EHC-RSB-%")).all()]
        assert len(ids) == CAP + 100 and start >= 0
        db.execute(insert(SpdEnrollment), [
            {"patient_id": pid, "program_code": "rs_bulk", "org_id": small["orgs"]["b"], "status": "active"}
            for pid in ids
        ])
        db.commit()
    return {"n": CAP + 100}


def test_不再被上限截断_总数与人口结构(client, admin, bulk):
    s = _region(client, admin, program_code="rs_bulk")
    assert s["total"] == bulk["n"], "总数被截断在上限上了"
    assert s["gender_distribution"]["女"] == bulk["n"]
    assert s["age_distribution"]["75+"] == bulk["n"]
    assert sum(s["age_distribution"].values()) == bulk["n"]
