"""专病规则版本快照"同版只留一份"下沉到库之后的回归（P1-30）。

洞的形状：`update_program` 是彻头彻尾的读-改-写——读 `program.version` → 存一份
带该版本号的快照 → 把 `program.version` 升一格 → 提交，中间没有任何闸门。两个
配置员同时改同一病种，两路都读到 `v1`：各写一份 `(program_id, 'v1')` 快照、却
只升一次版到 `v2`，后提交的那笔把先提交的字段改动整片盖掉。**不报错**，落库两条
一模一样的快照，而中间那一版规则永久消失。这比撞 `IntegrityError` 更坏：考核端
问"这批人当初按哪版规则纳的管"时，`v1` 有两份互相矛盾的答案，`v2` 那版压根没留痕。

迁移 `f4e3d2c1b0a9` 把这条不变式下沉为**全量唯一索引** `uq_spd_program_version`
（两列都是 NOT NULL，不存在 `NULL != NULL` 的漏洞，因而不必做成部分索引），
接口层把快照插入改走 `insert_or_conflict`，并让它与字段改动、升版共享同一次提交
——输家撞索引后整笔回滚，拿到 409 而不是"看似成功、实则丢了一版"。

本档钉四件事：

1. **行为面**：版本标签已被退役时再改规则得到 409、文案固定，且本次改的字段
   （名称、规则）连同版本号一起回滚；合法的连续改版不受影响（v1、v2 各留一份）。
2. **静态防拆卸**：索引必须留在模型上、必须 `unique=True`、必须是**全量**索引
   （加了 WHERE 就会放走一部分重复）。
3. **库上真的建过**：模型声明了、库里没建（漏迁移）等于没有约束。
4. **绕开接口层直插**：SQLite 的库级写锁让线程探针对"拆掉索引"不敏感，
   直接写库才是确定性的网——那正是并发抢输者实际到达的位置。
"""
import pytest
from sqlalchemy import inspect as sa_inspect

from app.database import engine
from app.models import Base
from app.spd.models import SpdProgramVersion


def _program(client, admin, code, **extra):
    body = {"code": code, "name": f"{code}病种", "category": "specialty",
            "include_rules": [{"field": "age", "op": ">=", "value": 60}]}
    body.update(extra)
    resp = client.post("/api/spd/programs", json=body, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _versions(client, admin, program_id):
    resp = client.get(f"/api/spd/programs/{program_id}/versions", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ================================================================ 行为面


def test_连续改两次规则各留一份快照且版本号逐格上升(client, admin):
    """合法的多条必须照旧：一个病种改 N 次规则就该有 N 份快照。

    全量唯一索引锁的是"同一个版本标签只退役一次"，不是"一个病种只许有一份快照"
    ——做成后者会把这条正常链路拒掉，那是另一种坏。
    """
    program = _program(client, admin, "spv_legit")
    assert program["version"] == "v1"

    first = client.patch(
        f"/api/spd/programs/{program['id']}",
        json={"include_rules": [{"field": "age", "op": ">=", "value": 65}], "note": "上调年龄"},
        headers=admin,
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == "v2"

    second = client.patch(
        f"/api/spd/programs/{program['id']}",
        json={"stages": [{"code": "s1", "name": "初始"}], "note": "补阶段"},
        headers=admin,
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == "v3"

    versions = _versions(client, admin, program["id"])
    assert [v["version"] for v in versions] == ["v2", "v1"], "两次改版应各留一份、按倒序透出"
    assert versions[1]["snapshot"]["include_rules"][0]["value"] == 60, "v1 快照存的是改之前"
    assert versions[0]["snapshot"]["include_rules"][0]["value"] == 65, "v2 快照存的是改之前"


def test_只改名字不升版也不留快照(client, admin):
    """非规则字段的改动走的是另一条分支（不构造快照、直接提交），不该被闸门波及。"""
    program = _program(client, admin, "spv_rename")
    resp = client.patch(
        f"/api/spd/programs/{program['id']}",
        json={"name": "改过名的病种", "description": "只动描述"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "v1", "只改名不该升版"
    assert resp.json()["name"] == "改过名的病种"
    assert _versions(client, admin, program["id"]) == [], "没改规则就不该留快照"


def test_当前版本标签已被退役时改规则得到409且整笔回滚(client, admin):
    """并发抢输者实际到达的位置，用"先手已把 v1 退役"这一确定性状态复现。

    抢输的那一路读到的 `v1` 已经不再是"未退役的版本"了——它的快照撞索引，于是
    这一笔里的所有改动（名称、规则、版本号）一起回滚。断言的重点不只是 409：
    **部分生效**才是这个洞最难查的后果，所以逐个字段确认没落库。
    """
    from app.database import SessionLocal

    program = _program(client, admin, "spv_taken")
    # 模拟"先手已经提交了 v1 的退役快照"：绕开接口层直接写库，
    # 并在发 HTTP 之前提交 + 关闭（文件型 SQLite 的写锁会和 TestClient 线程互卡）
    db = SessionLocal()
    try:
        db.add(SpdProgramVersion(
            program_id=program["id"], version="v1", snapshot={},
            changed_by="先手配置员", note="先一步退役 v1",
        ))
        db.commit()
    finally:
        db.close()

    resp = client.patch(
        f"/api/spd/programs/{program['id']}",
        json={"include_rules": [{"field": "age", "op": ">=", "value": 70}],
              "name": "抢输的改名", "note": "后手"},
        headers=admin,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "该专病规则刚被他人修改，请刷新后重试"

    after = client.get(f"/api/spd/programs/{program['id']}", headers=admin).json()
    assert after["version"] == "v1", "版本号不许在快照没落库的情况下自己往前走"
    assert after["name"] == "spv_taken病种", "同一笔里的改名必须一起回滚"
    assert after["include_rules"][0]["value"] == 60, "同一笔里的规则改动必须一起回滚"
    assert len(_versions(client, admin, program["id"])) == 1, "不该多出第二份 v1 快照"


# ================================================================ 防拆卸静态钉


def test_同病种同版本号的唯一索引不许消失():
    """模型侧的声明就是这条不变式的落点，删掉就等于把"静默双写"的洞放回去。

    同时钉住"是全量索引"：加上 WHERE 会放走一部分重复，而这两列都是 NOT NULL，
    本来就不需要靠部分索引绕开 `NULL != NULL`。
    """
    index = next(
        (i for i in Base.metadata.tables["spd_program_versions"].indexes
         if i.name == "uq_spd_program_version"),
        None,
    )
    assert index is not None, "uq_spd_program_version 没了——同版双快照的洞回来了"
    assert index.unique, "uq_spd_program_version 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == ["program_id", "version"], "索引的键变了"
    for dialect in ("sqlite", "postgresql"):
        where = str(index.dialect_options[dialect].get("where", "") or "")
        assert where == "", f"{dialect} 上被改成了部分索引（WHERE {where}），会放走一部分重复"


def test_唯一索引真的建在库上(client):
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    names = {i["name"] for i in sa_inspect(engine).get_indexes("spd_program_versions")}
    assert "uq_spd_program_version" in names, "库上没有 uq_spd_program_version（库与模型对不上）"


def test_绕开接口层直插时库里真的拦得住(client, admin):
    """索引"在不在"与"拦不拦得住"是两回事。

    接口层在顺序请求下已经给出 409，行为用例因此分辨不出兜底是否真的生效
    （SQLite 的库级写锁又让线程探针对拆卸不敏感）。这里绕开接口层直接写库看
    数据库自己是否抬手；同时确认**换个版本号仍然放行**——拦的是重复，不是写入。
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal

    program = _program(client, admin, "spv_direct")
    row = dict(program_id=program["id"], version="v1", snapshot={},
               changed_by="直插验证", note="")
    db = SessionLocal()
    try:
        db.add(SpdProgramVersion(**row))
        db.commit()

        db.add(SpdProgramVersion(**row))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(SpdProgramVersion(**{**row, "version": "v2"}))
        db.commit()  # 下一格版本号是合法的第二条，不该被拦
    finally:
        db.close()
