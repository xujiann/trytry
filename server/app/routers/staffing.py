"""人员下沉调度台账（阶段八）。

原先 `secondments` 只有起止日期，答得了"现在有几个人在派"，答不了国家监测
指标问的那句"中级及以上医师派驻 6 个月以上有几人"——既没有职称等级，
也分不清长期派驻与临时支援。

两处刻意不做推断：

- **职称等级不从 `title` 文本猜**。"主治医师"/"主管护师"/"副主任医师"各院
  写法不一，靠关键词匹配，一个"助理全科医生"就能把统计带偏。
- **派驻类型必须显式选**。巡诊与短期支援不是下沉，混在一起统计会把指标做虚。

派驻天数按自然日算，跨年派驻按"落在统计年度内的天数"计——这是监测口径，
写在返回里。
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..datetypes import DateStr, OptionalDateStr
from ..visibility import assert_obj_org_writable
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, resolve_org_scope
from ..models import Employee, Organization, Secondment, User

router = APIRouter(prefix="/api/staffing", tags=["人员调度"],
                   dependencies=[Depends(get_current_user)])

ASSIGNMENT_TYPES = {
    "long_term": "长期派驻", "support": "短期支援", "rounds": "巡诊", "other": "其他",
}
TITLE_LEVELS = {
    "none": "未填", "junior": "初级", "intermediate": "中级",
    "deputy_senior": "副高", "senior": "正高",
}
# 国家监测口径：中级及以上
SENIOR_LEVELS = ("intermediate", "deputy_senior", "senior")
# 6 个月按 183 天计（半年取整），与监测口径一致
LONG_TERM_DAYS = 183


class SecondmentIn(BaseModel):
    employee_id: int
    from_org_id: int
    to_org_id: int
    start_date: DateStr
    end_date: OptionalDateStr = ""
    assignment_type: str = Field(default="long_term", pattern="^(long_term|support|rounds|other)$")
    position: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=256)


class TitleLevelIn(BaseModel):
    title_level: str = Field(pattern="^(none|junior|intermediate|deputy_senior|senior)$")


def _days(start: str, end: str, today: date) -> int:
    """派驻天数：未结束的算到今天。"""
    try:
        s = datetime.strptime(start, "%Y-%m-%d").date()
    except ValueError:
        return 0
    e = today
    if end:
        try:
            e = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            e = today
    return max((e - s).days, 0)


def _out(row: Secondment, emp: Employee | None, names: dict, today: date) -> dict:
    return {
        "id": row.id,
        "employee_id": row.employee_id,
        "employee_name": emp.name if emp else "",
        "title": emp.title if emp else "",
        "title_level": emp.title_level if emp else "none",
        "title_level_name": TITLE_LEVELS.get(emp.title_level if emp else "none", ""),
        "from_org_id": row.from_org_id,
        "from_org_name": names.get(row.from_org_id, ""),
        "to_org_id": row.to_org_id,
        "to_org_name": names.get(row.to_org_id, ""),
        "start_date": row.start_date,
        "end_date": row.end_date,
        "ongoing": not row.end_date,
        "assignment_type": row.assignment_type,
        "assignment_type_name": ASSIGNMENT_TYPES.get(row.assignment_type, row.assignment_type),
        "position": row.position,
        "days": _days(row.start_date, row.end_date, today),
        "note": row.note,
    }


@router.post("/secondments", status_code=201,
             dependencies=[Depends(require_roles("director", "operator"))])
def create_secondment(body: SecondmentIn, db: Session = Depends(get_db)):
    """建立派驻记录。同一员工同一去向不得有两条未结束的记录——人不能同时在两处在派。"""
    employee = db.get(Employee, body.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    for org_id in (body.from_org_id, body.to_org_id):
        if db.get(Organization, org_id) is None:
            raise HTTPException(status_code=404, detail="机构不存在")
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="派出与接收机构不能相同")
    if body.end_date and body.end_date < body.start_date:
        raise HTTPException(status_code=422, detail="结束日期不得早于开始日期")
    ongoing = (
        db.query(Secondment)
        .filter(
            Secondment.employee_id == body.employee_id,
            Secondment.end_date == "",
        )
        .first()
    )
    if ongoing is not None and not body.end_date:
        raise HTTPException(
            status_code=409,
            detail=f"该员工尚有未结束的派驻（自 {ongoing.start_date}），请先结束再新建",
        )
    row = Secondment(**body.model_dump())
    db.add(row)
    # 在派期间员工状态标记为 seconded，便于"谁在岗"一眼可见
    if not body.end_date:
        employee.status = "seconded"
    db.commit()
    names = {o.id: o.name for o in db.query(Organization).all()}
    return _out(row, employee, names, date.today())


@router.get("/secondments")
def list_secondments(
    response: Response,
    to_org_id: int | None = None,
    group_id: int | None = None,
    assignment_type: str | None = None,
    ongoing: bool | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """派驻台账。`group_id` 按接收机构所属分组筛选。"""
    query = db.query(Secondment)
    scope = resolve_org_scope(db, group_id, to_org_id)
    if scope is not None:
        query = query.filter(Secondment.to_org_id.in_(scope))
    if assignment_type:
        query = query.filter(Secondment.assignment_type == assignment_type)
    if ongoing is True:
        query = query.filter(Secondment.end_date == "")
    elif ongoing is False:
        query = query.filter(Secondment.end_date != "")
    rows = paginate(query.order_by(Secondment.id.desc()), response, offset, limit)
    employees = {e.id: e for e in db.query(Employee).all()}
    names = {o.id: o.name for o in db.query(Organization).all()}
    today = date.today()
    return [_out(r, employees.get(r.employee_id), names, today) for r in rows]


@router.post("/secondments/{secondment_id}/end",
             dependencies=[Depends(require_roles("director", "operator"))])
def end_secondment(
    secondment_id: int,
    end_date: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(Secondment, secondment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="派驻记录不存在")
    assert_obj_org_writable(db, user, row, org_attr="from_org_id")
    if row.end_date:
        raise HTTPException(status_code=409, detail="该派驻已结束")
    finish = end_date or date.today().isoformat()
    if finish < row.start_date:
        raise HTTPException(status_code=422, detail="结束日期不得早于开始日期")
    row.end_date = finish
    employee = db.get(Employee, row.employee_id)
    if employee is not None and employee.status == "seconded":
        employee.status = "active"
    db.commit()
    names = {o.id: o.name for o in db.query(Organization).all()}
    return _out(row, employee, names, date.today())


@router.patch("/employees/{employee_id}/title-level",
              dependencies=[Depends(require_roles("director", "operator"))])
def set_title_level(employee_id: int, body: TitleLevelIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """维护职称等级。**不从职称文本推断**，必须显式选。"""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    assert_obj_org_writable(db, user, employee)
    employee.title_level = body.title_level
    db.commit()
    return {
        "id": employee.id,
        "name": employee.name,
        "title": employee.title,
        "title_level": employee.title_level,
        "title_level_name": TITLE_LEVELS.get(employee.title_level, ""),
    }


@router.get("/dispatch-stats")
def dispatch_stats(
    year: int | None = None, group_id: int | None = None, db: Session = Depends(get_db)
):
    """下沉调度统计：按接收机构给出在派人数与**长期派驻满 6 个月人数**。

    这是国家监测指标"中级及以上医师派驻 6 个月以上人数"的取数口径：

    - 只计 `assignment_type=long_term`（巡诊与短期支援不是下沉）；
    - 只计 `title_level` 为中级及以上（未填等级的单独计数报出来，不默认算入）；
    - 派驻天数取落在统计年度内的部分，跨年派驻不重复计满。
    """
    target_year = year or date.today().year
    year_start = date(target_year, 1, 1)
    year_end = date(target_year, 12, 31)
    today = date.today()

    # P1-2：这里原先把 Employee 与 Secondment 整表拉进内存再在 Python 里筛。
    # 数据量小的时候看不出来，但随年份累积会慢慢变差。改为 SQL 侧先筛：
    # 用 ISO 日期字符串的字典序与时序一致这一点，把"整段落在统计年度外"的
    # 派驻直接排除掉；职称等级用外连接一次带出，不再整表建字典。
    year_start_str, year_end_str = year_start.isoformat(), year_end.isoformat()
    query = (
        db.query(Secondment, Employee.title_level)
        .outerjoin(Employee, Employee.id == Secondment.employee_id)
        .filter(Secondment.start_date <= year_end_str)
        .filter((Secondment.end_date == "") | (Secondment.end_date >= year_start_str))
    )
    scope = resolve_org_scope(db, group_id, None)
    if scope is not None:
        query = query.filter(Secondment.to_org_id.in_(scope))
    rows = query.all()

    org_ids = {row.to_org_id for row, _level in rows}
    names = (
        {
            o.id: o.name
            for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
        }
        if org_ids
        else {}
    )

    stats: dict[int, dict] = {}
    unknown_level = 0
    invalid_date = 0
    for row, title_level in rows:
        # 截取落在统计年度内的区间
        try:
            start = datetime.strptime(row.start_date, "%Y-%m-%d").date()
        except ValueError:
            # D-3：新数据已在入口挡下非法日期，存量数据里可能还有。
            # **单独报出来，不能像原先那样 continue 掉**——一条记录无声无息地
            # 从指标里消失，比指标少一个人更难查。
            invalid_date += 1
            continue
        end = today
        if row.end_date:
            try:
                end = datetime.strptime(row.end_date, "%Y-%m-%d").date()
            except ValueError:
                invalid_date += 1
                continue
        span_start, span_end = max(start, year_start), min(end, year_end)
        if span_end < span_start:
            continue  # 该派驻整段不在统计年度内
        days_in_year = (span_end - span_start).days

        entry = stats.setdefault(
            row.to_org_id,
            {
                "org_id": row.to_org_id,
                "org_name": names.get(row.to_org_id, ""),
                "ongoing": 0,
                "total": 0,
                "long_term_6m": 0,
                "long_term_6m_senior": 0,
            },
        )
        entry["total"] += 1
        if not row.end_date:
            entry["ongoing"] += 1
        if row.assignment_type == "long_term" and days_in_year >= LONG_TERM_DAYS:
            entry["long_term_6m"] += 1
            level = title_level or "none"
            if level in SENIOR_LEVELS:
                entry["long_term_6m_senior"] += 1
            elif level == "none":
                unknown_level += 1

    return {
        "year": target_year,
        "group_id": group_id,
        "orgs": sorted(stats.values(), key=lambda x: -x["long_term_6m_senior"]),
        # 未填职称等级的派驻单独报出来：不默认算入"中级及以上"，也不悄悄丢掉
        "unknown_title_level": unknown_level,
        # 存量数据里日期非法的条数（新数据已在入口拦下）。同样是单列而不丢弃。
        "invalid_date_records": invalid_date,
        "caliber": {
            "long_term_6m": f"派驻类型为长期派驻且当年在派满 {LONG_TERM_DAYS} 天的人次"
                            "（跨年派驻只计落在本年度内的天数）",
            "senior": "职称等级为中级/副高/正高；等级未填的不计入，单独报 unknown_title_level",
        },
    }
