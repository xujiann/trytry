"""积分账户「查不到就建」：同一个人头一回同时来两笔，后建的撞 `user_id` 唯一约束（P2-336）。

入账（`service.award_points`）与每日签到各写一份「查不到就 `add` + `flush`」：两路都查不到、都去建，后建的抛
`IntegrityError`——签到是 500；入账那一路是转诊到院、随访办结这些业务动作的一部分，整个动作跟着回滚。

修法：`service.point_account_for`，建账户走 `insert_if_absent`（撞了就用对方建好的那个）。这里把「查过没有、还没建」
钉成确定的时序：在这一路 `.first()` 查完（查不到）之后，让另一路先把账户建好提交。
"""
import ast
from pathlib import Path

import pytest
from conftest import login
from sqlalchemy.orm import Session

from app.database import SessionLocal


def _race_after_lookup(monkeypatch, user_id):
    """这一路查积分账户查不到之后、自己去建之前，另一路先把这位用户的账户建好提交。返回「插过没有」的记号。"""
    from app.spd.models import SpdPointAccount

    real, fired = Session.query, []

    class Racing:
        def __init__(self, query):
            self._query = query

        def filter(self, *args, **kwargs):
            return Racing(self._query.filter(*args, **kwargs))

        def first(self):
            row = self._query.first()
            if row is None and not fired:
                fired.append(True)
                with SessionLocal() as other:
                    other.add(SpdPointAccount(user_id=user_id, org_id=None, balance=0, earned=0, used=0))
                    other.commit()
            return row

    def query(self, *entities, **kwargs):
        built = real(self, *entities, **kwargs)
        return Racing(built) if entities and entities[0] is SpdPointAccount and not fired else built

    monkeypatch.setattr(Session, "query", query)
    return fired


def _accounts(user_id):
    from app.spd.models import SpdPointAccount

    with SessionLocal() as db:
        return db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user_id).count()


def _new_user(client, admin, username):
    created = client.post("/api/users", headers=admin, json={
        "username": username, "password": "pw123456", "full_name": username, "role": "doctor"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


@pytest.fixture(scope="module", autouse=True)
def rules(client, admin):
    for code, event in (("P2336_SIGNIN", "signin"), ("P2336_FU", "followup")):
        got = client.post("/api/spd/point-rules", headers=admin,
                          json={"code": code, "name": f"P2336 {event}", "event": event, "points": 2})
        assert got.status_code in (201, 409), got.text


def test_头一回签到撞上并发建账户_不再500(client, admin, monkeypatch):
    user_id = _new_user(client, admin, "p2336_signin")
    headers = login(client, "p2336_signin", "pw123456")
    fired = _race_after_lookup(monkeypatch, user_id)
    got = client.post("/api/spd/point-accounts/signin", headers=headers)
    monkeypatch.undo()
    assert fired
    assert got.status_code == 200, got.text   # 修前：IntegrityError（500）
    assert got.json()["balance"] == got.json()["points"]
    assert _accounts(user_id) == 1


def test_头一回入账撞上并发建账户_入账照常(client, admin, monkeypatch):
    from app.spd.service import award_points

    user_id = _new_user(client, admin, "p2336_award")
    fired = _race_after_lookup(monkeypatch, user_id)
    with SessionLocal() as db:
        record = award_points(db, user_id, "followup", ref_type="task", ref_id=None, note="P2336")
        db.commit()
        points = record.points if record is not None else None
    monkeypatch.undo()
    assert fired
    assert points is not None   # 修前：IntegrityError，整个业务动作回滚
    assert _accounts(user_id) == 1


def test_积分账户只在一处建():
    """静态钉：`SpdPointAccount(...)` 只在 `service.point_account_for` 里构造（路由那条防复发闸门只扫路由，看不见 service 里的写入点）。"""
    spd = Path(__file__).resolve().parents[1] / "app" / "spd"
    sites = []
    for path in sorted(spd.rglob("*.py")):
        for func in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SpdPointAccount":
                    sites.append(f"{path.relative_to(spd).as_posix()}::{func.name}")
    assert sites == ["service.py::point_account_for"], sites
