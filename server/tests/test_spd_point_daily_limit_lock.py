"""积分每日上限的判定与入账在账户行的锁里，并发入账不再一起越过上限（P2-1581，第四十六批扫描 AJ4-5）。

`service.award_points` 原先先 `SELECT SUM` 判当天该规则已入账分值到没到上限，到 `add_amount` 才第一次锁账户行；取账户的
`point_account_for` 是普通 SELECT。PG 的 READ COMMITTED 下同一村医两笔入账同时到（两位患者的随访同时办结），两边的求和都看不到
对方未提交的流水、都过判断，后到的一路在 `add_amount` 上等前一路提交后照样入账——上限 30 分的规则当天能记到 33 分。
`concurrency.py` 写明「判定与扣减必须在同一条 SQL 里」，P1-117 同族。扫描按代码读出；修复时在开发库上按确定时序复现了同一个
形状（A 过了判定停住、B 整笔入账并提交、再放 A）：上限 9 分（单次 3 分）、已入账 6 分，A、B 都入了账，当天 12 分。

修法：判上限之前先 `SELECT … FOR UPDATE` 锁住账户行，同一账户的判定与入账排队。SQLite 方言不渲染 FOR UPDATE、开发库上行为
不变，所以这一档钉两件事：上限照旧生效；按 PG 方言编译，锁账户行那一句发在求和之前。真并发取证在
`test_spd_point_daily_limit_races.py`（真 PostgreSQL，默认跳过）。
"""
import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql

from app.database import SessionLocal
from app.spd.models import SpdPointRecord
from app.spd.service import award_points

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    """一位村医；「随访完成」规则的每日上限设为单次分的 3 倍。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21581 村卫生室", "org_type": "village", "level": "village"}).json()["id"]
    user = client.post("/api/users", headers=admin, json={
        "username": "p21581_vd", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "P21581 村医"})
    assert user.status_code == 201, user.text
    rule = next(r for r in client.get(f"{B}/point-rules", headers=admin).json() if r["event"] == "followup")
    patched = client.patch(f"{B}/point-rules/{rule['id']}", headers=admin, json={"daily_limit": rule["points"] * 3})
    assert patched.status_code == 200, patched.text
    return {"user": user.json()["id"], "org": org, "code": rule["code"], "points": rule["points"]}


def _award(world, ref_id):
    with SessionLocal() as db:
        record = award_points(db, world["user"], "followup", ref_type="p21581", ref_id=ref_id, org_id=world["org"])
        db.commit()
        return record is not None


def test_上限照旧生效_第四笔不入账(world):
    assert [_award(world, n) for n in range(4)] == [True, True, True, False]
    with SessionLocal() as db:
        points = [r.points for r in db.query(SpdPointRecord).filter(SpdPointRecord.rule_code == world["code"])]
    assert sum(points) == world["points"] * 3, points


def test_按PG方言_锁账户行发在求和之前(world):
    """开发库上这一句不带 FOR UPDATE（方言不渲染），所以按 PG 方言把本次入账发出的语句逐条编译出来看先后。"""
    sent: list[str] = []
    with SessionLocal() as db:
        event.listen(db, "do_orm_execute",
                     lambda state: sent.append(str(state.statement.compile(dialect=postgresql.dialect()))))
        award_points(db, world["user"], "followup", ref_type="p21581", ref_id=99, org_id=world["org"])
        db.rollback()
    lock = next((i for i, sql in enumerate(sent) if "FROM spd_point_accounts" in sql and "FOR UPDATE" in sql), None)
    total = next(i for i, sql in enumerate(sent) if "sum(spd_point_records.points)" in sql)
    assert lock is not None and lock < total, sent   # 修前没有这一句：求和与入账之间谁都能插进来
