"""单选 / 多选题的作答与异常规则的比较值都要对得上题目选项（P2-1636，第四十八批扫描 AL1-3）。

建 / 改问卷（`followup._check_abnormal_rules`）只核规则的题目 key 与级别（P1-122），执行随访与居民自助作答
（`service.answers_problem`）只核数值题（P2-711）。修前实测：选项是「良好 / 红肿 / 渗液」，规则写成「切口 == 渗出」照样
201、对单选题写「切口 >= 2」也 201，真实作答「渗液」判无异常；管理端执行随访的多选题是「（多选，逗号分隔）」自由文本，
录成「胸 痛」不命中「胸痛 in [...]」；选项之外的作答「裂开流脓」「胸口痛」原样落库——三条随访都判无异常，处置任务 0 条。
P1-122 已要求规则字段必须是本问卷题目，P2-712 / P2-1117 也拒收永不命中的比较值，比较值落在选项之外是同一种永不命中。

修法：有选项的题，等于 / 不等于 / 属于 / 不属于的比较值必须是选项之一，大小比较与「介于」只许用在数值题上，否则 422
点名；执行与居民自助作答的单选值、多选的每一项必须在选项里，否则 422 点名（与数值题同一处 `answers_problem`）。比对口径
与规则求值同一个（`rules.is_option`：去首尾空白、读得成数按数比）。存量问卷里的坏规则不改库，判级照旧（不命中）。
管理端执行随访的多选题改成复选框（与居民端同一个形态）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import clock

B = "/api/spd"
P = "/api/portal/spd"
PHONE = "13900016360"
SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
WOUND = {"key": "wound", "title": "切口愈合", "type": "single",
         "options": [{"label": "良好"}, {"label": "红肿"}, {"label": "渗液"}]}
SYM = {"key": "sym", "title": "近期症状", "type": "multi", "options": [{"label": "无"}, {"label": "胸痛"}, {"label": "气短"}]}
PAIN = {"key": "pain", "title": "疼痛评分", "type": "number"}
NOTE = {"key": "note", "title": "其他情况"}   # 没有选项的题：作答不管
GOOD_RULES = [
    {"when": {"field": "wound", "op": "==", "value": "渗液"}, "level": "high", "action": "立即联系手术医师"},
    {"when": {"field": "sym", "op": "in", "value": ["胸痛"]}, "level": "high", "action": "胸痛立即上转"},
    {"when": {"field": "sym", "op": "!=", "value": " 无 "}, "level": "low", "action": "电话追访"},
    {"when": {"field": "pain", "op": ">=", "value": 7}, "level": "mid", "action": "评估镇痛方案"},
]


def _rule(field, op, value, level="high"):
    return {"when": {"field": field, "op": op, "value": value}, "level": level, "action": "处置"}


def _create(client, admin, code, rules, items=None):
    return client.post(f"{B}/questionnaires", headers=admin, json={
        "code": code, "name": f"{code} 问卷", "items": items or [WOUND, SYM, PAIN, NOTE], "abnormal_rules": rules})


# ================================================================ 建 / 改问卷：比较值对题目选项
@pytest.mark.parametrize(("rule", "detail"), [
    (_rule("wound", "==", "渗出"), "异常分级规则「切口愈合 等于 渗出」的比较值不是这道题的选项（选项：良好 / 红肿 / 渗液）"),
    (_rule("wound", ">=", 2), "异常分级规则「切口愈合」不是数值题，不能用「大于等于」：比大小与介于只用在数值题上"),
    (_rule("sym", "in", ["胸痛", "胸口痛"]),
     "异常分级规则「近期症状 属于 胸口痛」的比较值不是这道题的选项（选项：无 / 胸痛 / 气短）"),
    (_rule("sym", "not_in", ["头晕"]), "异常分级规则「近期症状 不属于 头晕」的比较值不是这道题的选项（选项：无 / 胸痛 / 气短）"),
    (_rule("wound", "!=", "愈合"), "异常分级规则「切口愈合 不等于 愈合」的比较值不是这道题的选项（选项：良好 / 红肿 / 渗液）"),
    (_rule("sym", "between", [1, 2]), "异常分级规则「近期症状」不是数值题，不能用「介于」：比大小与介于只用在数值题上"),
    (_rule("note", "<", 3), "异常分级规则「其他情况」不是数值题，不能用「小于」：比大小与介于只用在数值题上"),
], ids=["等于选项外", "单选比大小", "属于里有选项外", "不属于选项外", "不等于选项外", "多选介于", "无选项文本题比大小"])
def test_建问卷_永不命中的比较值一律422(client, admin, rule, detail):
    resp = _create(client, admin, "P21636_BAD", [GOOD_RULES[0], rule])
    assert resp.status_code == 422 and resp.json() == {"detail": detail}, resp.text   # 修前 201


def test_合法规则照收_存的字节不变(client, admin):
    resp = _create(client, admin, "P21636_OK", GOOD_RULES)
    assert resp.status_code == 201, resp.text
    assert resp.json()["abnormal_rules"] == GOOD_RULES
    # 选项是数字文字的，比较值按数比（与求值同一个口径）；直接写文字选项的旧写法同样认
    numeric = {"key": "grade", "title": "分级", "type": "single", "options": ["0", "1", " 2 "]}
    resp = _create(client, admin, "P21636_NUM", [_rule("grade", "==", 2), _rule("grade", "in", ["1", "2.0"])],
                   items=[numeric])
    assert resp.status_code == 201, resp.text


def test_改问卷_换成选项外的比较值或单选比大小同样422_不留半截(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdQuestionnaire

    created = _create(client, admin, "P21636_EDIT", GOOD_RULES[:1])
    assert created.status_code == 201, created.text
    url = f"{B}/questionnaires/{created.json()['id']}"
    for rules in ([_rule("wound", "==", "渗出")], [_rule("wound", ">", 1)], [_rule("sym", "in", ["胸 痛"])]):
        resp = client.patch(url, headers=admin, json={"abnormal_rules": rules})
        assert resp.status_code == 422, resp.text   # 修前 200
    # 只改题目、把规则引用的选项删掉，同样查出来
    resp = client.patch(url, headers=admin, json={"items": [{**WOUND, "options": [{"label": "良好"}, {"label": "红肿"}]}]})
    assert resp.status_code == 422 and "渗液" in resp.json()["detail"], resp.text
    with SessionLocal() as db:
        row = db.get(SpdQuestionnaire, created.json()["id"])
        assert row.abnormal_rules == GOOD_RULES[:1] and row.items[0] == WOUND


# ================================================================ 执行与居民自助：作答对题目选项
@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21636 随访院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21636 居民", "id_card": "330106197001011636", "gender": "女",
        "birth_date": "1970-01-01", "phone": PHONE}).json()["id"]
    assert _create(client, admin, "P21636_RUN", GOOD_RULES).status_code == 201
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    ph = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", json={"name": "P21636 居民", "id_card": "330106197001011636"},
                        headers=ph)
    assert bound.status_code in (200, 409), bound.text
    return {"org": org, "patient": patient, "ph": ph}


def _record(world, questionnaire_code="P21636_RUN"):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], questionnaire_code=questionnaire_code,
                                   org_id=world["org"], planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        return record.id


def _status(record_id):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        row = db.get(SpdFollowupRecord, record_id)
        return row.status, row.answers


@pytest.mark.parametrize(("answers", "detail"), [
    ({"wound": "裂开流脓"}, "「切口愈合」的作答「裂开流脓」不是这道题的选项（选项：良好 / 红肿 / 渗液）"),
    ({"sym": ["胸 痛", "气短"]}, "「近期症状」的作答「胸 痛」不是这道题的选项（选项：无 / 胸痛 / 气短）"),
    ({"wound": "渗液", "sym": ["气短", "胸口痛"]}, "「近期症状」的作答「胸口痛」不是这道题的选项（选项：无 / 胸痛 / 气短）"),
    ({"sym": "胸痛,气短"}, "「近期症状」的作答「胸痛,气短」不是这道题的选项（选项：无 / 胸痛 / 气短）"),
], ids=["单选选项外", "多选自由文本", "多选里有一项选项外", "多选交成整串"])
def test_执行随访_选项外的作答422_不办结(client, admin, world, answers, detail):
    record_id = _record(world)
    resp = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin, json={"answers": answers})
    assert resp.status_code == 422 and resp.json() == {"detail": detail}, resp.text   # 修前 200、判无异常
    assert _status(record_id) == ("planned", {})


def test_执行随访_选项内的作答照收_渗液判重度派处置任务(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    record_id = _record(world)
    resp = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin, json={
        "answers": {"wound": " 渗液 ", "sym": ["气短"], "pain": 3, "note": "随便写什么都行"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["abnormal_level"] == "high"
    with SessionLocal() as db:
        titles = [t.title for t in db.query(SpdTask).filter(SpdTask.patient_id == world["patient"],
                                                             SpdTask.source == "followup")]
    assert "随访异常处置：立即联系手术医师" in titles, titles
    # 失访不看作答（与数值题同一句）
    unreachable = client.post(f"{B}/followup-records/{_record(world)}/execute", headers=admin,
                              json={"answers": {"wound": "裂开流脓"}, "unreachable": True})
    assert unreachable.status_code == 200 and unreachable.json()["status"] == "unreachable", unreachable.text


def test_居民自助作答_选项外的作答同样422(client, world):
    record_id = _record(world)
    resp = client.post(f"{P}/followups/{record_id}/self-answer", headers=world["ph"],
                       json={"patient_id": world["patient"], "answers": {"sym": ["胸口痛"]}})
    assert resp.status_code == 422, resp.text   # 修前 200
    assert resp.json() == {"detail": "「近期症状」的作答「胸口痛」不是这道题的选项（选项：无 / 胸痛 / 气短）"}
    assert _status(record_id) == ("planned", {})
    ok = client.post(f"{P}/followups/{record_id}/self-answer", headers=world["ph"],
                     json={"patient_id": world["patient"], "answers": {"sym": ["胸痛"]}})
    assert ok.status_code == 200 and ok.json()["abnormal_level"] == "high", ok.text


def test_存量问卷里的坏规则不改库_判级照旧不命中(client, admin, world):
    """修前落库的坏规则（选项外的比较值、单选比大小）不在这里改：选项内的作答照收，那两条照旧不命中。"""
    from app.database import SessionLocal
    from app.spd.models import SpdQuestionnaire

    legacy_rules = [_rule("wound", "==", "渗出"), _rule("wound", ">=", 2, "mid")]
    with SessionLocal() as db:
        db.add(SpdQuestionnaire(code="P21636_LEGACY", name="存量问卷", items=[WOUND], abnormal_rules=legacy_rules))
        db.commit()
    resp = client.post(f"{B}/followup-records/{_record(world, 'P21636_LEGACY')}/execute", headers=admin,
                       json={"answers": {"wound": "渗液"}})
    assert resp.status_code == 200 and resp.json()["abnormal_level"] == "none", resp.text
    with SessionLocal() as db:
        assert db.query(SpdQuestionnaire).filter_by(code="P21636_LEGACY").one().abnormal_rules == legacy_rules


def test_种子问卷都过得了比较值校验():
    from app.spd.routers.followup import _check_abnormal_rules
    from app.spd.seed import SEED_QUESTIONNAIRES

    for q in SEED_QUESTIONNAIRES:
        _check_abnormal_rules(q.get("abnormal_rules", []), q.get("items", []))


# ================================================================ 页面：执行随访的多选题是复选框
def _function(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start:SRC.index("\n}\n", start) + 2]


def _node(script: str, payload) -> object:
    out = subprocess.run(["node", "-e", script, json.dumps(payload, ensure_ascii=False)],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_执行随访的多选题是复选框_量表照旧是文本框():
    script = (_function("spdQuestionFields")
              + "\nconst items = JSON.parse(process.argv[1]);"
              + "\nconsole.log(JSON.stringify([spdQuestionFields(items, 'q_', { checks: true }),"
              + " spdQuestionFields(items, 'q_')]));")
    checks, plain = _node(script, [SYM, {**SYM, "key": "free", "options": []}, WOUND])
    assert checks[0] == {"name": "q_sym", "label": "近期症状（多选）", "type": "checks",   # 修前是「（多选，逗号分隔）」文本框
                         "options": [{"value": o, "label": o} for o in ("无", "胸痛", "气短")]}
    assert checks[1] == {"name": "q_free", "label": "近期症状（多选，逗号分隔）"}   # 没有选项的多选题仍是文本框
    assert checks[2]["type"] == "select" and plain[2] == checks[2]
    assert plain[0] == {"name": "q_sym", "label": "近期症状（多选，逗号分隔）"}   # 量表筛查 / 评估不传 checks：形态不动
    # 执行随访的弹框传了 checks；spdModal 认得复选框组、交上去是勾中的列表
    assert 'spdQuestionFields(items, "q_", { checks: true })' in SRC
    modal = _function("spdModal")
    assert 'f.type === "checks"' in modal and "el.checked" in modal


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_复选框交上来的列表照原样进作答_没勾的不交():
    script = (_function("spdCollectAnswers")
              + "\nconst [items, form] = JSON.parse(process.argv[1]);"
              + "\nconsole.log(JSON.stringify(spdCollectAnswers(items, form, 'q_')));")
    # 选项文字里带顿号的（「头晕、乏力」）整项交上去——原先按 [,，、] 切开成两个选项外的作答
    sym = {**SYM, "options": [*SYM["options"], {"label": "头晕、乏力"}]}
    form = {"q_sym": ["胸痛", "头晕、乏力"], "q_wound": "渗液", "q_pain": "4", "q_free": []}
    assert _node(script, [[sym, WOUND, PAIN, {**SYM, "key": "free"}], form]) == {
        "sym": ["胸痛", "头晕、乏力"], "wound": "渗液", "pain": 4}
