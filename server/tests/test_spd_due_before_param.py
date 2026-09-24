"""「到期不晚于」筛选先校验日期再比（P1-87）。

在管患者清单 `GET /api/spd/enrollments?due_before=` 与任务清单 `GET /api/spd/tasks?due_before=` 原先把
`due_before` 原样拿去比 `YYYY-MM-DD` 的日期列（字符串比较）。写法一变就静默筛错：`2026/09/30`
里的 `/` 比 `-` 大，于是 2026 年里**所有**日期都「不晚于」它——筛出来的是全年，而调用方以为
是九月底之前的。日期查询参数的棘轮（`test_date_query_params.py`）按名字认日期参数，
`due_before` 不在名单上，P1-58 清零时两处都漏在外面；判据已补上 `_before` / `_after` 后缀。

修法：与其余日期查询参数同一个口径，`require_date` 校验，非法 422；合法值照旧筛。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Organization, Patient
from app.spd.models import SpdEnrollment, SpdTask

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    """两条在管纳管、两条任务：一条九月到期，一条十二月到期。"""
    with SessionLocal() as db:
        org = Organization(name="到期筛选卫生院", org_type="township", level="township")
        db.add(org)
        db.flush()
        db.execute(insert(Patient), [
            {"ehc_no": f"EHC-DUE-{i}", "name": f"到期筛选{i}", "id_card": f"DUE{i:015d}", "gender": "男",
             "birth_date": "1960-01-01"} for i in range(2)])
        pids = [pid for (pid,) in db.query(Patient.id).filter(Patient.ehc_no.like("EHC-DUE-%")).order_by(Patient.id)]
        db.execute(insert(SpdEnrollment), [
            {"patient_id": pid, "program_code": "due_prog", "org_id": org.id, "status": "active",
             "next_followup_at": due} for pid, due in zip(pids, ("2031-09-20", "2031-12-20"))])
        db.execute(insert(SpdTask), [
            {"patient_id": pid, "program_code": "due_prog", "org_id": org.id, "task_type": "followup",
             "title": f"到期筛选任务{due}", "status": "pending", "due_date": due}
            for pid, due in zip(pids, ("2031-09-20", "2031-12-20"))])
        db.commit()


LISTS = [
    (f"{B}/enrollments", "next_followup_at"),
    (f"{B}/tasks", "due_date"),
]


@pytest.mark.parametrize("path,field", LISTS)
def test_特征化_合法日期照旧只筛到不晚于它的(client, admin, world, path, field):
    r = client.get(path, params={"program_code": "due_prog", "due_before": "2031-09-30"}, headers=admin)
    assert r.status_code == 200, r.text
    assert [row[field] for row in r.json()] == ["2031-09-20"]


@pytest.mark.parametrize("path,field", LISTS)
@pytest.mark.parametrize("bad", ["2031/09/30", "2031-9-30", "不是日期", "2031-02-31"])
def test_写法不对的日期_422而不是静默筛错(client, admin, world, path, field, bad):
    r = client.get(path, params={"program_code": "due_prog", "due_before": bad}, headers=admin)
    assert r.status_code == 422, (bad, r.status_code, r.text[:200])
    assert "due_before" in r.text
