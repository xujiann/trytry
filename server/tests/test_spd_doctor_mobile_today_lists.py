"""医生移动端「今日随访 / 今日复诊」有清单可看、桌面两块看板筛得出「本人 + 当天」（P2-1317，第三十八批扫描 AB1-3 之二）。

医生移动端工作台报「今日随访 1 / 今日复诊 1」，`m/doctor.js` 与 `m/doctor.html` 只有待办、转诊、患者、积分四段，全文件不调
随访记录与复诊清单（`care.list_revisits` 的 docstring 自称服务「医生移动端 #12」）——是哪几位在手机上无处可看。桌面的随访
看板只有状态 / 场景 / 只看超期，复诊看板只有状态 / 只看逾期，筛不出「本人 + 今天」：随访清单早有 `mine` / `date_from` /
`date_to`，页面不传；复诊清单只有要填账号编号的 `doctor_user_id`，管理端页面拿不到本人编号。日历计数还只数没做完的
（P2-734），两份清单却没有「只列没做完的」，同一个日子取回来的条数与计数对不上。

修后随访、复诊两份清单各加 `open_only`（与日历计数同一份 `FOLLOWUP_OPEN_STATUSES` / `REVISIT_OPEN_STATUSES`），复诊清单加
`mine`（复诊医生是本人，与日历同一句）；医生移动端加「今日随访」「今日复诊」两段，按「本人 + 工作台的今天 + 没做完」取；
桌面两块看板加「只看本人」与「计划日」，送既有的 `mine` / `date_from` / `date_to`。
"""
import re
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PAGE = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
DOCTOR_JS = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
DOCTOR_HTML = (STATIC / "m" / "doctor.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    """今天的随访 / 复诊各六条、五条，只有「本人 + 今天 + 没做完」的两条算进日历：待随访 / 已排期一条、已超期一条
    （超期扫描按截止日置状态，计数认它），其余是已完成 / 已移除 / 明天的 / 别人的。"""
    from app import clock
    from app.spd.models import SpdFollowupRecord, SpdRevisit

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21317 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    users = {}
    for key in ("doc", "other"):
        made = client.post("/api/users", headers=admin, json={
            "username": f"p21317_{key}", "password": "passw0rd1", "role": "doctor", "org_id": org,
            "full_name": f"P21317 {key}"})
        assert made.status_code == 201, made.text
        users[key] = made.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21317 患者", "id_card": "330106197001011317"}).json()["id"]
    made = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org, "doctor_user_id": users["doc"]})
    assert made.status_code == 201, made.text   # 在本机构建了档：复诊清单按患者可见性收口，医生看得见这位患者
    today = clock.today()
    day, tomorrow = today.isoformat(), (today + timedelta(days=1)).isoformat()
    with SessionLocal() as db:
        followups = {key: SpdFollowupRecord(patient_id=patient, program_code="hypertension", org_id=org,
                                            planned_at=planned, executor_id=users[who], status=status)
                     for key, planned, who, status in (
                         ("planned", day, "doc", "planned"), ("overdue", day, "doc", "overdue"),
                         ("done", day, "doc", "done"), ("removed", day, "doc", "removed"),
                         ("tomorrow", tomorrow, "doc", "planned"), ("other", day, "other", "planned"))}
        revisits = {key: SpdRevisit(patient_id=patient, program_code="hypertension", plan_date=planned,
                                    doctor_user_id=users[who], status=status)
                    for key, planned, who, status in (
                        ("planned", day, "doc", "planned"), ("overdue", day, "doc", "overdue"),
                        ("done", day, "doc", "done"), ("tomorrow", tomorrow, "doc", "planned"),
                        ("other", day, "other", "planned"))}
        db.add_all([*followups.values(), *revisits.values()])
        db.commit()
        ids = {"followups": {k: r.id for k, r in followups.items()}, "revisits": {k: r.id for k, r in revisits.items()}}
    return {"h": login(client, "p21317_doc", "passw0rd1"), "day": day, "ids": ids}


def _listed(client, world, path, **params):
    resp = client.get(f"{B}/{path}", headers=world["h"], params={"limit": 100, **params})
    assert resp.status_code == 200, resp.text
    return int(resp.headers["X-Total-Count"]), {r["id"] for r in resp.json()}


def test_日历计数等于移动端两段清单的条数(client, world):
    calendar = client.get(f"{B}/workbench/doctor-mobile", headers=world["h"]).json()["calendar"]
    assert calendar["today"] == world["day"]
    today = {"mine": "true", "open_only": "true", "date_from": calendar["today"], "date_to": calendar["today"]}
    fu_total, fu_ids = _listed(client, world, "followup-records", **today)
    rv_total, rv_ids = _listed(client, world, "revisits", **today)
    assert (fu_total, rv_total) == (calendar["followups"], calendar["revisits"]) == (2, 2)   # 修前清单 (4, 4)
    assert fu_ids == {world["ids"]["followups"][k] for k in ("planned", "overdue")}
    assert rv_ids == {world["ids"]["revisits"][k] for k in ("planned", "overdue")}


def test_复诊清单只看本人_与日历同一句(client, world):
    _, ids = _listed(client, world, "revisits", mine="true")
    assert ids == {world["ids"]["revisits"][k] for k in ("planned", "overdue", "done", "tomorrow")}   # 修前别人的也在


def test_没做完的参数不带_照旧全部状态(client, world):
    _, ids = _listed(client, world, "followup-records", mine="true", date_from=world["day"], date_to=world["day"])
    assert ids == {world["ids"]["followups"][k] for k in ("planned", "overdue", "done", "removed")}


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start)]


def test_医生移动端有今日随访今日复诊两段_按本人当天没做完取():
    seg = DOCTOR_HTML[DOCTOR_HTML.index('<div class="seg">', DOCTOR_HTML.index('id="tab-spd"')):]
    seg = seg[:seg.index("</div>")]
    assert '<button class="seg-btn" data-dspd="followup">今日随访</button>' in seg   # 修前只有待办、转诊、患者、积分
    assert '<button class="seg-btn" data-dspd="revisit">今日复诊</button>' in seg
    dispatch = _fn(DOCTOR_JS, "loadSpdList")
    assert 'else if (activeDoctorSpd === "followup") await loadSpdTodayFollowups(box);' in dispatch
    assert 'else if (activeDoctorSpd === "revisit") await loadSpdTodayRevisits(box);' in dispatch
    for loader, path in (("loadSpdTodayFollowups", "followup-records"), ("loadSpdTodayRevisits", "revisits")):
        body = _fn(DOCTOR_JS, loader)
        assert f"/api/spd/{path}?mine=true&open_only=true&date_from=${{day}}&date_to=${{day}}" in body, loader
        assert "spdCalendarDay()" in body   # 「今天」取工作台的业务日（与计数同一天），不取手机本地日期
    assert "spdCalendar = wb.calendar;" in _fn(DOCTOR_JS, "loadSpdTab")


def test_桌面随访看板加只看本人与计划日_送既有参数():
    start = PAGE.index('<form class="inline" id="spd-fu-filter">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<input type="checkbox" name="mine" value="true"> 只看本人' in form   # 修前只有状态、场景、只看超期
    assert re.search(r'<input type="date" name="day"[^>]*>', form)
    submit = PAGE[PAGE.index('$("#spd-fu-filter").onsubmit'):]
    submit = submit[:submit.index("\n  };\n")]
    assert "spdDayRange(formJson(e.target))" in submit
    helper = _fn(PAGE, "spdDayRange")
    assert "q.date_from = q.day;" in helper and "q.date_to = q.day;" in helper and "delete q.day;" in helper


def test_桌面复诊看板加只看本人与计划日_送既有参数():
    start = PAGE.index('<form class="inline" id="spd-revisit-filter"')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<input type="checkbox" name="mine" value="true"> 只看本人' in form   # 修前只有状态、只看逾期
    assert re.search(r'<input type="date" name="day"[^>]*>', form)
    submit = PAGE[PAGE.index('$("#spd-revisit-filter").onsubmit'):]
    submit = submit[:submit.index("\n  };\n")]
    assert 'if (e.target.mine.checked) params.set("mine", "true");' in submit
    assert 'params.set("date_from", e.target.day.value);' in submit and 'params.set("date_to", e.target.day.value);' in submit
