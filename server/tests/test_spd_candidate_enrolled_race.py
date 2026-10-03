"""目标池「已纳管」被并发的改状态 / 复核 / 批量识别写回疑似、目标或排除（P2-1177，第三十四批扫描 L1-9）。

签约建档把池里这一行置为「已纳管」（`create_enrollment`）。另三处写池行都是锁外判「不是已纳管」、之后往对象上赋值，flush
出来的 UPDATE 只有 `WHERE id = ?`：

- 改状态（`set_candidate_status`）：读到疑似之后别人刚建档并提交，修前照样 200、池行写成排除，档案却在管；池行不再是
  已纳管，「已纳管患者请走生命周期接口调整」那道 409 此后也挡不住了——再改照样 200（实测）。
- 复核（`review_screening`）：确认 / 排除把池行写成目标 / 排除，同一个窗口。
- 批量识别（`auto_screen` → `_upsert_candidate`，docstring 写「已纳管的不回退状态」）：改过的池行要等下一个命中纳入规则的
  患者才 flush，中间每个患者十余次查询，窗口更宽。

修法：三处都改成带 `status != 'enrolled'` 的条件 UPDATE（`concurrency.move_row`）。改状态抢输了回滚、409（与顺序发生时同一句）；
复核与批量识别抢输的不动池行（与顺序发生时「已纳管的不动」同一个结果）。

时序钉法：改状态照 `test_spd_lifecycle_death_race._dies_meanwhile`，在判定之后、写入之前另一个会话里跑真实的建档。复核与批量
识别读池行之前已有写入（复核先翻筛查、批量先落筛查），SQLite 的库级写锁让另一个会话在这之间提交不了（PG 上行锁不相干、
提交得了，扫描报告也是按代码读的），这里在同一条连接上、池行 UPDATE 发出之前把它置为已纳管，钉住条件写法。
"""
import pytest

from test_spd_service_apply_handle_race import _enroll_direct, enrolled_before_pool_write

from app.database import SessionLocal

B = "/api/spd"
YES = {"family": "是", "salt": "是", "overweight": "是", "symptom": "是"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21177 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _patient(client, admin, world):
    world["n"] += 1
    made = client.post("/api/patients", headers=admin, json={
        "name": f"P21177 患者{world['n']}", "id_card": f"33010619700101{world['n']:04d}", "birth_date": "1970-01-01"})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


def _screened(client, admin, world):
    """一位患者筛查为疑似、进了目标池；返回（筛查编号, 池行编号, 患者编号）。"""
    from app.spd.models import SpdCandidate

    patient = _patient(client, admin, world)
    got = client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"],
        "scale_code": "scr_hypertension", "answers": YES})
    assert got.status_code == 201 and got.json()["result"] == "suspect", got.text
    with SessionLocal() as db:
        candidate = db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient).one()
        assert candidate.status == "suspect"
        return got.json()["id"], candidate.id, patient


def _statuses(patient_id):
    """（池行状态, 档案状态）。"""
    from app.spd.models import SpdCandidate, SpdEnrollment

    with SessionLocal() as db:
        return ([c.status for c in db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient_id)],
                [e.status for e in db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient_id)])


def test_改状态与签约建档并发_409_池行仍是已纳管(client, admin, world, monkeypatch):
    from app.spd.routers import population

    _, candidate, patient = _screened(client, admin, world)
    real, fired = population.assert_org_writable, []

    def enrolled_meanwhile(*args, **kwargs):   # 取到池行之后、写入之前（判定用的是先前取到的那份）
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            _enroll_direct(patient, world["org"])
        return result

    monkeypatch.setattr(population, "assert_org_writable", enrolled_meanwhile)
    resp = client.post(f"{B}/candidates/{candidate}/status", headers=admin,
                       json={"status": "excluded", "reason": "本人拒绝管理"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200 excluded
    assert resp.json() == {"detail": "已纳管患者请走生命周期接口调整"}   # 与顺序发生时同一句
    assert _statuses(patient) == (["enrolled"], ["active"])   # 修前池行 excluded、档案 active
    again = client.post(f"{B}/candidates/{candidate}/status", headers=admin, json={"status": "target"})
    assert again.status_code == 409, again.text   # 修前池行已不是 enrolled，这道 409 也挡不住：200 target


@pytest.mark.parametrize("review_result", ["confirmed", "excluded"])
def test_复核写池行之前池行刚被置为已纳管_不写回(client, admin, world, review_result):
    screening, candidate, patient = _screened(client, admin, world)
    with enrolled_before_pool_write(candidate) as fired:
        resp = client.post(f"{B}/screenings/{screening}/review", headers=admin,
                           json={"review_result": review_result, "review_note": "P21177"})
    assert fired
    assert resp.status_code == 200, resp.text   # 与顺序发生时一样：复核照记，已纳管的池行不动
    assert resp.json()["review_result"] == review_result
    assert _statuses(patient)[0] == ["enrolled"]   # 修前写回 target / excluded


def test_批量识别写池行之前池行刚被置为已纳管_不回退(client, admin, world):
    from app.spd.models import SpdCandidate

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21177 批量识别卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = _patient(client, admin, world)
    visit = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
    assert visit.status_code in (200, 201), visit.text
    first = client.post(f"{B}/screenings/auto-run", headers=admin, json={"program_code": "hypertension", "org_id": org})
    assert first.status_code == 200 and first.json()["suspect"] == 1, first.text
    with SessionLocal() as db:   # 池行已被分发成目标（批量再识别要把它写回疑似，状态真的变了才会整行写）
        row = db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient).one()
        row.status = "target"
        db.commit()
        candidate = row.id
    with enrolled_before_pool_write(candidate) as fired:
        again = client.post(f"{B}/screenings/auto-run", headers=admin,
                            json={"program_code": "hypertension", "org_id": org})
    assert fired
    assert again.status_code == 200 and again.json()["suspect"] == 1, again.text
    assert _statuses(patient)[0] == ["enrolled"]   # 修前写回 suspect


def test_没有竞争时照常改状态_已纳管的照旧409(client, admin, world):
    _, candidate, patient = _screened(client, admin, world)
    resp = client.post(f"{B}/candidates/{candidate}/status", headers=admin,
                       json={"status": "excluded", "reason": "本人拒绝管理"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["status"], resp.json()["reason"]) == ("excluded", "本人拒绝管理")
    resp = client.post(f"{B}/candidates/{candidate}/status", headers=admin, json={"status": "target"})
    assert resp.status_code == 200 and resp.json()["status"] == "target", resp.text
    assert resp.json()["reason"] == "本人拒绝管理"   # 没给理由的不动理由
    _enroll_direct(patient, world["org"])
    blocked = client.post(f"{B}/candidates/{candidate}/status", headers=admin, json={"status": "excluded"})
    assert blocked.status_code == 409 and blocked.json() == {"detail": "已纳管患者请走生命周期接口调整"}
