"""慢专病清单按姓名 / 证件号搜索，不再只在前 500 / 200 个匹配的患者里找（P1-83）。

四个清单的搜索写法一样：先 `db.query(Patient).filter(姓名或证件号 contains).limit(N)` 取出患者号，
再按 `patient_id IN (...)` 筛——

- 候选人群 `GET /api/spd/candidates?keyword=`、在管患者 `GET /api/spd/enrollments?keyword=`（N = 500）；
- 呼叫任务 `GET /api/spd/call-tasks?patient_name=`、个案上报 `GET /api/spd/case-reports?id_card=`（N = 200）。

匹配的患者超过 N 个时，只在**任意** N 个里找（取患者那一句没有排序），其余人的纳管、候选、呼叫、
上报静默不出现，`X-Total-Count` 也跟着少——按常见姓氏或证件号的地区前缀一搜，名单少一截且
无从察觉（P2-8 那一族「清单吃截断样本」的又一例，这次截断藏在筛选条件里）。

修法：患者号那一步换成子查询（`IN (SELECT id FROM patients WHERE ...)`），匹配条件一字不改
（含 PII 加密开态的证件号仅全值命中）。匹配的患者不超过 N 个时结果逐条相同（特征化用例钉住）。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Organization, Patient
from app.spd.models import SpdCallTask, SpdCandidate, SpdCaseReport, SpdEnrollment

N_BULK = 510  # 超过两种上限（500 / 200）


def _get(client, admin, path, **params):
    r = client.get(path, params=params, headers=admin)
    assert r.status_code == 200, r.text
    return r


@pytest.fixture(scope="module")
def world(client, admin):
    """一位名字独特的患者（特征化用），外加 `N_BULK` 位同姓、同证件号前缀的患者；每人各有候选、纳管、呼叫、上报一条。"""
    with SessionLocal() as db:
        org = Organization(name="搜索截断卫生院", org_type="township", level="township")
        db.add(org)
        db.flush()
        rows = [{"ehc_no": "EHC-KW-ONE", "name": "搜索特征化甲", "id_card": "990001199001010001",
                 "gender": "女", "birth_date": "1990-01-01"}]
        rows += [{"ehc_no": f"EHC-KW-{i}", "name": f"搜截姓{i}", "id_card": f"99019919{i:010d}",
                  "gender": "男", "birth_date": "1960-01-01"} for i in range(N_BULK)]
        db.execute(insert(Patient), rows)
        ids = {ehc: pid for pid, ehc in db.query(Patient.id, Patient.ehc_no).filter(Patient.ehc_no.like("EHC-KW-%"))}
        pids = list(ids.values())
        db.execute(insert(SpdCandidate), [
            {"patient_id": pid, "program_code": "kw_prog", "status": "target", "org_id": org.id, "matched_rules": []}
            for pid in pids])
        db.execute(insert(SpdEnrollment), [
            {"patient_id": pid, "program_code": "kw_prog", "org_id": org.id, "status": "active"} for pid in pids])
        db.execute(insert(SpdCallTask), [{"patient_id": pid, "phone": "13900000000"} for pid in pids])
        db.execute(insert(SpdCaseReport), [
            {"patient_id": pid, "program_code": "kw_prog", "org_id": org.id} for pid in pids])
        db.commit()
    return {"one": ids["EHC-KW-ONE"]}


SEARCHES = [
    ("/api/spd/candidates", "keyword", {"program_code": "kw_prog"}),
    ("/api/spd/enrollments", "keyword", {"program_code": "kw_prog"}),
    ("/api/spd/call-tasks", "patient_name", {}),
]


# ---------------------------------------------------------------- 特征化
@pytest.mark.parametrize("path,param,extra", SEARCHES)
def test_特征化_名字独特时只命中那一位(client, admin, world, path, param, extra):
    r = _get(client, admin, path, **{param: "搜索特征化甲"}, **extra)
    rows = r.json()
    assert [x["patient_id"] for x in rows] == [world["one"]]
    assert r.headers["X-Total-Count"] == "1"


@pytest.mark.parametrize("path,param,extra", SEARCHES)
def test_特征化_谁都不匹配时是空的(client, admin, world, path, param, extra):
    r = _get(client, admin, path, **{param: "没有这个人的名字"}, **extra)
    assert r.json() == [] and r.headers["X-Total-Count"] == "0"


def test_特征化_证件号搜索个案上报(client, admin, world):
    r = _get(client, admin, "/api/spd/case-reports", id_card="990001199001010001")
    assert [x["patient_id"] for x in r.json()] == [world["one"]]


# ---------------------------------------------------------------- 缺陷回归
@pytest.mark.parametrize("path,param,extra", SEARCHES)
def test_同姓的人超过上限也一个不少(client, admin, world, path, param, extra):
    r = _get(client, admin, path, **{param: "搜截姓"}, **extra)
    assert r.headers["X-Total-Count"] == str(N_BULK), f"{path} 按姓名搜索被截在上限上了"


def test_证件号前缀命中的人超过上限也一个不少(client, admin, world):
    r = _get(client, admin, "/api/spd/case-reports", id_card="99019919")
    assert r.headers["X-Total-Count"] == str(N_BULK), "个案上报按证件号搜索被截在上限上了"


def test_在管患者也能按证件号前缀搜全(client, admin, world):
    r = _get(client, admin, "/api/spd/enrollments", keyword="99019919", program_code="kw_prog")
    assert r.headers["X-Total-Count"] == str(N_BULK)
