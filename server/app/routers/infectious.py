"""传染病病例报告与多点触发监测预警。"""
from datetime import date, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import clock
from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_business_date
from ..models import InfectiousCase, InfectiousDisease, Organization, User
from ..visibility import assert_org_writable
from ..schemas import InfectiousCaseCreate, InfectiousCaseOut, InfectiousDiseaseOut
from .reports import _csv_response

router = APIRouter(prefix="/api/infectious", tags=["传染病监测"], dependencies=[Depends(get_current_user)])


# 响应契约：字段与原手拼 dict 一一对应，保持向后兼容。
class AlertOut(BaseModel):
    disease_code: str
    disease_name: str
    case_count: int
    org_count: int
    window_days: int
    severity: str


class LateReportOut(BaseModel):
    case_id: int
    org_id: int
    disease_code: str
    disease_name: str
    category: str
    report_hours: int
    onset_date: str
    reported_at: str
    days_late: int

DEFAULT_WINDOW_DAYS = 7
DEFAULT_THRESHOLD = 5

# 法定传染病目录种子（启动时写入 infectious_diseases 表）：
# 甲类（A）2小时报告；乙类（B）/丙类（C）24小时报告
SEED_DISEASES = [
    {"code": "A20", "name": "鼠疫", "category": "A", "report_hours": 2},
    {"code": "A00", "name": "霍乱", "category": "A", "report_hours": 2},
    {"code": "U071", "name": "新型冠状病毒感染", "category": "B", "report_hours": 24},
    {"code": "A15", "name": "肺结核", "category": "B", "report_hours": 24},
    {"code": "B15", "name": "病毒性肝炎", "category": "B", "report_hours": 24},
    {"code": "B20", "name": "艾滋病", "category": "B", "report_hours": 24},
    {"code": "A82", "name": "狂犬病", "category": "B", "report_hours": 24},
    {"code": "A38", "name": "猩红热", "category": "B", "report_hours": 24},
    {"code": "J11", "name": "流行性感冒", "category": "C", "report_hours": 24},
    {"code": "B084", "name": "手足口病", "category": "C", "report_hours": 24},
    {"code": "A09", "name": "感染性腹泻病", "category": "C", "report_hours": 24},
]


@router.get("/diseases", response_model=list[InfectiousDiseaseOut])
def list_diseases(category: str | None = None, db: Session = Depends(get_db)):
    """法定传染病目录（含分类与报告时限）。"""
    query = db.query(InfectiousDisease)
    if category:
        query = query.filter(InfectiousDisease.category == category)
    return query.order_by(InfectiousDisease.category, InfectiousDisease.code).all()


@router.post(
    "/cases",
    response_model=InfectiousCaseOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2: 传染病报告
)
def report_case(
    body: InfectiousCaseCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    # P0-35：报告机构由请求声明。多点预警按「几家机构报了同一病种」计——一个人以几家机构的名义各报
    # 一例就能凭空触发预警；同一件事在症候群上报上早就守住了。
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="报告机构不存在")
    # 发病日期不得晚于今天（P2-454）：平台自己的质控规则 QC015（「传染病报告发病日期不得晚于当日」，严重级）事后才点名，
    # 写入时却照收——2099 年发病的鼠疫报卡 201、迟报天数算成 -26394、永不进迟报清单；一张把月份敲错成下个月的手足口病
    # 报卡落在预警窗口之外，同病种凑够 5 例的多点触发预警就少一例、整条不出
    if body.onset_date > clock.today().isoformat():
        raise HTTPException(status_code=422, detail=f"发病日期（{body.onset_date}）不得晚于今天")
    case = InfectiousCase(**body.model_dump())
    # 目录内病种自动回填甲/乙/丙分类
    disease = (
        db.query(InfectiousDisease).filter(InfectiousDisease.code == body.disease_code).first()
    )
    if disease is not None:
        case.category = disease.category
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.get("/cases", response_model=list[InfectiousCaseOut])
def list_cases(disease_code: str | None = None, db: Session = Depends(get_db)):
    query = db.query(InfectiousCase)
    if disease_code:
        query = query.filter(InfectiousCase.disease_code == disease_code)
    return query.order_by(InfectiousCase.id.desc()).limit(500).all()


@router.get("/alerts", response_model=list[AlertOut])
def multi_point_alerts(
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=0, le=3650),  # 加天数的上界（P1-96）：原先无界，传个大数 date - timedelta 溢出，整个请求 500
    threshold: int = DEFAULT_THRESHOLD,
    today: str | None = None,
    db: Session = Depends(get_db),
):
    """多点触发预警：滑动窗口内同病种病例数≥阈值，且涉及机构数≥2 时升级预警。

    L-2：默认取服务端当前日期；today 覆盖参数仅限测试/管理排查用途（YYYY-MM-DD）。
    """
    end = resolve_business_date(today)
    # 「7 天窗口」含今天共 7 个日历日，与症候群多点预警（`surveillance.multi_point_alerts`）同一口径：原先从 end − 7 起算、
    # 两头都含，实际 8 天（P2-159）；0 与 1 都只看今天
    start = (end - timedelta(days=max(window_days - 1, 0))).isoformat()
    # 「同病种」按病种编码认（P2-159）：名称是报告时手填的自由文本，原先按（编码, 名称）分组——同是 J11，甲院写「流行性感冒」、
    # 乙院写「流感」，两家各 3 例被拆成两组、都不到阈值 5，跨机构的聚集一条预警都不出
    rows = (
        db.query(
            InfectiousCase.disease_code,
            func.max(InfectiousCase.disease_name).label("disease_name"),
            func.count(InfectiousCase.id).label("case_count"),
            func.count(func.distinct(InfectiousCase.org_id)).label("org_count"),
        )
        .filter(InfectiousCase.onset_date >= start, InfectiousCase.onset_date <= end.isoformat())
        .group_by(InfectiousCase.disease_code)
        .having(func.count(InfectiousCase.id) >= threshold)
        .order_by(InfectiousCase.disease_code)
        .all()
    )
    # 目录里有这个病种的显示目录名，没有的显示报告里的一个写法
    catalog_names = {
        code: name
        for code, name in db.query(InfectiousDisease.code, InfectiousDisease.name)
        .filter(InfectiousDisease.code.in_([r.disease_code for r in rows] or [""]))
        .order_by(InfectiousDisease.code)
        .all()
    }
    return [
        {
            "disease_code": r.disease_code,
            "disease_name": catalog_names.get(r.disease_code) or r.disease_name,
            "case_count": r.case_count,
            "org_count": r.org_count,
            "window_days": window_days,
            # 多机构同时报告，聚集性风险升级
            "severity": "high" if r.org_count >= 2 else "medium",
        }
        for r in rows
    ]


# ---------- 工程包 I1：法定上报导出（传染病报告卡） ----------

#: 传染病分类的中文（报告卡导出与驾驶舱下钻同一份，P2-646）；目录外的病种没有分类
INFECTIOUS_CATEGORY_NAMES = {"A": "甲类", "B": "乙类", "C": "丙类"}


class CaseReportCardOut(BaseModel):
    """传染病报告卡导出契约（平台留存字段集 + 报告及时性判定）。

    及时性口径与 GET /late-reports 完全一致（报告日−发病日折算小时数超
    目录时限即迟报）。目录外病种三个及时性字段都为 null；发病日期非法时
    法定时限照给（目录里有），迟报天数与是否迟报为 null（P2-311 订正：原先
    写成「三个都为 null」，实现一直给着法定时限）。
    """

    case_id: int
    org_id: int
    org_name: str
    disease_code: str
    disease_name: str
    category: str
    category_name: str
    onset_date: str
    reported_at: str
    report_hours: int | None
    days_late: int | None
    late: bool | None


def _timeliness(
    case: InfectiousCase, meta: tuple[str, str, int] | None
) -> tuple[int | None, int | None, bool | None]:
    """(法定时限小时, 迟报天数, 是否迟报)；迟报清单与报告卡导出共用这一处，判不了返回 None。

    报告日取 `reported_at` 的**本地**日期（P2-528）：`reported_at` 落库是 naive UTC，发病日期是临床按当地日历填的——
    原先直接 `.date()` 取 UTC 日期，东八区 0–8 点报的卡报告日算成前一天、迟报天数少 1：甲类（2 小时）次日早上
    7 点半报的判「及时」，乙类第三天早上报的同样漏判。
    """
    if meta is None:
        return None, None, None
    _, _, report_hours = meta
    try:
        onset = date.fromisoformat(case.onset_date)
    except ValueError:
        return report_hours, None, None
    reported_on = case.reported_at.replace(tzinfo=timezone.utc).astimezone().date()
    days_late = (reported_on - onset).days
    return report_hours, days_late, days_late * 24 > report_hours


def _timeliness_text(late: bool | None, report_hours: int | None) -> str:
    """报告卡导出「及时性」一栏，与报告卡页面同一套说法（P2-686）：原先判不了的留空，页面却写「无法定时限」——
    目录外病种确实没有法定时限；发病日期非法时时限照给、只是算不出迟没迟，要写「无法判定」。"""
    if late is None:
        return "无法定时限" if report_hours is None else "无法判定"
    return "迟报" if late else "及时"


def _case_card(case: InfectiousCase, org_names: dict, meta_by_code: dict) -> dict:
    meta = meta_by_code.get(case.disease_code)
    report_hours, days_late, late = _timeliness(case, meta)
    return {
        "case_id": case.id,
        "org_id": case.org_id,
        "org_name": org_names.get(case.org_id, ""),
        "disease_code": case.disease_code,
        # 目录内的病种印目录名（P2-943，与预警 P2-159 同一口径）：原先印报告人手填的写法——同是 J11，「流感」「甲流？」
        # 「流行性感冒」照抄进法定报告卡与供手工网报的导出。目录外的照旧取报告里的写法；手填写法在病例列表（登记簿）里照看
        "disease_name": meta[0] if meta is not None else case.disease_name,
        "category": case.category,
        "category_name": INFECTIOUS_CATEGORY_NAMES.get(case.category, "目录外"),
        "onset_date": case.onset_date,
        "reported_at": case.reported_at.isoformat(),
        "report_hours": report_hours,
        "days_late": days_late,
        "late": late,
    }


def _disease_meta(db: Session) -> dict:
    return {
        d.code: (d.name, d.category, d.report_hours) for d in db.query(InfectiousDisease).all()
    }


@router.get(
    "/cases/export.csv",
    response_model=str,
    dependencies=[Depends(require_roles("director"))],  # 法定上报导出=管理层
)
def export_case_report_cards_csv(
    disease_code: str | None = None,
    late_only: bool = False,
    db: Session = Depends(get_db),
):
    """传染病报告卡批量导出（CSV，Excel 可直接打开）。

    **报送方式说明**：平台与县疾控无网络直报专线，本导出供**手工网报**
    （录入中国疾病预防控制信息系统/大疫情网）或交换前置机对接使用；
    及时性列与"未及时上报清单"（GET /late-reports）同口径联动，
    `late_only=true` 即只导迟报清单。
    """
    meta_by_code = _disease_meta(db)
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    query = db.query(InfectiousCase)
    if disease_code:
        query = query.filter(InfectiousCase.disease_code == disease_code)
    # 不设上限（P1-113）：原先 `.limit(2000)` 按编号正序取，超量时截掉的是**最新**的卡，「只导迟报」又在截断之后
    # 才筛——新近的迟报一张都进不了清单。与死因报告卡导出（P1-50）同一口径：法定上报的导出不许静默少一截
    cards = [
        (case, _case_card(case, org_names, meta_by_code))
        for case in query.order_by(InfectiousCase.id).all()
    ]
    if late_only:
        cards = [(case, c) for case, c in cards if c["late"]]
    rows = [
        [
            # 报告时间写带偏移的本地时间（P2-536）：手工网报照着誊录，原先写落库的 naive UTC，差 8 小时
            c["case_id"], c["org_name"], c["disease_code"], c["disease_name"],
            c["category_name"], c["onset_date"], clock.local_iso(case.reported_at),
            c["report_hours"] if c["report_hours"] is not None else "",
            c["days_late"] if c["days_late"] is not None else "",
            _timeliness_text(c["late"], c["report_hours"]),
        ]
        for case, c in cards
    ]
    return _csv_response(
        "infectious_report_cards.csv",
        ["卡片编号", "报告机构", "病种编码", "病种名称", "分类", "发病日期", "报告时间",
         "法定时限(小时)", "迟报天数", "及时性"],
        rows,
    )


@router.get(
    "/cases/{case_id}/report-card",
    response_model=CaseReportCardOut,
    dependencies=[Depends(require_roles("director"))],  # 法定上报导出=管理层
)
def case_report_card(case_id: int, db: Session = Depends(get_db)):
    """单张传染病报告卡（JSON，平台留存的法定字段集 + 及时性判定）。

    **报送方式说明**：导出供手工网报（大疫情网）或县疾控前置机对接，
    平台不直连国家传染病网络直报系统。病例登记未含患者个体标识
    （infectious_cases 仅记报告机构/病种/发病日期），卡片即按此字段集导出，
    不虚构未存储的字段。
    """
    case = db.get(InfectiousCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="病例不存在")
    org = db.get(Organization, case.org_id)
    return _case_card(case, {case.org_id: org.name if org else ""}, _disease_meta(db))


@router.get("/late-reports", response_model=list[LateReportOut])
def late_reports(db: Session = Depends(get_db)):
    """迟报清单：reported_at 与 onset_date 间隔超过目录报告时限的病例（粗略按天折算）。

    判定口径：报告日（本地日期）与发病日相差天数 × 24 小时 > report_hours 即视为迟报，
    即甲类（2h）跨日报告即迟报，乙/丙类（24h）相隔≥2天迟报。判定与报告卡导出共用 `_timeliness`（P2-528：原先
    两处各写一遍，都拿 UTC 日期算报告日）。
    """
    hours_by_code = {d.code: (d.name, d.category, d.report_hours) for d in db.query(InfectiousDisease).all()}
    rows = []
    for case in db.query(InfectiousCase).order_by(InfectiousCase.id).all():
        meta = hours_by_code.get(case.disease_code)
        report_hours, days_late, late = _timeliness(case, meta)   # 目录外病种、发病日期坏了的都判不了，不进清单
        if meta is None or not late:
            continue
        name, category, _ = meta
        rows.append(
            {
                "case_id": case.id,
                "org_id": case.org_id,
                "disease_code": case.disease_code,
                "disease_name": name,   # 目录名，与报告卡同一口径（P2-943）；只有目录内病种进得了这张清单
                "category": category,
                "report_hours": report_hours,
                "onset_date": case.onset_date,
                "reported_at": case.reported_at.isoformat(),
                "days_late": days_late,
            }
        )
    return rows
