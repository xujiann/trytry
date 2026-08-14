"""第九轮：横向越权矩阵与敏感读留痕。

平台的纵向越权矩阵一直是绿的、还报"覆盖率 100.0%"——它测的是"医生不能干
管理员的事"。**横向越权一条没测**：甲机构的人能不能看乙机构的数据。
实测过，能：乙镇卫生院的医生凭 ehc_no 就调得出甲县医院患者的全部诊疗信息。

这个文件要做两件事：

1. 把"跨机构访问"这条维度也变成**可量化**的矩阵。第八轮的教训是一条只覆盖
   11% 的规则同样会全绿——所以这里不但要断言已守住的守住了，还要把
   **没守住的逐条列出来**（`UNGUARDED`），让缺口是显式的、会变小的，
   而不是靠"测试绿了"来假装不存在。
2. 断言留痕：能看的每一次都记下依据，事后答得出"谁在什么时候凭什么看了谁"。
"""
import ast
import os

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import AccessLog

ROUTER_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "routers")


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client):
    """两家互不相干的机构，各一名医生；患者只在甲院有就诊记录。"""
    admin = _login(client, "admin", "admin123")
    a = client.post(
        "/api/organizations",
        json={"name": "横向甲县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    b = client.post(
        "/api/organizations",
        json={"name": "横向乙卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    for name, org in (("hz_doc_a", a), ("hz_doc_b", b)):
        client.post(
            "/api/users",
            json={"username": name, "password": "pw123456", "full_name": name,
                  "role": "doctor", "org_id": org["id"]},
            headers=admin,
        )
    patient = client.post(
        "/api/patients",
        json={"name": "横向患者", "id_card": "320000199505057788"},
        headers=admin,
    ).json()
    doc_a = _login(client, "hz_doc_a")
    doc_b = _login(client, "hz_doc_b")
    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
        headers=doc_a,
    )
    return {"admin": admin, "a": a, "b": b, "doc_a": doc_a, "doc_b": doc_b, "patient": patient}


@pytest.fixture(scope="module")
def stranger(client, world):
    """一家**永远不会**与该患者产生业务关系的机构，专用于拒绝断言。

    起初这些断言直接用乙院医生，结果乙院在"下转"那条用例里拿到了转诊关系，
    后面的拒绝断言就跟着变成通过——**用例之间靠执行顺序互相影响**。
    可见性判定本来就会随业务推进而变化，所以断言"拒绝"必须找一个状态不会变的人。
    """
    org = client.post(
        "/api/organizations",
        json={"name": "横向丙村卫生室", "org_type": "village", "level": "village"},
        headers=world["admin"],
    ).json()
    client.post(
        "/api/users",
        json={"username": "hz_doc_x", "password": "pw123456", "full_name": "丙医生",
              "role": "doctor", "org_id": org["id"]},
        headers=world["admin"],
    )
    return {"org": org, "headers": _login(client, "hz_doc_x")}


# ================================================================ 患者档案


def test_无关机构调阅患者档案应当被拒(client, world, stranger):
    """实测过的那条洞：无关机构的医生凭 ehc_no 拿到甲院患者的 360 全景。"""
    r = client.get(f"/api/archive/{world['patient']['ehc_no']}", headers=stranger["headers"])
    assert r.status_code == 403, f"无关机构仍能调阅档案：{r.status_code}"
    assert "无权调阅" in r.json()["detail"]


def test_有服务关系的机构照常调阅(client, world):
    """隔离不能把正常诊疗挡掉——这条比上一条更容易出错。"""
    r = client.get(f"/api/archive/{world['patient']['ehc_no']}", headers=world["doc_a"])
    assert r.status_code == 200, f"本机构医生被挡住了：{r.text[:120]}"


def test_管理员全域可见但同样留痕(client, world):
    r = client.get(f"/api/archive/{world['patient']['ehc_no']}", headers=world["admin"])
    assert r.status_code == 200
    db = SessionLocal()
    try:
        row = (
            db.query(AccessLog)
            .filter(AccessLog.username == "admin", AccessLog.patient_id == world["patient"]["id"])
            .order_by(AccessLog.id.desc())
            .first()
        )
    finally:
        db.close()
    assert row is not None and row.basis == "global", "全域角色也必须留痕"


# ================================================================ 依据的建立


@pytest.mark.parametrize(
    "resource,url",
    [
        ("vaccination", "/api/vaccination/records?patient_id={pid}"),
        ("medication", "/api/medication/profile/{pid}"),
        ("publichealth", "/api/publichealth/reminders/{pid}"),
    ],
)
def test_患者维度接口一律按同一套可见性判定(client, world, stranger, resource, url):
    """不是只有 360 要守。挨个模块守才有意义——漏一个就等于没守，
    因为拿数据的人只需要找到那一个。"""
    target = url.format(pid=world["patient"]["id"])
    assert client.get(target, headers=stranger["headers"]).status_code == 403, f"{resource} 未拦住"
    assert client.get(target, headers=world["doc_a"]).status_code == 200, f"{resource} 误伤本机构"


def test_转诊建立后乙院即可调阅且依据记为转诊(client, world):
    """医共体的核心场景：下转之后，接收机构必须看得到既往记录。

    隔离做过头会挡掉双向转诊，那比不做隔离更糟——它会让人把整套隔离关掉。
    """
    pid = world["patient"]["id"]
    client.post(
        "/api/referrals",
        json={"patient_id": pid, "from_org_id": world["a"]["id"],
              "to_org_id": world["b"]["id"], "direction": "down", "reason": "康复期下转"},
        headers=world["doc_a"],
    )
    r = client.get(f"/api/archive/{world['patient']['ehc_no']}", headers=world["doc_b"])
    assert r.status_code == 200, "下转之后接收机构仍打不开档案"

    db = SessionLocal()
    try:
        row = (
            db.query(AccessLog)
            .filter(AccessLog.username == "hz_doc_b", AccessLog.patient_id == pid)
            .order_by(AccessLog.id.desc())
            .first()
        )
    finally:
        db.close()
    assert row is not None and row.basis == "referral", f"依据应记为转诊，实际 {row and row.basis}"


def test_就诊清单不带患者时只返回本机构(client, world, stranger):
    """原先返回全表：任何账号翻一页就拿到全县就诊记录。"""
    rows = client.get("/api/encounters", headers=stranger["headers"]).json()
    assert rows == [], f"无关机构看到了别家的就诊记录：{rows[:2]}"
    mine = client.get("/api/encounters", headers=world["doc_a"]).json()
    assert mine and all(e["org_id"] == world["a"]["id"] for e in mine)


# ================================================================ 留痕


def test_留痕记全谁在什么时候凭什么看了谁(client, world):
    pid = world["patient"]["id"]
    client.get(f"/api/archive/{world['patient']['ehc_no']}", headers=world["doc_a"])
    db = SessionLocal()
    try:
        row = (
            db.query(AccessLog)
            .filter(AccessLog.username == "hz_doc_a", AccessLog.patient_id == pid)
            .order_by(AccessLog.id.desc())
            .first()
        )
    finally:
        db.close()
    assert row is not None
    assert row.org_id == world["a"]["id"]
    assert row.resource == "archive_360"
    assert row.basis in {"encounter", "service"}
    assert row.created_at is not None


def test_被拒的调阅不写留痕(client, world, stranger):
    """403 不该产生"看过"的记录——留痕表是"谁看了什么"，不是"谁试过"。
    试探行为由写审计与访问日志覆盖，混进来会让这张表没法直接用于合规举证。"""
    db = SessionLocal()
    try:
        before = db.query(AccessLog).count()
    finally:
        db.close()
    client.get(f"/api/patients/{world['patient']['id']}/authorizations", headers=stranger["headers"])
    client.get(f"/api/medication/profile/{world['patient']['id']}", headers=stranger["headers"])
    db = SessionLocal()
    try:
        rows = db.query(AccessLog).order_by(AccessLog.id.desc()).limit(3).all()
        after = db.query(AccessLog).count()
    finally:
        db.close()
    # 授权办理是"只留痕不阻断"那一档，会多一条 consent_admin；用药画像被拒，不该有
    assert after - before == 1, f"被拒的调阅写了留痕：{[(r.resource, r.basis) for r in rows]}"
    assert rows[0].basis == "consent_admin"


# ================================================================ 机构维度


def test_机构维度管理数据不可跨机构读取(client, world, stranger):
    """实测过的洞：乙卫生院的账号带 ?org_id=甲 就能拉到甲院的职工名册、
    资产清单、药品库存。财务与人事是横向隔离里最敏感的一类。"""
    a = world["a"]["id"]
    cases = [
        ("职工名册", f"/api/mgmt/employees?org_id={a}"),
        ("资产清单", f"/api/mgmt/assets?org_id={a}"),
        ("科室设置", f"/api/mgmt/departments?org_id={a}"),
        ("药品库存", f"/api/pharmacy/stocks?org_id={a}"),
        ("医废清单", f"/api/medwaste?org_id={a}"),
        ("会计凭证", f"/api/accounting/vouchers?org_id={a}"),
        ("预算执行", f"/api/mgmt/budgets/execution?org_id={a}&year=2026"),
        ("病历质控清单", f"/api/quality/records?org_id={a}"),
        ("手术申请", f"/api/surgery/requests?org_id={a}"),
    ]
    leaked = []
    for label, url in cases:
        code = client.get(url, headers=stranger["headers"]).status_code
        if code == 404:
            leaked.append(f"{label} 路由 404（用例写错了）：{url}")
        elif code != 403:
            leaked.append(f"{label} 未拦住：{code}")
    assert leaked == [], "以下机构维度接口对外机构仍然开放：\n  " + "\n  ".join(leaked)


def test_不带org_id时清单自动缩到本机构(client, world, stranger):
    """不带参数不能等于"看全县"——原先 /api/mgmt/employees 不带 org_id 返回全表。"""
    rows = client.get("/api/mgmt/employees", headers=stranger["headers"]).json()
    assert all(r["org_id"] == stranger["org"]["id"] for r in rows), \
        f"无关机构不带参数看到了别家职工：{rows[:2]}"


def test_财务汇总只含可见机构(client, world, stranger):
    """集中核算的汇总接口原先返回全县各家收支。现在乙院账号只能看到自己。"""
    admin = world["admin"]
    client.post("/api/mgmt/finance", json={"org_id": world["a"]["id"], "period": "2026-08",
                "category": "income", "amount": 999999.0}, headers=admin)
    data = client.get("/api/mgmt/finance/summary", headers=stranger["headers"]).json()
    orgs = {o["org_id"] for o in data["orgs"]}
    assert world["a"]["id"] not in orgs, f"无关机构在财务汇总里看到了甲院：{data['orgs']}"


def test_同医共体成员可见统计汇总(client, world):
    """A 案的另一半：牵头医院要看得到片区汇总，隔离不能把医共体的管理职能关掉。

    把甲乙两院编进同一个分组后，乙院的财务汇总里应当**看得到甲院的行**——
    汇总统计取医共体范围（stats_org_ids），比明细宽一档，这是刻意的。
    """
    admin = world["admin"]
    group = client.post(
        "/api/org-groups",
        json={"name": "横向片区", "group_type": "zone", "lead_org_id": world["a"]["id"]},
        headers=admin,
    ).json()
    for org in (world["a"], world["b"]):
        client.post(f"/api/org-groups/{group['id']}/members",
                    json={"org_id": org["id"]}, headers=admin)
    data = client.get("/api/mgmt/finance/summary", headers=world["doc_b"]).json()
    orgs = {o["org_id"] for o in data["orgs"]}
    assert world["a"]["id"] in orgs, "同医共体成员在汇总里看不到牵头医院——统计口径被收得过紧"


# ================================================================ 写侧


@pytest.fixture(scope="module")
def stranger_op(client, world, stranger):
    """丙机构的**经办**账号。写侧拒绝断言必须用它而不是医生：
    建职工、记财务这些接口的角色守卫本来就拒医生，用医生去测，
    用例会因角色而绿——机构守卫删掉它也不红，等于没测。"""
    client.post(
        "/api/users",
        json={"username": "hz_op_x", "password": "pw123456", "full_name": "丙经办",
              "role": "operator", "org_id": stranger["org"]["id"]},
        headers=world["admin"],
    )
    return _login(client, "hz_op_x")


def test_不得以别家机构名义写入(client, world, stranger, stranger_op):
    """写侧的洞比读侧重：实测乙院的账号能给甲院记一笔 88888 的支出、
    建一个假职工——这是在替别家做账。集中核算的数字要是能被任何成员机构
    写进别家账本，汇总就没有意义了。"""
    a = world["a"]["id"]
    sh = stranger_op
    cases = [
        ("建职工", "/api/mgmt/employees", {"org_id": a, "name": "假职工", "title": "医师"}),
        ("记支出", "/api/mgmt/finance",
         {"org_id": a, "period": "2026-08", "category": "expense", "amount": 88888.0}),
        ("建医废点位", "/api/medwaste/locations", {"org_id": a, "name": "假点位"}),
        ("建科室", "/api/mgmt/departments", {"org_id": a, "code": "FKD", "name": "假科室"}),
    ]
    leaked = []
    for label, url, body in cases:
        r = client.post(url, json=body, headers=sh)
        if r.status_code == 404:
            leaked.append(f"{label} 路由 404（用例写错了）：{url}")
        elif r.status_code != 403 or "机构名义" not in r.json().get("detail", ""):
            # 只认机构守卫的 403：角色守卫的 403 不算数——那是纵向矩阵的事，
            # 认了它，机构守卫删掉这条用例也不红
            leaked.append(f"{label} 未被机构守卫拦住：{r.status_code} {r.text[:60]}")
    # 症候群上报的角色关是 public_health|doctor，经办过不了，单独用医生号测
    r = client.post(
        "/api/surveillance/syndromes",
        json={"org_id": a, "syndrome": "fever", "case_count": 999, "threshold": 0,
              "record_date": "2026-08-13"},
        headers=stranger["headers"],
    )
    if r.status_code != 403 or "机构名义" not in r.json().get("detail", ""):
        leaked.append(f"上报症候群 未被机构守卫拦住：{r.status_code}")
    assert leaked == [], "以下写接口可替别家机构写入：\n  " + "\n  ".join(leaked)


def test_以本机构名义写入照常(client, world, stranger, stranger_op):
    """守卫不能把正常录入挡掉。丙村卫生室的经办给自己建职工必须成功。"""
    r = client.post(
        "/api/mgmt/employees",
        json={"org_id": stranger["org"]["id"], "name": "丙村医", "title": "乡村医生"},
        headers=stranger_op,
    )
    assert r.status_code == 201, f"本机构写入被误伤：{r.text[:120]}"


# ================================================================ 覆盖率矩阵


def _patient_scoped_endpoints() -> dict[str, list[str]]:
    """按 patient_id / ehc_no 取数的 GET 接口。"""
    found: dict[str, list[str]] = {}
    for name in sorted(os.listdir(ROUTER_DIR)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(ROUTER_DIR, name), encoding="utf-8").read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if not any(".get(" in ast.unparse(d) for d in fn.decorator_list):
                continue
            args = {a.arg for a in fn.args.args}
            if not ({"patient_id", "ehc_no"} & args):
                continue
            found.setdefault(name, []).append(fn.name)
    return found


# 未纳入可见性判定的患者维度接口，逐条写明理由。
# 第一批做完时这里有 20 条欠账，第二批清完只剩下面这一条。
# 保留这份清单的意义不在于它现在多短，而在于**缺口必须是显式的**：
# 第八轮那条只覆盖 11% 的扫描之所以骗过人，正是因为没覆盖到的部分不出现在任何地方。
UNGUARDED = {
    # 出站对接接口：调用方是区域平台一侧的对接账号，按设计要能导出全县任意患者，
    # 天然没有"业务关系"。改为只留痕（basis=export）。真正对症的是给对接账号
    # 单独一类身份并声明导出范围，平台暂无此类账号——登记为遗留项。
    "integration.py:export_fhir_patient",
    # 身份检索，非诊疗数据：返回的是脱敏标识（证件号打码），且必须已经持有
    # 电子健康卡号才查得到，不可遍历。挂号建档要先找得到人，加关系判定会让
    # 初诊办不了——真正要守的是诊疗明细，那些已经守住了。
    "patients.py:get_patient",
}
# 居民端：另一套鉴权（scope=portal，只能看自己），不适用机构可见性。
PORTAL = {
    "portal.py:my_archive", "portal.py:my_archive_token", "portal.py:portal_my_contract",
    "portal.py:portal_my_bills", "portal.py:portal_my_referrals",
    "portal.py:portal_my_admissions", "portal.py:portal_my_surgeries",
}


def test_横向越权覆盖率矩阵():
    """报出覆盖率，并盯住两件事：欠账不许变长，清单不许腐烂。"""
    endpoints = {f"{f}:{fn}" for f, fns in _patient_scoped_endpoints().items() for fn in fns}
    business = endpoints - PORTAL
    guarded = business - UNGUARDED
    pct = len(guarded) * 100 / len(business)
    print(
        f"\n横向越权矩阵：患者维度业务接口 {len(business)} 个 = 已纳入可见性 {len(guarded)} "
        f"+ 待纳入 {len(business & UNGUARDED)}；覆盖率 {pct:.1f}%"
    )

    stale = (UNGUARDED | PORTAL) - endpoints
    assert stale == set(), f"清单里这些接口已不存在，应删除：{sorted(stale)}"

    # 欠账只许变少。改小这个数字要连同实现一起改——它是这一轮唯一防止
    # "看起来做完了"的机制。
    assert len(business & UNGUARDED) <= 2, (
        f"未纳入可见性判定的患者维度接口变多了：{sorted(business & UNGUARDED)}"
    )


def test_已纳入的接口确实会拒绝无关机构(client, world, stranger):
    """光看清单不算数——逐个真发一次请求。

    第八轮的教训：一条规则说自己覆盖了什么，和它实际拦得住什么，是两回事。
    """
    pid = world["patient"]["id"]
    ehc = world["patient"]["ehc_no"]
    cases = [
        ("archive_360", f"/api/archive/{ehc}"),
        ("encounters", f"/api/encounters?patient_id={pid}"),
        ("vaccination", f"/api/vaccination/records?patient_id={pid}"),
        ("contraindications", f"/api/vaccination/contraindications?patient_id={pid}"),
        ("pre_check", f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=V1"),
        ("medication", f"/api/medication/profile/{pid}"),
        ("publichealth", f"/api/publichealth/reminders/{pid}"),
        ("treatments", f"/api/outpatient/treatments?patient_id={pid}"),
        # 第二批
        ("appointments", f"/api/appointments?patient_id={pid}"),
        ("bill_details", f"/api/billing/details?patient_id={pid}"),
        ("settlements", f"/api/billing/settlements?patient_id={pid}"),
        ("certs", f"/api/certs?patient_id={pid}"),
        ("checkups", f"/api/checkups?patient_id={pid}"),
        ("contracts", f"/api/contracts?patient_id={pid}"),
        ("credentials", f"/api/credentials?patient_id={pid}"),
        ("enrollments", f"/api/disease-programs/enrollments?patient_id={pid}"),
        ("eldercare", f"/api/eldercare/assessments?patient_id={pid}"),
        ("followups", f"/api/followups?patient_id={pid}"),
        ("admissions", f"/api/inpatient/admissions?patient_id={pid}"),
        ("insurance", f"/api/insurance/settlements?patient_id={pid}"),
        ("women_health", f"/api/maternal/women-health?patient_id={pid}"),
        ("consents", f"/api/outpatient/consents?patient_id={pid}"),
        ("aefi", f"/api/vaccine-supply/aefi?patient_id={pid}"),
        ("consumables", f"/api/materials/consumables?patient_id={pid}"),
        ("home_visits", f"/api/homevisits?patient_id={pid}"),
        ("unified", f"/api/service-requests?patient_id={pid}"),
    ]
    leaked = []
    for label, url in cases:
        code = client.get(url, headers=stranger["headers"]).status_code
        if code == 404:  # 路由对不上则用例本身失效，直接报出来
            leaked.append(f"{label} 路由 404（用例写错了）：{url}")
        elif code != 403:
            leaked.append(f"{label} 未拦住无关机构，返回 {code}")
    assert leaked == [], "以下患者维度接口对无关机构仍然开放：\n  " + "\n  ".join(leaked)
