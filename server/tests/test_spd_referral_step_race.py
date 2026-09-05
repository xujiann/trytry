"""转诊环节轨迹（`spd_referral_steps`）的并发防线（P1-30）。

洞的形状：轨迹表的每一行都对应父单 `spd_referral_cases` 的一次状态跃迁
（referral.py 模块 docstring：「每推进一格写一条 SpdReferralStep」），可五个推进
端点原先全是 `db.get` → Python 判 `case.status` → ORM 属性赋值 → `commit`——
flush 出来的 UPDATE 只有 `WHERE id=?`。PG READ COMMITTED 下两路都读到同一旧态、
都推进、各写一条轨迹，并各自触发 `spawn_task`（无幂等）/ `award_points`
（只有 daily_limit、没有 ref_id 去重）：轨迹与终态互相矛盾，任务与积分翻倍。

**不变式长在父单上，不是本表的键**：同一张 accepted 单上「到院」与「下转」并发、
submitted 单上「审核」与「撤回」并发，环节名并不相同，`(case_id, step)` 唯一索引
一条也拦不住，写出的却同样是自相矛盾的轨迹。所以守法是父单条件 UPDATE
（`referral._advance_case`：`UPDATE spd_referral_cases … WHERE id=:id AND status=期望态`），
本表只在命中后同事务追加——与 `prescriptions._apply_review` / `maternal._mark_high_risk`
同一范式，登记在 `tests/test_stage14_concurrency.py` 的 GUARDED_BY_PARENT_UPDATE。

这套用例分四层，**确定性不靠线程**：

- 行为面：顺序重复推进仍是原来那句 409（状态码 + 文案逐字断言），合法的多行
  （闭环六格、同患者第二张单）照旧放行；
- 窗口重放：在「前置校验通过」与「条件 UPDATE」之间用另一个会话把赢家的提交塞
  进去（monkeypatch 打在调用边界上，不改被测代码），抢输者必须 409 且轨迹/任务/
  积分一个都不落——这是本文件里对旧代码**确定性变红**的那一层；
- 线程探针：八路并发到院、审核与退回对撞，守的是修后不倒退（SQLite 的库级写锁
  让它对「拆掉条件 UPDATE」并不敏感）；
- 防拆卸静态钉：五个 handler 必须各含 `_advance_case(`、不得回潮为 `case.status =`，
  且子行必须写在父单 UPDATE 之后。

真并发语义由 `tests/test_spd_referral_step_unique_races.py`（真 PG，默认 skip）守。
"""
import ast
import inspect
import re
import textwrap
import threading

import pytest
from sqlalchemy import update as sa_update

from conftest import login

import app.spd.routers.referral as referral_mod

# 五个推进端点：静态钉与「顺序重复 → 409」都按这份清单走，日后新增推进端点
# 必须一并登记，否则窗口会悄悄重开。
ADVANCING_HANDLERS = (
    "review_referral", "arrive_referral", "down_referral",
    "receive_followup", "withdraw_referral",
)


@pytest.fixture(scope="module")
def world(client, admin):
    """三级机构树（county←township←village）+ 各级医生 + 一名纳管在村室的患者。"""
    def _org(name, level, org_type, parent_id=None):
        body = {"name": name, "org_type": org_type, "level": level}
        if parent_id is not None:
            body["parent_id"] = parent_id
        resp = client.post("/api/organizations", json=body, headers=admin)
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    county = _org("轨迹竞态县医院", "county", "lead_hospital")
    township = _org("轨迹竞态卫生院", "township", "township", county["id"])
    village = _org("轨迹竞态村卫生室", "village", "village", township["id"])

    def _doctor(username, org_id):
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": "doctor",
                  "org_id": org_id},
            headers=admin,
        )
        return login(client, username, "pass123456")

    vdoc = _doctor("refstep_vdoc", village["id"])
    tdoc = _doctor("refstep_tdoc", township["id"])
    cdoc = _doctor("refstep_cdoc", county["id"])

    patient = client.post(
        "/api/patients",
        json={"name": "轨迹竞态患者", "id_card": "330182198207070095", "phone": "13900009095"},
        headers=admin,
    ).json()
    client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": village["id"], "risk_level": "high"},
        headers=admin,
    )
    from app.database import SessionLocal
    from app.models import User as PlatformUser

    with SessionLocal() as db:
        vdoc_id = db.query(PlatformUser).filter_by(username="refstep_vdoc").first().id
    return {
        "county": county, "township": township, "village": village,
        "vdoc": vdoc, "tdoc": tdoc, "cdoc": cdoc, "vdoc_id": vdoc_id,
        "patient": patient, "admin": admin,
    }


# ---------------------------------------------------------------- 取数小工具


def _new_case(world, status, current_org_id=None, current_level="county"):
    """直接造一张停在指定状态的单子（含「发起」轨迹行），不依赖审核链的执行顺序。"""
    from app.database import SessionLocal
    from app.spd.models import SpdReferralCase, SpdReferralStep

    with SessionLocal() as db:
        case = SpdReferralCase(
            patient_id=world["patient"]["id"], program_code="hypertension",
            direction="up", initiator_org_id=world["village"]["id"],
            initiator_id=world["vdoc_id"],
            current_org_id=current_org_id if current_org_id is not None else world["county"]["id"],
            current_level=current_level, status=status, reason="轨迹竞态用例",
        )
        db.add(case)
        db.flush()
        db.add(SpdReferralStep(
            case_id=case.id, step="发起", action="submit",
            actor_id=world["vdoc_id"], org_id=world["village"]["id"], opinion="轨迹竞态用例",
        ))
        db.commit()
        return case.id


def _steps(case_id, step=None):
    from app.database import SessionLocal
    from app.spd.models import SpdReferralStep

    with SessionLocal() as db:
        query = db.query(SpdReferralStep).filter(SpdReferralStep.case_id == case_id)
        if step is not None:
            query = query.filter(SpdReferralStep.step == step)
        return [(s.step, s.action) for s in query.order_by(SpdReferralStep.id).all()]


def _status(case_id):
    from app.database import SessionLocal
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        return db.get(SpdReferralCase, case_id).status


def _point_records(case_id):
    """本单产生的积分明细条数（`award_points` 只有 daily_limit，没有 ref_id 去重）。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPointRecord

    with SessionLocal() as db:
        return (
            db.query(SpdPointRecord)
            .filter(SpdPointRecord.ref_type == "referral", SpdPointRecord.ref_id == case_id)
            .count()
        )


def _tasks(patient_id, title):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return (
            db.query(SpdTask)
            .filter(SpdTask.patient_id == patient_id, SpdTask.title == title)
            .count()
        )


@pytest.fixture
def in_window(monkeypatch):
    """把「赢家的提交」确定性地塞进前置校验与条件 UPDATE 之间的那条缝。

    竞态窗口的两端在源码里是两个相邻语句，真并发下能不能撞上要看运气；这里在
    handler **调用别的模块级函数**的边界打桩（被测的判定/写入逻辑一行未改），
    让抢输侧每次都发生：前置校验拿到的是旧态，条件 UPDATE 面对的是新态。
    """
    def install(hook_name, case_id, **values):
        from app.database import SessionLocal
        from app.spd.models import SpdReferralCase

        original = getattr(referral_mod, hook_name)

        def patched(*args, **kwargs):
            result = original(*args, **kwargs)
            with SessionLocal() as db:  # 另一个会话 = 另一个赢家
                db.execute(
                    sa_update(SpdReferralCase)
                    .where(SpdReferralCase.id == case_id).values(**values)
                )
                db.commit()
            return result

        monkeypatch.setattr(referral_mod, hook_name, patched)

    return install


# ================================================================ 行为面


def test_闭环六格与第二张单的到院各自成行(client, world):
    """合法的多行照旧放行：一张单顺序走完六格，同一患者的第二张单再走一遍。

    条件 UPDATE 拦的是「同一格被推两次」，不是「这张表里有多行」——(case_id, step)
    也好、step 也好，本表都不设唯一约束，否则第二张单的「到院」会被拒。
    """
    case_id = client.post(
        "/api/spd/referrals",
        json={"patient_id": world["patient"]["id"], "program_code": "hypertension",
              "reason": "血压持续不达标"},
        headers=world["vdoc"],
    ).json()["id"]
    for reviewer in (world["tdoc"], world["cdoc"]):
        resp = client.post(f"/api/spd/referrals/{case_id}/review",
                           json={"action": "pass"}, headers=reviewer)
        assert resp.status_code == 200, resp.text
    for path, body, actor in (
        ("arrive", {"effective_visit": True}, world["cdoc"]),
        ("down", {"target_org_id": world["township"]["id"]}, world["cdoc"]),
        ("receive-followup", {}, world["tdoc"]),
    ):
        resp = client.post(f"/api/spd/referrals/{case_id}/{path}", json=body, headers=actor)
        assert resp.status_code == 200, resp.text
    assert [s for s, _ in _steps(case_id)] == [
        "发起", "卫生院审核", "县级医院接收", "到院", "下转", "随访接收"
    ]

    second = _new_case(world, "accepted")
    assert client.post(f"/api/spd/referrals/{second}/arrive",
                       json={"effective_visit": True}, headers=world["cdoc"]).status_code == 200
    assert _steps(second, "到院") == [("到院", "arrive")]
    assert _steps(case_id, "到院") == [("到院", "arrive")], "另一张单的到院不该被牵连"


@pytest.mark.parametrize(
    "status,path,body,actor_key,detail,step",
    [
        ("accepted", "arrive", {"effective_visit": True}, "cdoc",
         "只有已接收的转诊单可登记到院", "到院"),
        ("arrived", "down", {"target_org_id": "<township>"}, "cdoc",
         "只有已接收/已到院的患者可下转", "下转"),
        ("down_referred", "receive-followup", {}, "cdoc",
         "只有已下转的转诊单可接收随访", "随访接收"),
        ("submitted", "withdraw", None, "admin",
         "已进入上级审核的转诊单不能撤回", "撤回"),
    ],
)
def test_顺序重复推进仍是原来那句409(client, world, status, path, body, actor_key, detail, step):
    """条件 UPDATE 接管之后，顺序重放的状态码与文案一字未变（第 1、7 条）。"""
    case_id = _new_case(world, status)
    if body is not None and body.get("target_org_id") == "<township>":
        body = {"target_org_id": world["township"]["id"]}
    headers = world[actor_key]
    url = f"/api/spd/referrals/{case_id}/{path}"
    first = client.post(url, json=body, headers=headers) if body is not None \
        else client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    again = client.post(url, json=body, headers=headers) if body is not None \
        else client.post(url, headers=headers)
    assert again.status_code == 409
    assert again.json() == {"detail": detail}
    assert len(_steps(case_id, step)) == 1, "第二次推进不得再落一行轨迹"


def test_重复到院不重复计分(client, world):
    """积分只有 daily_limit、没有 ref_id 去重：重复到院一旦放行就是双倍计分。"""
    case_id = _new_case(world, "accepted")
    assert client.post(f"/api/spd/referrals/{case_id}/arrive",
                       json={"effective_visit": True}, headers=world["cdoc"]).status_code == 200
    assert _point_records(case_id) == 1
    assert client.post(f"/api/spd/referrals/{case_id}/arrive",
                       json={"effective_visit": True}, headers=world["cdoc"]).status_code == 409
    assert _point_records(case_id) == 1, "抢输/重放都不得再入一笔有效上转积分"


def test_终态单据的前置校验仍先于条件UPDATE(client, world):
    """已结束的单子走的还是前置校验那句 409，不进 UPDATE（快路径未被吃掉）。"""
    case_id = _new_case(world, "rejected")
    resp = client.post(f"/api/spd/referrals/{case_id}/review",
                       json={"action": "pass"}, headers=world["admin"])
    assert resp.status_code == 409 and resp.json() == {"detail": "该转诊单已结束"}
    assert _steps(case_id) == [("发起", "submit")]


# ================================================================ 窗口重放（确定性）


def test_到院抢输时轨迹与积分一个都不落(client, world, in_window):
    """赢家在窗口里把单子推到 arrived：抢输者拿同一句 409，且不写轨迹、不计分。"""
    case_id = _new_case(world, "accepted")
    in_window("_assert_holds_case", case_id, status="arrived", effective_visit=True)
    resp = client.post(f"/api/spd/referrals/{case_id}/arrive",
                       json={"effective_visit": True}, headers=world["cdoc"])
    assert resp.status_code == 409
    assert resp.json() == {"detail": "只有已接收的转诊单可登记到院"}
    assert _steps(case_id, "到院") == [], "抢输者的轨迹行必须随事务一起回滚"
    assert _point_records(case_id) == 0, "抢输者不得留下积分"
    assert _status(case_id) == "arrived"


def test_随访接收抢输时不重复闭环(client, world, in_window):
    case_id = _new_case(world, "down_referred", current_org_id=world["township"]["id"])
    in_window("_assert_holds_case", case_id, status="closed")
    resp = client.post(f"/api/spd/referrals/{case_id}/receive-followup",
                       json={}, headers=world["tdoc"])
    assert resp.status_code == 409
    assert resp.json() == {"detail": "只有已下转的转诊单可接收随访"}
    assert _steps(case_id, "随访接收") == []
    assert _point_records(case_id) == 0


def test_下转抢输给到院仍走合法顺序路径(client, world, in_window):
    """期望态是 IN ('accepted','arrived')：输给并发的到院登记不该被拒。

    accepted→arrived→down_referred 本就是合法顺序路径，收窄成等值匹配会把
    「到院后下转」这条正常业务判成冲突。
    """
    case_id = _new_case(world, "accepted")
    in_window("_assert_holds_case", case_id, status="arrived")
    resp = client.post(f"/api/spd/referrals/{case_id}/down",
                       json={"target_org_id": world["township"]["id"]}, headers=world["cdoc"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "down_referred"
    assert _steps(case_id, "下转") == [("下转", "down")]
    # 反向：已下转之后再补到院要被拒，否则轨迹会出现「下转之后又到院」
    assert client.post(f"/api/spd/referrals/{case_id}/arrive",
                       json={"effective_visit": True}, headers=world["tdoc"]).status_code == 409
    assert _steps(case_id, "到院") == []


def test_撤回抢输给卫生院审核时按前置校验措辞(client, world, in_window):
    """跨环节竞态（撤回 vs 审核）——(case_id, step) 唯一索引对它完全无效。"""
    case_id = _new_case(world, "submitted", current_org_id=world["village"]["id"],
                        current_level="village")
    in_window("now_naive", case_id, status="township_reviewed", current_level="township")
    resp = client.post(f"/api/spd/referrals/{case_id}/withdraw", headers=world["admin"])
    assert resp.status_code == 409
    assert resp.json() == {"detail": "已进入上级审核的转诊单不能撤回"}
    assert _steps(case_id, "撤回") == []
    assert _status(case_id) == "township_reviewed", "赢家的推进不得被抢输者回滚掉"


@pytest.mark.parametrize(
    "winner,detail",
    [
        ({"status": "township_reviewed"}, "该转诊单刚被其他操作推进，请刷新后重试"),
        ({"status": "rejected"}, "该转诊单已结束"),
        ({"status": "accepted"}, "当前状态不需要审核"),
    ],
)
def test_审核抢输后按父单真实状态措辞(client, world, in_window, winner, detail):
    """抢输侧必须 rollback + refresh 之后再读状态。

    ORM 的 `synchronize_session='evaluate'` 会把内存里的 `case.status` 置成新值——
    哪怕 rowcount 为 0；不回滚不刷新就会拿自己没写成的值去措辞。
    """
    case_id = _new_case(world, "submitted", current_org_id=world["village"]["id"],
                        current_level="village")
    before = _tasks(world["patient"]["id"], "上转患者到院跟踪")
    in_window("_assert_review_authority", case_id, **winner)
    resp = client.post(f"/api/spd/referrals/{case_id}/review",
                       json={"action": "pass", "opinion": "抢输的这一票"},
                       headers=world["admin"])
    assert resp.status_code == 409
    assert resp.json() == {"detail": detail}
    assert _steps(case_id) == [("发起", "submit")], "抢输者不得写下第二条环节轨迹"
    assert _tasks(world["patient"]["id"], "上转患者到院跟踪") == before, "副作用必须一并回滚"


# ================================================================ 线程探针


def test_八路并发到院恰一路成功(client, world):
    """守的是修后不倒退：SQLite 的库级写锁让这条对「拆掉条件 UPDATE」并不敏感，
    确定性看上面的窗口重放与下面的静态钉。"""
    case_id = _new_case(world, "accepted")
    results: list = [None] * 8
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait(timeout=30)
        resp = client.post(f"/api/spd/referrals/{case_id}/arrive",
                           json={"effective_visit": True}, headers=world["cdoc"])
        results[i] = (resp.status_code, resp.json().get("detail"))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    codes = [code for code, _ in results]
    assert codes.count(200) == 1, f"到院只能成功一路，实际：{results}"
    assert codes.count(409) == 7, f"其余七路都该拿 409，实际：{results}"
    assert {d for code, d in results if code == 409} == {"只有已接收的转诊单可登记到院"}
    assert _steps(case_id, "到院") == [("到院", "arrive")]
    assert _point_records(case_id) == 1
    assert _status(case_id) == "arrived"


def test_审核通过与退回并发只成一路(client, world):
    """同一格上 pass 与 reject 对撞：终态与轨迹必须互相自洽（任务随赢家一起落）。"""
    case_id = _new_case(world, "township_reviewed", current_org_id=world["township"]["id"],
                        current_level="township")
    before = _tasks(world["patient"]["id"], "上转患者到院跟踪")
    results: list = [None] * 8
    barrier = threading.Barrier(8)

    def go(i):
        action = "pass" if i % 2 == 0 else "reject"
        barrier.wait(timeout=30)
        resp = client.post(f"/api/spd/referrals/{case_id}/review",
                           json={"action": action, "opinion": f"第{i}路"},
                           headers=world["admin"])
        results[i] = resp.status_code

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert results.count(200) == 1, f"一格只能被推一次，实际：{results}"
    assert len(_steps(case_id, "县级医院接收")) == 1, "同一格只能留下一条轨迹"
    status = _status(case_id)
    assert status in ("accepted", "rejected")
    spawned = _tasks(world["patient"]["id"], "上转患者到院跟踪") - before
    assert spawned == (1 if status == "accepted" else 0), "任务只随赢家落一份"


# ================================================================ 防拆卸静态钉


def _body_source(func) -> str:
    """函数体源码（剔除 docstring）：文案里出现 "commit" 不该把静态钉带红。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    statements = tree.body[0].body
    if (
        isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    return "\n".join(ast.unparse(node) for node in statements)


def test_五个端点都是条件UPDATE不许回潮():
    """判定与写入必须在同一条 SQL 里，且子行写在父单 UPDATE 命中之后。"""
    for name in ADVANCING_HANDLERS:
        src = _body_source(getattr(referral_mod, name))  # 注释与 docstring 不算数
        assert "_advance_case(" in src, (
            f"{name} 丢了父单条件 UPDATE——check-then-act 的窗口重新打开了"
        )
        assert not re.search(r"case\.status\s*=(?!=)", src), (
            f"{name} 回潮为 Python 侧给 case.status 赋值：读与写之间的窗口回来了"
        )
        assert "db.rollback()" in src, (
            f"{name} 抢输后没有 rollback：那条 0 行的 UPDATE 已开了写事务"
        )
        if "_add_step(" in src:
            assert src.index("_advance_case(") < src.index("_add_step("), (
                f"{name} 把轨迹行写在了父单 UPDATE 之前——抢输者也会留下轨迹"
            )


def test_advance_case是一条带状态条件的UPDATE():
    src = _body_source(referral_mod._advance_case)
    assert "update(SpdReferralCase)" in src and ".rowcount" in src
    assert "SpdReferralCase.status ==" in src and "SpdReferralCase.status.in_(" in src, (
        "单值等值与 IN 两种期望态都要在：down/withdraw 允许两个入口状态"
    )


def test_add_step仍是同事务裸插入():
    """轨迹行必须与父单 UPDATE、spawn_task、award_points 同属一个事务。

    改走 `insert_or_conflict` 之类会在中途 commit 的助手，等于把事务拆成两半：
    轨迹已落、任务未落，抢输者的 rollback 也收不回来。
    """
    body = _body_source(referral_mod._add_step)  # 去掉 docstring，只看真正执行的语句
    assert "db.add(" in body
    assert "commit" not in body and "flush" not in body


def test_轨迹表不设唯一约束():
    """本表**故意**不加 (case_id, step) 唯一索引：条件 UPDATE 之下它恒冗余，
    却拦不住到院/下转、审核/撤回这类跨环节竞态（环节名不同），还会拒掉存量轨迹。"""
    from app.database import Base

    table = Base.metadata.tables["spd_referral_steps"]
    assert not [i for i in table.indexes if i.unique], (
        "唯一性守在父单状态跃迁上（GUARDED_BY_PARENT_UPDATE）；"
        "若确要加索引，_add_step 不 commit，需按 b8e3d5f70a91 范式先探存量重复"
    )
