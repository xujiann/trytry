"""「当前生效」的积分规则与高危自动干预模板按编号取第一条，不由库的返回次序决定（P2-693，第十七批「最近 / 最新」扫描 U4-5）。

积分入账（`award_points`）、每日签到（`signin`）按事件取启用的积分规则，高危自动干预（`_auto_intervene`）按病种与等级取
启用的模板，三处都是不排序的 `.first()`。积分规则的编码唯一、事件不唯一，同一事件可以有几条启用的规则（「随访完成
3 分」旧规则没停、又建一条「随访完成 5 分」）；PG 不保证无 ORDER BY 的次序，改过的行排到堆尾（P2-304 在真 PG 上实测过）
——改一下旧规则的名称，同样的随访就从 3 分变 5 分、每日上限也跟着换，积分能兑奖品；两套同病种同等级的自动模板开哪套
也是如此。修法与转诊规则试算（P2-304）、随访方案匹配（P2-369）同一个次序：按编号取第一条。

SQLite 无 ORDER BY 时按 rowid 返回、恰好就是编号序，行为上测不出修前修后之分——防回退靠静态钉（与 P2-369 同一做法）；
几条同时启用该怎么算（只许一条 / 几套都开）与 P2-391 同一个口径，另行裁定。
"""
import ast
import inspect
import textwrap

import pytest

from app.database import SessionLocal


def _first_chains(func, model: str) -> list[str]:
    """函数里所有以 `.first()` 收尾、查 `model` 的查询链源码。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return [ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "first"
            and f"query({model})" in ast.unparse(node)]


@pytest.mark.parametrize("module, name, model", [
    ("app.spd.service", "award_points", "SpdPointRule"),
    ("app.spd.routers.assess", "signin", "SpdPointRule"),
    ("app.spd.routers.care", "_auto_intervene", "SpdInterventionTemplate"),
])
def test_按编号取第一条(module, name, model):
    import importlib

    chains = _first_chains(getattr(importlib.import_module(module), name), model)
    assert chains and all(f"order_by({model}.id)" in c for c in chains), chains   # 修前不排序


def test_同一事件两条启用的规则_入账按编号小的那条(client, admin):
    """行为上记下约定的次序（SQLite 上修前修后都绿，见模块文档）。"""
    from app.models import User
    from app.spd.models import SpdPointRule
    from app.spd.service import award_points

    with SessionLocal() as db:
        db.add_all([SpdPointRule(code="P2693_OLD", name="随访完成（旧）", event="p2693_done", points=3),
                    SpdPointRule(code="P2693_NEW", name="随访完成（新）", event="p2693_done", points=5)])
        db.commit()
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        record = award_points(db, admin_id, "p2693_done", note="P2693")
        db.commit()
        assert (record.rule_code, record.points) == ("P2693_OLD", 3)
