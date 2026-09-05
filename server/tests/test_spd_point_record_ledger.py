"""spd 积分流水：一个业务事件只入账一次（P1-30 · spd_point_records）。

不变式不在本表的键上，而长在**父行的状态跃迁**上。`spd_point_records` 里带
`ref_id` 的入账行（`direction='in'`）是"某次业务事件给村医记了一笔分"的凭证：
同一个 `(rule_code, ref_type, ref_id)` 出现两行，意味着同一件事付了两次分——
余额与"累计获得"双双虚高，事后只能人工冲销，且从流水上看不出哪一笔是多的。

五个入账调用点里三个天然只入一次（任务办结走条件 UPDATE、签约撞 enrollment
的唯一约束、异常上报按新建 report 的主键）；剩下两个在转诊侧：
`arrive_referral` / `receive_followup` 先在 Python 里判 `case.status`、再无条件
改父行、再 `award_points`——两路并发都读到 `accepted`，就会各写一条"有效上转"。

**本档不改转诊路由**：`app/spd/routers/referral.py` 归同一工程包的
spd_referral_steps 组所有，父行条件推进（`_advance_case`）由它落地。本档负责把
不变式本身钉住，钉法在"修之前"与"修之后"都必须成立：

- 行为面：顺序重复到院 / 重复接收随访必须是同一句 409，且带 ref 的入账**恰一笔**；
  换一张转诊单则合法地再记一笔——不变式是"每个事件一笔"，不是"每人一笔"；
- 边界面：本组自有的两个直写点（签到 / 兑换，`app/spd/routers/assess.py`）落在键
  **外**——签到不带 `ref_id`、兑换是 `direction='out'`，同一账户多行都合法。
  这正是 spd_point_records 不加唯一索引的前提，写成用例才不会被后来的改动悄悄推翻；
- 防拆卸面：带 ref 的入账只能由 `service.award_points` 写出、调用点集合不许悄悄
  扩张（多一个不带父行守卫的入账点，就是把洞挖回来）；转诊侧的条件推进帮手一旦
  落地，最后一条静态钉自动生效。

真并发的证明在 `tests/test_spd_point_record_unique_races.py`（真 PG）：SQLite 的
库级写锁把判定与写入之间的窗口一并锁掉，线程探针对"拆掉守卫"不敏感。
"""
import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: 允许构造 `SpdPointRecord(...)` 的位置。多一个就得回答"它带 ref 吗、父行守住了吗"。
EXPECTED_RECORD_WRITERS = {
    ("app/spd/service.py", "award_points"),
    ("app/spd/routers/assess.py", "signin"),
    ("app/spd/routers/assess.py", "redeem"),
}
#: 允许调用 `award_points(...)` 的位置（全部带 ref_id，各自由父行守住只入一次）。
EXPECTED_AWARD_CALLERS = {
    ("app/spd/routers/population.py", "create_enrollment"),
    ("app/spd/routers/care.py", "create_case_report"),
    ("app/spd/routers/referral.py", "arrive_referral"),
    ("app/spd/routers/referral.py", "receive_followup"),
    ("app/spd/routers/tasks.py", "_finish_task"),
}


# ================================================================ 世界与工具


@pytest.fixture(scope="module")
def world(client, admin):
    """三级机构树 + 三名医生 + 患者 + 纳管档案（口径同 test_spd_flow 的转诊链用例）。"""
    from conftest import login

    def _org(name, level, org_type, parent_id=None):
        body = {"name": name, "org_type": org_type, "level": level}
        if parent_id is not None:
            body["parent_id"] = parent_id
        resp = client.post("/api/organizations", json=body, headers=admin)
        assert resp.status_code == 201, resp.text
        return resp.json()

    county = _org("积分县医院", "county", "lead_hospital")
    township = _org("积分卫生院", "township", "township", county["id"])
    village = _org("积分村卫生室", "village", "village", township["id"])

    def _doctor(username, org_id):
        resp = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": "doctor",
                  "org_id": org_id},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        return login(client, username, "pass123456"), resp.json()["id"]

    vdoc, vdoc_id = _doctor("point_vdoc", village["id"])   # 发起（计分对象）
    tdoc, _ = _doctor("point_tdoc", township["id"])        # 卫生院审核 / 下转承接
    cdoc, _ = _doctor("point_cdoc", county["id"])          # 县医院接收 / 到院登记

    patient = client.post(
        "/api/patients",
        json={"name": "积分流水患者", "id_card": "330182198606060091", "phone": "13900009091"},
        headers=admin,
    ).json()
    # 纳管由管理员代录（村医对尚无任何关系的患者不可见），但把 village_doctor_id
    # 明确指到 vdoc：计分对象因此是确定的，不依赖 award_points 回落到发起人。
    enroll = client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": village["id"], "risk_level": "high", "village_doctor_id": vdoc_id},
        headers=admin,
    )
    assert enroll.status_code == 201, enroll.text
    return {
        "county": county, "township": township, "village": village,
        "vdoc": vdoc, "vdoc_id": vdoc_id, "tdoc": tdoc, "cdoc": cdoc,
        "patient": patient,
    }


def _accepted_case(client, world, reason):
    """村医发起 → 卫生院审核 → 县医院接收，返回停在 accepted 的转诊单 id。"""
    case_id = client.post(
        "/api/spd/referrals",
        json={"patient_id": world["patient"]["id"], "program_code": "hypertension",
              "reason": reason},
        headers=world["vdoc"],
    ).json()["id"]
    for reviewer in (world["tdoc"], world["cdoc"]):
        resp = client.post(f"/api/spd/referrals/{case_id}/review",
                           json={"action": "pass"}, headers=reviewer)
        assert resp.status_code == 200, resp.text
    return case_id


def _ledger(**filters):
    """绕开接口层直接读流水：断言的是库里真实落了几行，不是响应里说了几行。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPointRecord

    db = SessionLocal()
    try:
        query = db.query(SpdPointRecord)
        for column, value in filters.items():
            query = query.filter(getattr(SpdPointRecord, column) == value)
        return query.order_by(SpdPointRecord.id).all()
    finally:
        db.close()


def _account_of(user_id):
    from app.database import SessionLocal
    from app.spd.models import SpdPointAccount

    db = SessionLocal()
    try:
        return db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user_id).first()
    finally:
        db.close()


# ================================================================ 行为面


def test_重复登记到院_同一句409且有效上转只入一笔(client, world):
    """到院是"有效上转"的计分点：重复登记必须被同一句 409 挡住，流水只留一笔。

    旧写法在并发下会两路都过前置校验、各记一笔 10 分；顺序重放看不到那一幕，
    但"重复请求只入一笔"这条不变式对两者是同一条，先把它钉住。
    """
    case_id = _accepted_case(client, world, "重复到院")
    first = client.post(f"/api/spd/referrals/{case_id}/arrive",
                        json={"effective_visit": True}, headers=world["cdoc"])
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "arrived"

    second = client.post(f"/api/spd/referrals/{case_id}/arrive",
                         json={"effective_visit": True}, headers=world["cdoc"])
    assert second.status_code == 409
    assert second.json() == {"detail": "只有已接收的转诊单可登记到院"}

    rows = _ledger(rule_code="pt_ref_up", ref_type="referral", ref_id=case_id, direction="in")
    assert len(rows) == 1, f"同一次有效上转记了 {len(rows)} 笔分——余额与累计获得双双虚高"
    assert rows[0].points == 10


def test_重复接收随访_同一句409且下转承接只入一笔(client, world):
    """闭环的最后一格同理：`receive-followup` 是"下转承接"的计分点。"""
    case_id = _accepted_case(client, world, "重复接收随访")
    assert client.post(f"/api/spd/referrals/{case_id}/arrive",
                       json={"effective_visit": True}, headers=world["cdoc"]).status_code == 200
    down = client.post(f"/api/spd/referrals/{case_id}/down",
                       json={"target_org_id": world["township"]["id"]}, headers=world["cdoc"])
    assert down.status_code == 200, down.text

    first = client.post(f"/api/spd/referrals/{case_id}/receive-followup",
                        json={}, headers=world["tdoc"])
    assert first.status_code == 200 and first.json()["status"] == "closed"
    second = client.post(f"/api/spd/referrals/{case_id}/receive-followup",
                         json={}, headers=world["tdoc"])
    assert second.status_code == 409
    assert second.json() == {"detail": "只有已下转的转诊单可接收随访"}

    rows = _ledger(rule_code="pt_ref_down", ref_type="referral", ref_id=case_id, direction="in")
    assert len(rows) == 1, f"同一次下转承接记了 {len(rows)} 笔分"
    assert rows[0].points == 8


def test_另一张转诊单合法地再记一笔(client, world):
    """不变式是"每个事件一笔"，不是"每人一笔"——别把合法的多行也拦掉。

    这条是"不许把守卫写成整表唯一"的反向约束：真给 spd_point_records 建个
    (rule_code, ref_type) 唯一索引，村医第二次有效上转就拿不到分了。
    """
    before = {r.ref_id for r in _ledger(rule_code="pt_ref_up", ref_type="referral", direction="in")}
    case_id = _accepted_case(client, world, "第二张单也该计分")
    assert case_id not in before
    resp = client.post(f"/api/spd/referrals/{case_id}/arrive",
                       json={"effective_visit": True}, headers=world["cdoc"])
    assert resp.status_code == 200, resp.text

    after = _ledger(rule_code="pt_ref_up", ref_type="referral", direction="in")
    assert {r.ref_id for r in after} == before | {case_id}
    per_event = [r for r in after if r.ref_id == case_id]
    assert len(per_event) == 1, "新单也只该记一笔"


def test_签到与兑换的流水落在事件键之外(client, world, admin):
    """本组自有的两个直写点：都不带 `ref_id`，同一账户多行合法。

    签到靠 `spd_signins (account_id, day)` 唯一 + IntegrityError 守住"一天一次"；
    兑换是 `direction='out'`、且每次兑换本就该留一行。它们落在
    "(rule_code, ref_type, ref_id) 每个事件一笔"这条键之外——这正是本表不建唯一
    索引的前提：建了会把这两处合法的多行一并拦掉。
    """
    signin = client.post("/api/spd/point-accounts/signin", headers=world["vdoc"])
    assert signin.status_code == 200, signin.text
    again = client.post("/api/spd/point-accounts/signin", headers=world["vdoc"])
    assert again.status_code == 409 and again.json() == {"detail": "今日已签到"}

    account = _account_of(world["vdoc_id"])
    assert account is not None
    signin_rows = _ledger(account_id=account.id, ref_type="signin")
    assert len(signin_rows) == 1
    assert signin_rows[0].ref_id is None, "签到流水不带 ref_id，不在事件键内"

    goods = client.post(
        "/api/spd/goods",
        json={"code": "PT-TOWEL", "name": "毛巾", "points": 5, "stock": 10},
        headers=admin,
    )
    assert goods.status_code == 201, goods.text
    goods_id = goods.json()["id"]
    for _ in range(2):
        resp = client.post("/api/spd/redeems", json={"goods_id": goods_id},
                           headers=world["vdoc"])
        assert resp.status_code == 201, resp.text

    redeem_rows = _ledger(account_id=account.id, ref_type="redeem")
    assert len(redeem_rows) == 2, "同一账户多次兑换本就合法，多行不是缺陷"
    assert {r.direction for r in redeem_rows} == {"out"}
    assert {r.ref_id for r in redeem_rows} == {None}


# ================================================================ 防拆卸静态钉


def _calls_in(path, name):
    """文件里对 `name` 的调用：[(最内层函数名, 关键字参数名集合)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        label = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
        if label != name:
            continue
        owner = max(
            (f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)),
            key=lambda f: f.lineno, default=None,
        )
        found.append((owner.name if owner else "<module>",
                      {kw.arg for kw in node.keywords if kw.arg}))
    return found


def _scan_app(name):
    sites = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR.parent).as_posix()
        for func, kwargs in _calls_in(path, name):
            sites.setdefault((rel, func), []).append(kwargs)
    return sites


def test_积分流水的写入点集合不许悄悄扩张():
    """带 ref 的入账只能由 `award_points` 写出，直写点只有签到与兑换两处。

    多一个直写点，就多一个"谁保证它只写一次"的问号；`award_points` 里没有也
    不该有按 ref 的去重（去重属于各调用点的父行状态机），所以入口收窄就是防线。
    """
    writers = set(_scan_app("SpdPointRecord"))
    assert writers == EXPECTED_RECORD_WRITERS, (
        f"SpdPointRecord 的构造点变了：多出 {sorted(writers - EXPECTED_RECORD_WRITERS)}，"
        f"少了 {sorted(EXPECTED_RECORD_WRITERS - writers)}。新增入账点必须先回答："
        "它带 ref_id 吗？带的话哪一条父行条件 UPDATE 保证它只写一次？"
    )


def test_两个直写点不带ref_id才落在事件键之外():
    """签到 / 兑换一旦带上 `ref_id`，就进了"每个事件一笔"的键，需要另立守卫。"""
    sites = _scan_app("SpdPointRecord")
    for key in (("app/spd/routers/assess.py", "signin"),
                ("app/spd/routers/assess.py", "redeem")):
        for kwargs in sites[key]:
            assert "ref_id" not in kwargs, (
                f"{key[1]} 的积分流水带上了 ref_id：它随即落进 "
                "(rule_code, ref_type, ref_id) 这条键，而这两处没有父行守卫"
            )


def test_带ref入账的调用点集合不许悄悄扩张():
    """五个调用点各有各的父行守卫，这份名单就是"谁在发分"的全集。

    * `create_enrollment`：撞 enrollment 的唯一约束，ref 是新建档案的主键；
    * `create_case_report`：ref 是刚插入的上报单主键，两个请求两个键；
    * `_finish_task`：`UPDATE spd_tasks … WHERE status NOT IN (…)` 命中才发分；
    * `arrive_referral` / `receive_followup`：靠父单 spd_referral_cases 的状态跃迁
      （条件 UPDATE 由 spd_referral_steps 组落地）。
    """
    sites = _scan_app("award_points")
    callers = set(sites)
    assert callers == EXPECTED_AWARD_CALLERS, (
        f"award_points 的调用点变了：多出 {sorted(callers - EXPECTED_AWARD_CALLERS)}，"
        f"少了 {sorted(EXPECTED_AWARD_CALLERS - callers)}。新增调用点要在 PR 里写明它靠"
        "哪条父行条件 UPDATE 保证「一个事件只发一次分」"
    )
    for key, calls in sites.items():
        for kwargs in calls:
            assert "ref_id" in kwargs, (
                f"{key} 的入账不带 ref_id：这笔分从流水上认不出对应哪次业务事件，"
                "对不上账也冲销不掉"
            )


def test_转诊侧的状态跃迁必须是条件UPDATE():
    """转诊路由归 spd_referral_steps 组，帮手一落地本钉自动生效（届时不再 skip）。

    钉的是"判定与写入同一条 SQL"：一旦回潮成 Python 侧 `case.status = "arrived"`，
    两路并发就又能各记一笔"有效上转"。
    """
    import inspect

    from app.spd.routers import referral as referral_mod

    helper = getattr(referral_mod, "_advance_case", None) or getattr(
        referral_mod, "_flip_status", None)
    if helper is None:
        pytest.skip("referral 的条件推进帮手尚未落地（spd_referral_steps 组），本钉合入后自动生效")
    src_helper = inspect.getsource(helper)
    assert "update(SpdReferralCase)" in src_helper and ".rowcount" in src_helper, (
        "父行推进丢了条件 UPDATE——两路并发又能各记一笔分"
    )
    for name in ("arrive_referral", "receive_followup"):
        src = inspect.getsource(getattr(referral_mod, name))
        assert helper.__name__ + "(" in src, f"{name} 没走条件推进帮手"
