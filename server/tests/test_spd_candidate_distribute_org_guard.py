"""ADR-0019 的回归：目标池分发必须校验机构归属（来源与去向各一次）。

**这个洞是实测出来的，不是推演的。** 修之前跑同样的三步：建甲、乙两院与一个挂甲院的
`doctor`，往乙院落一条目标池记录，用甲院 doctor 调 `POST /api/spd/candidates/distribute`——

    乙院目标池记录 id = 1  org_id = 2
    甲院 doctor 分发乙院记录 -> 200 {"distributed":1,"not_found":0}
    改后 org_id = 1  status = target  assigned_user_id = 1

一次请求把别家的目标人群改挂到自己机构、改了状态、指派给了自己，返回 200。
危害不止"看见别家"：目标池是慢专病纳管的入口，被改挂之后签约建档、任务派发、
考核计数都会跟着挪到错误的机构。`candidate_ids` 上限 500，可批量。

同文件的 `claim_candidate` 一直是对的（它调 `assert_org_writable`）——所以这不是
"没想到要校验"，是**同一个文件里两套写法只有一套对**。

**为什么既有的横向越权棘轮没抓到它**（`test_stage15_horizontal.py`）：那条规则的判据
比缺陷窄了两层——①只看**路径带 `{}`** 的端点，而这里的 id 是从 **body** 收的；
②只认 `db.get(Model, ...)`，而这里用的是 `db.query(...).filter(...id.in_(...))`。
判据比缺陷窄，等于给缺陷留了个看不见的后门（ADR-0009 第二批记过同一条教训）。
按形状扫的结果与后续处置登记在 TECH_DEBT P1-43。
"""
from app.database import SessionLocal
from app.models import SpdCandidate
from conftest import login

B = "/api/spd"


def _org(client, admin, name, level="township"):
    org_type = "lead_hospital" if level == "county" else "township"
    return client.post("/api/organizations", headers=admin,
                       json={"name": name, "org_type": org_type, "level": level}).json()


def _candidate(patient_id: int, org_id: int, code: str) -> int:
    """直接落一条目标池记录——走筛查接口要先配病种规则，与本用例要证的事无关。"""
    with SessionLocal() as db:
        c = SpdCandidate(patient_id=patient_id, program_code=code, status="suspect",
                         source="screening", org_id=org_id, risk_level="mid",
                         matched_rules=[], reason="用例夹具")
        db.add(c)
        db.commit()
        return c.id


def test_跨机构分发目标池必须403且一个字段都不许变(client, admin):
    org_a = _org(client, admin, "分发甲院", "county")
    org_b = _org(client, admin, "分发乙院")
    client.post("/api/users", headers=admin,
                json={"username": "dist_doc_a", "password": "pass123456",
                      "role": "doctor", "org_id": org_a["id"]})
    doc_a = login(client, "dist_doc_a", "pass123456")
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "分发用例患者", "id_card": "330424199006061234",
                                "phone": "13700110091"}).json()
    cid = _candidate(patient["id"], org_b["id"], "dist_hyp")

    resp = client.post(f"{B}/candidates/distribute", headers=doc_a,
                       json={"candidate_ids": [cid], "org_id": org_a["id"],
                             "assigned_user_id": 1})
    assert resp.status_code == 403, resp.text

    # 403 之后**记录必须原封不动**——半成功比直接失败更难查
    with SessionLocal() as db:
        after = db.get(SpdCandidate, cid)
        assert after.org_id == org_b["id"]
        assert after.status == "suspect"
        assert after.assigned_user_id is None


def test_一批里混进一条别家的记录就整批拒绝(client, admin):
    """越权不做"跳过那几条"处理：批量半成功会让调用方以为整批都办了。"""
    org_a = _org(client, admin, "混批甲院", "county")
    org_b = _org(client, admin, "混批乙院")
    client.post("/api/users", headers=admin,
                json={"username": "mix_doc_a", "password": "pass123456",
                      "role": "doctor", "org_id": org_a["id"]})
    doc_a = login(client, "mix_doc_a", "pass123456")
    p1 = client.post("/api/patients", headers=admin,
                     json={"name": "混批甲患", "id_card": "330424199007071234",
                           "phone": "13700110092"}).json()
    p2 = client.post("/api/patients", headers=admin,
                     json={"name": "混批乙患", "id_card": "330424199008081234",
                           "phone": "13700110093"}).json()
    mine = _candidate(p1["id"], org_a["id"], "mix_hyp")
    theirs = _candidate(p2["id"], org_b["id"], "mix_hyp2")

    resp = client.post(f"{B}/candidates/distribute", headers=doc_a,
                       json={"candidate_ids": [mine, theirs], "team_id": None,
                             "assigned_user_id": 1})
    assert resp.status_code == 403, resp.text
    with SessionLocal() as db:
        assert db.get(SpdCandidate, mine).assigned_user_id is None, "整批拒绝时本院那条也不许被改"


def test_本机构分发照常放行(client, admin):
    """守卫不能把正常业务一起挡了——同院分发仍然 200。"""
    org_a = _org(client, admin, "同院甲院", "county")
    client.post("/api/users", headers=admin,
                json={"username": "same_doc_a", "password": "pass123456",
                      "role": "doctor", "org_id": org_a["id"]})
    doc_a = login(client, "same_doc_a", "pass123456")
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "同院患者", "id_card": "330424199009091234",
                                "phone": "13700110094"}).json()
    cid = _candidate(patient["id"], org_a["id"], "same_hyp")

    resp = client.post(f"{B}/candidates/distribute", headers=doc_a,
                       json={"candidate_ids": [cid], "assigned_user_id": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"distributed": 1, "not_found": 0}
    with SessionLocal() as db:
        assert db.get(SpdCandidate, cid).status == "target"


def test_全域角色跨机构分发仍然放行(client, admin):
    """`director`/`admin` 在 GLOBAL_ROLES 里，中心端跨机构分发是设计内的，不能被误伤。"""
    org_a = _org(client, admin, "全域甲院", "county")
    org_b = _org(client, admin, "全域乙院")
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "全域患者", "id_card": "330424199010101234",
                                "phone": "13700110095"}).json()
    cid = _candidate(patient["id"], org_b["id"], "global_hyp")

    resp = client.post(f"{B}/candidates/distribute", headers=admin,
                       json={"candidate_ids": [cid], "org_id": org_a["id"]})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        assert db.get(SpdCandidate, cid).org_id == org_a["id"]


def test_改挂进自己写不了的机构同样拒绝(client, admin):
    """去向也要校验：把**本院**的记录改挂到别家，一样是越权。"""
    org_a = _org(client, admin, "去向甲院", "county")
    org_b = _org(client, admin, "去向乙院")
    client.post("/api/users", headers=admin,
                json={"username": "dest_doc_a", "password": "pass123456",
                      "role": "doctor", "org_id": org_a["id"]})
    doc_a = login(client, "dest_doc_a", "pass123456")
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "去向患者", "id_card": "330424199011111234",
                                "phone": "13700110096"}).json()
    cid = _candidate(patient["id"], org_a["id"], "dest_hyp")

    resp = client.post(f"{B}/candidates/distribute", headers=doc_a,
                       json={"candidate_ids": [cid], "org_id": org_b["id"]})
    assert resp.status_code == 403, resp.text
    with SessionLocal() as db:
        assert db.get(SpdCandidate, cid).org_id == org_a["id"]
