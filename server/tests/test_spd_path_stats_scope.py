"""工作台的路径统计不再把统计范围内的纳管整表读进内存（P2-44）。

`spd/workbench.py:_path_stats` 供卫健、区域结构、专家、团队四个工作台用，原先先把统计范围内的
**全部**纳管记录整行查出来（`_apply_scope(db.query(SpdEnrollment), ...).all()`），再把它们的 id 拼成
一条 `SpdPathInstance.enrollment_id IN (...)`。全域视角（卫健、admin）就是整张纳管表：县域纳管
量上来之后，工作台每刷一次都要把十几万行读进内存、再拼一条十几万个参数的 SQL（与 P1-51 修掉的
`Patient.id.in_([...两万个参数])` 同一个形状）。

修法：换成子查询 `IN (SELECT id FROM spd_enrollments WHERE ...)`，范围条件一字不改；统计数字不变
（特征化用例钉住）。缺陷回归看的是**绑定参数的个数**：纳管 300 条时，原实现有一条语句带 300 多个
参数，修后任何一条语句的参数个数都与纳管量无关。
"""
import pytest
from sqlalchemy import event, insert

from app.database import SessionLocal, engine
from app.models import Organization, Patient
from app.spd.models import SpdEnrollment, SpdPathInstance

BULK = 300


def _paths(client, admin, **params):
    r = client.get("/api/spd/workbench/health-commission", params=params, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()["paths"]


@pytest.fixture(scope="module")
def world(client, admin):
    """甲院两条纳管（路径一在办一完成）、乙院一条（在办）；甲院另有 `BULK` 条没入径的纳管。"""
    with SessionLocal() as db:
        a = Organization(name="路径统计甲院", org_type="township", level="township")
        b = Organization(name="路径统计乙院", org_type="township", level="township")
        db.add_all([a, b])
        db.flush()
        db.execute(insert(Patient), [
            {"ehc_no": f"EHC-PS-{i}", "name": f"路径统计{i}", "id_card": f"PS{i:016d}", "gender": "女",
             "birth_date": "1955-05-05"} for i in range(3 + BULK)])
        pids = [pid for (pid,) in db.query(Patient.id).filter(Patient.ehc_no.like("EHC-PS-%")).order_by(Patient.id)]
        orgs = [a.id, a.id, b.id] + [a.id] * BULK
        db.execute(insert(SpdEnrollment), [
            {"patient_id": pid, "program_code": "ps_prog", "org_id": org, "status": "active"}
            for pid, org in zip(pids, orgs)])
        eids = [eid for (eid,) in db.query(SpdEnrollment.id).filter(SpdEnrollment.program_code == "ps_prog")
                .order_by(SpdEnrollment.id)]
        # 测试库不开外键约束，模板号给个占位即可（与 test_spd_enrollment_org_guard 同一做法）
        db.execute(insert(SpdPathInstance), [
            {"enrollment_id": eids[0], "template_id": 1, "status": "running"},
            {"enrollment_id": eids[1], "template_id": 1, "status": "completed"},
            {"enrollment_id": eids[2], "template_id": 1, "status": "running"},
        ])
        db.commit()
        return {"a": a.id, "b": b.id}


def test_特征化_全域与按机构的路径统计(client, admin, world):
    assert _paths(client, admin) == {"total": 3, "running": 2, "completed": 1, "completion_rate": 33.3}
    assert _paths(client, admin, org_id=world["a"]) == {"total": 2, "running": 1, "completed": 1,
                                                          "completion_rate": 50.0}
    assert _paths(client, admin, org_id=world["b"]) == {"total": 1, "running": 1, "completed": 0,
                                                          "completion_rate": 0.0}


def test_不再把纳管号逐个绑定进一条语句(client, admin, world):
    widest = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if not executemany and parameters:
            widest.append((len(parameters), statement[:120]))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        _paths(client, admin)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    n, statement = max(widest)
    assert n < 50, f"有一条语句带了 {n} 个参数，随纳管量增长：{statement}"
