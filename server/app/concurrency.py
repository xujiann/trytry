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
import contextlib
import threading
from collections.abc import Iterator
from typing import Any, TypeVar, cast

from fastapi import HTTPException
from sqlalchemy import ColumnElement, case, func, literal, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_T = TypeVar("_T")

__all__ = [
    "insert_or_conflict",
    "insert_with_retry",
    "upsert_unique",
    "claim_quota",
    "insert_if_absent",
    "add_amount",
    "take_amount",
    "append_text",
    "appended_text",
    "serialized_on",
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


def ensure_present(obj: _T | None, what: str) -> _T:
    """`insert_if_absent(...)` 之后重查，本该必得一行；拿不到就明确报错。

    仓库里有五处这个形状——入库/发血/领料/培训成绩/病历质控都靠它做"没有就建、
    有就累加"：

        insert_if_absent(db, DrugStock(...))
        stock = db.query(DrugStock).filter(...).first()
        add_amount(db, DrugStock, stock.id, ...)      # stock 在类型上是 X | None

    正常路径下 `.first()` 必不为 None（行刚被保证存在）。但"必不为 None"是
    **推理**不是保证：另一个请求在这两句之间把行删了，`stock.id` 就是
    `AttributeError` → 500，日志里只有一句 NoneType，看不出发生了什么。

    收成一句 `ensure_present(...)`：把这条推理写出来，并在它不成立时给出
    409 与人话——这是并发撞车，重试即可，不是服务器坏了。
    """
    if obj is None:
        raise HTTPException(status_code=409, detail=f"{what}刚被其他操作删除，请重试")
    return obj


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


def add_amount(db: Session, model, obj_id: int, col: str, step) -> None:
    """原子累加：`UPDATE ... SET col = col + step WHERE id = :id`。

    替代 `obj.col += step`。那句是读-改-写：先把当前值读进 Python，加完再整体写回，
    并发下两个请求读到同一个旧值，后写的把先写的盖掉——**丢更新**。

    实测（改之前）：药品库存建行 10 支后 8 路并发各入库 10 支，应为 90，
    实际 30，**凭空少了 60 支**。药品、血液、物资都是要盘点对账的东西，
    账实不符查起来极费劲，因为每一笔入库的日志看上去都成功了。

    调用方随后仍要 `db.commit()`；要读回新值请在提交后 `db.refresh(obj)`——
    这条 UPDATE 走的是 Core，不经过 ORM，会话里那个对象仍是旧值。
    """
    column = getattr(model, col)
    db.execute(update(model).where(model.id == obj_id).values(**{col: column + step}))


def take_amount(db: Session, model, obj_id: int, col: str, amount) -> bool:
    """原子扣减：`UPDATE ... SET col = col - amount WHERE col >= amount`。

    用于出库、发血这类"够才能扣"的场景，返回是否扣到。
    与 `claim_quota` 同理——**判定与扣减必须在同一条 SQL 里**。
    先 `if 库存 < 数量: 409` 再 `-=` 的写法，并发下两个请求都判定够，
    最后扣出负库存或少扣一笔。
    """
    column = getattr(model, col)
    # `Session.execute` 的静态返回是 `Result`，只有 `CursorResult` 才声明了
    # rowcount；DML 语句拿到的实际就是 CursorResult，故在此收窄。
    result = cast(CursorResult, db.execute(
        update(model)
        .where(model.id == obj_id, column >= amount)
        .values(**{col: column - amount})
    ))
    return bool(result.rowcount)


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
    claimed = cast(CursorResult, db.execute(
        update(model)
        .where(model.id == obj_id, used + step <= limit)
        .values(**{used_col: used + step})
    ))
    return bool(claimed.rowcount)


def appended_text(column, text: str, sep: str = "；") -> ColumnElement[Any]:
    """"在 column 末尾追加 text"的 SQL 表达式：空/NULL 时不带分隔符，否则 `col || sep || text`。

    给需要把追加与别的条件压进**同一条 UPDATE** 的调用方（处方审核：状态迁移与意见追加
    要在同一条带状态条件的 UPDATE 里，见 `prescriptions._apply_review`；首次标高危 +
    追加风险因素，见 `maternal._mark_high_risk`）；只是追加一列的直接用 `append_text`。
    """
    return case(
        (func.coalesce(column, "") == "", literal(text)),
        else_=column + sep + literal(text),
    )


def append_text(db: Session, model, obj_id: int, col: str, text: str, sep: str = "；") -> None:
    """原子追加字符串：`UPDATE ... SET col = col || sep || :text WHERE id = :id`。

    替代 `obj.col = (obj.col + sep if obj.col else "") + text`。那句同 `add_amount`
    要替代的 `+=` 一样是读-改-写，只是把数字换成了字符串：两路并发各往孕产妇档案
    追加一条风险因素，后写的把先写的盖掉——高危档案上少一条风险因素，随访就少盯
    一项，而两笔的日志看上去都成功了。拼接下沉到 SQL 里由行锁排队，两笔都留下
    （先后由谁先拿到行锁决定）。

    **追加之外还有判定的，别用这条**：追加本身原子了，外面包一层
    `if not obj.flag:` 照样是 check-then-act（两路都读到 False、都追加）。把判定压进
    同一条 UPDATE 的 WHERE 里，用 `appended_text` 自己拼语句（范式见
    `maternal._mark_high_risk` / `prescriptions._apply_review`）。

    调用方随后仍要 `db.commit()`；要读回新值请在提交后 `db.refresh(obj)`——
    这条 UPDATE 走的是 Core，会话里那个对象仍是旧值。
    """
    column = getattr(model, col)
    db.execute(
        update(model).where(model.id == obj_id).values(**{col: appended_text(column, text, sep)})
    )


#: SQLite 侧的进程内互斥（见 `serialized_on`）。RLock 而非 Lock：结算的临界区里
#: 还会再进押金冲抵这一段，同线程重入不该把自己锁死。
_SQLITE_ROW_LOCK = threading.RLock()


@contextlib.contextmanager
def serialized_on(db: Session, model, row_id: int) -> Iterator[None]:
    """把"读 → 判定/计算 → 写"整段圈成以某一行为界的临界区，两种方言各用各的办法。

    留给**一条 SQL 压不进去**的读-改-写。上面几个 `UPDATE ... WHERE` 之所以对，是因为
    UPDATE 会对**既有行**取行锁，锁到手后重新求值 WHERE / SET（PG 的 EvalPlanQual）；
    以下两类没有这条可走：

    - 判定读的不是一列而是流水现算（押金余额）。扣减写的是 `INSERT ... FROM SELECT`
      ——INSERT **不给任何既有行加锁**，聚合子查询读的是语句开始时的快照，
      READ COMMITTED 下并发事务彼此不可见。实测（PG）：预交 1000、八路并发各退 200，
      八笔全过，refunded=1600、balance=-600。SQLite 的库级写锁把这条掩盖了，
      所以它只在生产库上现形。
    - 追加的是 JSON 列（复诊日志、外呼证据、召回联系记录）。两种方言都没有可移植的
      "原子往数组末尾追加"，只能锁住这一行、重读、整体写回。

    两种方言：

    - PostgreSQL：对目标行 `SELECT ... FOR UPDATE`。锁到手之后的每条语句都取新快照
      （READ COMMITTED 逐语句取快照），此后读到的就是上一个赢家提交后的值。
      锁随事务提交/回滚释放，因此 **commit 必须写在 with 块内**。
    - SQLite：没有 FOR UPDATE，且库级写锁只在第一条**写**语句才生效，判定阶段的读
      根本不排队。沿用 main.py 审计链的分流写法——单进程内用一把进程内锁串行化
      （SQLite 本就只用于开发/单实例）。

    **块内先 `db.refresh(obj)` 再读改写**：进临界区之前拿到的 ORM 对象是锁外读的
    旧值，锁到手不会自动把它刷新，不刷新就照旧覆盖别人刚提交的那一笔。
    `test_stage14_concurrency.py` 的读-改-写规则只豁免"`serialized_on` 块内、
    且已 refresh 之后"的赋值——顺序写反了照样判红。

    临界区按行划分而不是全局一把锁：不同住院登记、不同复诊计划之间互不阻塞。
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(model.id).where(model.id == row_id).with_for_update())
        yield
        return
    with _SQLITE_ROW_LOCK:
        yield
