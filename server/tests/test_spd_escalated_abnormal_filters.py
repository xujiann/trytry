"""任务中心「已升级」、随访看板「异常随访」两张标红卡片点得进清单：清单取到的与卡片同一句判据（P2-1318，第三十八批扫描 AB1-3 之三）。

任务中心「已升级」卡片只数未结束的里头升级过的（P2-247），页面筛选栏却没有这一项，导出也不认 `escalated` / `open_only`；
随访看板「异常随访」卡片 = 中度 + 重度（`followup_abnormal`，P2-292），随访清单的 `abnormal_level` 只收单个等级，取不出这一格。
两张卡片标红了却点不进去。

修后「已升级」卡片点进清单走既有参数的组合 `escalated=true&open_only=true`——卡片计数（`service.task_escalated_open`，
中心端 / 医生移动端工作台的升级计数同用）与这个组合逐句相同；`escalated` 是公共参数，单用照旧按标记筛、不分状态（督办复盘
要查「升级过、已办结」的照样查得到），语义不改。导出补上 `escalated` / `open_only` 两个参数，与清单逐句相同；任务中心加
「只看已升级」，勾上送这两个参数，导出跟着走。随访清单加 `abnormal=true`，判据与卡片同一句；随访看板加「只看异常」。

API 层「卡片 = `escalated=true&open_only=true`」修前就相等（清单早有 `open_only`，这正是不必改语义的依据）——修前红的是页面
不送 `open_only`、导出不认这两个参数、随访清单取不出「异常」这一格。
"""
import re
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    from app import clock
    from app.spd.models import SpdFollowupRecord, SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21318 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = client.post("/api/users", headers=admin, json={
        "username": "p21318_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "P21318 医生"})
    assert made.status_code == 201, made.text
    doctor = made.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21318 患者", "id_card": "330106197001011318"}).json()["id"]
    today = clock.today().isoformat()
    with SessionLocal() as db:
        tasks = {key: SpdTask(patient_id=patient, title=f"P21318 {key}", org_id=org, assignee_id=doctor, status=status,
                              escalated=escalated, priority=2 if escalated else 1, due_date=today)
                 for key, status, escalated in (("claimed", "claimed", True), ("done", "done", True),
                                                ("cancelled", "cancelled", True), ("plain", "pending", False))}
        records = {level or "blank": SpdFollowupRecord(patient_id=patient, org_id=org, planned_at=today, executor_id=doctor,
                                                       status="done" if level else "planned", abnormal_level=level,
                                                       scene="outpatient")
                   for level in ("mid", "high", "low", "none", "")}
        db.add_all([*tasks.values(), *records.values()])
        db.commit()
        ids = {"tasks": {k: t.id for k, t in tasks.items()}, "records": {k: r.id for k, r in records.items()}}
    return {"h": login(client, "p21318_doc", "passw0rd1"), "ids": ids}


def _listed(client, world, path, **params):
    resp = client.get(f"{B}/{path}", headers=world["h"], params={"limit": 100, **params})
    assert resp.status_code == 200, resp.text
    return int(resp.headers["X-Total-Count"]), {r["id"] for r in resp.json()}


def test_已升级卡片等于清单的escalated加open_only(client, world):
    card = client.get(f"{B}/tasks/summary", headers=world["h"]).json()["escalated"]
    total, ids = _listed(client, world, "tasks", escalated="true", open_only="true")
    assert total == card == 1
    assert ids == {world["ids"]["tasks"]["claimed"]}


def test_escalated单用照旧返回全部升级任务_语义不改(client, world):
    """兼容性特征化：`escalated` 是公共参数，单用按标记筛、不分状态——已办结、已取消的升级任务照样返回（修前修后都绿）。"""
    total, ids = _listed(client, world, "tasks", escalated="true")
    assert total == 3
    assert ids == {world["ids"]["tasks"][k] for k in ("claimed", "done", "cancelled")}
    _, done = _listed(client, world, "tasks", escalated="true", status="done")
    assert done == {world["ids"]["tasks"]["done"]}   # 督办复盘要查的「升级过、已办结」


def test_导出跟着清单走_两个参数都认(client, world):
    params = {"escalated": "true", "open_only": "true"}
    exported = client.get(f"{B}/tasks-export", headers=world["h"], params=params)
    assert exported.status_code == 200, exported.text
    assert exported.json()["matched"] == _listed(client, world, "tasks", **params)[0] == 1   # 修前导出两个都不认：4
    assert {row[0] for row in exported.json()["rows"]} == {world["ids"]["tasks"]["claimed"]}
    only_escalated = client.get(f"{B}/tasks-export", headers=world["h"], params={"escalated": "true"}).json()
    assert only_escalated["matched"] == 3   # 与清单单用 escalated 同一个语义


def test_escalated_false照旧只按标记筛(client, world):
    _, ids = _listed(client, world, "tasks", escalated="false")
    assert world["ids"]["tasks"]["plain"] in ids
    assert not ids & {world["ids"]["tasks"][k] for k in ("claimed", "done", "cancelled")}


def test_异常随访卡片等于清单的总数_中度加重度(client, world):
    card = client.get(f"{B}/followup-stats", headers=world["h"]).json()["abnormal"]
    total, ids = _listed(client, world, "followup-records", abnormal="true")
    assert total == card == 2   # 修前清单不认这个参数：五条全回来
    assert ids == {world["ids"]["records"][k] for k in ("mid", "high")}


def _block(anchor: str) -> str:
    start = PAGE.index(anchor)
    return PAGE[start:PAGE.index("\n  };\n", start)]


def test_任务中心只看已升级_勾上送escalated与open_only_导出同一处():
    start = PAGE.index('<form class="inline" id="spd-task-filter">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<input type="checkbox" name="escalated" value="true"> 只看已升级' in form   # 修前没有
    helper = _block("const taskFilters = () => {")
    assert 'const q = formJson($("#spd-task-filter"));' in helper
    assert 'if (q.escalated) q.open_only = "true";' in helper   # 两个参数一起送，合起来是卡片那一句
    assert "await drawTasks(taskFilters())" in _block('$("#spd-task-filter").onsubmit')
    export = PAGE[PAGE.index("if (exportBtn) {"):]
    export = export[:export.index("return;")]
    assert "const filters = taskFilters();" in export   # 导出跟着表格走


def test_随访看板只看异常():
    start = PAGE.index('<form class="inline" id="spd-fu-filter">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<input type="checkbox" name="abnormal" value="true"> 只看异常' in form   # 修前没有
    assert re.search(r"drawRecords\(spdDayRange\(formJson\(e\.target\)\)\)", PAGE)   # 勾选项随表单一起送
