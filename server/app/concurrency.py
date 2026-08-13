"""并发写入的三个共用做法。

这个模块是被同一个错误逼出来的。平台早在阶段七就踩过一次 check-then-act
（D-2 全域基金池并发建出两个），阶段九修掉并把教训写进了注释；结果阶段九·五
新写的三个模块又各犯了一遍：

- 医废收集：`COUNT+1` 算追溯码后直接插入，并发下 8 个请求只落库 5 条，
  另外 3 个抛未捕获的 `IntegrityError` → **500，记录丢了**；
- 疫苗接种：先判 `库存 - 已用 > 0` 再 `used_quantity += 1`，
  并发下库存 1 支实测打出 4 针，台账与实际对不上——疫苗是按批号强监管的品类；
- 症候群日报：先查有没有、没有就插，并发下同样 500。

**知道这个坑并不足以不掉进去**。所以不只是把三处补上 try/except，而是把三种
正确写法固定成函数，并配一条扫描用例（`test_stage14_concurrency.py`）盯住
"往带唯一约束的表里写、却没处理约束冲突"的形状。
"""
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

__all__ = [
    "insert_or_conflict",
    "insert_with_retry",
    "upsert_unique",
    "claim_quota",
    "insert_if_absent",
]


def insert_or_conflict(db: Session, obj, detail: str, status_code: int = 409):
    """插入；撞唯一约束就回滚并给出可读的 409。

    用于"本来就不该重复"的场景（同一机构同编码的资源、同一疫苗同批号）。
    调用方拿到的是一句人话，而不是一条 500 与一段栈。
    """
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status_code, detail=detail) from None
    db.refresh(obj)
    return obj


def insert_with_retry(db: Session, build, attempts: int = 12):
    """按"重算序号 → 插入"重试，直到成功或用完次数。

    用于**服务端生成的顺序编号**（医废追溯码、病理标本号）。这类冲突不该让
    调用方重试：调用方重试的是整个业务动作，而且并发压力下它会撞进下一次冲突；
    真正该重试的只是"取下一个号"这一步，那是服务端的事。

    `build` 每次被调用都要**重新计算编号并返回一个新对象**——传同一个实例进来
    是没用的，它的编号已经定死了。

    `attempts` 默认 12：N 个请求同时抢号时，最后一个最坏要重试 N 次，
    原来的 5 次在 8 路并发下实测就顶不住了（一个请求 503）。取号本身很便宜，
    宁可多试几次。**但它仍是有上界的**——持续高压下会返回 503 而不是
    悄悄丢件，这是刻意的取舍：编号必须唯一，宁可让调用方重来。
    """
    for attempt in range(attempts):
        obj = build()
        db.add(obj)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if attempt == attempts - 1:
                raise HTTPException(
                    status_code=503, detail="编号分配连续冲突，请稍后重试"
                ) from None
            continue
        db.refresh(obj)
        return obj
    raise AssertionError("unreachable")  # pragma: no cover


def upsert_unique(db: Session, model, keys: dict, values: dict):
    """按唯一键"有则覆盖、无则新建"，并发下也不会 500。

    先试插入：撞约束说明有人抢先建了，回滚后按唯一键取回那一行再更新。
    反过来写（先查再插）就是 check-then-act，两个请求都查不到就都去插。

    返回 `(对象, 是否为覆盖)`。
    """
    obj = model(**keys, **values)
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    else:
        db.refresh(obj)
        return obj, False

    existing = db.query(model).filter_by(**keys).first()
    if existing is None:  # pragma: no cover - 约束冲突却查不到，说明约束定义有误
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    for field, value in values.items():
        setattr(existing, field, value)
    db.commit()
    db.refresh(existing)
    return existing, True


def insert_if_absent(db: Session, obj) -> bool:
    """批量导入里的单行插入：已存在就跳过，返回是否真的落库。

    用于"先把已有的查出来、不在里面的才插"这类批量幂等导入（字典导入、
    审方规则导入、权限点同步、内置角色预置）。那个写法本身是 check-then-act：
    两个导入并发跑，都查不到同一条编码，就都去插，后插的抛 `IntegrityError`——
    而这类接口是**一次 commit 提交整批**的，一条撞车会让整批回滚，
    最后是 500 加上"一条都没导进去"。

    这里用 SAVEPOINT 把冲突圈在单行内：撞了就退回这一行，整批继续。
    只 flush 不 commit——什么时候提交整批仍由调用方决定。

    **调用方必须自己结束外层事务**。SAVEPOINT 回滚只退掉这一行，外层写事务
    还开着；若就此 return，整个请求会一路握着写锁，后面的审计落库直接
    `database is locked`。返回 False 后要提前返回的，先 `db.rollback()`。

    成功也要 `savepoint.commit()` 把 SAVEPOINT 释放掉。漏了这一句，每行都往
    外层事务上再压一层没释放的 SAVEPOINT，最后 `db.commit()` 要递归收束几百层：
    实测权限点同步（640 个写接口）直接 `RecursionError`，应用起不来。
    只用 5 行的小样例试不出来——这类错误要拿真实规模才撞得到。
    """
    savepoint = db.begin_nested()
    db.add(obj)
    try:
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        return False
    savepoint.commit()  # 释放 SAVEPOINT，不影响外层事务
    return True


def claim_quota(db: Session, model, obj_id: int, used_col: str, limit_col: str, step: int = 1) -> bool:
    """原子占额：`UPDATE ... SET used = used + step WHERE used + step <= limit`。

    这是平台早就在用的做法（`appointments.book_slot` 的原子占号），
    只是没抽出来，于是疫苗批次那里又用回了"先读再判再加"——
    并发下每个请求都读到同一个 used，各自判定还有货，最后只有一次加法生效。

    返回是否占到。**判定与扣减必须在同一条 SQL 里**，中间隔了一次 Python 判断，
    这个函数就没有意义了。
    """
    used = getattr(model, used_col)
    limit = getattr(model, limit_col)
    claimed = (
        db.execute(
            update(model)
            .where(model.id == obj_id, used + step <= limit)
            .values(**{used_col: used + step})
        ).rowcount
    )
    return bool(claimed)
