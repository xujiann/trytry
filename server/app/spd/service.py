"""全域慢专病 · 领域服务层：跨路由复用的业务动作。

放在这里而不是各路由内联，理由与 `visibility.scope_patient_list` 同一条：
**没抽出来的正确做法等于没有**。以下五件事分别有 3~6 个调用点，
内联就意味着以后有人只改了其中一处：

1. `build_facts`      —— 汇集患者事实供规则求值（筛查、纳入判定、转诊触发共用）
2. `start_path`/`advance_path` —— 路径实例推进与任务派生
3. `spawn_task`       —— 统一任务生成（路径、随访、干预、复诊都从这里出）
4. `award_points`     —— 村医积分入账（签约、上转、随访、上报四处触发）
5. `close_open_work`  —— 死亡/迁出/排除时终止后续任务（三处生命周期事件共用）
"""
import math
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy import String, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .. import clock
from ..clock import now_naive
from ..concurrency import add_amount, ensure_present, insert_if_absent, serialized_on
from ..numtypes import non_finite_path
from .platform import (User, diagnosis_codes, diagnosis_names, notify_user, patient_of, unusable_user,
                       usable_or_none)
from .models import (
    SpdCallTask,
    SpdCandidate,
    SpdDevice,
    SpdEnrollment,
    SpdFollowupRecord,
    SpdFollowupRule,
    SpdIntervention,
    SpdMeasurement,
    SpdPathInstance,
    SpdPathNode,
    SpdPackageUsage,
    SpdPathTemplate,
    SpdPointAccount,
    SpdPointRecord,
    SpdPointRule,
    SpdProgram,
    SpdReferralCase,
    SpdReferralStep,
    SpdRevisit,
    SpdScale,
    SpdScreening,
    SpdTarget,
    SpdTask,
    SpdVillageDoctor,
)
from .rules import FIELD_SOURCES, evaluate, judge_level, scale_problem

#: 慢专病服务工作（任务、目标患者、复诊、干预、路径）的办理角色：各路由文件 `require_roles(*SERVICE_ROLES)` 的那一组。
#: 路由各自留一份同名常量（角色守卫的静态扫描按文件内的模块级常量认星号展开），这一份给系统替人挑责任人用
#: （`usable_or_none`，第二十二批 X3-1），两边相等由 `tests/test_spd_role_unfit_assignee.py` 钉住
SERVICE_ROLES = ("doctor", "public_health", "director")
#: 在线咨询接诊的角色：`care.py` 咨询回复 / 结束两个端点的 `require_roles("doctor", "director")`，同由上面的用例钉住
CONSULT_ROLES = ("doctor", "director")

#: 监测指标在 facts 里的键就是 `SpdMeasurement.metric`，与 `spd/rules.py::FIELD_SOURCES` 对齐。
MEASURE_FIELDS = (
    "bp_sys", "bp_dia", "glucose_fasting", "glucose_pp2h", "hba1c", "ua", "spo2",
    "bmi", "ldl", "creatinine", "egfr",
)

#: 百分数指标的上限（P1-101）
_PERCENT_MEASURES = {"spo2": 100.0, "hba1c": 100.0}


def measure_value_problem(metric: str, value: float) -> str | None:
    """监测值的生理可能性（P1-101）：有问题返回一句人话（调用方报 422），没问题返回 None。

    上面这些指标测出 0 / 负数只能是设备失败或录错；管理目标多数只设上限（收缩压 ≤ 140、糖化 ≤ 7），
    按目标判级时 0 就判成「正常」，该有的异常提醒就此漏掉。不在指标目录里的键不管——那些指标的口径由配置决定。
    """
    if metric not in MEASURE_FIELDS:
        return None
    name = FIELD_SOURCES.get(metric, metric)
    if value <= 0:
        return f"{name}须为正数（收到 {value:g}）"
    cap = _PERCENT_MEASURES.get(metric)
    if cap is not None and value > cap:
        return f"{name}不得超过 {cap:g}%（收到 {value:g}）"
    return None


def answers_problem(items: list | None, answers: dict) -> str | None:
    """随访问卷作答的取值（P2-711）：数值题的作答读不成有限的数、或题目编码是监测指标目录里的（收缩压、空腹血糖……）
    却过不了 `measure_value_problem`，返回一句人话（调用方报 422）；没问题返回 None。

    医护执行与居民自助作答原先把作答原样交给 `grade_abnormal`：异常规则按数比、读不成数就判不命中——收缩压答 0、
    -185、「185/110」都照收，一条不命中、判「无异常」、不派处置任务；同一个指标走监测录入，0 早就 422（P1-101）。
    没作答的题、非数值题不管（问卷不强制每题必答）；读得成数的文本（"185"）照收，与规则求值同一个读法。
    """
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "number":
            continue
        key = item.get("key")
        if not isinstance(key, str):
            continue
        raw = answers.get(key)
        if raw is None or raw == "":
            continue
        title = item.get("title") or key
        try:
            value = math.nan if isinstance(raw, bool) else float(raw)
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            return f"「{title}」须填一个数（收到 {raw}）"
        problem = measure_value_problem(key, value)
        if problem:
            return f"「{title}」：{problem}"
    return None


def _age_of(birth_date: str) -> int | None:
    try:
        born = date.fromisoformat(birth_date)
    except (ValueError, TypeError):
        return None
    today = clock.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    # 出生日期在将来（P2-713 之前建档 / 更正存下的）算出负数：当「不知道」，不拿它去比「未满 18 岁」
    return age if age >= 0 else None


def build_facts(db: Session, patient_id: int, extra: dict | None = None, *, answers: dict | None = None) -> dict:
    """汇集一名患者的事实字典，供纳入/排除/转诊规则求值。

    诊断取自 `platform.diagnosis_codes`（全部历史就诊 + ICD 父目，理由见那里）；
    指标取最近一次值——那才是"现在控制得怎么样"。

    `extra` 由调用方给定、盖在事实上（档案的风险等级 / 阶段、试算时手填的事实）；`answers` 是问卷答案，**只补库里
    推不出来的**（P2-368）：原先筛查登记把答案当 `extra` 整个盖上去——种子「糖尿病高危筛查问卷」有一题键名就叫 `age`
    （「年龄≥45岁」），76 岁的患者年龄成了「是」，按年龄写的纳入规则全不命中；题目键名叫 `diagnosis` 的还会把诊断整个换掉。
    """
    facts: dict = {}
    patient = patient_of(db, patient_id)
    if patient is not None:
        facts["age"] = _age_of(patient.birth_date)
        facts["gender"] = patient.gender
    # 诊断怎么取（全部历史 + ICD 父目）是**平台数据的形状**，实现放适配层，
    # 这里只管把它填进事实字典
    facts["diagnosis"] = diagnosis_codes(db, patient_id)
    facts["diagnosis_name"] = diagnosis_names(db, patient_id)

    for metric in MEASURE_FIELDS:
        latest = (
            db.query(SpdMeasurement)
            .filter(SpdMeasurement.patient_id == patient_id, SpdMeasurement.metric == metric)
            # 同一测定时刻的多条（批量导入 / 设备上传）取后录的那条，与监测清单排第一的是同一条（P2-707）
            .order_by(SpdMeasurement.measured_at.desc(), SpdMeasurement.id.desc())
            .first()
        )
        if latest is not None:
            facts[metric] = latest.value
    if answers:
        facts.update({k: v for k, v in answers.items() if v is not None and k not in facts})
    if extra:
        facts.update({k: v for k, v in extra.items() if v is not None})
    return facts


def unknown_program(db: Session, code: str, *, active_only: bool = False, already: str = "") -> str:
    """请求体里的病种编码写库之前先查它在不在（P1-120）：在返回空串，不在返回报错文案，由路由决定怎么报——
    与 `platform.unusable_user` 同一写法。

    `program_code` 是字符串软外键（库里没有约束）：填错一个编码照样落库，这条记录就挂到一个不存在的病种上——
    按病种筛的清单、规则匹配、统计口径从此永远不含它；考核计分还会按这个编码筛出一片空数据，把这一期的
    正式分数覆盖成零。空串是「不限病种 / 通用」，照收。

    `active_only`：新纳入（居民自查筛查、申请加入）不收停用的病种，与建档 / 筛查同一口径（P1-89）；在管患者
    的业务记录与各类配置照收停用病种——病种停用不等于在管的人当天就不管了，配置也可能是为重新启用备的。

    `already` 是改档前的值：与它相同即不再查——存量里悬空的编码不挡与它无关的改动（改名、停用），与
    `update_team` 对负责人的口径一致（P1-121 一并补上）。
    """
    if not code or code == already:
        return ""
    program = db.query(SpdProgram).filter(SpdProgram.code == code).first()
    if active_only:
        return "专病档案不存在或已停用" if program is None or not program.active else ""
    return "专病档案不存在" if program is None else ""


def unknown_programs(db: Session, codes: list[str] | None, *, already: list[str] | None = None) -> str:
    """列表形态的病种编码（团队 / 团队成员 / 考核指标 / 考核方案的 `program_codes`）写库之前先查（P1-120 第二层）。

    与 `unknown_program` 同一口径：空串不算编码、停用的病种照收（配置）；`already`（改档前的值）里原有的不再查。
    返回点名不存在编码的文案，没问题返回空串。"""
    wanted = [c for c in dict.fromkeys(codes or []) if c and c not in (already or [])]
    if not wanted:
        return ""
    known = {c for (c,) in db.query(SpdProgram.code).filter(SpdProgram.code.in_(wanted)).all()}
    missing = [c for c in wanted if c not in known]
    return f"专病档案不存在：{'、'.join(missing)}" if missing else ""


def unknown_ids(db: Session, model: Any, ids: list[int] | None, what: str, *, already: list[int] | None = None) -> str:
    """列表形态的整数编号（报告推送的机构……）写库之前先查在不在（P1-124）：点名不存在的那几个，没问题返回空串。

    这类列是 JSON 列表，库里没有外键；可它们到了用的时候往往要写进带外键的列（报告实例的机构），
    填错一个，用的那一刻撞约束。`already`（改档前的值）里原有的不再查，与 `unknown_programs` 同一口径。"""
    wanted = [i for i in dict.fromkeys(ids or []) if i not in (already or [])]
    if not wanted:
        return ""
    known = {i for (i,) in db.query(model.id).filter(model.id.in_(wanted)).all()}
    missing = [str(i) for i in wanted if i not in known]
    return f"{what}不存在：{'、'.join(missing)}" if missing else ""


def unknown_code(db: Session, model: Any, code: str, what: str, *, already: str = "") -> str:
    """请求体里指向目录表的字符串编码（随访问卷、宣教素材、转诊规则）写库之前先查在不在（P1-121）。

    目录表的编码列都叫 `code`（唯一）。空串 = 没填，照收；只查存在、不看启用——配置先于启用、历史引用都合法；
    `already` 同 `unknown_program`。最重的是随访方案的问卷：执行随访时按编码查不到问卷，异常分级整段跳过，
    高危答案记成「无异常」、不派处置任务。
    """
    if not code or code == already:
        return ""
    found = db.query(model.id).filter(model.code == code).first()
    return "" if found is not None else f"{what}不存在：{code}"


def package_items_ok(items: list | None) -> bool:
    """服务包的项目都有编码、次数读得成正整数（P2-82）。

    绑定服务包时按 `int(times)` 折成可用次数：次数写成文字，绑定即 `ValueError`、500。建服务包原先在校验这一句
    里就 `int()` 抛错（500），改服务包干脆不查。读得成整数的照旧算数（"3"、2.5）。
    次数写成 Infinity：`int(inf)` 抛的是 OverflowError，不在下面接住的两种里，建服务包即 500（P2-466）。"""
    if non_finite_path(items, "items"):
        return False
    for item in items or []:
        if not isinstance(item, dict) or not item.get("code"):
            return False
        try:
            if int(item.get("times", 0)) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def scale_unusable(scale: SpdScale) -> str:
    """作答前查量表配置（P2-80）：修前存进去的坏量表（选项写成字符串、分值写成文字……）作答即 500；
    现在返回说清楚的文案，由路由报 422。没问题返回空串。"""
    problem = scale_problem(scale.items or [], scale.scoring or {})
    return f"量表配置有误（{problem}），暂不能作答，请联系管理员修正" if problem else ""


def scale_version_problem(scale: SpdScale, answered_id: int | None) -> str:
    """作答的那一版不是现行发布版时说出来（P2-921）；没带作答版本的旧调用照旧按现行版评分，返回空串。

    筛查、评估、居民自查都按量表编码取「最新发布」的那一版评分，还把那一版记进评估记录；页面按载入时的目录出题、提交只送
    编码。页面打开之后发布了同编码的新版（或停用了作答的那一版、回落到旧版），这一页上照旧弹出原来的题目，提交后按另一版的
    题目与分段评分（按 v1 作答 5 分高危，按 v2 评成 0 分低危），评估记录还写成另一版——「量表版本随记录固化」对不上
    （P1-136 ③ 后端那一半）。调用方带上作答的那一版，不是现行版就 409，请刷新后重答。
    """
    if answered_id is None or answered_id == scale.id:
        return ""
    return f"量表「{scale.name}」现行发布的是 {scale.version}，不是作答时的那一版，题目与评分可能已变，请刷新后重答"


def scale_program_mismatch(scale: SpdScale, program_code: str, what: str) -> str:
    """量表挂在病种上（空串是通用量表）：拿别的病种的量表给这个病种筛查 / 评估，按那张量表的分数判高危——筛查即进
    这个病种的疑似目标池，评估即回写这个病种档案的风险等级、高危自动派干预与复诊（P2-98）。对得上返回空串。"""
    if scale.program_code and program_code and scale.program_code != program_code:
        return f"{what}量表的病种与{what}病种不一致"
    return ""


def match_program(
    db: Session, patient_id: int, program: SpdProgram, extra: dict | None = None, *, answers: dict | None = None,
):
    """对单个病种做纳入/排除判定，返回 `spd/rules.py::screen` 的结果 + 使用的规则版本。`answers` 见 `build_facts`。"""
    from .rules import screen

    facts = build_facts(db, patient_id, extra, answers=answers)
    result = screen(program.include_rules or [], program.exclude_rules or [], facts)
    result["program_code"] = program.code
    result["rule_version"] = program.version
    return result


def exclusion_problem(
    db: Session, patient_id: int, program: SpdProgram, extra: dict | None = None, *, answers: dict | None = None,
) -> str:
    """这位患者命中这个病种的排除规则时说出来（P2-935），没命中返回空串。

    医护筛查、批量识别、就诊触发都先跑排除规则、排除压过量表高危；复核只认「疑似」（P2-591：排除规则挡在门外的人——
    比如未成年——不能经复核改回目标人群）。居民自查、服务申请、受理、复核这一路原先不跑：15 岁的居民自查高危、申请、
    受理，目标池那一行从「排除」翻成「目标」，随后签约建档进了成人高血压管理。"""
    matched = match_program(db, patient_id, program, extra, answers=answers)
    if matched["result"] != "excluded":
        return ""
    return "按病种规则不纳入（" + "；".join(str(m.get("label") or m.get("field")) for m in matched["excluded_by"]) + "）"


def target_for(db: Session, program_code: str, stage: str, metric: str) -> SpdTarget | None:
    """取某病种某指标的管理目标，三级回落：本阶段 → 不分阶段 → 该病种任一阶段。

    第三级回落是刻意的：患者常常停在"筛查""诊断评估"这类前置阶段，而目标通常
    只配在"治疗干预""稳定期"上。没有回落的话，一个收缩压 178 的筛查期患者
    会被判成"正常"——因为他所在的阶段没配目标。**配了目标就该用上**，
    比"这个阶段没配所以不判"更接近临床预期。
    """
    program = db.query(SpdProgram).filter(SpdProgram.code == program_code).first()
    if program is None:
        return None
    query = db.query(SpdTarget).filter(
        SpdTarget.program_id == program.id,
        SpdTarget.metric == metric,
        SpdTarget.active.is_(True),
    )
    return (
        query.filter(SpdTarget.stage == stage).first()
        or query.filter(SpdTarget.stage == "").first()
        or query.order_by(SpdTarget.id).first()
    )


def actively_enrolled(db: Session, patient_id: int, program_code: str) -> bool:
    """这位患者这个病种有没有在管档案。目标池说的「已在池中（含已纳管）」要把它算上（P2-359）：直接走签约建档纳管的
    患者（没经过目标池）在池里没有行，原先就诊事件识别、批量自动识别、登记筛查照样把他按疑似插进池里——在管的人又成了
    疑似，认领、签约一路 409。"""
    return (
        db.query(SpdEnrollment.id)
        .filter(SpdEnrollment.patient_id == patient_id, SpdEnrollment.program_code == program_code,
                SpdEnrollment.status == "active")
        .first()
        is not None
    )


def enrollment_still_active(db: Session, enrollment_id: int) -> bool:
    """这份档案此刻还在不在管：给 `serialized_on(db, SpdEnrollment, …)` 临界区里复判用（P2-1179）。

    只挂在管档案的新工作（启动路径、手工建任务、绑服务包、异常监测派任务、高危自动干预与复诊，P2-226）原先锁外判在管、
    锁里只查重或干脆不进锁：读到在管之后别人登记死亡并提交（结案收尾 `close_open_work` 已经跑过），这一路照旧挂上去，
    之后再没人收。按列直查而不是 `db.get` / `db.refresh`（照 `billing.create_bill_detail` 锁里复判在院的写法）：会话里
    那份档案是锁外读的，身份映射会把旧对象原样还回来；refresh 又会丢掉调用方挂在档案上、还没 flush 的改动。"""
    return db.query(SpdEnrollment.status).filter(SpdEnrollment.id == enrollment_id).scalar() == "active"


def enrollment_for(db: Session, patient_id: int, program_code: str) -> tuple[str, SpdEnrollment | None]:
    """一条业务记录挂哪份纳管档案（P1-139）：写了病种的，取这个病种的档案（在管的优先）；没写的，患者只在管一个病种
    的挂这份、病种取它的；在管几个病种的不替人猜，返回 ("", None)。

    原先没写病种就不挂档案：管理端发起转诊、批量下发干预的病种都默认留空，于是有效上转的积分记给录单的人、干预与
    它派的执行任务按档案看不到。"""
    if program_code:
        query = db.query(SpdEnrollment).filter(
            SpdEnrollment.patient_id == patient_id, SpdEnrollment.program_code == program_code
        )
        return program_code, (
            query.filter(SpdEnrollment.status == "active").first()
            or query.order_by(SpdEnrollment.id.desc()).first()
        )
    active = (
        db.query(SpdEnrollment)
        .filter(SpdEnrollment.patient_id == patient_id, SpdEnrollment.status == "active")
        .order_by(SpdEnrollment.id)   # 截断取数一律带排序（P2-69）：只数有几份，但取到的那一份要确定
        .limit(2)
        .all()
    )
    if len(active) == 1:
        return active[0].program_code, active[0]
    return "", None


def measure_program_for(db: Session, patient_id: int, program_code: str, metric: str) -> str:
    """一次监测值挂哪个病种判级（P1-138）：写了病种的照写；没写的，取这位患者**在管档案**里给这个指标配了管理目标的
    病种（按建档先后取第一个）。都没有才留空——空串没有管理目标可比，一律判「正常」。

    原先没写就是空串：管理端录入表单的病种下拉默认「全部病种」（空）、居民端自报是个「病种编码（可留空）」的文本框，
    居民不认得 hypertension 这种编码——没人写的时候，高血压在管患者收缩压 190 也判「正常」，异常处置任务一条不派，
    异常清单里没有它。"""
    if program_code:
        return program_code
    enrolled = (
        db.query(SpdEnrollment.program_code)
        .filter(SpdEnrollment.patient_id == patient_id, SpdEnrollment.status == "active")
        .order_by(SpdEnrollment.id)
        .all()
    )
    for (code,) in enrolled:
        if target_for(db, code, "", metric) is not None:
            return code
    return ""


def judge_measurement(db: Session, program_code: str, stage: str, metric: str, value) -> str:
    """按管理目标判定单次监测值的等级。没有目标就是 normal，见 `spd/rules.py::judge_level`。"""
    target = target_for(db, program_code, stage, metric)
    if target is None:
        return "normal"
    return judge_level(value, target.target_low, target.target_high)


#: 任务类型（`spd_tasks.task_type`）与优先级（`spd_tasks.priority`）的中文名：规则元数据接口给界面、报告段落印表格
#: 都取这一份（P2-644）——报告原先直接印 followup / 1，同一张表在页面上是「随访」「普通」。
TASK_TYPE_NAMES = {
    "path": "路径节点", "followup": "随访", "intervention": "干预",
    "assess": "评估", "revisit": "复诊", "referral": "转诊",
    "report": "上报", "recall": "召回", "edu": "宣教", "screen": "筛查复核",
}
TASK_PRIORITY_NAMES = {1: "普通", 2: "紧急", 3: "特急"}

#: 慢专病任务（`spd_tasks.status`）的「已结束」：办结与取消。
TASK_CLOSED_STATUSES = ("done", "cancelled")
#: 「未结束」：除了办结与取消都算，**含退回（rejected）**——审核退回即回到办理人手里重办，提交 / 办结接口
#: 照收它。原先七处各写一份这个清单、七份都漏了它：路径越过退回待重办的任务往下走、「我的待办」与各处待办数
#: 里看不到它、结案与路径取消不收它、过期不超期、催办升级 409「已结束」（P1-127）。清单只在这里写，查询里别再
#: 手写（`tests/test_spd_task_status_sets.py` 盯着）。
TASK_OPEN_STATUSES = ("pending", "claimed", "doing", "submitted", "rejected", "overdue")
#: 还在办理人手里、没提交也没超期的：超期扫描扫这些，报告的「待办」表列这些（超期的另列一表）
TASK_IN_HAND_STATUSES = ("pending", "claimed", "doing", "rejected")
#: 能「接收」（claim）的：待接收与已超期。单条与批量同一口径——批量原先按「未结束」放行，自己提交在等审核的任务
#: 批量一勾就被接收回「已接收」、拉出审核队列（P2-83）
TASK_CLAIMABLE_STATUSES = ("pending", "overdue")
#: 能「直接办结」（complete）的：未结束的除了待审核——提交了等审核的任务只能由审核人审（通过即办结、退回即重办），
#: 原先办结接口按「未结束」放行，待审核的点一下办结就绕过了审核（P2-244；两端界面早就不给待审核的摆办结，接口没挡）
TASK_COMPLETABLE_STATUSES = tuple(s for s in TASK_OPEN_STATUSES if s != "submitted")


def move_task(db: Session, task_id: int, to_status: Any, *, expect: tuple[str, ...] | str = TASK_OPEN_STATUSES,
              **values: Any) -> bool:
    """任务状态的条件翻转：`UPDATE … SET status = :to WHERE id = :id AND status IN (:expect)`，返回是否翻到（P2-114）。

    原先各处「内存里判未结束 → `task.status = …` → commit」，flush 出来的 UPDATE 只有 `WHERE id = ?`：锁外读到「未结束」
    的一路（超期扫描、结案、提交、转派、退回……）会把别人刚办结的任务改回未结束或改成取消。复活的任务再办一次，随访计分
    再记一笔——积分流水没有唯一键，「只记一次」全靠办结那一下的条件翻转（`_finish_task`）兜着；被改成取消的，已办完的工作
    从完成数里消失。要一并写的字段放进 `values`，与状态同一条 SQL；`to_status` 也可以是 SQL 表达式（按行上的现值改）。
    `expect` 只收上面几个共享集合或单个状态（审核只从「待审核」翻），别在调用处手写清单（`test_spd_task_status_sets` 盯着）。
    调用方别再往 `task.status` 上赋值：这里不同步会话里的对象，提交后按库里的值重读。"""
    return _move_row(db, SpdTask, task_id, expect, status=to_status, **values)


def _move_row(db: Session, model: Any, row_id: int, expect: tuple[str, ...] | str, **values: Any) -> bool:
    """`UPDATE model SET … WHERE id = :id AND status IN (:expect)`（单个状态即 `=`），返回是否改到（`move_task` 的通用版）。"""
    moved = db.execute(
        update(model)
        .where(model.id == row_id, model.status == expect if isinstance(expect, str) else model.status.in_(expect))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return bool(cast(CursorResult, moved).rowcount)

#: 随访记录（`spd_followup_records.status`）与复诊（`spd_revisits.status`）的「未完成」：还没做、含已超期。超期扫描把过了
#: 日期的 planned 置为 overdue，只认 planned 的查询一扫就漏掉它们——工作台的超期随访恒为 0、到期数只剩今天的、结案不收
#: 逾期复诊（P1-128）
FOLLOWUP_OPEN_STATUSES = ("planned", "overdue")
#: 手工调整（移除 / 恢复 / 改期 / 改执行人）收哪些随访记录：除「已完成」外都收——与 `update_followup_record` 的预检互为补集
#: （状态取值是封闭的五个，见 `close_followup_record`）
FOLLOWUP_ADJUSTABLE_STATUSES = ("planned", "overdue", "removed", "unreachable")
REVISIT_OPEN_STATUSES = ("planned", "overdue")
#: 路径实例（`spd_path_instances.status`）的「未结束」：执行中与暂停（暂停的能恢复）
PATH_OPEN_STATUSES = ("running", "paused")


def followup_overdue(today: str):
    """「超期随访」的判定：已标超期的 + 扫描间隙里还是 planned、日期已过的（两者都是超期；各处计数共用）。"""
    return or_(
        SpdFollowupRecord.status == "overdue",
        and_(SpdFollowupRecord.status == "planned", SpdFollowupRecord.planned_at != "",
             SpdFollowupRecord.planned_at < today),
    )


def task_overdue(today: str):
    """「超期任务」的判定：已标超期的 + 扫描间隙里还在手、截止日已过的（P2-549，与 `followup_overdue` 同一个形状）。

    超期靠 `sweep_overdue` 落状态；只有部分入口进门先扫（中心工作台、任务清单），卫健 / 团队 / 医生移动端工作台、报告、
    考核取数只数 `status == 'overdue'`——调度没跑或两次扫描之间，同一批过期任务在这些地方是 0、在中心工作台是 N。
    在手的范围与扫描同一份（`TASK_IN_HAND_STATUSES`），扫完之后两种数法结果一样。
    """
    return or_(
        SpdTask.status == "overdue",
        and_(SpdTask.status.in_(TASK_IN_HAND_STATUSES), SpdTask.due_date != "", SpdTask.due_date < today),
    )


def task_unclaimed():
    """「无人认领」的判定：没有责任人、能被接收的任务——待接收的与已超期的（P2-245）。

    中心工作台的计数与任务清单的 `unassigned=true` 共用一句（P2-825）：原先清单取不出这一格——工作台报「无人认领 N」，
    任务中心只取最新一页、又没有责任人列，是哪几条找不到。"""
    return and_(SpdTask.assignee_id.is_(None), SpdTask.status.in_(TASK_CLAIMABLE_STATUSES))


def candidate_undistributed():
    """「待分发」的判定：还没有团队、也还没有责任人的目标人群（P2-601）。

    中心工作台的计数与目标患者清单的 `unassigned=true` 共用一句（P2-825）：清单的 team_id / assigned_user_id 是整数参数，
    「为空」表达不出来（传空串 422），早入池、还没分出去的人挤出最新一页就查不到编号、分发不了。"""
    return and_(SpdCandidate.status == "target", SpdCandidate.team_id.is_(None),
                SpdCandidate.assigned_user_id.is_(None))


#: 「异常随访」的两档：答卷判出中度 / 重度——也就是会派处置任务的那两档（轻度只记不派）
FOLLOWUP_ABNORMAL_LEVELS = ("mid", "high")


def followup_abnormal():
    """「异常随访」的判定：答卷判出中度 / 重度（各处计数共用，P2-292）。

    医生移动端工作台原先自己写 `abnormal_level IN ('mid', 'high')`；随访看板的「异常随访」卡片读的 `abnormal`
    接口根本不给，恒显示 0。两处都走这一个判定。
    """
    return SpdFollowupRecord.abnormal_level.in_(FOLLOWUP_ABNORMAL_LEVELS)


#: 县级医院接收那一格的环节名（`referral._NEXT` 照它写）：下转之后，上转去的是哪家从这一步的机构取回（P2-558）
REFERRAL_ACCEPT_STEP = "县级医院接收"
REFERRAL_DOWN_STEP = "下转"
#: 轨迹每一步动作（`SpdReferralStep.action`）的中文名，与转诊页的按钮同名（P2-1022）。环节名不等于结论：县级医院那一格通过与
#: 退回写的是同一个环节名「县级医院接收」（`referral_ends` 靠它取上转去的机构，存量不改），光看环节名分不出收了还是退了
REFERRAL_ACTION_NAMES = {
    "submit": "发起", "pass": "通过", "reject": "退回", "arrive": "到院", "down": "下转", "receive": "随访接收",
    "withdraw": "撤回",
}


def referral_ends(db: Session, cases: list[SpdReferralCase]) -> dict[int, tuple[int | None, int | None]]:
    """每张转诊单的（上转目的机构, 下转目标机构）（P2-558）。

    下转时 `target_org_id` / `current_org_id` 都改写成下转目标——上转去的是哪家县医院只剩轨迹里有：县级医院接收那一步的
    机构；没有（全域账号代接收不带机构）就退到下转那一步的机构（下转由当时的持有机构办）。
    没下转过的同样先取县级医院接收那一步的机构（P2-1191）：`target_org_id` 只是发起时填的目标，可以留空（「由审核环节
    定」），接收权又按机构树、不按目标——原先一律取它，留空的接收、到院后「转入」仍是空，填了别家的写成没接收的那家。
    轨迹里没有这一步的机构（还没接收、被退回，或全域账号代接收不带机构）才取 `target_org_id`。
    """
    accept_org: dict[int, int] = {}
    down_actor_org: dict[int, int] = {}
    downed: set[int] = set()
    for case_id, step, action, org_id in (
        db.query(SpdReferralStep.case_id, SpdReferralStep.step, SpdReferralStep.action, SpdReferralStep.org_id)
        .filter(SpdReferralStep.case_id.in_([c.id for c in cases] or [0]),
                SpdReferralStep.step.in_((REFERRAL_ACCEPT_STEP, REFERRAL_DOWN_STEP)))
        .order_by(SpdReferralStep.id)
    ):
        if step == REFERRAL_ACCEPT_STEP and action == "pass" and org_id is not None:
            accept_org[case_id] = org_id
        elif step == REFERRAL_DOWN_STEP:
            downed.add(case_id)
            if org_id is not None:
                down_actor_org[case_id] = org_id
    return {
        c.id: ((accept_org.get(c.id) or down_actor_org.get(c.id)), c.target_org_id) if c.id in downed
        else (accept_org.get(c.id) or c.target_org_id, None)
        for c in cases
    }


#: 转诊「审核环节」的状态：待卫生院审核（含收敛前存量的服务站已复核）、待县级医院接收——即 `routers/referral.py` 状态机
#: `_NEXT` 的键（`tests/test_spd_referral_review_overdue.py` 钉住两边同一组）。「转诊审核超时」只数这几态（P2-1192）：
#: 转诊页的超时预警与医生移动端「超时督办」原先按「不是终态」数，已接收待到院、已到院、已下转的都算超时——到院之后的
#: 下一步是「病情稳定再下转」，没有 48 小时时限，住得越久越排在前面，真卡在审核上的被挤到后面
REFERRAL_REVIEW_STATUSES = ("submitted", "station_reviewed", "township_reviewed")


def referral_last_moved_at():
    """转诊单最近一次推进的时刻：最后一条环节轨迹的时间（发起也写一条）；没有轨迹的存量单退回建单时间。

    「超过 N 小时未推进」按它判，转诊页的超时预警与医生移动端工作台共用（P2-140）——原先两处都按建单时间判：
    三天前发起、一小时前刚被卫生院审核通过的单子照报超时，而真正卡在一个环节上的单子排序也不按卡了多久。
    """
    last_step = (
        select(func.max(SpdReferralStep.created_at))
        .where(SpdReferralStep.case_id == SpdReferralCase.id)
        .correlate(SpdReferralCase)
        .scalar_subquery()
    )
    return func.coalesce(last_step, SpdReferralCase.created_at)


#: 统一任务标题的列宽（P2-1044）：标题多是拼出来的——「随访异常处置：{处置措施}」「自助随访异常处置：{处置措施}」
#: 「干预执行：{干预目标}」「{路径名}·{节点名}」，各段各自在上限内、拼起来就超：开发库照存，生产库撞列宽即 500，重度异常的
#: 随访执行、居民自助作答、批量下发干预整笔回滚。这里是「所有任务都从这里出」的汇合点，按列宽截断（P1-164 同一口径）
SPD_TASK_TITLE_MAX = cast(String, SpdTask.__table__.c.title.type).length or 128
#: 配置 JSON 里的自由文本派生写进窄列的两处（P2-1048）：量表分段的「建议」写进评估 / 筛查记录（512），服务包项目名写进
#: 扣减流水（64）。建 / 改配置时按它们 422；存量配置里已经超长的，写入时按列宽截断，不让评估、筛查、居民自查、扣减在生产库上 500
SCALE_ADVICE_MAX = cast(String, SpdScreening.__table__.c.advice.type).length or 512
PACKAGE_ITEM_NAME_MAX = cast(String, SpdPackageUsage.__table__.c.item_name.type).length or 64


def spawn_task(
    db: Session,
    *,
    patient_id: int,
    title: str,
    task_type: str = "followup",
    program_code: str = "",
    enrollment: SpdEnrollment | None = None,
    instance: SpdPathInstance | None = None,
    node: SpdPathNode | None = None,
    assignee_id: int | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    due_days: int = 7,
    priority: int = 1,
    source: str = "auto",
    form_code: str = "",
    require_evidence: bool = False,
) -> SpdTask:
    """生成一条统一任务。**所有任务都从这里出**，包括手工建的。

    责任人缺省顺序：显式指定 > 节点执行角色对应的团队成员 > 纳管档案的主管医生。
    找不到人也照样建任务，落成待接收（`pending`）而不是报错——
    "没人认领的任务"在中心端待办里看得见，"没建出来的任务"谁也看不见。
    停用的账号同样算「找不到人」（第十五批 S1-1）：主管医生后来停用了、转诊发起人离开了，任务照旧挂给他，别人认领
    409、中心端「未分配」不数它，就没人办了——落成待接收，谁都认领得了。角色办不了任务的同样（第二十二批 X3-1）：主管
    医生后来被改成经办 / 药师，他办理 403，别人接收 409。
    """
    due = clock.today() + timedelta(days=max(due_days, 0))
    task = SpdTask(
        program_code=program_code or (enrollment.program_code if enrollment else ""),
        patient_id=patient_id,
        enrollment_id=enrollment.id if enrollment else None,
        instance_id=instance.id if instance else None,
        node_key=node.key if node else "",
        task_type=task_type,
        title=title[:SPD_TASK_TITLE_MAX],
        org_id=org_id or (enrollment.org_id if enrollment else None),
        team_id=team_id or (enrollment.team_id if enrollment else None),
        assignee_id=usable_or_none(db, assignee_id or (enrollment.doctor_user_id if enrollment else None),
                                   roles=SERVICE_ROLES),
        exec_role=node.exec_role if node else "",
        status="pending",
        priority=priority,
        due_date=due.isoformat(),
        form_code=form_code or (node.form_code if node else ""),
        require_evidence=require_evidence or (node.require_evidence if node else False),
        source=source,
    )
    db.add(task)
    db.flush()
    return task


def close_followup_record(
    db: Session, record_id: int, new_status: str, *, allowed_from: tuple[str, ...]
) -> bool:
    """随访记录的终态跃迁：判定与写入压在**同一条带状态条件的 UPDATE** 里，返回是否跃迁到。

    两个调用方（医护 `followup.execute_followup`、居民 `portal.self_answer_followup`）
    此前都是"读 → Python 判 status → `record.status = 'done'` → commit"。这层
    check-then-act 守的不只是随访记录本身：办结命中异常规则时要**派一条处置任务**，
    而 `spd_tasks` 上没有指向随访记录的列，"一次执行只派一条任务"这条不变式在子表上
    压根建不出唯一索引（同患者同病种同日两条不同随访各派一条是合法的）。PG
    READ COMMITTED 下两路并发（两名医护、或医护与居民同时）都读到 planned 就都办结、
    都派单，多出来的那条待办随后被 `sweep_overdue` 翻成超期，一路挂进督办与考核，
    要人手工作废。

    `WHERE status IN 期望态` 让后到的那路 rowcount 为 0（PG 取行锁后
    EvalPlanQual 按赢家提交后的状态重算条件），调用方据此给出与顺序请求一致的 409，
    处置任务只在返回 True 之后才 `db.add`。

    `allowed_from` 由调用方给，因为两条通道的可办结态本就不同：医护执行允许
    `planned|overdue|unreachable`（失访后拿到答案再补录是现有行为），居民自助只允许
    `planned|overdue`。二者都恰是各自预检的补集——**状态取值是封闭的五个**
    （planned|done|overdue|removed|unreachable）；日后新增状态值必须同时改预检与这里，
    否则快路径放行、闸门 409，两边说法不一致。
    """
    closed = cast(CursorResult, db.execute(
        update(SpdFollowupRecord)
        .where(SpdFollowupRecord.id == record_id, SpdFollowupRecord.status.in_(allowed_from))
        .values(status=new_status)
    ))
    if closed.rowcount and new_status == "done":   # 失访不收：失访的还能补录，外呼可以接着打
        withdraw_followup_calls(db, [record_id], "随访已办结，撤出待呼叫")
    return bool(closed.rowcount)


#: 呼叫任务（`spd_call_tasks.status`）还能回写结果的：待呼叫，以及随访结束时撤出队列的（P2-498）
CALL_SETTLEABLE_STATUSES = ("pending", "withdrawn")


def withdraw_calls(db: Session, ref_type: str, ref_ids: list[int], note: str) -> int:
    """随访 / 复诊结束（办结 / 移除 / 档案结束一并移除）后，挂在它上面的待呼叫撤出队列（`withdrawn`），返回撤了几条
    （P2-498 随访；P2-735 复诊）。

    原先不动：随访在门诊当面做完、被手工移除、患者死亡结案一并收走之后，外呼队列里挂着它的呼叫任务照旧「待呼叫」——
    坐席照单打过去，打给的是已经随访过的人，甚至是死者家属。只翻待呼叫的：已接通 / 未接通 / 已取消的是通话留痕，不动。
    呼叫任务按 `ref_type` + `ref_id` 引用来源（转呼叫接口写明随访 / 复诊 / 宣教 / 异常处置都可以转），撤回按同一对键。

    撤回不是取消：这一刻坐席可能正在通话，接了呼叫中心网关的也撤不回已派发的呼叫（网关没有撤销接口）——真实打出去的
    电话照旧能回写一次结果（`settle_call_task` 从待呼叫或已撤回翻）。原先置成「已取消」，这一路回写就 409、通话记录与
    录音地址丢掉（随访并发真 PG 档实测）。移除后又恢复的不连带恢复外呼（要打再发起）。**不 commit**。
    """
    if not ref_ids:
        return 0
    withdrawn = db.execute(
        update(SpdCallTask)
        .where(SpdCallTask.ref_type == ref_type, SpdCallTask.ref_id.in_(ref_ids),
               SpdCallTask.status == "pending")
        .values(status="withdrawn", result=note)
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult, withdrawn).rowcount


def withdraw_followup_calls(db: Session, record_ids: list[int], note: str) -> int:
    """挂在这些随访记录上的待呼叫撤出队列（P2-498），见 `withdraw_calls`。**不 commit**。"""
    return withdraw_calls(db, "followup", record_ids, note)


def settle_call_task(db: Session, task_id: int, **values: Any) -> bool:
    """呼叫任务回写结果：只从「待呼叫」翻，结果各列与状态同一条 UPDATE，返回是否翻到（P2-289）。

    「结果只回写一次」（P2-87）原先只是锁外预检：网关超时重发回调、或网关回调与坐席手工回写同时到，两路都读到待呼叫、都 200，
    接通时两路先后往关联的随访记录各追加一遍沟通结果与录音地址；一路接通、一路未接通时任务停在后提交的那路，随访记录里却
    已写进接通结果。**不 commit**。
    """
    # 已撤回的也收（P2-498）：随访结束时撤出队列的那一刻，坐席可能正在通话、网关可能已经拨出
    return _move_row(db, SpdCallTask, task_id, CALL_SETTLEABLE_STATUSES, **values)


def note_call_dispatch_failure(db: Session, task_id: int, note: str) -> bool:
    """呼叫任务派发没受理：把原因记进结果列，只在仍待呼叫时写，返回是否写到（P2-367）。

    原先无条件写 `result`：网关超时（5 秒）之后其实已受理、回调先一步把结果回写好了的，沟通结果被盖成「呼叫网关异常」——
    与回写结果「只从待呼叫翻」（`settle_call_task`，P2-289）同一口径。**不 commit**。
    """
    return _move_row(db, SpdCallTask, task_id, "pending", result=note)


#: 干预方案（`spd_interventions.status`）还能被居民标记完成的：没被移除的都算（已完成的再点一次照旧是已完成）
INTERVENTION_FINISHABLE_STATUSES = ("planned", "doing", "done")


def mark_task_escalated(db: Session, task_id: int) -> None:
    """标记升级并把优先级抬到「紧急」——只往上抬，两条带条件的 UPDATE（P2-194）。**不 commit**。

    原先读出优先级在内存里 max 再写回：另一路刚把它调到更高，这里写回的旧值 max 会把它压回 2。单条、批量升级（P2-246：
    批量版原先还是 `task.escalated, task.priority = True, max(task.priority, 2)`）与超期扫描的节点超时升级（P2-608）共用
    这一处。"""
    db.query(SpdTask).filter(SpdTask.id == task_id).update({SpdTask.escalated: True}, synchronize_session=False)
    db.query(SpdTask).filter(SpdTask.id == task_id, SpdTask.priority < 2).update(
        {SpdTask.priority: 2}, synchronize_session=False)


def mark_intervention_done(db: Session, intervention_id: int) -> bool:
    """居民把干预方案标记为已完成：翻转与「没被移除」压进同一条 UPDATE，返回是否翻到（P2-302）。

    `portal.feedback_intervention` 原先是「预检不是已移除 → 赋值 → commit」：居民点「已完成」的同时医生移除了这条方案
    （或档案结束时 `close_open_work` 一并收掉），removed 被写回 done、重新算进完成数——预检旁边那句注释写的正是
    「不能再把它翻成已完成」。**不 commit**。
    """
    return _move_row(db, SpdIntervention, intervention_id, INTERVENTION_FINISHABLE_STATUSES, status="done")


def adjust_followup_record(db: Session, record_id: int, **values: Any) -> bool:
    """手工调整随访记录（移除 / 恢复 / 改期 / 改执行人）：改的列与「还没完成」的判定压进同一条 UPDATE，返回是否改到（P2-287）。

    原先「读 → 判不是已完成 → 逐字段赋值 → commit」，flush 出来的 UPDATE 只有 `WHERE id = ?`：护士点「移除」的同时医生执行了
    这条随访（`close_followup_record` 的条件翻转 planned → done 已提交、处置任务已派），护士这边随后提交，done 被改成 removed——
    办完的随访从完成数、工作量、质控抽样池里消失，处置任务挂在一条「已移除」的随访上；失访补录执行（→ done）与「恢复为待随访」
    交错时，done 被改回 planned，能再执行一次、再派一条处置任务。与 `close_open_work` 同一个 `_move_row`。**不 commit**。
    """
    moved = _move_row(db, SpdFollowupRecord, record_id, FOLLOWUP_ADJUSTABLE_STATUSES, **values)
    if moved and values.get("status") == "removed":
        withdraw_followup_calls(db, [record_id], "随访已移除，撤出待呼叫")
    return moved


def feedback_appended(current: str | None, text: str, limit: int = 512) -> str:
    """干预反馈追加一段（P2-961），不整格覆盖。

    `spd_interventions.feedback` 只有一列：居民在手机上交的反馈（`portal.feedback_intervention`）与医护办结时填的「患者反馈」
    （`care.update_intervention`）原先都整格写——居民报的「吃药后头晕，早上量血压 95/60」被医护电话随访后写的一句话整段替换，
    库里别处没有留存；反过来医护先写、居民后交也一样。与执行随访追加结果（P2-291）同一口径改成追加、不加来源标注（两处记的
    都是患者的反馈），第一段原样存——只一方写过的与修前一字不差；超出列宽时留最新的，旧的从头上截掉（最新的反馈才是眼下的
    情况）。**不 commit**，调用方在同一行的临界区里、refresh 之后调。"""
    merged = f"{current}；{text}" if current else text
    return merged[-limit:]


def spawn_followup_abnormal_task(db: Session, record: SpdFollowupRecord, level: str, title: str) -> SpdTask | None:
    """随访答卷命中中度 / 重度异常时派一条处置任务；医护执行与居民自助作答共用这一处（P2-131）。

    原先两条通道各写一份：居民那份不挂纳管档案、重度中度一律次日到期，而它的说明写着「异常分级与派单逻辑与医护
    执行时完全一致」。不挂档案的任务，结案收尾（`close_open_work` 按档案找任务）取消不到——患者登记死亡后它照旧
    到期、超期；档案的 360 画像、居民端的就医旅程也都按档案看任务，看不见它。

    挂哪份档案：写了病种的按 `enrollment_for`（在管的优先）；随访记录没写病种的不挂（与原先医护那份一致）。
    到期：重度次日、中度三天。**不 commit**。

    走 `spawn_task`（P1-184）：原先在这里自己拼 `SpdTask`，绕过了「所有任务都从这里出」的责任人缺省——挂着纳管档案也
    不落主管医生、不落团队，处置任务停在待接收、谁的待办里都没有；预置问卷的处置动作写的正是「通知主管医师」「立即联系
    手术医师」。重度异常另给责任人发一条站内消息（与催办同一个 `notify_user`）：居民自助作答的重度异常，原先要等有人去
    翻任务中心才看得见。
    """
    if level not in FOLLOWUP_ABNORMAL_LEVELS:
        return None
    enrollment = enrollment_for(db, record.patient_id, record.program_code)[1] if record.program_code else None
    task = spawn_task(
        db, patient_id=record.patient_id, title=title, task_type="report", program_code=record.program_code,
        enrollment=enrollment, org_id=record.org_id, due_days=1 if level == "high" else 3,
        priority=3 if level == "high" else 2, source="followup",
    )
    if level == "high" and task.assignee_id is not None:
        notify_user(
            db, task.assignee_id, category="spd_task", title=SEVERE_ABNORMAL_NOTICE,
            body=f"{title}（患者 {record.patient_id}，次日到期）", link_type="spd_task", link_id=task.id,
        )
    return task


#: 随访重度异常处置任务的站内消息标题：派生时发给责任人（P1-184），换了责任人发给接手的人（P2-885）
SEVERE_ABNORMAL_NOTICE = "随访重度异常待处置"


def notify_severe_abnormal_handover(db: Session, task: SpdTask, assignee_id: int, previous_id: int | None) -> None:
    """随访重度异常的处置任务换了责任人（转派、无责任人的事后分派）时，给接手的人发派生时那一条消息（P2-885）。**不 commit**。

    派生那一刻只发给当时的责任人：转派之后新责任人一条消息都没有，原责任人那条「随访重度异常待处置」还挂着、点去接收
    得 409——手册教的「停用账号名下的任务逐条转派」、P1-209 无责任人任务的事后分派，接手的人都收不到推送。单条与批量
    分配共用这一处。认法：只有随访异常派生的任务 `source` 是 followup，重度的派成特急（优先级只在建任务时定，升级只抬到
    紧急）。其余任务建的时候就不发消息，转派也不发；待审核的不发（接手的人这时办不了，由审核人审）。
    """
    if task.source != "followup" or task.priority < 3 or assignee_id == previous_id or task.status == "submitted":
        return
    due = f"，{task.due_date} 到期" if task.due_date else ""
    notify_user(
        db, assignee_id, category="spd_task", title=SEVERE_ABNORMAL_NOTICE,
        body=f"{task.title}（患者 {task.patient_id}{due}，已转给您处置）", link_type="spd_task", link_id=task.id,
    )


def plan_offsets(rule: SpdFollowupRule) -> list[int]:
    """按随访方案排随访时用的时间点，去掉重复的、保留原顺序（P2-719）。按方案生成、自动匹配、出院即派生三处共用：
    写入口查重复之前存下的 [7, 7, 30] 原先在同一天排两条随访，两条都要执行、都进完成率与超期数。"""
    return list(dict.fromkeys(int(p) for p in rule.points or []))


#: 节点时限的上界（天）：与模板节点的时限同一个界（`config/paths.py` 的 `PathNodeIn.due_days`，le=3650）
NODE_DUE_DAYS_MAX = 3650


def node_due_days(instance: SpdPathInstance, node: SpdPathNode) -> int:
    """节点任务的时限（天）：实例的个性化覆盖优先（`overrides[节点键]["due_days"]`），没覆盖按模板（P2-258）。

    实例表的列注释写着「个性化覆盖：{"node_key": {"due_days": 3}}，为空表示完全按模板」，启动与调整接口也照收——原先
    派任务一律取模板的时限，覆盖存了不用。覆盖写得不成形（不是字典、不是 0～3650 的整数）的按模板，不因为它派不出
    任务：写入口查结构之前（P2-717）存下的 10**9 天加到今天上溢出，派任务整条 500。"""
    override = (instance.overrides or {}).get(node.key) if isinstance(instance.overrides, dict) else None
    days = override.get("due_days") if isinstance(override, dict) else None
    if isinstance(days, int) and not isinstance(days, bool) and 0 <= days <= NODE_DUE_DAYS_MAX:
        return days
    return node.due_days


def path_overrides_problem(overrides: dict, node_keys: set[str]) -> str:
    """路径实例个性化覆盖的结构问题，没问题返回空串（P2-717，启动与调整共用）。

    覆盖是按节点求值的配置（`node_due_days`），原先写接口只要是个对象就收：时限 36500 天照存，派出去的任务百年后
    才到期；10**9 天在派任务那一刻溢出、500；节点键写错、`due_days` 拼错的悄悄不生效。只认模板里的节点键、只认
    `due_days`，时限与模板节点同一个界（0～3650 天）；节点写成空对象等于不覆盖。"""
    bad = non_finite_path(overrides, "overrides")   # 与各配置校验同一道（P2-466）
    if bad:
        return f"{bad} 不能是 NaN / Infinity"
    for key, value in overrides.items():
        if key not in node_keys:
            return f"覆盖里的节点 {key} 不在这条路径的模板里（可选：{'、'.join(sorted(node_keys)) or '无'}）"
        if not isinstance(value, dict):
            return f'节点 {key} 的覆盖要写成 {{"due_days": 天数}} 这样的对象'
        unknown = sorted(str(k) for k in value if k != "due_days")
        if unknown:
            return f"节点 {key} 的覆盖只认 due_days（收到 {'、'.join(unknown)}）"
        days = value.get("due_days")
        if "due_days" in value and (isinstance(days, bool) or not isinstance(days, int)
                                    or not 0 <= days <= NODE_DUE_DAYS_MAX):
            return f"节点 {key} 的时限（due_days）须是 0～{NODE_DUE_DAYS_MAX} 的整数天（收到 {days!r}）"
    return ""


def start_path(
    db: Session, enrollment: SpdEnrollment, template: SpdPathTemplate, owner_user_id: int | None,
    overrides: dict | None = None,
) -> SpdPathInstance:
    """按模板为患者启动路径实例，并生成首节点任务。

    只允许引用**已发布**的模板：草稿模板还在改，引用它等于让患者跟着草稿走。模板的病种须是档案的病种：原先不看，
    高血压档案能跑上糖尿病的路径——任务挂在高血压档案上、内容是糖尿病的节点，阶段取自糖尿病的阶段定义（P2-95）。
    """
    if template.status != "published":
        raise ValueError("只能引用已发布的路径模板")
    program = db.get(SpdProgram, template.program_id)
    if program is None or program.code != enrollment.program_code:
        raise ValueError("路径模板的病种与纳管档案不一致")
    nodes = (
        db.query(SpdPathNode)
        .filter(SpdPathNode.template_id == template.id)
        .order_by(SpdPathNode.seq, SpdPathNode.id)
        .all()
    )
    if not nodes:
        raise ValueError("路径模板没有节点")
    first = nodes[0]
    instance = SpdPathInstance(
        enrollment_id=enrollment.id,
        template_id=template.id,
        template_code=template.code,
        current_node_key=first.key,
        current_stage=first.stage,
        status="running",
        owner_user_id=owner_user_id,
        overrides=overrides or {},
    )
    db.add(instance)
    db.flush()
    # 进首节点与推进、恢复同一句：节点带阶段就同步到纳管档案（P2-259）。原先只有推进与恢复写，启动不写——实例上是
    # 「治疗期」，档案还是原来的阶段，按阶段取的管理目标、随访周期、测量值分级都拿错了阶段
    if first.stage:
        enrollment.stage = first.stage
    spawn_task(
        db,
        patient_id=enrollment.patient_id,
        title=f"{template.name}·{first.name}",
        task_type="path",
        enrollment=enrollment,
        instance=instance,
        node=first,
        due_days=node_due_days(instance, first),
        source="path",
    )
    return instance


def advance_path(db: Session, instance: SpdPathInstance) -> dict:
    """节点完成后推进到下一节点，并派生下一节点任务。

    下一节点取 `next_key`，没配就按 `seq` 顺延——两种编排方式在实施期都会
    出现（有人画流程图连线，有人就想要个清单），支持一种会被另一种绊住。
    """
    nodes = (
        db.query(SpdPathNode)
        .filter(SpdPathNode.template_id == instance.template_id)
        .order_by(SpdPathNode.seq, SpdPathNode.id)
        .all()
    )
    if not nodes:
        return {"status": instance.status, "current_node_key": instance.current_node_key}
    by_key = {n.key: n for n in nodes}
    current = by_key.get(instance.current_node_key)
    nxt = None
    if current is not None:
        if current.next_key:
            nxt = by_key.get(current.next_key)
        else:
            index = nodes.index(current)
            nxt = nodes[index + 1] if index + 1 < len(nodes) else None

    # 进度 = 办结过任务的不同节点数 ÷ 节点数（P2-1121）：原先数办结的任务条数，路径按 next_key 回到走过的节点（回环是有意
    # 支持的，见 `tasks._resume_paused`）再办一趟就多算一条——复诊 → 评估 → 复诊绕一圈显示 100%，状态仍是执行中、环外的
    # 结案节点一次没走到。环合不合法、发布时拦不拦另待裁定（随 P2-1033）
    done_nodes = {
        t.node_key
        for t in db.query(SpdTask)
        .filter(SpdTask.instance_id == instance.id, SpdTask.task_type == "path")
        .all()
        if t.status == "done"
    }
    instance.progress = min(int(len(done_nodes.intersection(by_key)) / len(nodes) * 100), 100)

    if nxt is None:
        instance.status = "completed"
        instance.current_node_key = ""
        instance.finished_at = now_naive()
        instance.progress = 100
        return {"status": "completed", "current_node_key": ""}

    instance.current_node_key = nxt.key
    instance.current_stage = nxt.stage
    enrollment = db.get(SpdEnrollment, instance.enrollment_id)
    template = db.get(SpdPathTemplate, instance.template_id)
    if enrollment is None:
        return {"status": instance.status, "current_node_key": nxt.key, "next_node": nxt.name}

    # P1-4：进入条件在**自动流转**时也生效。此前只有显式端点会校验，
    # 条件配了却拦不住自动派单，配置形同虚设。
    # 不满足时**暂停并通知**，不静默跳过——静默跳过的表现是
    # "路径停在那里且没人知道为什么"。
    allowed, _matched = node_enter_allowed(db, instance, nxt)
    if not allowed:
        instance.status = "paused"
        # 通知主管医生与路径负责人（P2-503）：原先只通知主管医生——档案没配主管医生时一条都不发，正是上面说的「停在那里
        # 且没人知道为什么」；启动路径、在路径页调整它的负责人也不知道。两人是同一个的只发一条。
        # 停用的账号不发（第十六批 T2-2，与派任务的 `usable_or_none` 同一口径）：原先照发，消息落进一个登不上的收件箱，
        # 在岗的人照样不知道路径停了；两人都停用时一个都不剩，该回落给谁另行裁定。角色不筛（`roles=()`）：这是知会不是
        # 派活，改成经办 / 药师的人登得上、看得到、能转告；派活才按角色挑人（第二十二批 X3-1）
        recipients = (usable_or_none(db, uid, roles=())
                      for uid in (enrollment.doctor_user_id, instance.owner_user_id))
        for recipient in dict.fromkeys(uid for uid in recipients if uid):
            notify_user(
                db, recipient, category="spd_path",
                title="专病路径已暂停",
                body=f"患者路径进入「{nxt.name}」的条件未满足，已暂停；"
                     "条件满足后可在路径页手工推进恢复",
                link_type="spd_path_instance", link_id=instance.id,
            )
        return {"status": "paused", "current_node_key": nxt.key,
                "next_node": nxt.name, "paused_reason": "进入条件未满足"}

    if nxt.stage:
        enrollment.stage = nxt.stage
    spawn_task(
        db,
        patient_id=enrollment.patient_id,
        title=f"{template.name if template else '路径'}·{nxt.name}",
        task_type="path",
        enrollment=enrollment,
        instance=instance,
        node=nxt,
        due_days=node_due_days(instance, nxt),
        source="path",
    )
    return {"status": instance.status, "current_node_key": nxt.key, "next_node": nxt.name}


def node_enter_allowed(db: Session, instance: SpdPathInstance, node: SpdPathNode) -> tuple[bool, list]:
    """判断患者是否满足某节点的进入条件。空条件视为可进入。"""
    if not node.enter_condition:
        return True, []
    enrollment = db.get(SpdEnrollment, instance.enrollment_id)
    if enrollment is None:
        return False, []
    facts = build_facts(
        db,
        enrollment.patient_id,
        {"risk_level": enrollment.risk_level, "stage": enrollment.stage},
    )
    return evaluate(node.enter_condition, facts, mode="all")


def point_account_for(db: Session, user_id: int, org_id: int | None) -> SpdPointAccount:
    """取这位用户的积分账户，没有就建（P2-336）。

    原先入账（`award_points`）与签到各写一份「查不到就 `add` + `flush`」：同一个人头一回同时来两笔（两条随访一起办结、
    签到与办结撞在一起），两路都查不到、都去建，后建的撞 `user_id` 唯一约束抛 `IntegrityError`——签到是 500，
    入账那一路更糟：它是转诊到院、随访办结这些业务动作的一部分，整个动作跟着 500 回滚。建账户改走 `insert_if_absent`
    （撞了就退回这一行、用对方建好的那个）。**不 commit**。
    """
    account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user_id).first()
    if account is None:
        insert_if_absent(db, SpdPointAccount(user_id=user_id, org_id=org_id, balance=0, earned=0, used=0))
        account = ensure_present(
            db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user_id).first(), "积分账户")
    return account


def award_points(
    db: Session,
    user_id: int | None,
    event: str,
    *,
    ref_type: str = "",
    ref_id: int | None = None,
    note: str = "",
    org_id: int | None = None,
) -> SpdPointRecord | None:
    """按积分规则给村医入账，命中每日上限则不入账并返回 None。

    每日上限按"当天该规则已入账分值"算而不是"次数"：规则里配的是分值上限
    （`daily_limit` 单位是分），这样调整单次分值时不用同时调次数上限。

    停用的账号、停用的村医档案不入账（P2-845，与 P2-663 同一口径——别处一律把停用村医当「已回收」）：村医离岗后他签约
    的档案仍挂着他，接手的人办结随访、上报异常、有效上转，积分原先照记给已停用的村医，工作量与考核跟着算他的。停用
    期间的这些积分该记给谁（不记，还是记实际办理人）随 P2-840 待裁定；两个选项都不该记给停用的人，这里只做不记。
    没有村医档案的账号不看档案（P2-663 同一句）。
    """
    if user_id is None:
        return None
    if unusable_user(db, user_id):
        return None
    profile = db.query(SpdVillageDoctor.active).filter(SpdVillageDoctor.user_id == user_id).first()
    if profile is not None and not profile.active:
        return None
    # 同一事件配了几条启用的规则时取编号最小的那条（P2-693）：原先不排序，PG 上改过的行排到堆尾（P2-304 实测），
    # 改一下规则名称，同样的随访就从 3 分变成 5 分、每日上限也跟着换——与转诊规则试算、随访方案匹配同一个次序
    rule = (
        db.query(SpdPointRule)
        .filter(SpdPointRule.event == event, SpdPointRule.active.is_(True))
        .order_by(SpdPointRule.id)
        .first()
    )
    if rule is None:
        return None
    account = point_account_for(db, user_id, org_id)
    if rule.daily_limit:
        # 「当天」是本地业务日，与同一套积分的每日签到（`spd_signins.day` 取 `clock.today()`）同一把尺子；流水时间戳是
        # naive UTC，把本地这一天换成 UTC 区间再比（P2-534）。原先拿流水的 UTC 日期比本地的今天：东八区 0–8 点入的账
        # 算进「昨天」，这段时间上限形同虚设，一天能入到上限的两倍多
        start, end = clock.local_day_utc_range(clock.today())
        earned_today = (
            db.query(func.coalesce(func.sum(SpdPointRecord.points), 0))
            .filter(
                SpdPointRecord.account_id == account.id,
                SpdPointRecord.rule_code == rule.code,
                SpdPointRecord.direction == "in",
                SpdPointRecord.created_at >= start,
                SpdPointRecord.created_at < end,
            )
            .scalar()
        )
        if earned_today + rule.points > rule.daily_limit:
            return None
    # 与签到、兑换同一口径：积分入账一律走原子 UPDATE，不做读-改-写
    add_amount(db, SpdPointAccount, account.id, "balance", rule.points)
    add_amount(db, SpdPointAccount, account.id, "earned", rule.points)
    db.flush()
    db.refresh(account)
    account.updated_at = now_naive()
    record = SpdPointRecord(
        account_id=account.id, rule_code=rule.code, direction="in", points=rule.points,
        balance_after=account.balance, ref_type=ref_type, ref_id=ref_id,
        note=note or rule.name,
    )
    db.add(record)
    db.flush()
    return record


def close_open_work(db: Session, enrollment: SpdEnrollment, reason: str, *, keep_org_id: int | None = None) -> dict:
    """终止一名患者在该病种下的全部未完成任务、路径、干预、复诊与随访。

    死亡 / 迁出 / 排除三处生命周期事件共用。**不删除记录**，只置为取消并写明理由：
    删掉等于把"这个人曾经被管过"一并抹掉，考核与追溯都会对不上。

    随访记录原先不在其中（P1-129）：死者名下计划好的随访照旧到期、被扫成超期，排在随访清单与超期数里。
    只收本机构（或没挂机构）的——迁入确认时目标机构自己的随访不能被原档案的结案带走；已完成、失访的是留痕，不动。

    `keep_org_id`：迁入确认时传目标机构，它自己的医生排的同病种复诊不随原档案移除（P2-849，同上一句的复诊版）。
    """
    stats = {"tasks": 0, "instances": 0, "interventions": 0, "revisits": 0, "followups": 0}
    # 每一条都是条件翻转、按编号取（P2-114）：原先查出一批、逐条改内存、由调用方提交时才发 UPDATE（只有 `WHERE id = ?`），
    # 这期间别人办结的任务、走完的路径、居民标了完成的干预、做完的复诊与随访，都被改成取消 / 移除——办完的工作从完成数里
    # 消失（任务还可能分记了、单子却是取消的）。先实例、后任务：与办结（锁实例、再翻任务）同一个加锁顺序，PG 上不互等
    instances = (
        db.query(SpdPathInstance)
        .filter(SpdPathInstance.enrollment_id == enrollment.id,
                SpdPathInstance.status.in_(PATH_OPEN_STATUSES))
        .order_by(SpdPathInstance.id)
        .all()
    )
    for instance in instances:
        if _move_row(db, SpdPathInstance, instance.id, PATH_OPEN_STATUSES,
                     status="cancelled", finished_at=now_naive()):
            stats["instances"] += 1

    tasks = (
        db.query(SpdTask)
        .filter(
            SpdTask.enrollment_id == enrollment.id,
            SpdTask.status.in_(TASK_OPEN_STATUSES),
        )
        .order_by(SpdTask.id)
        .all()
    )
    for task in tasks:
        if move_task(db, task.id, "cancelled", review_note=reason, finished_at=now_naive()):
            stats["tasks"] += 1

    interventions = (
        db.query(SpdIntervention)
        .filter(
            SpdIntervention.enrollment_id == enrollment.id,
            SpdIntervention.status.in_(["planned", "doing"]),
        )
        .order_by(SpdIntervention.id)
        .all()
    )
    for item in interventions:
        if _move_row(db, SpdIntervention, item.id, ("planned", "doing"), status="removed"):
            stats["interventions"] += 1

    revisit_query = db.query(SpdRevisit).filter(
        SpdRevisit.patient_id == enrollment.patient_id,
        SpdRevisit.program_code == enrollment.program_code,
        SpdRevisit.status.in_(REVISIT_OPEN_STATUSES),
    )
    if keep_org_id is not None:
        # 迁入确认：目标机构自己排的复诊不随原档案收尾（P2-849）——原先待确认期间目标机构排好的复诊，一确认就成了「已移除」
        # （日志「迁出至其他机构」、待呼叫一并撤掉），同一时候排的随访却留着。复诊表没有机构列，按复诊医生所在机构认；
        # 没填医生的认不出是谁排的，照旧随原档案移除。死亡 / 排除 / 召回不传，照旧同病种一并移除
        revisit_query = revisit_query.filter(or_(
            SpdRevisit.doctor_user_id.is_(None),
            SpdRevisit.doctor_user_id.not_in(select(User.id).where(User.org_id == keep_org_id)),
        ))
    revisits = revisit_query.order_by(SpdRevisit.id).all()
    removed_revisits: list[int] = []
    for revisit in revisits:
        # 日志是 JSON 列整体覆写（P2-708，与 `update_revisit` 同一处理）：锁住这一行、重读、再追加——原先拼的是整批载入时
        # 读到的旧日志，这期间护士刚记下的「已联系」那条被盖掉
        with serialized_on(db, SpdRevisit, revisit.id):
            db.refresh(revisit)
            if _move_row(db, SpdRevisit, revisit.id, REVISIT_OPEN_STATUSES, status="removed",
                         log=(revisit.log or []) + [{"at": clock.today().isoformat(), "note": reason}]):
                stats["revisits"] += 1
                removed_revisits.append(revisit.id)
    # 从复诊转出的待呼叫同样撤出队列（P2-735）：原先只撤随访的，死者名下「复诊」一类的外呼照旧排在坐席队列里
    withdraw_calls(db, "revisit", removed_revisits, f"复诊随档案结束移除（{reason}），撤出待呼叫"[:512])

    followups = (
        db.query(SpdFollowupRecord)
        .filter(
            SpdFollowupRecord.patient_id == enrollment.patient_id,
            SpdFollowupRecord.program_code == enrollment.program_code,
            or_(SpdFollowupRecord.org_id == enrollment.org_id, SpdFollowupRecord.org_id.is_(None)),
            SpdFollowupRecord.status.in_(FOLLOWUP_OPEN_STATUSES),
        )
        .order_by(SpdFollowupRecord.id)
        .all()
    )
    removed: list[int] = []
    for record in followups:
        # 与手工「移除」同一个状态，错移了可以手工恢复
        if _move_row(db, SpdFollowupRecord, record.id, FOLLOWUP_OPEN_STATUSES, status="removed"):
            stats["followups"] += 1
            removed.append(record.id)
    # 挂在这些随访上的待呼叫一并撤出队列（P2-498）：原先死者名下的外呼照旧排在坐席队列里
    withdraw_followup_calls(db, removed, f"随访随档案结束移除（{reason}），撤出待呼叫"[:512])
    return stats


def sweep_overdue_on_read(db: Session, business_day: date) -> dict:
    """查询接口进门顺手刷新超期（待办统计、各端工作台、`overdue=true` 的随访与复诊看板）走这一个入口。

    查询按 `?today=` 覆盖算，**写库不跟它往后拨**（P0-47）：覆盖按接口对接规范「仅限测试与管理排查用途」，可扫描不分
    机构、结果是永久的（置超期、按节点配置升级并通知，考核按状态取数）——原先任一登录账号带个未来日期查一次，全县在办
    的任务、复诊、随访当场全成了超期。截止日取覆盖日期与真实今天里早的那个：往回看的覆盖照用，早于今天的截止日扫得到
    的，今天也一定扫得到。定时任务与服务层直接调 `sweep_overdue`，不经这里。
    """
    return sweep_overdue(db, min(business_day, clock.today()))


def sweep_overdue(db: Session, today: date | None = None) -> dict:
    """三类超期一次扫：任务、复诊、随访。到期未办的置为 overdue。

    做成一个函数供定时任务与"进页面时刷新"两处调用：
    只靠定时任务，演示环境没开调度就永远看不到超期；
    只靠进页面刷新，没人进页面的机构就永远不超期。

    P0-2 之前只扫任务——复诊与随访的"逾期"是各查询现场用日期比出来的，
    督办清单按现算、考核取数按 status，两边数字对不上，而考核数字要进绩效。
    现在三类都落 status，查询一律按 status 过滤，口径只剩一个。

    随访的 `unreachable`（失访）**不会**被覆盖成 overdue：失访是执行过但没联系上，
    完成率的分母含它、分子不含；标成超期等于把"打过电话"抹掉了。
    """
    today = today or clock.today()
    cutoff = today.isoformat()
    pending = (
        db.query(SpdTask)
        .filter(
            SpdTask.status.in_(TASK_IN_HAND_STATUSES),
            SpdTask.due_date != "",
            SpdTask.due_date < cutoff,
        )
        .order_by(SpdTask.id)   # 逐条条件翻转，按编号取：与别的整批写入同一个加锁顺序
        .all()
    )
    escalated = marked = 0
    for task in pending:
        # 条件翻转（P2-114）：一趟扫描先载入整批、逐条改完才提交，窗口是整趟扫描。原先这期间办结的任务被改回「超期」——
        # 重新进待办，再办一次随访计分再记一笔
        if not move_task(db, task.id, "overdue", expect=TASK_IN_HAND_STATUSES):
            continue
        marked += 1
        node = None
        if task.instance_id and task.node_key:
            instance = db.get(SpdPathInstance, task.instance_id)
            if instance is not None:
                node = (
                    db.query(SpdPathNode)
                    .filter(
                        SpdPathNode.template_id == instance.template_id,
                        SpdPathNode.key == task.node_key,
                    )
                    .first()
                )
        if node is not None and node.timeout_action == "escalate":
            # 只往上抬、两条带条件的 UPDATE（P2-608，与手工升级同一处 `mark_task_escalated`）：原先在载入整批时读到的对象上
            # `max(priority, 2)` 再随提交写回——扫描期间别人刚把它调成「特急」，这里写回的 2 把它压了回去
            mark_task_escalated(db, task.id)
            escalated += 1

    # 复诊：plan_date 已过且仍是 planned → overdue，并写日志（谁标的、何时标的）
    overdue_revisits = (
        db.query(SpdRevisit)
        .filter(SpdRevisit.status == "planned", SpdRevisit.plan_date != "",
                SpdRevisit.plan_date < cutoff)
        .order_by(SpdRevisit.id)
        .all()
    )
    revisits_marked = 0
    for revisit in overdue_revisits:
        # 条件翻转（P2-114 同一族）：扫描期间做完的复诊别被改回「逾期」。日志是 JSON 列整体覆写（P2-708，与 `update_revisit`
        # 同一处理）：锁住这一行、重读、再追加——原先拼的是整批载入时读到的旧日志，扫描期间护士刚记下的「已联系」那条被盖掉
        with serialized_on(db, SpdRevisit, revisit.id):
            db.refresh(revisit)
            if _move_row(db, SpdRevisit, revisit.id, "planned", status="overdue",
                         log=(revisit.log or []) + [{"at": cutoff, "note": "超期扫描：计划日期已过，置为逾期"}]):
                revisits_marked += 1

    # 随访：只动 planned；unreachable / removed / done 一律不碰
    overdue_followups = (
        db.query(SpdFollowupRecord)
        .filter(SpdFollowupRecord.status == "planned", SpdFollowupRecord.planned_at != "",
                SpdFollowupRecord.planned_at < cutoff)
        .order_by(SpdFollowupRecord.id)
        .all()
    )
    followups_marked = 0
    for record in overdue_followups:
        # 条件翻转（P2-114 同一族）：扫描期间完成的随访别被改回「超期」——超期数与完成率都按 status 取数
        if _move_row(db, SpdFollowupRecord, record.id, "planned", status="overdue"):
            followups_marked += 1

    return {
        "overdue": marked,
        "escalated": escalated,
        "revisits": revisits_marked,
        "followups": followups_marked,
    }


# ---------------------------------------------------------------------------
# 居民端转诊读侧聚合的 spd 源（ADR-0003 方案 B）
# ---------------------------------------------------------------------------

#: `spd_referral_cases.status` → 中文。措辞与居民端 `static/m/m.js` 的 `SPD_REF_TEXT`
#: **逐字一致**：口径统一的意义就在于只有一套说法，后端另造一套只会让同一个状态
#: 在两个页面读起来不一样。
#:
#: 与平台 `referrals` 存在**同名不同义**：平台 `accepted` 是"已接收"（两点之间
#: 那一次转诊被对方接了），这里是"县级医院已接收"（村→乡→县链路走到了县级）。
#: 所以聚合列表里每条都带 `source`、标签分源映射，不能合并成一张表。
_STATUS_LABELS = {
    "submitted": "村医已发起，待卫生院审核",
    # 存量兼容，新单不再产生（ADR-0005）
    "station_reviewed": "服务站已复核，待卫生院审核(存量)",
    "township_reviewed": "卫生院已审核，待县级医院接收",
    "accepted": "县级医院已接收",
    "arrived": "已到院就诊",
    "down_referred": "已下转基层",
    "followup_received": "下转随访已接收",
    "closed": "已完成闭环",
    "rejected": "已退回",
    "withdrawn": "已撤回",
}
#: 居民端看到的转诊状态文案：聚合列表与「我的慢专病」历程共用这一张（§13「状态文案取自后端」）
REFERRAL_STATUS_LABELS = _STATUS_LABELS

# 测量来源、宣教素材形式的文案（措辞照抄 SpdMeasurement.source / SpdEduMaterial.media_type 列注释；
# 档案全景、测量记录、素材库与居民端宣教都用它，页面上别再原样显示 device / video——P2-74）
MEASUREMENT_SOURCE_NAMES = {"manual": "手工", "device": "设备", "his": "院内系统", "poct": "POCT",
                            "publichealth": "公卫随访同步"}
MEDIA_TYPE_NAMES = {"text": "图文", "audio": "音频", "video": "视频"}

#: 考核指标的取数口径：码 → (名称, {变量: 含义})。指标公式只能引用这里列的变量（建 / 改指标按它校验公式），
#: 管理端建指标的口径下拉与变量提示也取自这里（`GET /api/spd/meta` 的 `indicator_sources`，前端不另抄一份）。
#: 取数实现在 `routers/assess.py::collect_metrics_batch`，两边的变量名由 `tests/test_spd_indicator_sources.py`
#: 逐口径钉住——那边多产出一个变量这里没列，公式就引用不到；这里列了那边不产出，公式校验过得去、计分时求值失败。
INDICATOR_SOURCES: dict[str, tuple[str, dict[str, str]]] = {
    "task": ("慢专病任务", {"total": "期内派发的任务数", "done": "其中已完成", "overdue": "其中超期"}),
    "enrollment": ("纳管档案", {"enrolled": "在管档案数", "target": "目标人群数", "high_risk": "在管的高危 / 极高危"}),
    "path": ("标准路径", {"total": "期末前入径数", "completed": "其中已完成", "running": "其中进行中"}),
    "referral": ("转诊", {"total": "期内转诊单数", "closed": "其中已闭环", "effective": "其中有效就诊"}),
    "measurement": ("监测", {"total": "期内在管患者的监测次数", "normal": "其中正常", "abnormal": "其中异常"}),
    "assessment": ("风险评估", {"assessed": "期内评估过的在管患者数", "enrolled": "在管患者数"}),
    "archive": ("建档", {"archived": "在管且已建档", "enrolled": "在管档案数"}),
    "case_report": ("异常上报", {"reported": "期内上报数", "handled": "其中已处置"}),
}


def referral_feed(db: Session, patient_id: int) -> list[dict]:
    """把本子系统的转诊单产出成聚合列表的统一形状。

    只读、不改任何状态；`/api/portal/spd/referrals` 那个单源接口原样保留
    （既有契约），这里是并给 `/api/portal/me/referrals/all` 用的另一份视图。
    """
    from .platform import REFERRAL_FEED_LIMIT, org_names as _org_names, referral_feed_item

    rows = (
        db.query(SpdReferralCase)
        .filter(SpdReferralCase.patient_id == patient_id)
        .order_by(SpdReferralCase.id.desc())
        .limit(REFERRAL_FEED_LIMIT)
        .all()
    )
    # 「转入」是上转去的那家（P2-558）：下转改写了 target_org_id，原先卡片上的转入机构跟着变成下转目标
    ends = referral_ends(db, rows)
    # 只取这几条单子用到的机构名，不整表拉 organizations
    names = _org_names(db, {r.initiator_org_id for r in rows} | {ends[r.id][0] for r in rows})
    return [
        referral_feed_item(
            source="spd",
            id=r.id,
            direction=r.direction,
            status=r.status,
            status_label=_STATUS_LABELS.get(r.status, r.status),
            reason=r.reason,
            from_org=names.get(r.initiator_org_id, ""),
            # 目标机构可能尚未确定（逐级审核中），此时留空而不是编一个
            to_org=names.get(ends[r.id][0], "") if ends[r.id][0] else "",
            created_at=r.created_at.isoformat(),
            # 必须带上 patient_id：一个居民账号可以代管家属，详情端点按这个参数
            # 决定看谁的档案，不带就会拿默认患者去查，代管家属的单子直接 404。
            detail_path=f"/api/portal/spd/referrals/{r.id}?patient_id={patient_id}",
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 居民端入组读侧聚合的 spd 源（ADR-0003 方案 B）
# ---------------------------------------------------------------------------

#: `spd_enrollments.status` → 中文。取值见该列的注释（迁入确认的 409 文案也用它，P2-527）。
ENROLL_STATUS_LABELS = {
    "active": "在管", "excluded": "已排除", "migrated": "已迁出",
    "dead": "已死亡", "lost": "脱管", "recalled": "召回中", "completed": "已结案",
}

#: 档案已结束、患者不在这份档案下管了：死亡、迁出、排除、结案。召回中 / 脱管不算——人还挂在本机构、正在找回来
ENROLLMENT_ENDED_STATUSES = ("dead", "migrated", "excluded", "completed")
#: 没结束、也不在管的：脱管、召回中（P2-1050）——这份档案要恢复，不该另建一份、也不该让居民再去申请
ENROLLMENT_PAUSED_STATUSES = ("lost", "recalled")


def paused_enrollment(db: Session, patient_id: int, program_code: str) -> SpdEnrollment | None:
    """这位患者这个病种脱管 / 召回中的档案（P2-1050）。居民端首页、自查、申请与建档都认它：原先只认在管的，召回中的居民
    首页被告知「没有签约的慢专病管理」、自查提示去申请、申请照收，受理后再建档出两份档案，原来那份的召回永远结不了。"""
    return (
        db.query(SpdEnrollment)
        .filter(SpdEnrollment.patient_id == patient_id, SpdEnrollment.program_code == program_code,
                SpdEnrollment.status.in_(ENROLLMENT_PAUSED_STATUSES))
        .order_by(SpdEnrollment.id.desc())
        .first()
    )
#: 迁出登记之后、确认之前原档案成了这些状态的，这次迁出不再生效：死亡（P1-111），已迁出 / 已排除 / 已结案（P2-527）
MIGRATION_VOID_STATUSES = ENROLLMENT_ENDED_STATUSES


def migration_void_reason(enrollment_status: str) -> str:
    """这次迁出还能不能确认：能确认返回空串，不能返回原因。

    确认迁入的 409 文案、生命周期清单的「不再生效」、工作台「待确认迁入」的计数同一句（P2-592）——原先只有确认接口
    认它，计数照数、清单照画「确认迁入」，点下去才 409。
    """
    if enrollment_status == "dead":
        return "该患者已登记死亡，这次迁出不再生效"
    if enrollment_status in MIGRATION_VOID_STATUSES:
        return f"原档案{ENROLL_STATUS_LABELS.get(enrollment_status, enrollment_status)}，这次迁出不再生效"
    return ""


#: 服务包绑定（`spd_package_bindings.status`）的中文：居民端首页的服务包照它显示（P2-557）
PACKAGE_BINDING_STATUS_NAMES = {"bound": "绑定中", "unbound": "已解绑"}

#: `risk_level` → 中文（成员端四级危险分层）。**不与平台的 1/2/3 互相映射**：
#: 那是控制情况、这是并发症风险，两把尺子量的不是同一件事。写进给人看的文字（自动干预的目标）也用它（P2-767）
RISK_LEVEL_NAMES = {"low": "低危", "mid": "中危", "high": "高危", "very_high": "极高危"}


def touch_device_sync(db: Session, device_sn: str) -> None:
    """带设备号的监测值落库时刷新设备台账的「最近同步」（P2-975）：医护端录入、设备批量、居民端回传共用这一处。

    原先只有医护端（`care._record_measurement`）刷新，居民 App 回传收同样的字段、只写监测值——两台都绑好的血压计，居民家里
    回传的那台「最近同步」一直是空，台账看不出哪台还在用。只认台账里有的序列号，不比对绑给了谁（要不要校验绑定随 P2-746 定）。
    不 commit。"""
    if not device_sn:
        return
    device = db.query(SpdDevice).filter(SpdDevice.sn == device_sn).first()
    if device is not None:
        device.last_sync_at = now_naive()


def candidate_reason(matched: list | None) -> str:
    """目标池「纳入依据」：命中规则的名称用「；」连起来（P2-974）。建行（筛查 / 批量识别、就诊识别）与复筛共用这一处算法。"""
    return "；".join(str(m.get("label") or m.get("field")) for m in matched or [])[:256]


def enrollment_feed(db: Session, patient_id: int) -> list[dict]:
    """把本子系统的入组档案产出成聚合列表的统一形状。只读、不改任何状态。"""
    from .platform import ENROLLMENT_FEED_LIMIT, enrollment_feed_item, org_names

    rows = (
        db.query(SpdEnrollment)
        .filter(SpdEnrollment.patient_id == patient_id)
        .order_by(SpdEnrollment.id.desc())
        .limit(ENROLLMENT_FEED_LIMIT)
        .all()
    )
    names = {
        p.code: p.name
        for p in db.query(SpdProgram)
        .filter(SpdProgram.code.in_([r.program_code for r in rows] or [""]))
        .all()
    }
    orgs = org_names(db, {r.org_id for r in rows})
    return [
        enrollment_feed_item(
            source="spd", id=r.id,
            program_code=r.program_code,
            program_name=names.get(r.program_code, r.program_code),
            status=r.status,
            status_label=ENROLL_STATUS_LABELS.get(r.status, r.status),
            level_code=r.risk_level,
            level_label=RISK_LEVEL_NAMES.get(r.risk_level, r.risk_level),
            stage=r.stage,
            org=orgs.get(r.org_id, ""),
            # 下次随访取档案这一列（P2-973）：原先不传、恒为空，居民端「疾病管理档案」的慢专病一栏永远「待安排」，同一居民的
            # 慢专病首页（/spd/home）却显示着日期。只给在管的——与首页只列在管同一口径；结案、迁出、死亡时这一列不清，原样给会
            # 露出过期的日期
            next_followup_due=(r.next_followup_at or "") if r.status == "active" else "",
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]
