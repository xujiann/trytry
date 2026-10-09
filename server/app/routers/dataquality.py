"""数据质控规则引擎（块3）：规则驱动扫描存量数据，输出违规明细与汇总。

- 规则表 QcRule：code 唯一、target_table 指向被检表、rule_type 决定执行器、
  config 携带参数、severity 区分 error/warn、active 控制是否参与扫描
- 5 类执行器：required / range / enum / cross_ref / logic（命名逻辑校验）
- 启动时按 app/data/qc_rules_seed.py 幂等种子化 15 条规则（已存在编码不覆盖本地调整）
"""
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import true
from sqlalchemy.orm import Session

from .. import clock
from ..concurrency import insert_or_conflict
from ..data.qc_rules_seed import SEED_QC_RULES
from ..database import get_db
from ..deps import get_current_user, require_admin, row_dict
from ..numtypes import non_finite_path
from ..models import (
    Admission,
    ChronicDiseaseType,
    ChronicPatient,
    CodeEntry,
    CodeSystem,
    Encounter,
    ExamReport,
    ExamRequest,
    FollowUp,
    InfectiousCase,
    MedicalCert,
    Patient,
    Prescription,
    PrescriptionItem,
    QcRule,
    User,
)
from ..pii import EncryptedPII
from ..privacy import mask_id_card, mask_phone
from ..texttypes import NON_BLANK, is_blank_text
from .dictionaries import SYSTEM_CODES
from .exams import CRITICAL_STATUS_NAMES

router = APIRouter(
    prefix="/api/dataquality", tags=["数据质控"], dependencies=[Depends(get_current_user)]
)
logger = logging.getLogger("medplat.dataquality")

RULE_TYPES = {
    "required": "必填项",
    "range": "数值区间",
    "enum": "取值枚举",
    "cross_ref": "引用校验",
    "logic": "逻辑校验",
}
SEVERITIES = {"error": "错误", "warn": "警告"}

#: 内置规则（种子）的编码。种子「只增不改」、每次启动按编码补缺——内置规则删了，下次启动就按种子原样补回来，
#: 本地对它的停用与严重度调整也一并丢了。所以内置规则只能停用、不许删（P2-564）
BUILTIN_RULE_CODES = frozenset(rule["code"] for rule in SEED_QC_RULES)

# 可被质控规则引用的表（target_table → 模型）；新增被检表在此登记即可
_TABLE_MODELS = {
    m.__tablename__: m
    for m in (
        Patient,
        Encounter,
        ExamRequest,
        ExamReport,
        Prescription,
        PrescriptionItem,
        ChronicPatient,
        FollowUp,
        InfectiousCase,
        Admission,
        MedicalCert,
        ChronicDiseaseType,
    )
}
# 分批扫描的批大小：内存里同一时刻只放这么多行（防全表拉爆内存），整张表照样扫完。
SCAN_LIMIT = 5000


def _scan(query, model):
    """按主键分批把查询扫完（P1-115）。

    原先每条规则 `query.limit(SCAN_LIMIT)` 一次取完就判：超出的行**永远扫不到**——开发库按插入序取前 5000 行、
    生产库按堆序取任意 5000 行，`/run` 与 `/summary` 却把这部分里查出的数当全量报出。原注释说「超出部分下次
    整改后再扫」，可整改只改值不删行，前 5000 行永远是那 5000 行。县域患者、就诊动辄数万条，后面的违规一条都
    查不出。

    分批按主键往后翻（`id > 上一批最后一个 ORDER BY id LIMIT 批大小`）：内存上限与原先一样，扫描顺序按主键、
    两库一致，不漏不重。
    """
    last_id = 0
    while True:
        batch = query.filter(model.id > last_id).order_by(model.id).limit(SCAN_LIMIT).all()
        if not batch:
            return
        yield from batch
        last_id = batch[-1].id


def _fields_query(db: Session, model, *fields: str):
    """只取主键与判定要读的列：整表扫描不必把每行拼成 ORM 对象（5 万行 0.99s → 0.16s，P1-115）。

    规则配置里的字段名在模型上不是列（配错、留空）时退回整行查询——逐行 `getattr(row, field, None)` 得 None，
    判定结果与原先一致。
    """
    columns = {a.key for a in sa_inspect(model).column_attrs}
    wanted = [f for f in dict.fromkeys(fields) if f != "id"]
    if all(f in columns for f in wanted):
        return db.query(model.id, *(getattr(model, f) for f in wanted))
    return db.query(model)


def _is_blank(value) -> bool:
    """没填：None，或文字里一个看得见的字符都没有。与必填文本的 `NON_BLANK` 同一个判据（`is_blank_text`，P2-1148）：
    原先按 `strip()` 判，只有零宽空格 / BOM / 控制字符的姓名（入站、存量进得了库）QC002 查不出来。"""
    return value is None or (isinstance(value, str) and is_blank_text(value))


class _PiiMessage(str):
    """印了 PII 列取值的违规说明（P2-1566）。

    字符串本身就是原来那句明文（admin 看到的，逐字节不变）；`masked` 是同一句把 PII 取值换成掩码的版本。取值只在执行器
    里拿得到，谁在看只在出口（`run_checks`）知道：执行器把两句一起备好、出口按角色挑，不必把调用方一路传进各执行器。
    文本不变、只多带一样东西，照 `egress.UnresolvedHost` 的写法。
    """

    masked: str


def _pii_masker(model, field: str) -> Callable[[str], str] | None:
    """这一列的取值印进违规说明前要过的掩码；不是 PII 列返回 None（P2-1566）。

    PII 列认两样：列类型是加密列 `EncryptedPII`（与 `test_pii_query_point_guard` 从模型元数据推导的同一份清单，以后新加的
    加密列自动算进来）；或列名是 `id_card` / `phone`、以 `_id_card` / `_phone` 结尾（与出口脱敏闸门
    `test_privacy_egress_guard` 认字段名同一判据）。电话走 `privacy.mask_phone`，其余走 `privacy.mask_id_card`。
    """
    name = field.lower()
    if name == "phone" or name.endswith("_phone"):
        return mask_phone
    column = sa_inspect(model).columns.get(field)
    if name == "id_card" or name.endswith("_id_card") or (column is not None and isinstance(column.type, EncryptedPII)):
        return mask_id_card
    return None


def _with_masked(message: str, *shown: tuple[Callable[[str], str] | None, Any]) -> str:
    """给违规说明配上掩码版（P2-1566）。`shown` 是这句里印进去的（掩码, 取值），掩码取自 `_pii_masker`、不是 PII 列的为
    None。印了 PII 取值的返回 `_PiiMessage`（掩码版 = 同一句把这些取值换成掩码），没印的原样返回。"""
    masked = message
    for masker, value in shown:
        if masker is not None and isinstance(value, str) and value:
            masked = masked.replace(value, masker(value))
    if masked == message:
        return message
    out = _PiiMessage(message)
    out.masked = masked
    return out


# ---------------------------------------------------------------------------
# 命名逻辑校验（rule_type=logic）
# ---------------------------------------------------------------------------

_ID_CARD_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CARD_CHECK_CODES = "10X98765432"


def id_card_invalid_reason(value: str) -> str:
    """身份证号 GB 11643 校验：18 位、前17位数字、末位校验码正确。"""
    if _is_blank(value):
        return "身份证号为空"
    value = value.strip().upper()
    if len(value) != 18:
        return f"身份证号长度应为18位，实际 {len(value)} 位"
    # 只认 ASCII（P1-97）：全角数字原先能转 int、被判为合法；上标 / 圈码数字让整条规则抛异常
    if not (value[:17].isascii() and value[:17].isdigit()):
        return "身份证号前17位应全为数字"
    total = sum(int(value[i]) * _ID_CARD_WEIGHTS[i] for i in range(17))
    expected = _ID_CARD_CHECK_CODES[total % 11]
    if value[17] != expected:
        return f"身份证号校验位应为 {expected}，实际 {value[17]}"
    return ""


def _check_id_card(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "id_card")
    hits = []
    for row in _filtered(db, model, rule, field):   # 规则的 filter 照样生效（P2-1567）
        reason = id_card_invalid_reason(getattr(row, field, ""))
        if reason:
            hits.append((row.id, reason))
    return hits


def _check_critical_closed_loop(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    """危急值报告未走到处置反馈（critical_status != resolved）。

    违规说明印状态文案（P2-1366）：取 `exams.CRITICAL_STATUS_NAMES`，存量空串等同已通知，与危急值页、指标导出同一张表
    （P2-1026 的写法）；表外的值原样回显。原先把状态码原样印给质控人员看，存量空串另编了个「未回填」，像是要补录数据。
    """
    rows = _scan(
        db.query(ExamReport.id, ExamReport.critical_status)
        .filter(ExamReport.critical == true(), ExamReport.critical_status != "resolved"),   # 用得上危急值部分索引（P2-1156）
        ExamReport,
    )
    return [
        (r.id, f"危急值闭环状态为{CRITICAL_STATUS_NAMES.get(r.critical_status or 'notified', r.critical_status)}，"
               f"未达处置反馈（{CRITICAL_STATUS_NAMES['resolved']}）")
        for r in rows
    ]


def _day_of(value) -> str:
    """与另一边比「哪一天」：落库时间戳是 naive UTC，先换本地日期（P2-714，与报卡迟报 P2-528 同一口径）；日期串取本身。"""
    if isinstance(value, datetime):
        return clock.to_local(value).date().isoformat()
    return str(value)[:10]


def _shown(value) -> str:
    """违规文案里的时刻印本地时刻（P2-714）：原先印的是落库的 UTC，东八区看着差 8 小时。"""
    return f"{clock.to_local(value):%Y-%m-%d %H:%M}" if isinstance(value, datetime) else str(value)


def _check_datetime_order(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    """结束时间早于开始时间（结束为空视为进行中，不判违规）。

    两边都是时间戳按时刻比、都是日期串按串比；一边只到日期、一边是时间戳时按本地日期、日粒度比，同一天不算早于
    （P2-714）。原先起止任一是字符串就整个按 `str()` 比：「下次随访日不得早于建档时间」里下次随访定在建档当天，
    `"2026-09-20" < "2026-09-20 05:20:37"` 恒真；东八区 0–8 点报的卡落库是前一天的 UTC，被判「报告早于发病」。
    """
    start_field = rule.config.get("start_field", "")
    end_field = rule.config.get("end_field", "")
    start_pii, end_pii = _pii_masker(model, start_field), _pii_masker(model, end_field)   # P2-1566
    hits = []
    for row in _filtered(db, model, rule, start_field, end_field):   # 规则的 filter 照样生效（P2-1567）
        start, end = getattr(row, start_field, None), getattr(row, end_field, None)
        # 空串也是「没填」（P2-234）：日期多是 String(10)、缺省空串而不是 NULL，原先只认 None，空的结束日期
        # 按字符串比 `"" < "2026-…"` 恒真——每一条「进行中」的都被判成「结束早于开始」
        if start is None or end is None or _is_blank(start) or _is_blank(end):
            continue
        if isinstance(start, datetime) and isinstance(end, datetime):
            earlier = end < start
        elif isinstance(start, datetime) or isinstance(end, datetime):
            earlier = _day_of(end) < _day_of(start)
        else:
            earlier = str(end) < str(start)
        if earlier:
            hits.append((row.id, _with_masked(f"{end_field}（{_shown(end)}）早于 {start_field}（{_shown(start)}）",
                                              (end_pii, end), (start_pii, start))))
    return hits


def _check_date_not_future(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    today = clock.today().isoformat()
    pii = _pii_masker(model, field)   # P2-1566
    hits = []
    for row in _filtered(db, model, rule, field):   # 规则的 filter 照样生效（P2-1567）
        value = getattr(row, field, None)
        if isinstance(value, datetime):
            # 本地日期比本地的今天（P2-714）：原先取 UTC 日期，东八区次日 0–8 点的时刻算成今天、漏报
            value = _day_of(value)
        elif isinstance(value, date):
            value = value.isoformat()
        if _is_blank(value):
            continue
        if str(value) > today:
            hits.append((row.id, _with_masked(f"{field}（{value}）晚于当前日期（{today}）", (pii, value))))
    return hits


def _followup_metric(row: FollowUp, key: str):
    """与分级同一个读法（`chronic._metric_value`）：先取随访同名列，再取 metrics 里的同名键。"""
    value = getattr(row, key, None)
    return (row.metrics or {}).get(key) if value is None else value


def _catalog_indicators(db: Session) -> dict[str, tuple[list[str], bool]]:
    """病种目录里每个病种的分级指标与 require_all（写坏的规则当没写）。"""
    out: dict[str, tuple[list[str], bool]] = {}
    for code, rules in db.query(ChronicDiseaseType.code, ChronicDiseaseType.level_rules):
        metrics = rules.get("metrics") if isinstance(rules, dict) else None
        keys = [m["key"] for m in metrics or [] if isinstance(m, dict) and isinstance(m.get("key"), str) and m["key"]]
        if keys:
            out[code] = (list(dict.fromkeys(keys)), bool(rules.get("require_all", True)))
    return out


def _check_chronic_followup_indicator(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    """慢病随访须记录对应病种指标。

    要记哪些指标以病种目录的分级规则为准（P2-1049）：目录是「分级规则与随访周期的唯一数据源」，规则配置里手抄的一份把
    冠心病 / 脑卒中写成收缩压、舒张压，目录按每周心绞痛次数、改良 Rankin 评分定级——按目录录的被点名「缺少指标：sbp、dbp」，
    只录了血压的反倒过了质控、却定不了级；移动端这两个病种本就没有血压格。取值与定级同一个读法（随访同名列，再取 metrics），
    require_all=false 的病种记了任一项即可。目录里没写分级指标的病种退回规则配置里的那份，再没有的至少记一项。
    """
    mapping: dict[str, list[str]] = rule.config.get("disease_indicators", {})
    catalog = _catalog_indicators(db)
    diseases = row_dict(db.query(ChronicPatient.id, ChronicPatient.disease).all())
    hits = []
    for row in _scan(db.query(FollowUp), FollowUp):
        disease = diseases.get(row.chronic_id, "")
        required, require_all = catalog.get(disease) or (mapping.get(disease) or [], True)
        if required:
            missing = [f for f in required if _followup_metric(row, f) is None]
            if missing and (require_all or len(missing) == len(required)):
                hits.append((row.id, f"{disease} 随访缺少指标：{'、'.join(missing)}"))
            continue
        # 目录未列明指标要求的病种：至少记录一项指标（含通用 metrics）
        if row.sbp is None and row.dbp is None and row.glucose is None and not (row.metrics or {}):
            hits.append((row.id, f"{disease or '未知病种'} 随访未记录任何指标"))
    return hits


_LOGIC_CHECKS = {
    "id_card_checksum": _check_id_card,
    "critical_closed_loop": _check_critical_closed_loop,
    "datetime_order": _check_datetime_order,
    "date_not_future": _check_date_not_future,
    "chronic_followup_indicator": _check_chronic_followup_indicator,
}
#: 自己定了扫哪张表、按什么条件扫的逻辑校验（P2-1567）：查询写死在校验里（未闭环的危急值报告、全部慢病随访），不逐行看
#: 规则的被检表，规则的 filter 也落不到它扫的行上；违规明细按被检表标表名，被检表须写成它扫的那张（P2-1569）
_OWN_TABLE_CHECKS = {"critical_closed_loop": ExamReport, "chronic_followup_indicator": FollowUp}


# ---------------------------------------------------------------------------
# 通用执行器
# ---------------------------------------------------------------------------


def _filtered(db: Session, model, rule: QcRule, *fields: str):
    """按规则的 filter 收窄后逐行扫被检表（只取主键与 fields 这几列）。

    查询落在被检表上的逻辑校验（证件号校验位、起止先后、日期不晚于今天）也走这里（P2-1567）：原先只有必填 / 区间 / 枚举 /
    引用四类调用，逻辑类配了 filter 照收、扫描时不用——QC001 加「只查未注销档案」返回 200，已注销的档案照报 error。
    """
    query = _fields_query(db, model, *fields)
    for key, value in (rule.config.get("filter") or {}).items():
        column = getattr(model, key, None)
        if column is not None:
            query = query.filter(column == value)
    return _scan(query, model)


def _run_required(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    return [
        (row.id, f"{field} 为空")
        for row in _filtered(db, model, rule, field)
        if _is_blank(getattr(row, field, None))
    ]


def _run_range(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    low, high = rule.config.get("min"), rule.config.get("max")
    ex_low, ex_high = rule.config.get("exclusive_min", False), rule.config.get("exclusive_max", False)
    skip_empty = rule.config.get("skip_empty", False)
    pii = _pii_masker(model, field)   # 说明里的 PII 取值出口按角色掩码（P2-1566）
    hits = []
    for row in _filtered(db, model, rule, field):
        value = getattr(row, field, None)
        # 空串也是没填（P2-234），与 None 一样按缺失报（P2-1569）：文字列（birth_date 之类）上的空串原先拿去跟界比，只设下限
        # 时被报成「低于下限」，只设上限时一条不报。skip_empty 时缺失的跳过，与引用校验同一语义（原先只有引用校验读它）
        if _is_blank(value):
            if not skip_empty:
                hits.append((row.id, f"{field} 缺失，无法判定区间"))
            continue
        if low is not None and (value <= low if ex_low else value < low):
            hits.append((row.id, _with_masked(f"{field}={value} 低于下限 {low}{'（不含）' if ex_low else ''}",
                                              (pii, value))))
            continue
        if high is not None and (value >= high if ex_high else value > high):
            hits.append((row.id, _with_masked(f"{field}={value} 超出上限 {high}{'（不含）' if ex_high else ''}",
                                              (pii, value))))
    return hits


def _run_enum(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    allowed = set(rule.config.get("values", []))
    skip_empty = rule.config.get("skip_empty", False)   # None 与空串都跳过，与引用校验同一语义（P2-1569）
    pii = _pii_masker(model, field)   # P2-1566
    hits = []
    for row in _filtered(db, model, rule, field):
        value = getattr(row, field, None)
        if skip_empty and _is_blank(value):
            continue
        if value not in allowed:
            hits.append((row.id, _with_masked(f"{field}={value} 不在允许取值 {sorted(allowed)} 内", (pii, value))))
    return hits


def _run_cross_ref(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    skip_empty = rule.config.get("skip_empty", False)
    if rule.config.get("ref_code_system"):
        system_code = rule.config["ref_code_system"]
        system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
        valid = (
            {c for (c,) in db.query(CodeEntry.code).filter(CodeEntry.system_id == system.id).all()}
            if system
            else set()
        )
        ref_desc = f"{system_code} 字典"
    else:
        ref_model = _TABLE_MODELS.get(rule.config.get("ref_table", ""))
        if ref_model is None:
            raise HTTPException(status_code=422, detail=f"规则 {rule.code} 的引用表未登记")
        ref_field = getattr(ref_model, rule.config.get("ref_field", "code"))
        valid = {v for (v,) in db.query(ref_field).all()}
        ref_desc = f"{rule.config['ref_table']} 目录"
    pii = _pii_masker(model, field)   # P2-1566
    hits = []
    for row in _filtered(db, model, rule, field):
        value = getattr(row, field, None)
        if skip_empty and _is_blank(value):
            continue
        if value not in valid:
            hits.append((row.id, _with_masked(f"{field}={value or '空'} 不存在于 {ref_desc}", (pii, value))))
    return hits


def _run_logic(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    check = _LOGIC_CHECKS.get(rule.config.get("check", ""))
    if check is None:
        raise HTTPException(
            status_code=422, detail=f"规则 {rule.code} 的逻辑校验 {rule.config.get('check')} 未实现"
        )
    return check(db, rule, model)


_EXECUTORS = {
    "required": _run_required,
    "range": _run_range,
    "enum": _run_enum,
    "cross_ref": _run_cross_ref,
    "logic": _run_logic,
}


def _is_field(model, name) -> bool:
    return isinstance(name, str) and bool(name.strip()) and hasattr(model, name)


def _is_scalar(value) -> bool:
    """单个取值（文字 / 数 / true / false / null）：filter 按「列 = 取值」绑进 SQL，枚举取值要放进集合，列表与对象都不行。"""
    return value is None or isinstance(value, (str, int, float, bool))


def _column_type(model, name: str):
    """列的 Python 类型；不是列、或类型报不出来时返回 None（不查）。"""
    column = sa_inspect(model).columns.get(name)
    if column is None:
        return None
    try:
        return column.type.python_type
    except NotImplementedError:  # pragma: no cover - 自定义类型没实现 python_type
        return None


def _range_reversed(low, high) -> bool:
    """区间下限大于上限（两端同为数、或同为文字时才比；布尔不算数）。"""
    if isinstance(low, bool) or isinstance(high, bool):
        return False
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return low > high
    if isinstance(low, str) and isinstance(high, str):
        return low > high
    return False


def rule_config_problem(target_table: str, rule_type: str, config: dict) -> str:
    """规则配置里会让扫描抛错、或让规则悄悄失效的写法，没问题返回空串（P2-81）。

    原先建 / 改规则只查被检表登记与类型枚举，配置照单全收：区间的界与列类型对不上、枚举取值不是列表、
    引用列不存在、引用表未登记、逻辑校验未实现、`filter` 不是对象——**任何一条**都让「汇总」「运行检查」整体
    500 / 422，整个质控看板打不开。字段名写错的不报错却悄悄失效（`datetime_order` 的起止列不在就一条也不判）。
    建 / 改规则时 422；扫描时存量里的坏规则跳过，在出参 `skipped_rules` 里点名，其余规则照常扫。
    """
    model = _TABLE_MODELS.get(target_table)
    if model is None:
        return f"被检表 {target_table} 未登记"
    config = config or {}
    if not isinstance(config, dict):
        return "配置要写成对象"
    bad = non_finite_path(config, "config")   # 区间的界写成 NaN：和谁比都不成立，这条规则一条也判不出（P2-466）
    if bad:
        return f"{bad} 不能是 NaN / Infinity"
    row_filter = config.get("filter")
    if row_filter is not None:
        if not isinstance(row_filter, dict):
            return "filter 要写成 {字段: 取值} 这样的对象"
        wrong = [key for key in row_filter if not _is_field(model, key)]
        if wrong:
            return f"filter 里的 {'、'.join(map(str, wrong))} 不是 {target_table} 的字段"
        # 取值只收单个值（P2-1123）：想表达「属于这几种」写成列表很自然，可绑定参数不收列表——原先照存，之后汇总与
        # 运行检查整体 500，质控页打不开，页上的停用按钮也就点不到
        listed = [key for key, value in row_filter.items() if not _is_scalar(value)]
        if listed:
            return f"filter 里 {'、'.join(listed)} 的取值要写成单个值（按「字段 = 取值」过滤，不支持列表或对象）"
    for flag in ("skip_empty", "exclusive_min", "exclusive_max"):
        if flag in config and not isinstance(config[flag], bool):
            return f"{flag} 只能是 true / false"
    field = config.get("field", "")
    if rule_type in ("required", "range", "enum", "cross_ref") and not _is_field(model, field):
        return f"field（{field or '空'}）不是 {target_table} 的字段"
    if rule_type == "range":
        kind = _column_type(model, field)
        for key in ("min", "max"):
            bound = config.get(key)
            if bound is None:
                continue
            if kind is str and not isinstance(bound, str):
                return f"{field} 是文字列，区间的 {key} 也要写成文字"
            if kind in (int, float) and (isinstance(bound, bool) or not isinstance(bound, (int, float))):
                return f"{field} 是数值列，区间的 {key} 必须是数"
            if kind not in (None, str, int, float):
                return f"{field} 不是数值或文字列，不能按区间判定"
        low, high = config.get("min"), config.get("max")
        if _range_reversed(low, high):
            # 下限不大于上限（P2-712，与慢专病目标「下限不得大于上限」同一句）：min 300 / max 50 把每一行都判成违规
            return f"区间下限 {low} 大于上限 {high}，每一行都会判成违规"
    if rule_type == "enum":
        values = config.get("values", [])
        if not isinstance(values, list):
            return "values 要写成取值列表"
        if not all(_is_scalar(v) for v in values):   # 多套一层方括号：放不进集合，扫描即 500（P2-1123）
            return "values 里每个取值都要写成单个值（文字或数），不能再套一层列表或对象"
        # 取值按列类型查（P2-1568，照上面区间那条）：整数列 `chronic_patients.level` 写成 ["1", "2", "3"]，原先 201，扫描时
        # 1 不在 {"1", "2", "3"} 里、整表判违规；区间同样的写法早就 422。null 表示「这一列为空也算合规」，不跟列类型比
        kind = _column_type(model, field)
        for value in values:
            if value is None:
                continue
            if kind is str and not isinstance(value, str):
                return f"{field} 是文字列，枚举的取值也要写成文字"
            if kind in (int, float) and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return f"{field} 是数值列，枚举的取值必须是数"
            if kind is bool and not isinstance(value, bool):
                return f"{field} 只有是 / 否两种取值，枚举的取值只能写 true / false"
            if kind not in (None, str, int, float, bool):
                return f"{field} 不是文字、数值或是否列，不能按枚举判定"
    if rule_type == "cross_ref":
        if config.get("ref_code_system"):
            if not isinstance(config["ref_code_system"], str):
                return "ref_code_system 要写成字典编码"
            if config["ref_code_system"] not in SYSTEM_CODES:
                # 字典编码只认统一编码字典登记的那几个（P2-1568，与 `dictionaries.SYSTEM_CODES` 同一份）：写成 icd10（实为
                # diagnosis）原先 201，扫描时字典查不到、合法集合为空，字典里明明有的诊断全判 error；引用表写错早就 422
                return f"字典 {config['ref_code_system']} 未登记（可选：{'、'.join(sorted(SYSTEM_CODES))}）"
        else:
            # 引用表名只收文字（P2-1582）：写成列表 / 对象时原先拿去查表名字典，抛 TypeError——建规则 500，存量里有一条这样的，
            # 汇总与运行检查整体 500（P2-1123 同一类）
            ref_table = config.get("ref_table", "")
            ref_model = _TABLE_MODELS.get(ref_table) if isinstance(ref_table, str) else None
            if ref_model is None:
                return f"引用表 {config.get('ref_table') or '空'} 未登记"
            if not _is_field(ref_model, config.get("ref_field", "code")):
                return f"ref_field（{config.get('ref_field', 'code')}）不是 {config['ref_table']} 的字段"
    if rule_type == "logic":
        check = config.get("check", "")
        if not isinstance(check, str):
            # 校验名只收文字（P2-1582）：写成列表 / 对象时下面的 `in` 抛 TypeError，建规则 500、存量让汇总与运行检查整体 500
            return f"check 要写成逻辑校验名（可选：{'、'.join(sorted(_LOGIC_CHECKS))}）"
        if check not in _LOGIC_CHECKS:
            return f"逻辑校验 {check or '空'} 未实现（可选：{'、'.join(sorted(_LOGIC_CHECKS))}）"
        own = _OWN_TABLE_CHECKS.get(check)
        if own is not None and target_table != own.__tablename__:
            # 这两个校验不看被检表，违规明细却按被检表标表名（P2-1569）：critical_closed_loop 配到 patients 上，明细把危急值
            # 报告号标成「patients · 记录 1」，库里恰好也有 1 号患者
            return f"逻辑校验 {check} 扫的是 {own.__tablename__}，被检表要写 {own.__tablename__}"
        if row_filter and check in _OWN_TABLE_CHECKS:
            # 上面对所有类型都校验并放行 filter，这两个校验扫描时却根本不用它：配了照样 200、照样扫全部（P2-1567）——
            # 与 P2-81 修过的「filter 写错被忽略、扫了全表」同一种后果。空的 {} 等于没写，照收
            return (f"逻辑校验 {check} 不支持 filter（它按自己的条件扫 {_OWN_TABLE_CHECKS[check].__tablename__}，"
                    "filter 写了也不起作用）")
        if check in ("id_card_checksum", "date_not_future") and not _is_field(model, config.get(
                "field", "id_card" if check == "id_card_checksum" else "")):
            return f"field（{config.get('field') or '空'}）不是 {target_table} 的字段"
        if check == "datetime_order":
            wrong = [config.get(k) or "空" for k in ("start_field", "end_field") if not _is_field(model, config.get(k))]
            if wrong:
                return f"起止字段 {'、'.join(map(str, wrong))} 不是 {target_table} 的字段"
        if check == "chronic_followup_indicator":
            mapping = config.get("disease_indicators", {})
            if not isinstance(mapping, dict) or not all(
                    isinstance(fields, list) and all(isinstance(f, str) for f in fields) for fields in mapping.values()):
                return "disease_indicators 要写成 {病种编码: [指标字段, …]} 这样的对象"
    return ""


def run_rule(db: Session, rule: QcRule) -> list[dict]:
    """执行单条规则，返回违规明细（规则/表/记录id/问题描述/严重度）。"""
    model = _TABLE_MODELS.get(rule.target_table)
    if model is None:
        raise HTTPException(
            status_code=422, detail=f"规则 {rule.code} 的被检表 {rule.target_table} 未登记"
        )
    executor = _EXECUTORS.get(rule.rule_type)
    if executor is None:
        raise HTTPException(status_code=422, detail=f"规则 {rule.code} 的类型 {rule.rule_type} 不支持")
    return [
        {
            "rule_code": rule.code,
            "rule_name": rule.name,
            "rule_type": rule.rule_type,
            "severity": rule.severity,
            "table": rule.target_table,
            "record_id": record_id,
            "message": message,
        }
        for record_id, message in executor(db, rule, model)
    ]


def _active_rules(db: Session, rule_code: str | None = None, target_table: str | None = None):
    query = db.query(QcRule).filter(QcRule.active.is_(True))
    if rule_code:
        query = query.filter(QcRule.code == rule_code)
    if target_table:
        query = query.filter(QcRule.target_table == target_table)
    return query.order_by(QcRule.code).all()


class ViolationOut(BaseModel):
    """违规明细行：字段与顺序精确镜像 `run_rule` 的产出。"""

    rule_code: str
    rule_name: str
    rule_type: str
    severity: str
    table: str
    record_id: int
    message: str


class SkippedRuleOut(BaseModel):
    rule_code: str
    rule_name: str
    #: 配置哪里写坏了（`rule_config_problem` 的文案），或扫描时抛了什么错（`_scan_rule`，P2-1123）
    problem: str


class RunChecksOut(BaseModel):
    total: int
    error_total: int
    warn_total: int
    offset: int
    limit: int
    items: list[ViolationOut]
    #: 配置写坏、本次没扫的规则（P2-81，只加字段）：原先任何一条都让整次扫描 500
    skipped_rules: list[SkippedRuleOut]


def _usable_rules(rules: list[QcRule]) -> tuple[list[QcRule], list[dict]]:
    """把配置写坏的规则（修前存进去的）拣出来：不让一条规则拖垮整次扫描（P2-81）。"""
    usable, skipped = [], []
    for rule in rules:
        problem = rule_config_problem(rule.target_table, rule.rule_type, rule.config or {})
        if problem:
            skipped.append({"rule_code": rule.code, "rule_name": rule.name, "problem": problem})
        else:
            usable.append(rule)
    return usable, skipped


def _scan_rule(db: Session, rule: QcRule, skipped: list[dict]) -> list[dict] | None:
    """执行一条规则；扫描时抛错的跳过并点名（写明异常类）、返回 None，其余规则照常扫（P2-1123，口径同 P2-81）。

    `rule_config_problem` 认不全的写法照样存得进去：filter 的取值写成列表、枚举取值多套一层方括号，原先建规则 201，
    之后汇总与运行检查整体 500——质控页载入就取汇总，页打不开，页上的停用按钮也就点不到，只能直接调接口。那两种已在
    写入口拦下，这里兜住还没认出来的（真 PG 上列与取值的类型对不上、枚举取值文字与数混写时拼违规说明排不了序之类）。
    出错先回滚：真 PG 上一条语句出错整个事务作废，不回滚后面的规则一条也扫不了；汇总与运行检查都只读，回滚不丢东西。
    错误全文进日志。"""
    code, name = rule.code, rule.name
    try:
        return run_rule(db, rule)
    except Exception as exc:  # noqa: BLE001 - 一条规则扫挂了不拖垮整次扫描
        db.rollback()
        logger.exception("数据质控规则 %s 扫描出错，本次跳过", code)
        reason = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__
        skipped.append({"rule_code": code, "rule_name": name, "problem": f"扫描时出错（{reason}），这次没扫，请核对配置"})
        return None


@router.get("/run", response_model=RunChecksOut)
def run_checks(
    response: Response,
    rule_code: str | None = None,
    target_table: str | None = None,
    severity: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按启用规则扫描现有数据，返回违规明细（停用规则不参与扫描）。"""
    violations: list[dict] = []
    rules, skipped = _usable_rules(_active_rules(db, rule_code, target_table))
    for rule in rules:
        if severity and rule.severity != severity:
            continue
        violations.extend(_scan_rule(db, rule, skipped) or [])
    total = len(violations)
    limit = min(max(limit, 1), 1000)
    response.headers["X-Total-Count"] = str(total)
    page = violations[max(offset, 0) : max(offset, 0) + limit]
    if user.role != "admin":
        # 违规说明里的 PII 列取值非 admin 一律掩码（P2-1566，与患者检索走 `privacy.desensitize` 同一口径）：原先规则落在
        # 证件号上时，药师调这里读到的是明文身份证号——开着 PII 列加密时读出来的也已是解密后的明文
        for v in page:
            v["message"] = getattr(v["message"], "masked", v["message"])
    return {
        "total": total,
        "error_total": sum(1 for v in violations if v["severity"] == "error"),
        "warn_total": sum(1 for v in violations if v["severity"] == "warn"),
        "offset": max(offset, 0),
        "limit": limit,
        "items": page,
        "skipped_rules": skipped,
    }


class SummaryRuleOut(BaseModel):
    rule_code: str
    rule_name: str
    rule_type: str
    rule_type_name: str
    table: str
    severity: str
    violations: int


class SummaryOut(BaseModel):
    rules_checked: int
    total: int
    #: 键固定 error/warn（函数头初始化两键；启用规则逐条累加）
    by_severity: dict[str, int]
    #: 键是启用规则的被检表实际取值（数据决定）
    by_table: dict[str, int]
    by_rule: list[SummaryRuleOut]
    #: 配置写坏、本次没扫的规则（P2-81，只加字段）
    skipped_rules: list[SkippedRuleOut]


@router.get("/summary", response_model=SummaryOut)
def summary(db: Session = Depends(get_db)):
    """违规汇总：按规则、按严重度、按被检表三个维度。"""
    by_rule, by_severity = [], {"error": 0, "warn": 0}
    by_table: dict[str, int] = {}
    rules, skipped = _usable_rules(_active_rules(db))
    for rule in rules:
        hits = _scan_rule(db, rule, skipped)
        if hits is None:
            continue
        by_rule.append(
            {
                "rule_code": rule.code,
                "rule_name": rule.name,
                "rule_type": rule.rule_type,
                "rule_type_name": RULE_TYPES.get(rule.rule_type, rule.rule_type),
                "table": rule.target_table,
                "severity": rule.severity,
                "violations": len(hits),
            }
        )
        by_severity[rule.severity] = by_severity.get(rule.severity, 0) + len(hits)
        by_table[rule.target_table] = by_table.get(rule.target_table, 0) + len(hits)
    return {
        "rules_checked": len(by_rule),
        "total": sum(r["violations"] for r in by_rule),
        "by_severity": by_severity,
        "by_table": by_table,
        "by_rule": by_rule,
        "skipped_rules": skipped,
    }


# ---------- 规则维护（限管理员） ----------


class RuleCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    target_table: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    rule_type: str = Field(pattern="^(required|range|enum|cross_ref|logic)$")
    config: dict = Field(default_factory=dict)
    severity: str = Field(default="error", pattern="^(error|warn)$")
    active: bool = True


class RuleUpdate(BaseModel):
    # 改档与建档同口径（P1-98）：原先改名为空串照收
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=NON_BLANK)
    config: dict | None = None
    severity: str | None = Field(default=None, pattern="^(error|warn)$")
    active: bool | None = None


def _rule_out(r: QcRule) -> dict:
    return {
        "id": r.id,
        "code": r.code,
        "name": r.name,
        "target_table": r.target_table,
        "rule_type": r.rule_type,
        "rule_type_name": RULE_TYPES.get(r.rule_type, r.rule_type),
        "config": r.config,
        "severity": r.severity,
        "severity_name": SEVERITIES.get(r.severity, r.severity),
        "active": r.active,
        "builtin": r.code in BUILTIN_RULE_CODES,
    }


class QcRuleOut(BaseModel):
    """字段与顺序精确镜像 `_rule_out`（新建回执与列表行同形）。"""

    id: int
    code: str
    name: str
    target_table: str
    rule_type: str
    rule_type_name: str
    #: JSON 列，结构随 rule_type 而异（见模块 docstring），宽字典透传
    config: dict[str, Any]
    severity: str
    severity_name: str
    active: bool
    #: 内置规则（种子）：只能停用、不能删（P2-564）
    builtin: bool


@router.get("/rules", response_model=list[QcRuleOut])
def list_rules(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(QcRule)
    if active is not None:
        query = query.filter(QcRule.active.is_(active))
    return [_rule_out(r) for r in query.order_by(QcRule.code).all()]


@router.post(
    "/rules", status_code=201, response_model=QcRuleOut, dependencies=[Depends(require_admin)]
)
def create_rule(body: RuleCreate, db: Session = Depends(get_db)):
    if db.query(QcRule).filter(QcRule.code == body.code).first():
        raise HTTPException(status_code=409, detail="规则编码已存在")
    if body.target_table not in _TABLE_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"被检表未登记（可选：{'、'.join(sorted(_TABLE_MODELS))}）",
        )
    problem = rule_config_problem(body.target_table, body.rule_type, body.config)   # P2-81
    if problem:
        raise HTTPException(status_code=422, detail=f"规则配置非法：{problem}")
    rule = insert_or_conflict(db, QcRule(**body.model_dump()), "规则编码已存在")
    return _rule_out(rule)


@router.patch(
    "/rules/{rule_id}", response_model=QcRuleOut, dependencies=[Depends(require_admin)]
)
def update_rule(rule_id: int, body: RuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(QcRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    if body.config is not None:   # 与建规则同一句（P2-81）
        problem = rule_config_problem(rule.target_table, rule.rule_type, body.config)
        if problem:
            raise HTTPException(status_code=422, detail=f"规则配置非法：{problem}")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


class RuleDeleteOut(BaseModel):
    deleted: int


@router.delete(
    "/rules/{rule_id}", response_model=RuleDeleteOut, dependencies=[Depends(require_admin)]
)
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(QcRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    if rule.code in BUILTIN_RULE_CODES:
        # 原先照删、回 200：下次启动种子按编码补回来，删了等于没删，本地的停用与严重度调整反倒丢了（P2-564）
        raise HTTPException(status_code=409, detail="内置规则不能删除（删了下次启动会按种子补回），不用请停用")
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}
