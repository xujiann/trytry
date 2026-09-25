"""慢专病写接口收下不存在的字符串编码（P1-120 病种编码；P1-121 随访问卷、宣教素材、触发规则等其余编码）。

`program_code` 是字符串软外键（库里没有约束，见 docs/DATA_MODEL.md）：填错一个编码照样 201 落库，这条记录
就挂到一个不存在的病种上——按病种筛的清单、规则匹配、统计口径从此永远不含它，配置（量表、宣教、服务包、
随访方案、转诊规则……）挂到它名下就永远匹配不上任何人。最重的一处是**考核计分**：重跑同一周期会覆盖上次
结果（设计如此），病种编码填错时各指标按这个编码筛出一片空数据，这一期的正式分数被静默改写成零。

2026-09-25 量出：收 `program_code` 的写接口 30 个，查过病种的只有 4 个（筛查登记、自动筛查、建档、专病中心）。
余 26 个逐条判：25 个写库前补查（空串 = 不限病种 / 通用，照收）；1 个只拿它当筛选条件、不写库，按设计
（`BY_DESIGN`）。转诊规则试算看着像只读，勾了「自动开单」却按请求体的病种开转诊单——不绑机构的账号填错
编码修前是 500，归进补查的一边。居民端的自查筛查与申请加入是「新纳入」，与建档 / 筛查同一口径不收停用的
病种；其余是在管患者的业务记录或配置，照收停用病种（病种停用不等于在管的人当天就不管了）。

第二层（P1-120 同批）：团队 / 团队成员 / 考核指标 / 考核方案收的是病种编码**列表**（`program_codes`），同一口径
逐个查，报错点名不存在的那几个。

P1-121：同一形状的其余字符串编码。最重的是随访方案的问卷编码——执行随访时按它查问卷，查不到就整段跳过异常
分级，疼痛 9 分的高危答案记成「无异常」、不派处置任务。管理目标的宣教素材、手工发起转诊的触发规则同批补查。
改档时与现值相同的编码不再查（`already=`）：编辑页每次都带上原值，存量里悬空的编码不该挡住改名、停用。

修法集中在 `spd/service.py` 的 `unknown_program` / `unknown_programs` / `unknown_code`（返回问题文案、由路由报
404，与 `unusable_user` 同一写法）。文件末尾的闸门按字段表把「请求体里指向目录表的编码写库前没查过」钉成零
基线；请求体里名字像编码的字段都得判过——指向哪张目录表，或写明为什么不是引用。
"""
import ast
import pathlib

import pytest

from conftest import business_today

B = "/api/spd"
P = "/api/portal/spd"
REAL, NOPE, OFF = "P120_REAL", "P120_NOPE", "P120_OFF"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Patient, ResidentAccount

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P120 病种校验院", "org_type": "township", "level": "township"}).json()["id"]
    programs = {}
    for code, name in ((REAL, "P120 在用病种"), (OFF, "P120 停用病种")):
        r = client.post(f"{B}/programs", headers=admin, json={"code": code, "name": name, "category": "chronic"})
        assert r.status_code == 201, r.text
        programs[code] = r.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P120 患者", "id_card": "330106197001011239", "gender": "男", "birth_date": "1970-01-01",
        "phone": "13912001200"}).json()["id"]
    for code in (REAL, OFF):   # 两个病种都在管；OFF 随后停用——「在管患者、病种后来停用」
        r = client.post(f"{B}/enrollments", headers=admin, json={"patient_id": patient, "program_code": code,
                                                                "org_id": org})
        assert r.status_code == 201, r.text
    r = client.patch(f"{B}/programs/{programs[OFF]}", headers=admin, json={"active": False})
    assert r.status_code == 200, r.text
    scale = client.post(f"{B}/scales", headers=admin, json={
        "code": "P120_SCALE", "name": "P120 量表", "program_code": REAL,
        "items": [{"key": "q1", "type": "single", "options": [{"label": "是", "score": 1}]}]}).json()["id"]
    assert client.post(f"{B}/scales/{scale}/publish", headers=admin).status_code == 200
    crt = client.post(f"{B}/case-report-tasks", headers=admin, json={"code": "P120_CRT", "name": "P120 上报"}).json()["id"]
    edu = client.post(f"{B}/edu-materials", headers=admin, json={"code": "P120_EDU", "title": "P120 宣教"}).json()["id"]
    rule = client.post(f"{B}/followup-rules", headers=admin,
                       json={"code": "P120_FR", "name": "P120 随访方案", "points": [7]}).json()["id"]
    # 不限病种的转诊规则，靠请求带的额外事实命中——「规则试算开单」三个编码都走到开单那一步
    r = client.post(f"{B}/referral-rules", headers=admin, json={
        "code": "P120_ANY", "name": "P120 通用规则", "conditions": [{"field": "p120_flag", "op": "==", "value": "yes"}]})
    assert r.status_code == 201, r.text

    # 居民端：自己的档案 + 账号，短信验证码登录（与 test_spd_portal_contract 同一做法）
    with SessionLocal() as db:
        me = Patient(ehc_no="EHC-P120-ME", name="P120 居民", id_card="330106198001011235", gender="女",
                     birth_date="1980-01-01", phone="13912001201")
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone="13912001201", patient_id=me.id, nickname="P120", wechat_openid="",
                               status="active"))
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code",
                           json={"phone": "13912001201", "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login",
                            json={"phone": "13912001201", "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    resident = {"Authorization": f"Bearer {token}"}
    consult = client.post(f"{P}/consults", headers=resident, json={"content": "P120 咨询", "program_code": REAL})
    assert consult.status_code == 201, consult.text
    return {"org": org, "patient": patient, "crt": crt, "edu": edu, "rule": rule, "resident": resident,
            "consult": consult.json()["consult_id"], "program_id": programs[REAL]}


#: (用例名, 方法, 路径, 请求体, 居民端?)——路径与请求体都是 (world, 病种编码) 的函数；带唯一编码的配置按
#: 病种编码拼后缀，同一用例先后用两个编码各建一次不撞唯一约束
CASES = [
    ("监测录入", "post", lambda w, c: f"{B}/measurements",
     lambda w, c: {"patient_id": w["patient"], "metric": "bp_sys", "value": 128, "unit": "mmHg", "program_code": c}, False),
    ("量表评估", "post", lambda w, c: f"{B}/assessments",
     lambda w, c: {"patient_id": w["patient"], "scale_code": "P120_SCALE", "answers": {"q1": "是"}, "program_code": c},
     False),
    ("干预模板", "post", lambda w, c: f"{B}/intervention-templates",
     lambda w, c: {"code": f"IT{c}", "name": "P120 模板", "program_code": c}, False),
    ("派干预", "post", lambda w, c: f"{B}/interventions",
     lambda w, c: {"patient_ids": [w["patient"]], "content": "低盐饮食", "program_code": c}, False),
    ("复诊计划", "post", lambda w, c: f"{B}/revisits",
     lambda w, c: {"patient_id": w["patient"], "plan_date": "2026-12-01", "program_code": c}, False),
    ("上报任务", "post", lambda w, c: f"{B}/case-report-tasks",
     lambda w, c: {"code": f"CRT{c}", "name": "P120 上报任务", "program_code": c}, False),
    ("改上报任务", "patch", lambda w, c: f"{B}/case-report-tasks/{w['crt']}", lambda w, c: {"program_code": c}, False),
    ("个案上报", "post", lambda w, c: f"{B}/case-reports",
     lambda w, c: {"patient_id": w["patient"], "content": "指标异常", "program_code": c}, False),
    ("健康处方", "post", lambda w, c: f"{B}/health-prescriptions",
     lambda w, c: {"patient_id": w["patient"], "life_advice": "少盐", "program_code": c}, False),
    ("咨询转随访", "post", lambda w, c: f"{B}/consults/{w['consult']}/to-followup",
     lambda w, c: {"title": "P120 转随访", "program_code": c}, False),
    ("建量表", "post", lambda w, c: f"{B}/scales", lambda w, c: {"code": f"S{c}", "name": "P120 量表", "program_code": c},
     False),
    ("建宣教", "post", lambda w, c: f"{B}/edu-materials",
     lambda w, c: {"code": f"E{c}", "title": "P120 宣教", "program_code": c}, False),
    ("改宣教", "patch", lambda w, c: f"{B}/edu-materials/{w['edu']}", lambda w, c: {"program_code": c}, False),
    ("建服务包", "post", lambda w, c: f"{B}/service-packages",
     lambda w, c: {"code": f"PK{c}", "name": "P120 服务包", "program_code": c}, False),
    ("建随访方案", "post", lambda w, c: f"{B}/followup-rules",
     lambda w, c: {"code": f"FR{c}", "name": "P120 方案", "points": [7], "program_code": c}, False),
    ("改随访方案", "patch", lambda w, c: f"{B}/followup-rules/{w['rule']}", lambda w, c: {"program_code": c}, False),
    ("建转诊规则", "post", lambda w, c: f"{B}/referral-rules",
     lambda w, c: {"code": f"RR{c}", "name": "P120 规则", "program_code": c,
                   "conditions": [{"field": "risk_level", "op": "==", "value": "high"}]}, False),
    ("发起转诊", "post", lambda w, c: f"{B}/referrals",
     lambda w, c: {"patient_id": w["patient"], "reason": "上转", "program_code": c}, False),
    ("规则试算开单", "post", lambda w, c: f"{B}/referral-rules/check",
     lambda w, c: {"patient_id": w["patient"], "program_code": c, "extra": {"p120_flag": "yes"}, "auto_create": True},
     False),
    ("手工建任务", "post", lambda w, c: f"{B}/tasks",
     lambda w, c: {"patient_id": w["patient"], "title": "P120 任务", "org_id": w["org"], "program_code": c}, False),
    ("居民自测", "post", lambda w, c: f"{P}/measurements",
     lambda w, c: {"metric": "bp_sys", "value": 128, "program_code": c}, True),
    ("居民咨询", "post", lambda w, c: f"{P}/consults", lambda w, c: {"content": "P120 再问一句", "program_code": c}, True),
    ("居民自查", "post", lambda w, c: f"{P}/screenings", lambda w, c: {"program_code": c, "answers": {}}, True),
    ("居民申请加入", "post", lambda w, c: f"{P}/service-applies", lambda w, c: {"program_code": c}, True),
]
#: 「新纳入」——与建档 / 筛查同一口径，停用的病种也不收
NEW_INCLUSION = {"居民自查", "居民申请加入"}


def _call(client, admin, world, case, code):
    _name, method, path, body, as_resident = case
    headers = world["resident"] if as_resident else admin
    return getattr(client, method)(path(world, code), headers=headers, json=body(world, code))


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_不存在的病种编码是404_真实的照收(client, admin, world, case):
    bad = _call(client, admin, world, case, NOPE)
    expected = "专病档案不存在或已停用" if case[0] in NEW_INCLUSION else "专病档案不存在"
    assert bad.status_code == 404 and bad.json() == {"detail": expected}, (bad.status_code, bad.text[:300])
    good = _call(client, admin, world, case, REAL)
    assert good.status_code < 300, good.text[:300]   # 同一请求换成真实的病种照常成功：404 只因病种编码


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_停用的病种_新纳入不收_在管业务与配置照收(client, admin, world, case):
    resp = _call(client, admin, world, case, OFF)
    if case[0] in NEW_INCLUSION:
        assert resp.status_code == 404 and resp.json() == {"detail": "专病档案不存在或已停用"}, resp.text[:300]
    else:
        assert resp.status_code < 300, resp.text[:300]


def test_考核重跑填错病种不再把本期正式分数覆盖成零(client, admin, world):
    """修前：同一周期先按真实病种跑出 100 分，再用填错的编码重跑——各指标按它筛出空数据，
    这一期的分数被覆盖成 0、病种写成那个不存在的编码（重跑覆盖是设计，填错不拦是缺陷）。"""
    from app.database import SessionLocal
    from app.spd.models import SpdScore

    r = client.post(f"{B}/indicators", headers=admin, json={
        "code": "P120_ENR", "name": "P120 在管人数", "data_source": "enrollment", "object_type": "org",
        "formula": "enrolled", "score_rule": {"type": "ratio", "full": 100, "target": 1}})
    assert r.status_code == 201, r.text
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "P120_PLAN", "name": "P120 考核", "level": "township", "object_type": "org",
        "period_type": "month", "items": [{"indicator_code": "P120_ENR", "weight": 100}]})
    assert plan.status_code == 201, plan.text
    period = business_today().strftime("%Y-%m")
    run = {"plan_id": plan.json()["id"], "period": period, "object_ids": [world["org"]]}
    first = client.post(f"{B}/scores/run", headers=admin, json={**run, "program_code": REAL})
    assert first.status_code == 200 and first.json()["top"][0]["total_score"] == 100, first.text

    again = client.post(f"{B}/scores/run", headers=admin, json={**run, "program_code": NOPE})
    assert again.status_code == 404 and again.json() == {"detail": "专病档案不存在"}, again.text[:300]
    with SessionLocal() as db:
        row = db.query(SpdScore).filter(SpdScore.plan_id == plan.json()["id"], SpdScore.period == period,
                                        SpdScore.object_id == world["org"]).one()
        assert (row.total_score, row.program_code) == (100, REAL)   # 修前：(0.0, 'P120_NOPE')


def test_批量入组只拿病种当筛选条件_按设计(client, admin, world):
    """`BY_DESIGN` 那一处：病种编码只用来筛在管人群，写进组的是患者、不是编码——填错得到「入组 0 人」，不留脏数据。"""
    group = client.post(f"{B}/groups", headers=admin, json={
        "name": "P120 分组", "auto_rule": [{"field": "age", "op": ">=", "value": 0}]})
    assert group.status_code == 201, group.text
    url = f"{B}/groups/{group.json()['id']}/members"
    bad = client.post(url, headers=admin, json={"use_auto_rule": True, "program_code": NOPE})
    assert bad.status_code == 200 and bad.json()["added"] == 0, bad.text
    good = client.post(url, headers=admin, json={"use_auto_rule": True, "program_code": REAL})
    assert good.status_code == 200 and good.json()["added"] == 1, good.text


def test_随访方案挂不存在的问卷_404_真实的照收_执行随访照常分级(client, admin, world):
    """P1-121 最重的一处：执行随访按方案上的问卷编码查问卷，查不到就整段跳过异常分级——修前方案挂着填错的
    问卷编码时，疼痛 9 分记成「无异常」、不派处置任务。"""
    r = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "P121_Q", "name": "P121 术后问卷", "items": [{"key": "pain", "title": "疼痛评分", "type": "number"}],
        "abnormal_rules": [{"when": {"field": "pain", "op": ">=", "value": 7}, "level": "high",
                            "action": "通知主管医师"}]})
    assert r.status_code == 201, r.text
    base = {"name": "P121 术后随访", "points": [0], "program_code": REAL}
    bad = client.post(f"{B}/followup-rules", headers=admin,
                      json={**base, "code": "P121_FRX", "questionnaire_code": "P121_QX"})
    assert bad.status_code == 404 and bad.json() == {"detail": "随访问卷不存在：P121_QX"}, bad.text[:300]
    rule = client.post(f"{B}/followup-rules", headers=admin,
                       json={**base, "code": "P121_FRQ", "questionnaire_code": "P121_Q"})
    assert rule.status_code == 201, rule.text
    r = client.patch(f"{B}/followup-rules/{rule.json()['id']}", headers=admin, json={"questionnaire_code": "P121_QX"})
    assert r.status_code == 404 and r.json() == {"detail": "随访问卷不存在：P121_QX"}, r.text[:300]

    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": world["patient"], "rule_id": rule.json()["id"], "base_date": "2001-01-01", "org_id": world["org"]})
    assert plan.status_code == 201, plan.text
    record_id = plan.json()["items"][0]["id"]
    done = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin, json={"answers": {"pain": 9}})
    assert done.status_code == 200, done.text
    assert done.json()["abnormal_level"] == "high"   # 方案挂着查不到的问卷时这里是 none，也不派处置任务


def test_改档与现值相同的编码不再查_存量悬空不挡改名停用(client, admin, world):
    """编辑页每次都带上原值：存量里已经悬空的编码（修前落的库，或目录后来删了）不该挡住与它无关的改名、停用；
    真要换成另一个编码，照查。"""
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRule, SpdTeam

    with SessionLocal() as db:
        legacy_rule = SpdFollowupRule(code="P121_LEGACY", name="P121 存量方案", questionnaire_code="P121_GONE",
                                      points=[7])
        legacy_team = SpdTeam(name="P121 存量团队", org_id=world["org"], program_codes=["P121_GONE"])
        db.add_all([legacy_rule, legacy_team])
        db.commit()
        rule_id, team_id = legacy_rule.id, legacy_team.id
    r = client.patch(f"{B}/followup-rules/{rule_id}", headers=admin,
                     json={"name": "P121 存量方案（改名）", "questionnaire_code": "P121_GONE", "active": False})
    assert r.status_code == 200, r.text
    r = client.patch(f"{B}/followup-rules/{rule_id}", headers=admin, json={"questionnaire_code": "P121_GONE2"})
    assert r.status_code == 404 and r.json() == {"detail": "随访问卷不存在：P121_GONE2"}, r.text[:300]
    r = client.patch(f"{B}/teams/{team_id}", headers=admin, json={"program_codes": ["P121_GONE", REAL]})
    assert r.status_code == 200, r.text
    r = client.patch(f"{B}/teams/{team_id}", headers=admin, json={"program_codes": ["P121_GONE", NOPE]})
    assert r.status_code == 404 and r.json() == {"detail": f"专病档案不存在：{NOPE}"}, r.text[:300]


def test_管理目标挂不存在的宣教素材_404_真实的照收(client, admin, world):
    url = f"{B}/programs/{world['program_id']}/targets"
    body = {"stage": "P121", "metric": "bp_sys", "target_high": 140}
    bad = client.post(url, headers=admin, json={**body, "edu_code": "P121_EDUX"})
    assert bad.status_code == 404 and bad.json() == {"detail": "宣教素材不存在：P121_EDUX"}, bad.text[:300]
    good = client.post(url, headers=admin, json={**body, "edu_code": "P120_EDU"})
    assert good.status_code == 201, good.text
    r = client.patch(f"{B}/targets/{good.json()['id']}", headers=admin, json={"edu_code": "P121_EDUX"})
    assert r.status_code == 404 and r.json() == {"detail": "宣教素材不存在：P121_EDUX"}, r.text[:300]


def test_手工发起转诊引用不存在的触发规则_404_真实的照收(client, admin, world):
    """转诊单上写着「由某规则触发」，那条规则得真有。"""
    body = {"patient_id": world["patient"], "program_code": REAL, "reason": "P121 上转"}
    bad = client.post(f"{B}/referrals", headers=admin, json={**body, "trigger_rule_code": "P121_RRX"})
    assert bad.status_code == 404 and bad.json() == {"detail": "转诊规则不存在：P121_RRX"}, bad.text[:300]
    good = client.post(f"{B}/referrals", headers=admin, json={**body, "trigger_rule_code": "P120_ANY"})
    assert good.status_code == 201, good.text


#: 病种列表（`program_codes`）的四处写接口：(用例名, 建档路径, 建档请求体, 改档路径)
LIST_CASES = [
    ("团队", lambda lw: f"{B}/teams",
     lambda lw, codes: {"name": "P121 团队", "org_id": lw["org"], "program_codes": codes},
     lambda created: f"{B}/teams/{created['id']}"),
    ("团队成员", lambda lw: f"{B}/teams/{lw['team']}/members",
     lambda lw, codes: {"user_id": lw["doctor"], "program_codes": codes},
     lambda created: f"{B}/team-members/{created['id']}"),
    ("考核指标", lambda lw: f"{B}/indicators",
     lambda lw, codes: {"code": "P121_IND", "name": "P121 指标", "data_source": "enrollment", "object_type": "org",
                        "formula": "enrolled", "program_codes": codes},
     lambda created: f"{B}/indicators/{created['id']}"),
    ("考核方案", lambda lw: f"{B}/assess-plans",
     lambda lw, codes: {"code": "P121_PLAN", "name": "P121 考核", "level": "township", "object_type": "org",
                        "period_type": "month", "items": [{"indicator_code": "P121_IND_BASE", "weight": 100}],
                        "program_codes": codes},
     lambda created: f"{B}/assess-plans/{created['id']}"),
]


@pytest.fixture(scope="module")
def list_world(client, admin, world):
    """病种列表用例自带的前置：挂成员的团队、考核方案引用的指标、当成员的医生——不靠别的用例先跑。"""
    team = client.post(f"{B}/teams", headers=admin, json={"name": "P121 成员团队", "org_id": world["org"]})
    assert team.status_code == 201, team.text
    r = client.post(f"{B}/indicators", headers=admin, json={
        "code": "P121_IND_BASE", "name": "P121 方案用指标", "data_source": "enrollment", "object_type": "org",
        "formula": "enrolled", "score_rule": {"type": "ratio", "full": 100, "target": 1}})
    assert r.status_code == 201, r.text
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p121_doc", "password": "Passw0rd!x", "role": "doctor", "org_id": world["org"]})
    assert doctor.status_code == 201, doctor.text
    return {"org": world["org"], "team": team.json()["id"], "doctor": doctor.json()["id"]}


@pytest.mark.parametrize("case", LIST_CASES, ids=[c[0] for c in LIST_CASES])
def test_病种列表有不存在的编码_404_点名_停用的照收(client, admin, list_world, case):
    _name, path, body, patch_path = case
    bad = client.post(path(list_world), headers=admin, json=body(list_world, [REAL, NOPE, ""]))
    assert bad.status_code == 404 and bad.json() == {"detail": f"专病档案不存在：{NOPE}"}, bad.text[:300]
    good = client.post(path(list_world), headers=admin, json=body(list_world, [REAL, OFF]))
    assert good.status_code == 201, good.text[:300]   # 停用的病种照收：配置可能是为重新启用备的
    r = client.patch(patch_path(good.json()), headers=admin, json={"program_codes": [REAL, NOPE]})
    assert r.status_code == 404 and r.json() == {"detail": f"专病档案不存在：{NOPE}"}, r.text[:300]


# ================================================================ 闸门：请求体里指向目录表的编码写库前须查过
APP_SPD = pathlib.Path(__file__).resolve().parents[1] / "app" / "spd" / "routers"
BASELINE = 0

#: 请求体字段 → 「查过」的记号：函数连同两层同模块调用里出现任一即算查过（宽判据，只防「一眼没看」）
SOFT_FKS = {
    "program_code": ("unknown_program(", "SpdProgram"),
    "program_codes": ("unknown_programs(",),
    "questionnaire_code": ("SpdQuestionnaire",),
    "edu_code": ("SpdEduMaterial",),
    "trigger_rule_code": ("SpdReferralRule",),
    "scale_code": ("SpdScale",),
    "template_code": ("SpdReportTemplate",),
}

#: 名字像编码、却不指向任何目录表的请求体字段——逐条写明理由；新加的编码字段不进 SOFT_FKS 就得在这里说清
NOT_REFS = {
    "form_code": "表单没有目录表：任务 / 路径节点 / 管理目标上的 form_code 只标明用哪张填报表单，表单结构由各业务"
                 "前端定义（任务表的 form/result 是宽字典），库里没有可查的表单目录",
    "item_code": "服务包项目编码指向该服务包绑定自己的 items（JSON 列里的一项），扣减接口逐项比对、查不到即 404"
                 "「服务包中没有该项目」，不是目录表的软外键",
    "verify_code": "核销码是被查找的键（凭码找待核销记录），不是引用：查不到本身就是 404「核销码无效或已核销」",
}

#: 请求体带编码、却只拿它当筛选条件（不写进任何一行）的写接口字段——逐条写明理由，只减不增。
#: （转诊规则试算原也登记在这里，理由写的是「只试算、不提交」——可勾了自动开单它就按请求体的病种开单，
#: 理由不成立，已移出、补查）
BY_DESIGN = {
    "population.py:add_group_members:program_code":
        "按规则批量入组时用病种编码筛在管人群（`SpdEnrollment.program_code == …`），写进组的是患者，不是编码；"
        "填错得到「入组 0 人」",
}


def _code_fields(cls: ast.ClassDef) -> set[str]:
    """请求体模型里名字像编码的字段：`*_code` / `*_codes`，自身的主键编码 `code` 除外。"""
    return {s.target.id for s in cls.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            and s.target.id != "code" and s.target.id.endswith(("_code", "_codes"))}


def _write_endpoints(sources: dict[str, str] | None = None):
    """逐个给出写接口（post / put / patch）：(文件名, 函数名, 函数连同两层同模块调用的源码, 请求体里的编码字段)。"""
    files = {str(p.relative_to(APP_SPD)): p.read_text(encoding="utf-8")
             for p in sorted(APP_SPD.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    for name, text in files.items():
        tree = ast.parse(text)
        body_fields = {n.name: _code_fields(n) for n in tree.body if isinstance(n, ast.ClassDef)}
        funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

        def closure(fn, depth=2, seen=None) -> str:
            seen = set() if seen is None else seen
            src = ast.unparse(fn)
            if depth:
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                            and node.func.id in funcs and node.func.id not in seen:
                        seen.add(node.func.id)
                        src += "\n" + closure(funcs[node.func.id], depth - 1, seen)
            return src

        for fn in funcs.values():
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("post", "put", "patch") for d in fn.decorator_list):
                continue
            fields: set[str] = set()
            for arg in fn.args.args:
                if arg.annotation is not None:
                    fields |= body_fields.get(ast.unparse(arg.annotation), set())
            if fields:
                yield pathlib.PurePath(name).name, fn.name, closure(fn), fields


def unchecked_code_refs(sources: dict[str, str] | None = None) -> list[str]:
    """`文件:函数:字段`——请求体里指向目录表的编码（SOFT_FKS），函数里却一个「查过」的记号都没有。"""
    found = []
    for file, fn, src, fields in _write_endpoints(sources):
        for field in fields:
            markers = SOFT_FKS.get(field)
            if markers is not None and not any(m in src for m in markers):
                found.append(f"{file}:{fn}:{field}")
    return sorted(found)


def test_请求体里的字符串编码写库之前得先查在不在():
    bad = [f for f in unchecked_code_refs() if f not in BY_DESIGN]
    assert len(bad) <= BASELINE, (
        "以下写接口收指向目录表的编码，却从不看它指向的那一行在不在：\n  " + "\n  ".join(bad)
        + "\n\n写库前查一次（病种 `unknown_program` / 病种列表 `unknown_programs` / 其余目录 `unknown_code`），"
          "有问题报 404；只拿它当筛选条件的登记进 BY_DESIGN。"
    )


def test_请求体里名字像编码的字段都判过():
    seen = set().union(*(fields for *_, fields in _write_endpoints()))
    unjudged = seen - set(SOFT_FKS) - set(NOT_REFS)
    assert not unjudged, (
        f"请求体里新出现名字像编码的字段 {sorted(unjudged)}：指向目录表的登记进 SOFT_FKS（写库前查在不在），"
        "不是引用的写明理由登记进 NOT_REFS。"
    )
    assert set(NOT_REFS) <= seen, sorted(set(NOT_REFS) - seen)   # 名单里的字段都还在，不留死条目
    assert all(reason.strip() for reason in NOT_REFS.values())


def test_按设计名单只减不增_且条条都还在():
    assert set(BY_DESIGN) <= set(unchecked_code_refs()), sorted(set(BY_DESIGN) - set(unchecked_code_refs()))
    assert all(reason.strip() for reason in BY_DESIGN.values())


def test_判据自证_没查的点名_查过的与不带编码的不报():
    snippet = (
        "class AIn(BaseModel):\n    program_code: str = ''\n    questionnaire_code: str = ''\n"
        "class BIn(BaseModel):\n    name: str\n"
        "def _check(db, code):\n    return db.query(SpdProgram).filter(SpdProgram.code == code).first()\n"
        "@router.post('/a')\ndef bare(body: AIn, db=None):\n    db.add(X(**body.model_dump()))\n"
        "@router.post('/b')\ndef all_checked(body: AIn, db=None):\n    _check(db, body.program_code)\n"
        "    unknown_code(db, SpdQuestionnaire, body.questionnaire_code, '随访问卷')\n"
        "@router.patch('/c')\ndef half_checked(body: AIn, db=None):\n"
        "    problem = unknown_program(db, body.program_code)\n"
        "@router.post('/d')\ndef no_codes(body: BIn, db=None):\n    db.add(X(**body.model_dump()))\n"
    )
    assert [f for f in unchecked_code_refs({"probe.py": snippet}) if f.startswith("probe.py")] == [
        "probe.py:bare:program_code", "probe.py:bare:questionnaire_code", "probe.py:half_checked:questionnaire_code"]
