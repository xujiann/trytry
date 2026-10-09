"""随访问卷命中多条异常规则时，处置任务要带上命中的全部处置措施（P2-1635，第四十八批扫描 AL1-2）。

`rules.grade_abnormal` 同级不替换、只回一条处置措施：作答「胸痛=有、切口=渗液、发热=是」命中两条重度、一条中度，规则按
「胸痛, 渗液, 发热」写时派出「随访异常处置：胸痛：立即120转上级医院」，同样的作答、规则倒过来写派出的是「切口渗液：
安排返院清创」，胸痛「立即120」一字不提；中度的「发热：24小时内回访」两种顺序都丢。`grade_abnormal` 自己的 docstring
写着「规则的书写顺序不该决定病人的分级」；作答在页面上看不到（P2-698），任务标题是唯一的交接内容。

修法：`rules.abnormal_hits` 返回命中的全部规则（先按级别降序、同级按书写顺序），`followup.abnormal_outcome` 拼成标题、
回执与居民端提示（都是**最高级别**各条合并）与明细（命中的全部、含低一级的，写进处置任务的 `form`）；医护执行与居民
自助作答同一句。低一级的不进回执：预置慢病问卷收缩压 190 同时命中「≥180 重度：立即上转评估」「≥160 中度：两周内复诊
调整用药」，回执照旧只有「立即上转评估」。最高级别只命中一条时标题、回执字节不变，只命中一条时任务也不变（`form` 不写）；
`grade_abnormal` 照旧（特征化用例另有钉住）。
"""
from datetime import timedelta

import pytest

from app import clock

B = "/api/spd"
P = "/api/portal/spd"
PHONE = "13900016350"
ITEMS = [
    {"key": "chest", "title": "胸痛", "type": "single", "options": [{"label": "无"}, {"label": "有"}]},
    {"key": "wound", "title": "切口", "type": "single", "options": [{"label": "良好"}, {"label": "渗液"}]},
    {"key": "fever", "title": "发热", "type": "single", "options": [{"label": "否"}, {"label": "是"}]},
]
CHEST = {"when": {"field": "chest", "op": "==", "value": "有"}, "level": "high", "action": "胸痛：立即120转上级医院"}
WOUND = {"when": {"field": "wound", "op": "==", "value": "渗液"}, "level": "high", "action": "切口渗液：安排返院清创"}
FEVER = {"when": {"field": "fever", "op": "==", "value": "是"}, "level": "mid", "action": "发热：24小时内回访"}
ALL_YES = {"chest": "有", "wound": "渗液", "fever": "是"}


# ================================================================ 求值：命中的全部规则
def test_命中的全部规则_先按级别降序_同级按书写顺序():
    from app.spd.rules import abnormal_hits

    assert abnormal_hits([FEVER, CHEST, WOUND], ALL_YES) == [
        ("high", "胸痛：立即120转上级医院"), ("high", "切口渗液：安排返院清创"), ("mid", "发热：24小时内回访")]
    assert abnormal_hits([WOUND, FEVER, CHEST], ALL_YES) == [
        ("high", "切口渗液：安排返院清创"), ("high", "胸痛：立即120转上级医院"), ("mid", "发热：24小时内回访")]
    assert abnormal_hits([CHEST, WOUND, FEVER], {"chest": "无"}) == []
    # 表外级别不算命中（与 grade_abnormal 同一个判法）
    assert abnormal_hits([{**CHEST, "level": "severe"}], ALL_YES) == []


def test_判级与原先一致_grade_abnormal照旧():
    from app.spd.rules import grade_abnormal

    assert grade_abnormal([CHEST, WOUND, FEVER], ALL_YES) == ("high", "胸痛：立即120转上级医院")
    assert grade_abnormal([WOUND, CHEST, FEVER], ALL_YES) == ("high", "切口渗液：安排返院清创")
    assert grade_abnormal([FEVER], ALL_YES) == ("mid", "发热：24小时内回访")
    assert grade_abnormal([], ALL_YES) == ("none", "")


def test_标题回执明细的拼法():
    from app.spd.routers.followup import abnormal_outcome

    outcome = abnormal_outcome([FEVER, WOUND, CHEST], ALL_YES)
    assert outcome.level == "high"
    assert outcome.headline == "切口渗液：安排返院清创；胸痛：立即120转上级医院"     # 修前只有先写的那条
    assert outcome.action == outcome.headline   # 回执只合并最高级别的，中度的「发热」只进明细
    assert outcome.lines == ["重度异常：切口渗液：安排返院清创", "重度异常：胸痛：立即120转上级医院",
                             "中度异常：发热：24小时内回访"]
    # 最高级别只命中一条、另有低一级的：标题与回执与原先一字不差，低一级的只进明细
    one_top = abnormal_outcome([FEVER, CHEST], ALL_YES)
    assert (one_top.action, one_top.headline, one_top.lines) == (
        "胸痛：立即120转上级医院", "胸痛：立即120转上级医院",
        ["重度异常：胸痛：立即120转上级医院", "中度异常：发热：24小时内回访"])
    # 只命中一条：与原先一字不差（标题里的措施就是那条的，没写措施的用级别名）
    single = abnormal_outcome([CHEST], ALL_YES)
    assert (single.level, single.action, single.headline, single.lines) == (
        "high", "胸痛：立即120转上级医院", "胸痛：立即120转上级医院", ["重度异常：胸痛：立即120转上级医院"])
    blank = abnormal_outcome([{**CHEST, "action": ""}], ALL_YES)
    assert (blank.action, blank.headline) == ("", "重度异常")
    assert abnormal_outcome([CHEST], {"chest": "无"}) == ("none", "", "", [])


def test_预置慢病问卷收缩压190_回执与原先一字不差():
    """同一道题分档写的规则：190 同时命中「≥180 重度」「≥160 中度」，回执与标题只有重度那条（修前就是这样），
    「两周内复诊调整用药」只进处置任务的明细——拼进回执就与重度的「立即上转评估」自相矛盾。"""
    from app.spd.routers.followup import abnormal_outcome
    from app.spd.rules import grade_abnormal
    from app.spd.seed import SEED_QUESTIONNAIRES

    rules = next(q for q in SEED_QUESTIONNAIRES if q["code"] == "q_chronic")["abnormal_rules"]
    outcome = abnormal_outcome(rules, {"bp_sys": 190})
    assert grade_abnormal(rules, {"bp_sys": 190}) == ("high", "立即上转评估")   # 修前的回执
    assert (outcome.level, outcome.action, outcome.headline) == ("high", "立即上转评估", "立即上转评估")
    assert outcome.lines == ["重度异常：立即上转评估", "中度异常：两周内复诊调整用药"]


# ================================================================ 端点：医护执行与居民自助作答
@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21635 随访院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21635 居民", "id_card": "330106197001011635", "gender": "男",
        "birth_date": "1970-01-01", "phone": PHONE}).json()["id"]
    for code, rules in (("P21635_AB", [CHEST, WOUND, FEVER]), ("P21635_BA", [WOUND, CHEST, FEVER]),
                        ("P21635_ONE", [CHEST]), ("P21635_BLANK", [{**CHEST, "action": ""}])):
        created = client.post(f"{B}/questionnaires", headers=admin, json={
            "code": code, "name": code, "items": ITEMS, "abnormal_rules": rules})
        assert created.status_code == 201, created.text
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    ph = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", json={"name": "P21635 居民", "id_card": "330106197001011635"},
                        headers=ph)
    assert bound.status_code in (200, 409), bound.text
    return {"org": org, "patient": patient, "ph": ph}


def _record(world, questionnaire_code):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], questionnaire_code=questionnaire_code,
                                   org_id=world["org"], planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        return record.id


def _new_tasks(world, seen):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        rows = (db.query(SpdTask).filter(SpdTask.patient_id == world["patient"], SpdTask.source == "followup",
                                         SpdTask.id.notin_(seen or [0])).order_by(SpdTask.id).all())
        out = [{"id": t.id, "title": t.title, "priority": t.priority, "due_date": t.due_date, "form": t.form or {},
                "result": t.result or {}} for t in rows]
    seen.extend(t["id"] for t in out)
    return out


def _execute(client, admin, world, questionnaire_code):
    resp = client.post(f"{B}/followup-records/{_record(world, questionnaire_code)}/execute", headers=admin,
                       json={"answers": ALL_YES})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_医护执行_两种规则顺序派出的任务都带两条重度措施_中度那条也在(client, admin, world):
    seen: list[int] = []
    out_ab = _execute(client, admin, world, "P21635_AB")
    (task_ab,) = _new_tasks(world, seen)
    out_ba = _execute(client, admin, world, "P21635_BA")
    (task_ba,) = _new_tasks(world, seen)
    assert out_ab["abnormal_level"] == out_ba["abnormal_level"] == "high"
    for task in (task_ab, task_ba):
        # 修前：一条只有「胸痛：立即120转上级医院」，另一条只有「切口渗液：安排返院清创」，两条都没有发热
        assert "胸痛：立即120转上级医院" in task["title"] and "切口渗液：安排返院清创" in task["title"], task
        assert task["priority"] == 3
    assert set(task_ab["form"]["abnormal_actions"]) == set(task_ba["form"]["abnormal_actions"]) == {
        "重度异常：胸痛：立即120转上级医院", "重度异常：切口渗液：安排返院清创", "中度异常：发热：24小时内回访"}
    assert task_ab["form"]["abnormal_actions"][-1] == "中度异常：发热：24小时内回访"   # 级别降序：中度排在重度之后
    for out in (out_ab, out_ba):
        # 回执合并两条重度（修前只有先写的那条），中度的「发热」只进任务明细
        assert set(out["action"].split("；")) == {"胸痛：立即120转上级医院", "切口渗液：安排返院清创"}, out["action"]


def test_医护执行_只命中一条时任务与回执字节不变(client, admin, world):
    """特征化：只命中一条的标题、回执照原先的拼法，`form` 不写（与修前一字不差）。"""
    seen: list[int] = []
    _new_tasks(world, seen)
    out = _execute(client, admin, world, "P21635_ONE")
    (task,) = _new_tasks(world, seen)
    assert out["action"] == "胸痛：立即120转上级医院"
    assert (task["title"], task["priority"], task["form"], task["result"]) == (
        "随访异常处置：胸痛：立即120转上级医院", 3, {}, {})
    assert task["due_date"] == (clock.today() + timedelta(days=1)).isoformat()
    out = _execute(client, admin, world, "P21635_BLANK")
    (task,) = _new_tasks(world, seen)
    assert (out["action"], task["title"], task["form"]) == ("", "随访异常处置：重度异常", {})


def test_居民自助作答_提示合并两条重度_任务明细列全(client, world):
    seen: list[int] = []
    _new_tasks(world, seen)
    resp = client.post(f"{P}/followups/{_record(world, 'P21635_BA')}/self-answer", headers=world["ph"],
                       json={"patient_id": world["patient"], "answers": ALL_YES})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["abnormal_level"] == "high"
    # 手机上弹的就是 action：修前只有「切口渗液：安排返院清创」；低一级的「发热」不进提示
    assert body["action"] == "切口渗液：安排返院清创；胸痛：立即120转上级医院"
    (task,) = _new_tasks(world, seen)
    assert task["title"] == "自助随访异常处置：切口渗液：安排返院清创；胸痛：立即120转上级医院"
    assert task["form"] == {"abnormal_actions": [
        "重度异常：切口渗液：安排返院清创", "重度异常：胸痛：立即120转上级医院", "中度异常：发热：24小时内回访"]}


def test_医护执行_预置慢病问卷收缩压190_回执与标题字节不变_中度只进明细(client, admin, world):
    seen: list[int] = []
    _new_tasks(world, seen)
    resp = client.post(f"{B}/followup-records/{_record(world, 'q_chronic')}/execute", headers=admin,
                       json={"answers": {"bp_sys": 190}})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["abnormal_level"], resp.json()["action"]) == ("high", "立即上转评估")   # 与修前一字不差
    (task,) = _new_tasks(world, seen)
    assert task["title"] == "随访异常处置：立即上转评估"
    assert task["form"] == {"abnormal_actions": ["重度异常：立即上转评估", "中度异常：两周内复诊调整用药"]}


def test_居民自助作答_只命中一条时字节不变(client, world):
    seen: list[int] = []
    _new_tasks(world, seen)
    resp = client.post(f"{P}/followups/{_record(world, 'P21635_ONE')}/self-answer", headers=world["ph"],
                       json={"patient_id": world["patient"], "answers": ALL_YES})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": resp.json()["id"], "abnormal_level": "high", "abnormal_level_name": "重度",
                           "action": "胸痛：立即120转上级医院"}
    (task,) = _new_tasks(world, seen)
    assert (task["title"], task["form"]) == ("自助随访异常处置：胸痛：立即120转上级医院", {})
