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
import warnings

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import AccessLog

ROUTER_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "routers")
# 慢专病子系统的路由在 app/spd/routers/，一并扫——换个目录就绕过检查是这类
# 规则最典型的失效方式。
SPD_ROUTER_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "spd", "routers")


def _router_files():
    """全部路由文件的 (显示名, 绝对路径)；子系统文件带 spd/ 前缀与子目录便于定位。

    **必须递归**（os.walk 而不是 os.listdir）：`app/spd/routers/config/` 是一个子包，
    一层扫会漏掉整包 6 个文件 58 条路由——横向越权规则从此看不见它们，而闸门照样报绿。
    同一个盲区 `test_stage14_concurrency` 已经修过（并补了"必须递归"的自证用例），
    这里是同一处失效的另一半：**规则没被删，只是不再看新目录了**。
    """
    files = []
    for directory, label in ((ROUTER_DIR, ""), (SPD_ROUTER_DIR, "spd/")):
        root_dir = os.path.abspath(directory)
        for root, dirs, names in os.walk(root_dir):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            rel = os.path.relpath(root, root_dir)
            prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for name in sorted(names):
                if name.endswith(".py"):
                    files.append((f"{label}{prefix}{name}", os.path.join(root, name)))
    return sorted(files)


def test_路由扫描必须递归到子包():
    """防的是"路由拆进子包 → 扫描静默缩水"这一种失效（与 stage14 同一条自证）。"""
    scanned = {name for name, _ in _router_files()}
    assert any(name.startswith("spd/config/") for name in scanned), (
        "没扫到 app/spd/routers/config/ 子包——_router_files() 又退回成不递归了"
    )


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


def test_运营清单不跨机构泄露(client, world, stranger_op):
    """聚合/运营清单也会漏：CSSD 请求、采购单、缺药预警、用血申请这些接口
    原先不收 org_id、不过滤，任意账号一拉就是全县的。实测 CSSD 请求当场漏了
    甲院一条。这些不是统计聚合数，是**一条条带机构的运营明细**，按可见机构过滤。"""
    a = world["a"]["id"]
    adm = world["admin"]
    client.post("/api/cssd/requests", json={"org_id": a, "item_name": "甲器械包", "quantity": 5},
                headers=adm)
    sup = client.post("/api/pharmacy/suppliers", json={"org_id": a, "name": "甲供应商"},
                      headers=adm).json()
    client.post("/api/pharmacy/purchase-orders",
                json={"org_id": a, "supplier_id": sup["id"], "item_type": "drug",
                      "item_code": "DZ", "item_name": "甲药", "quantity": 100, "unit_price": 1.0},
                headers=adm)

    leaked = []
    for label, url in [
        ("CSSD请求", "/api/cssd/requests"),
        ("采购单", "/api/pharmacy/purchase-orders"),
        ("缺药预警", "/api/pharmacy/alerts"),
        ("用血申请", "/api/blood/requests"),
    ]:
        r = client.get(url, headers=stranger_op)
        rows = r.json() if isinstance(r.json(), list) else []
        seen = [x for x in rows if x.get("org_id") == a]
        if seen:
            leaked.append(f"{label} 漏了甲院 {len(seen)} 条")
    assert leaked == [], "以下运营清单跨机构泄露：\n  " + "\n  ".join(leaked)


# ================================================================ 写侧




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


def test_不得按id操作别家机构的记录(client, world, stranger, stranger_op):
    """写侧最隐蔽的一条：清单接口做了机构过滤，但 `/{id}` 型接口从 id 直取
    对象、跳过清单，过滤形同虚设。实测乙院经办据此领走了甲院 5 台 CT、
    交接了甲院医废、停用了甲院暂存间。"""
    a = world["a"]["id"]
    adm = world["admin"]
    asset = client.post(
        "/api/mgmt/assets",
        json={"org_id": a, "code": "BYID-CT", "name": "甲院CT", "category": "equipment",
              "quantity": 5},
        headers=adm,
    ).json()
    client.post("/api/medwaste/locations",
                json={"org_id": a, "name": "甲暂存", "location_type": "storage"}, headers=adm)
    waste = client.post(
        "/api/medwaste",
        json={"org_id": a, "waste_type": "infectious", "weight_kg": 2.0,
              "collected_date": "2026-08-13"},
        headers=adm,
    ).json()
    loc_id = client.get("/api/medwaste/locations", params={"org_id": a}, headers=adm).json()[0]["id"]

    cases = [
        ("领资产", "post", f"/api/mgmt/assets/{asset['id']}/movements",
         {"movement_type": "issue", "quantity": 5}),
        ("交接医废", "post", f"/api/medwaste/{waste['id']}/handover", {"handler_name": "冒名"}),
        ("停用点位", "delete", f"/api/medwaste/locations/{loc_id}", None),
    ]
    leaked = []
    for label, method, url, body in cases:
        r = getattr(client, method)(url, headers=stranger_op, **({"json": body} if body else {}))
        if r.status_code == 404:
            leaked.append(f"{label} 路由 404（用例写错）：{url}")
        elif r.status_code != 403 or "机构名义" not in r.json().get("detail", ""):
            leaked.append(f"{label} 未被机构守卫拦住：{r.status_code} {r.text[:50]}")
    assert leaked == [], "以下按 id 写接口可操作别家机构记录：\n  " + "\n  ".join(leaked)


def test_集中审方与远程会诊按设计跨机构不被机构守卫拦(client, world, stranger_op):
    """反向断言：不是所有跨机构写都该拦。集中审方（lead 药师审 member 处方）、
    远程会诊（lead 专家答 member 咨询）是医共体的核心协同，加机构写守卫会把
    功能关掉。这条用例盯住"别把它们误加回去"——真加回去，这里会红。

    这里只验证不因**机构守卫**而 403；具体审方结论另有用例覆盖。"""
    # stranger_op 是经办，审方要药师角色——这里只要确认不是 403(机构名义) 即可，
    # 405/403(角色)/404 都说明没被机构守卫拦。用一个不存在的处方号即可探针。
    r = client.post("/api/prescriptions/999999/review",
                    json={"approved": True, "comment": "x"}, headers=stranger_op)
    assert not (r.status_code == 403 and "机构名义" in r.text), \
        "集中审方被机构守卫拦住了——那会关掉 lead 审 member 处方的功能"


def test_以本机构名义写入照常(client, world, stranger, stranger_op):
    """守卫不能把正常录入挡掉。丙村卫生室的经办给自己建职工必须成功。"""
    r = client.post(
        "/api/mgmt/employees",
        json={"org_id": stranger["org"]["id"], "name": "丙村医", "title": "乡村医生"},
        headers=stranger_op,
    )
    assert r.status_code == 201, f"本机构写入被误伤：{r.text[:120]}"


# ================================================================ 覆盖率矩阵


# 按 id 写、却按业务设计就要跨机构的接口——**豁免，写明理由**。
# 集中审方与远程会诊是医共体最核心的两个协同：lead 药师审 member 处方、
# lead 专家答 member 咨询。给它们加机构写守卫会把功能关掉。
BYID_CROSS_ORG_OK = {
    "prescriptions.py:review_prescription",
    "prescriptions.py:comment_prescription",
    "telemedicine.py:reply",
    "telemedicine.py:close",
    # 慢专病逐级转诊（ADR-0005 三级链）：村医发起→卫生院审核→县级接收→下转承接，
    # 每一格都由**下一家机构**推进，加本机构写守卫等于把逐级链路关掉。
    # 单据可见性仍按"发起方/当前处理方/目标方任一在可见范围内"过滤。
    "spd/referral.py:review_referral",
    "spd/referral.py:arrive_referral",
    "spd/referral.py:down_referral",
    # 站内消息标记已读：归属**按人**不按机构，且已经是更严的口径——
    # handler 里 `notification.user_id != user.id` 直接按 404 处理（连存在性都
    # 不暴露）。分母扩到隔一跳后它被 `resident_account_id→ResidentAccount`
    # 这条边捞了进来，但机构守卫在这里是错的尺子：同机构的同事也不该替人已读。
    "notifications.py:mark_read",
}


def _owning_models():
    """归属模型两族：①自带 org_id/patient_id；②隔一跳外键指到①。

    **为什么要有②**：上线前审计实测到三个洞（停医嘱 / 修订报告 / 支付退款），
    这个闸门一个都没报出来——`InpatientOrder` 的归属在 `admissions.org_id`、
    `ExamReport` 的在 `exam_requests.patient_id`、`PaymentOrder` 的在
    `settlements.org_id`，**归属隔一跳的对象，只认"自己带 org_id"的判据结构上
    看不见**。那份自证报的 95.5% 是真的，只是分母漏了一整族。

    **为什么排除指向 `users` 的外键**：`created_by`/`requested_by`/`sampler_id`
    这类列指向 `users`，而 `users` 自己带 org_id——顺着它走会把"谁创建的"
    误当成"归属谁"，于是知识库条目、名老中医医案、课件都被判成需要机构守卫，
    推出"只能改本机构人写的知识条目"这种不存在的规则。实测这一条排除把误报从
    23 个压到 12 个，且剩下的每一个都有真实归属路径。审计列不是归属列。
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app import models
    classes = {
        c.__name__: c for c in models.Base.registry._class_registry.values()
        if hasattr(c, "__tablename__")
    }
    table_to_class = {c.__table__.name: n for n, c in classes.items()}

    def owns(c):
        return "org_id" in c.__table__.columns or "patient_id" in c.__table__.columns

    # ①自带 org_id：判据原本就有的那一族，**范围不变**。
    #   刻意不把"自带 patient_id"也并进来：那是另一次扩大（实测再多约 30 个端点，
    #   分布在 maternal / insurance / vaccination / tcm / emergency / spd/care 等），
    #   与"隔一跳"是两件事，混在一批里就分不清哪个洞是被哪次扩大照出来的。
    #   已登记在 ROADMAP，另案处理。
    direct = {n for n, c in classes.items() if "org_id" in c.__table__.columns}
    # ②隔一跳：外键指向"自带 org_id 或 patient_id"的表。跳的**目标**放宽到
    #   patient_id 是必须的——`ExamReport` 的归属正是 `exam_requests.patient_id`。
    owning = {n for n, c in classes.items() if owns(c)}
    onehop = set()
    for name, cls in classes.items():
        if name in owning:
            continue
        for col in cls.__table__.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name == "users":
                    continue  # 审计列，不是归属列
                target = table_to_class.get(fk.column.table.name)
                if target in owning:
                    onehop.add(name)
    return direct | onehop


def _byid_org_write_endpoints():
    """按 id 直取**有归属**主对象的写接口（归属可以隔一跳，见 `_owning_models`）。"""
    direct = _owning_models()
    guards = {"assert_obj_org_writable", "assert_org_writable", "assert_org_visible",
              "assert_patient_visible", "scope_org_list", "scope_patient_list",
              "log_patient_access"}
    unguarded = set()
    for name, path in _router_files():
        # 居民端两个文件走的是 portal 令牌 + accessible_patient（"这次能看谁的档案"），
        # 不在员工机构可见性体系内，与 portal.py 同一理由豁免。
        if name in ("portal.py", "spd/portal.py"):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        # 本模块内**自己带守卫**的辅助函数：判据按字符串认守卫，把校验抽进
        # helper（同一判断被三四个端点共用时该抽）会让端点函数体里只剩
        # `_assert_xxx(...)`，于是明明守住了却被报成没守——这是判据的假阳性，
        # 不是真欠账。故先扫一遍模块级函数，把"体内含守卫调用"的名字收进来，
        # 端点调用了它们同样算守住。只跟**一层**：再深就该怀疑守卫藏得太远了。
        helpers = {
            n.name for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and n.name.startswith("_")
            and any(g in ast.unparse(n) for g in guards)
        }
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            decs = [ast.unparse(d) for d in fn.decorator_list]
            if not any(m in d for d in decs for m in (".post(", ".put(", ".patch(", ".delete(")):
                continue
            if not any("{" in d for d in decs):
                continue
            u = ast.unparse(fn)
            if any(f"{h}(" in u for h in helpers):
                continue
            if any(g in u for g in guards):
                continue
            if any(f"db.get({m}," in u for m in direct):
                unguarded.add(f"{name}:{fn.name}")
    return unguarded


def test_按id写接口机构归属欠账不许变长():
    """第八轮那条 11% 的扫描教会的：缺口必须显式、可量化、只减不增。

    实测过的洞：乙院经办按 id 领走甲院 5 台 CT。补上机构写守卫后，
    只剩 4 个按业务设计跨机构的接口（集中审方 2 + 远程会诊 2），逐条写明理由。

    **自证覆盖面**：扫了多少文件一并打印出来。这条规则曾经在 `app/spd/routers/config/`
    拆成子包之后静默缩水（`os.listdir` 一层扫看不见子包），而它照样报绿——
    一个不声张自己覆盖范围的绿灯，和假装看过全部的哨兵一样危险。
    """
    scanned = _router_files()
    summary = (
        f"\n[横向越权闸门] 覆盖面自证\n"
        f"  扫描文件：{len(scanned)} 个"
        f"（app/routers + app/spd/routers，**递归**，其中子包文件 "
        f"{sum(1 for n, _ in scanned if '/' in n.replace('spd/', '', 1))} 个）"
    )
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    unguarded = _byid_org_write_endpoints()
    unexpected = unguarded - BYID_CROSS_ORG_OK
    assert unexpected == set(), (
        "以下按 id 写接口能操作别家机构记录，且不属于已声明的跨机构协同：\n  "
        + "\n  ".join(sorted(unexpected))
    )
    stale = BYID_CROSS_ORG_OK - unguarded
    assert stale == set(), f"这些豁免接口已加了守卫或不存在，应从清单删除：{sorted(stale)}"


def test_分母确实覆盖归属隔一跳的对象():
    """防空转：判据一旦退回"只认自己带 org_id"，这条必须转红。

    钉三个**实测出过洞**的模型——它们的归属都隔一跳，正是既有闸门看不见、
    要靠人工审计才翻出来的那一族。拿它们当哨兵而不是断言个数：
    个数会随模型增减漂，而"这三个必须在分母里"是判据的语义本身。
    """
    owning = _owning_models()
    for model in ("InpatientOrder", "ExamReport", "PaymentOrder"):
        assert model in owning, (
            f"{model} 的归属隔一跳（分别在 admissions / exam_requests / settlements 上），"
            "它掉出分母意味着判据退回了只认自带 org_id 的老形状——"
            "上线前审计正是靠人工才翻出这三个洞的。"
        )


def test_审计列不得被当成归属列():
    """防空转：`created_by` 这类指向 `users` 的外键必须**不算**归属路径。

    去掉那条排除会怎样：`users` 自己带 org_id，于是任何有 `created_by` 的表
    都被判成"归属隔一跳可达"，知识库条目、名老中医医案、课件、模拟病例全部
    涌进分母（实测误报 12 → 23），并推出"只能改本机构人写的知识条目"这种
    不存在的业务规则。**谁创建的 ≠ 归属谁**。

    拿 `KnowledgeEntry` 当哨兵：它除了 `created_by` 再没有别的外键，
    是这条排除的纯样本——排除一撤销，它立刻进分母。
    """
    owning = _owning_models()
    assert "KnowledgeEntry" not in owning, (
        "KnowledgeEntry 只有 created_by 一条外键（指向 users）。它进了分母，"
        "说明判据把审计列当成了归属列——统一知识库是全域配置，没有机构归属。"
    )


def _patient_scoped_endpoints() -> dict[str, list[str]]:
    """按 patient_id / ehc_no 取数的 GET 接口。"""
    found: dict[str, list[str]] = {}
    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if not any(".get(" in ast.unparse(d) for d in fn.decorator_list):
                continue
            args = {a.arg for a in fn.args.args}
            if not ({"patient_id", "ehc_no"} & args):
                continue
            found.setdefault(name, []).append(fn.name)
    return found


def _patient_byid_read_endpoints() -> tuple[set[str], set[str]]:
    """按 id 直取「挂在患者身上的资源」的 GET 接口，返回 (全部, 未防护)。

    P1-4 的分母修正：矩阵原来只算"入参含 patient_id/ehc_no"，而
    /chronic/{id}/followups、/billing/deposits?admission_id=、/emergency/cases/{id}/…
    这类接口返回的是同一批患者数据，只因入参换了个名字就不进分母——覆盖率
    因此虚高为 100%。这里按 `_byid_org_write_endpoints` 同一套机制补上这一族：
    凡 GET 处理函数里 `db.get(带 patient_id 列的模型, ...)` 的，都算
    "响应返回患者数据的端点"。打印/附件家族已在其中（printing.py 的四个打印
    端点、attachments 的挂接对象解析都走 db.get(带患者模型)，分别由
    assert_patient_visible / assert_owner_visible 判防护）。
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app import models
    linked = {
        c.__name__ for c in models.Base.registry._class_registry.values()
        if hasattr(c, "__tablename__") and "patient_id" in c.__table__.columns
    }
    guards = ("assert_patient_visible", "assert_owner_visible", "accessible_patient")
    all_eps: set[str] = set()
    unguarded: set[str] = set()
    for name, path in _router_files():
        # 居民端另一套鉴权（portal 令牌 + accessible_patient），与 PORTAL 同理豁免
        if name in ("portal.py", "spd/portal.py"):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if not any(".get(" in ast.unparse(d) for d in fn.decorator_list):
                continue
            if {"patient_id", "ehc_no"} & {a.arg for a in fn.args.args}:
                continue  # 已计入 _patient_scoped_endpoints，别重复算
            u = ast.unparse(fn)
            if not any(f"db.get({m}," in u for m in linked):
                continue
            key = f"{name}:{fn.name}"
            all_eps.add(key)
            if not any(g in u for g in guards):
                unguarded.add(key)
    return all_eps, unguarded


# 按 id 读患者资源、但按业务设计要跨机构开放的接口——豁免，写明理由
# （与 BYID_CROSS_ORG_OK 同一纪律：每一条都要答得出"为什么不守"）。
BYID_PATIENT_READ_OK = {
    # 集中审方是医共体核心协同：lead 药师审 member 处方，与患者天然无就诊/
    # 签约/转诊关系，加患者可见性判定等于把审方关掉（与 review/comment 的
    # 写侧豁免同理）。接口只回处方内药品的规则点评要点与剂量提示，
    # 患者身份信息不在响应里。
    "prescriptions.py:prescription_review_points",
    # 法定死因上报导出：与 UNGUARDED 里 integration.py:export_fhir_patient 同一
    # 口径——上报/对接类导出天然没有"业务关系"，机构可见性判定会把法定职责
    # 关掉。已收紧到 director 角色，且每次导出都 log_patient_access 留痕
    # （resource=death_report_card, basis=export），身份证号/电话按角色脱敏。
    "certs.py:death_report_card",
}


def test_按id读患者资源接口可见性欠账不许变长():
    """by-id 读侧与写侧同一纪律：缺口显式、可量化、只减不增。

    修正 P1-4 时实测过的洞：无关机构凭 chronic_id/admission_id/case_id 顺序
    遍历，就能拉到慢病随访史、住院押金流水、急救时间轴——都是患者数据，
    只是入参不叫 patient_id。
    """
    _all_eps, unguarded = _patient_byid_read_endpoints()
    unexpected = unguarded - BYID_PATIENT_READ_OK
    assert unexpected == set(), (
        "以下按 id 读患者资源的接口未纳入可见性判定，且不属于已声明的跨机构协同：\n  "
        + "\n  ".join(sorted(unexpected))
    )
    stale = BYID_PATIENT_READ_OK - unguarded
    assert stale == set(), f"这些豁免接口已加了守卫或不存在，应从清单删除：{sorted(stale)}"


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
    "portal.py:portal_my_consents",
}


def test_横向越权覆盖率矩阵():
    """报出覆盖率，并盯住两件事：欠账不许变长，清单不许腐烂。

    分母口径（P1-4 修正）：从"入参含 patient_id/ehc_no"扩到"响应返回患者
    数据的端点"——把按 id 直取患者资源的一族（/chronic/{id}/*、押金、急救、
    签约服务、文书完整性、打印、附件挂接解析等）也计入。扩前分母 65、
    覆盖率虚高；扩后欠账与豁免都显式列出。
    """
    param_eps = {f"{f}:{fn}" for f, fns in _patient_scoped_endpoints().items() for fn in fns}
    byid_all, byid_unguarded = _patient_byid_read_endpoints()
    endpoints = param_eps | byid_all
    business = endpoints - PORTAL
    debt = business & UNGUARDED
    exempt = business & (BYID_PATIENT_READ_OK & byid_unguarded)
    guarded = business - debt - exempt
    pct = len(guarded) * 100 / len(business)
    print(
        f"\n横向越权矩阵：患者数据端点 {len(business)} 个"
        f"（入参含患者标识 {len(param_eps - PORTAL)} + 按id直取患者资源 {len(byid_all)}）"
        f" = 已纳入可见性 {len(guarded)} + 待纳入 {len(debt)}"
        f" + 跨机构协同豁免 {len(exempt)}；覆盖率 {pct:.1f}%"
    )

    stale = (UNGUARDED | PORTAL) - endpoints
    assert stale == set(), f"清单里这些接口已不存在，应删除：{sorted(stale)}"

    # 欠账只许变少。改小这个数字要连同实现一起改——它是这一轮唯一防止
    # "看起来做完了"的机制。
    assert len(debt) <= 2, (
        f"未纳入可见性判定的患者维度接口变多了：{sorted(debt)}"
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


def test_按id直取的患者资源同样拒绝无关机构(client, world, stranger):
    """P1-4 修正的实弹验证：光把 by-id 一族计入分母不算数，逐个真发一次请求。

    这些接口的入参是 chronic_id/contract_id/case_id/encounter_id/admission_id，
    不叫 patient_id——正是原分母漏掉的形状。"""
    pid = world["patient"]["id"]
    a = world["a"]["id"]
    adm = world["admin"]
    doc_a = world["doc_a"]

    chronic = client.post(
        "/api/chronic",
        json={"patient_id": pid, "disease": "hypertension", "managed_by_org_id": a},
        headers=doc_a,
    ).json()
    contract = client.post(
        "/api/contracts",
        json={"patient_id": pid, "org_id": a, "doctor_name": "甲医生", "signed_date": "2026-08-01"},
        headers=doc_a,
    ).json()
    case = client.post(
        "/api/emergency/cases",
        json={"location": "甲县人民路", "patient_id": pid, "dest_org_id": a},
        headers=doc_a,
    ).json()
    encounter_id = client.get(
        f"/api/encounters?patient_id={pid}", headers=doc_a
    ).json()[0]["id"]
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": a, "name": "横向病区"}, headers=adm
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "H-01"}, headers=adm
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": pid, "ward_id": ward["id"], "bed_id": bed["id"]},
        headers=doc_a,
    ).json()

    cases = [
        ("慢病随访史", f"/api/chronic/{chronic['id']}/followups"),
        ("慢病风险评分", f"/api/chronic/{chronic['id']}/risk"),
        ("签约服务记录", f"/api/contracts/{contract['id']}/services"),
        ("急救时间轴", f"/api/emergency/cases/{case['id']}/timeline"),
        ("急救生命体征", f"/api/emergency/cases/{case['id']}/vitals"),
        ("文书完整性", f"/api/outpatient/encounters/{encounter_id}/completeness"),
        ("押金流水", f"/api/billing/deposits?admission_id={admission['id']}"),
        ("押金余额", f"/api/billing/deposits/balance?admission_id={admission['id']}"),
    ]
    leaked = []
    for label, url in cases:
        code = client.get(url, headers=stranger["headers"]).status_code
        if code == 404:
            leaked.append(f"{label} 路由 404（用例写错了）：{url}")
        elif code != 403:
            leaked.append(f"{label} 未拦住无关机构，返回 {code}")
        # 隔离不能误伤本机构的正常诊疗
        mine = client.get(url, headers=doc_a).status_code
        if mine != 200:
            leaked.append(f"{label} 误伤本机构：{mine}")
    assert leaked == [], "按 id 直取的患者资源仍有缺口：\n  " + "\n  ".join(leaked)


# ================================================================ 管理聚合角色口径（第十轮 P2）


def test_管理聚合统计限管理层(client, world, stranger_op):
    """DRG/基金/手术统计是给管理者的账（各机构结余、例数、均费），第十轮定为
    限 director/admin。乡镇经办、普通医生看不到全县各机构的财务/绩效汇总。"""
    for label, url in [
        ("DRG统计", "/api/drgs/stats"),
        ("基金统计", "/api/insurance/fund-stats"),
        ("手术统计", "/api/surgery/stats"),
    ]:
        # 经办被拦
        assert client.get(url, headers=stranger_op).status_code == 403, f"{label} 未对经办收紧"
        # 管理层照常
        assert client.get(url, headers=world["admin"]).status_code == 200, f"{label} 误伤管理层"


def test_县域监测预警对一线保持开放(client, world, stranger_op):
    """反向断言：不是所有跨机构聚合都收紧。多点触发预警是给一线的暴发信号，
    一线经办/医生要看得到辖区有没有暴发——收紧它等于把这个公卫功能关掉。
    这条盯住"别把监测误当管理聚合一起收紧了"。"""
    for url in ["/api/infectious/alerts", "/api/surveillance/alerts"]:
        code = client.get(url, headers=stranger_op).status_code
        assert code != 403, f"县域监测 {url} 被误当管理聚合收紧了：{code}"
