"""慢专病各页的待办只看「最新一页」：工作台报「待复核筛查 / 待确认迁入」N 条，页面固定取最新 20～50 条混排，挤出窗口的那几条
没有一处能办（P2-782，第二十批「同一个数，多处口径」扫描 M1-2）。

平台侧同一个毛病早修过并共用 `core.js` 的 `actionableFirst`（审方 P1-148、咨询 / 用血 / 上门 P2-408、会诊 / 转诊 / 病理 /
告知书 P2-456，见 `test_actionable_first_pages.py`），pages-spd.js 一处都没用。实测：中心工作台待复核筛查 1，筛查清单 30 行
（总 31），带「确认 / 排除」的待复核行 0；按 `?result=suspect&reviewed=false` 取得到。生命周期清单同形（「确认迁入」）。
修法同平台侧：待办按状态单独取一遍、排在最前、按 id 去重。七页九份清单：筛查（疑似未复核）、生命周期（待确认迁入）、召回
（待联系 / 已联系）、路径实例（执行中 / 已暂停）、转诊（在途）、外呼（待呼叫）、干预（计划 / 进行中）、个案上报（待处置 /
处置中）、咨询（进行中）。目标池「待分发」与质控「未判定」的清单接口没有对应的筛选，不在此列。
"""
import os

import pytest

from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
B = "/api/spd"


def _function(name: str) -> str:
    with open(os.path.join(STATIC, "pages-spd.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"async function {name}()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


@pytest.mark.parametrize("name, fetches, merged", [
    ("renderSpdPatients",
     ['api("/api/spd/screenings?result=suspect&reviewed=false&limit=200")',
      'api("/api/spd/lifecycle-events?event=migrate&confirmed=false&limit=200")'],
     ["const rows = actionableFirst(recent, pending);",
      "const rows = actionableFirst(recent, pending.filter((v) => v.can_confirm));"]),   # P2-829：本机构能确认的排前
    ("renderSpdCenter",
     ['api("/api/spd/recalls?status=pending&limit=200")', 'api("/api/spd/recalls?status=contacted&limit=200")'],
     ["const recalls = actionableFirst(recentRecalls, pendingRecalls, contactedRecalls);"]),
    ("renderSpdPath",
     ['api("/api/spd/path-instances?status=running&limit=200")', 'api("/api/spd/path-instances?status=paused&limit=200")'],
     ["const rows = actionableFirst(recent, running, paused);"]),
    ("renderSpdReferral",
     ['api("/api/spd/referrals?open_only=true&limit=200")'],
     ["const cases = actionableFirst(recentCases, openCases);"]),
    ("renderSpdFollowup",
     ['api("/api/spd/call-tasks?status=pending&limit=200")'],
     ["const calls = actionableFirst(recentCalls, pendingCalls);"]),
    ("renderSpdMember",
     ['api("/api/spd/interventions?status=planned&limit=200")', 'api("/api/spd/interventions?status=doing&limit=200")',
      'api("/api/spd/case-reports?status=pending&limit=200")', 'api("/api/spd/case-reports?status=handling&limit=200")'],
     ["const interventions = actionableFirst(recentItv, plannedItv, doingItv);",
      "const reports = actionableFirst(recentReports, pendingReports, handlingReports);"]),
    ("renderSpdManager",
     ['api("/api/spd/consults?status=open&limit=200")'],
     ["const consults = actionableFirst(recentConsults, openConsults);"]),
])
def test_待办单独取_排在最前(name, fetches, merged):
    body = _function(name)
    for fetch in fetches:
        assert fetch in body, fetch   # 修前只取最新一页
    for line in merged:
        assert line in body, line


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdEnrollment, SpdLifecycleEvent, SpdScreening

    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2782 {name}", "org_type": "township", "level": "township"}).json()["id"] for name in ("甲院", "乙院")]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2782 患者", "id_card": "330102195001012782"}).json()["id"]
    with SessionLocal() as db:
        oldest = SpdScreening(patient_id=patient, program_code="hypertension", org_id=orgs[0], result="suspect",
                              risk_level="high", reviewed=False)
        db.add(oldest)
        db.add_all([SpdScreening(patient_id=patient, program_code="hypertension", org_id=orgs[0], result="normal")
                    for _ in range(30)])   # 之后又登了 30 条未见异常的，把它挤出最新一页
        enrollment = SpdEnrollment(patient_id=patient, program_code="hypertension", org_id=orgs[0], status="migrated")
        db.add(enrollment)
        db.flush()
        pending = SpdLifecycleEvent(enrollment_id=enrollment.id, event="migrate", target_org_id=orgs[1], confirmed=False)
        db.add(pending)
        db.add_all([SpdLifecycleEvent(enrollment_id=enrollment.id, event="exclude", confirmed=True) for _ in range(20)])
        db.commit()
        return {"screening": oldest.id, "migration": pending.id}


def test_最新一页里没有的待复核筛查_按状态取得到(client, admin, world):
    recent = [s["id"] for s in client.get(f"{B}/screenings", headers=admin, params={"limit": 30}).json()]
    assert world["screening"] not in recent   # 判据自证：确实被挤出了窗口
    pending = client.get(f"{B}/screenings", headers=admin, params={"result": "suspect", "reviewed": "false", "limit": 200})
    assert world["screening"] in [s["id"] for s in pending.json()]


def test_最新一页里没有的待确认迁入_按状态取得到(client, admin, world):
    recent = [v["id"] for v in client.get(f"{B}/lifecycle-events", headers=admin, params={"limit": 20}).json()]
    assert world["migration"] not in recent
    pending = client.get(f"{B}/lifecycle-events", headers=admin,
                         params={"event": "migrate", "confirmed": "false", "limit": 200}).json()
    assert world["migration"] in [v["id"] for v in pending]


def test_待办取数用的查询参数_后端都认():
    """FastAPI 对不认识的查询参数一声不吭、照返全量——参数名写错时「待办」其实是没筛过的最新一页，上面的静态钉看不出来。"""
    import re
    from urllib.parse import parse_qsl, urlsplit

    import test_api_contract_governance as contract

    with open(os.path.join(STATIC, "pages-spd.js"), encoding="utf-8") as fh:
        source = fh.read()
    urls = sorted(set(re.findall(r'api\("(/api/spd/[\w/-]+\?[^"]*limit=200)"\)', source)))
    assert len(urls) >= 12, urls   # 判据自证：九份清单、十几个待办取数都认得出
    routes = {route.path: route for _module, route in contract._iter_endpoints() if "GET" in route.methods}
    for url in urls:
        parts = urlsplit(url)
        known = {param.alias for param in routes[parts.path].dependant.query_params}
        unknown = [name for name, _ in parse_qsl(parts.query) if name not in known]
        assert not unknown, (url, unknown)
