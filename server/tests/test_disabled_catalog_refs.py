"""写接口引用已停用的目录对象（P1-103）：报告推送任务建在停用模板上照样 201，之后一份报告也不出。

目录类的表（报告模板、服务团队、服务包、上报任务、科室、供应商、会计科目……）带一个启用标志 `active`，
停用的意思是「不再用于新业务」——清单只列启用的、调度见停用就跳过、同文件的兄弟端点早就拒停用的
（签合同 / 药房采购单拒停用供应商，员工挂科室拒停用科室，筛查 / 建档拒停用病种）。一族写接口按请求里的
编号取目录对象，只查了存在（P1-90），没看启用标志：

- **报告推送任务**（有停用开关）：2026-09-24 实测修前——模板停用 200，在它上面建推送任务 201、状态「启用」，
  调度到点生成 0 份、`last_run_at` 一直是空的。与有效期倒置（P2-56）同一种「照样 201、之后天天被跳过」；
- **分发目标患者 / 纳管档案挂团队**（有停用开关）：团队清单、考核对象、驾驶舱团队数都只认启用的团队，
  分过去的患者挂在一个下拉里选不到、考核里不计的团队名下；
- **绑服务包**（有停用开关）：绑包页的下拉本就只列启用的包，按编号直接调接口照样绑得上；
- **个案上报**（有停用开关）：上报页只列启用的上报任务，停用的照样收新上报；
- **分摊规则 / 物资采购 / 高值耗材入库 / 凭证分录 / 干预模板**：这几类目录眼下没有停用入口（只有运维改库
  才会停用），修的是与同文件兄弟端点、与清单同一口径——免得以后加了停用开关，这些入口一句不改就漏过去。

修法：取出来看启用标志，`404「X不存在或已停用」`（与 `materials.sign_contract`、`admin_mgmt.assign_department`
同一句）；凭证分录按「科目不存在或已停用」422（与原「科目不存在」同一状态码）。纳管档案改档时与现值相同的
团队不再查（团队后来停用了，只改风险分层的调用不该被它挡住）。报告推送任务页的模板下拉只列启用的。

**闸门**（派生、零基线）：写接口（POST / PUT / PATCH）按入参（请求体 / 查询参数；路径参数取的是被操作对象
本身，不算引用）取「带启用标志」的目录对象——`db.get(M, …)`、`db.query(M)…filter(M.列 == …).first()`、
表驱动的 `for 字段, (M, 名) in 常量.items()`，连同同模块被调函数两层——这一次取出来的对象（或取它的那条
查询）得看启用标志；本函数要新建的同一张表按查重算、不计。按设计不看的写进 `BY_DESIGN`（逐条写理由，
只减不增，失效即红）。
"""
from __future__ import annotations

import ast
import re

import pytest
import test_datestr_single_source as ds

from app.database import SessionLocal

# ---------------------------------------------------------------- 行为回归

B = "/api/spd"


def _disable(model_name: str, row_id: int) -> None:
    """眼下没有停用入口的目录（科室、供应商、会计科目、干预模板）只能改库停用——运维就是这么停的。"""
    import app.models as models

    with SessionLocal() as db:
        row = db.get(getattr(models, model_name), row_id)
        row.active = False
        db.commit()


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "停用目录卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    r = client.post(f"{B}/programs", json={"code": "p1103_prog", "name": "停用目录病种", "category": "chronic"},
                    headers=admin)
    assert r.status_code == 201, r.text
    counter = iter(range(1, 1000))

    def patient():
        n = next(counter)
        r = client.post("/api/patients", json={"name": f"停用目录患者{n}", "id_card": f"33019219810101{n:04d}",
                                               "gender": "男", "birth_date": "1981-01-01"}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def team(name):
        r = client.post(f"{B}/teams", json={"name": name, "org_id": org}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    return {"org": org, "patient": patient, "team": team}


def test_报告推送任务不收停用模板_启用的照常(client, admin):
    from app.spd.models import SpdReportTask

    sections = [{"key": "summary"}]
    off = client.post(f"{B}/report-templates", json={"code": "p1103_rpt_off", "name": "停用的报告模板",
                                                     "sections": sections}, headers=admin).json()["id"]
    assert client.patch(f"{B}/report-templates/{off}", json={"active": False}, headers=admin).status_code == 200
    r = client.post(f"{B}/report-tasks", json={"template_id": off, "name": "建在停用模板上的任务"}, headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text  # 修前 201，之后天天被调度跳过
    with SessionLocal() as db:
        assert db.query(SpdReportTask).filter(SpdReportTask.template_id == off).count() == 0

    on = client.post(f"{B}/report-templates", json={"code": "p1103_rpt_on", "name": "启用的报告模板",
                                                    "sections": sections}, headers=admin).json()["id"]
    r = client.post(f"{B}/report-tasks", json={"template_id": on, "name": "正常的任务"}, headers=admin)
    assert r.status_code == 201, r.text


def test_分发目标患者不分给停用团队(client, admin, world):
    off = world["team"]("停用的服务团队")
    assert client.patch(f"{B}/teams/{off}", json={"active": False}, headers=admin).status_code == 200
    # 团队查验在取目标池之前：候选编号填不存在的也无妨，404 一定来自团队
    r = client.post(f"{B}/candidates/distribute", json={"candidate_ids": [987654321], "team_id": off},
                    headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text
    on = world["team"]("启用的服务团队")
    r = client.post(f"{B}/candidates/distribute", json={"candidate_ids": [987654321], "team_id": on},
                    headers=admin)
    assert r.status_code == 200 and r.json()["not_found"] == 1, r.text


def test_纳管档案不挂停用团队_改档原样带回旧团队照常(client, admin, world):
    from app.spd.models import SpdEnrollment

    team = world["team"]("先启用后停用的团队")
    pid = world["patient"]()
    r = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p1103_prog",
                                              "org_id": world["org"], "team_id": team}, headers=admin)
    assert r.status_code == 201, r.text
    enrollment = r.json()["id"]
    assert client.patch(f"{B}/teams/{team}", json={"active": False}, headers=admin).status_code == 200

    # 建档挂停用团队：404 且不建档
    pid2 = world["patient"]()
    r = client.post(f"{B}/enrollments", json={"patient_id": pid2, "program_code": "p1103_prog",
                                              "org_id": world["org"], "team_id": team}, headers=admin)
    assert r.status_code == 404 and "服务团队已停用" in r.json()["detail"], r.text
    with SessionLocal() as db:
        assert db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == pid2).count() == 0

    # 改档换到另一个停用团队：404 且不改
    other = world["team"]("另一个停用团队")
    assert client.patch(f"{B}/teams/{other}", json={"active": False}, headers=admin).status_code == 200
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"team_id": other}, headers=admin)
    assert r.status_code == 404, r.text
    # 整份回传档案（团队原样、只改风险分层）照常：团队是后来停用的，不该挡住别的字段
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"team_id": team, "risk_level": "high"}, headers=admin)
    assert r.status_code == 200 and r.json()["risk_level"] == "high", r.text
    with SessionLocal() as db:
        assert db.get(SpdEnrollment, enrollment).team_id == team


def test_停用的服务包建档与绑包都不收(client, admin, world):
    from app.spd.models import SpdEnrollment, SpdPackageBinding

    items = [{"code": "visit", "name": "上门随访", "times": 4}]
    off = client.post(f"{B}/service-packages", json={"code": "p1103_pkg_off", "name": "停用的服务包", "items": items},
                      headers=admin).json()["id"]
    assert client.patch(f"{B}/service-packages/{off}", json={"active": False}, headers=admin).status_code == 200

    pid = world["patient"]()
    r = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p1103_prog",
                                              "org_id": world["org"], "package_id": off}, headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text
    with SessionLocal() as db:
        assert db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == pid).count() == 0

    r = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p1103_prog",
                                              "org_id": world["org"]}, headers=admin)
    assert r.status_code == 201, r.text
    enrollment = r.json()["id"]
    r = client.post(f"{B}/enrollments/{enrollment}/packages", json={"package_id": off}, headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text  # 修前 201：居民端多一张停用包的卡片
    with SessionLocal() as db:
        assert db.query(SpdPackageBinding).filter(SpdPackageBinding.enrollment_id == enrollment).count() == 0

    on = client.post(f"{B}/service-packages", json={"code": "p1103_pkg_on", "name": "启用的服务包", "items": items},
                     headers=admin).json()["id"]
    r = client.post(f"{B}/enrollments/{enrollment}/packages", json={"package_id": on}, headers=admin)
    assert r.status_code == 201, r.text


def test_个案上报不收停用的上报任务(client, admin, world):
    off = client.post(f"{B}/case-report-tasks", json={"code": "p1103_crt_off", "name": "停用的上报任务"},
                      headers=admin).json()["id"]
    assert client.patch(f"{B}/case-report-tasks/{off}", json={"active": False}, headers=admin).status_code == 200
    pid = world["patient"]()
    r = client.post(f"{B}/case-reports", json={"patient_id": pid, "task_id": off}, headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text
    on = client.post(f"{B}/case-report-tasks", json={"code": "p1103_crt_on", "name": "启用的上报任务"},
                     headers=admin).json()["id"]
    r = client.post(f"{B}/case-reports", json={"patient_id": pid, "task_id": on}, headers=admin)
    assert r.status_code == 201, r.text


def test_干预不引用停用模板(client, admin, world):
    tpl = client.post(f"{B}/intervention-templates", json={"code": "p1103_itv", "name": "停用的干预模板",
                                                           "content": "低盐饮食"}, headers=admin)
    assert tpl.status_code == 201, tpl.text
    tpl_id = tpl.json()["id"]
    pid = world["patient"]()
    body = {"patient_ids": [pid], "template_id": tpl_id, "create_task": False}
    _disable("SpdInterventionTemplate", tpl_id)
    r = client.post(f"{B}/interventions", json=body, headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text


@pytest.fixture(scope="module")
def depts(client, admin, world):
    def dept(code):
        r = client.post("/api/mgmt/departments", json={"org_id": world["org"], "code": code, "name": f"科室{code}"},
                        headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    return {"on": dept("P1103A"), "on2": dept("P1103B"), "off": dept("P1103C")}


def test_分摊规则与物资采购不收停用科室(client, admin, world, depts):
    _disable("Department", depts["off"])
    for pair in ((depts["on"], depts["off"]), (depts["off"], depts["on"])):
        r = client.post("/api/cost/allocation-rules", json={"from_dept_id": pair[0], "to_dept_id": pair[1],
                                                            "ratio_pct": 30}, headers=admin)
        assert r.status_code == 404 and "已停用" in r.json()["detail"], (pair, r.text)
    r = client.post("/api/cost/allocation-rules", json={"from_dept_id": depts["on"], "to_dept_id": depts["on2"],
                                                        "ratio_pct": 30}, headers=admin)
    assert r.status_code == 201, r.text

    base = {"org_id": world["org"], "item_name": "输液泵"}
    r = client.post("/api/materials/purchases", json={**base, "dept_id": depts["off"]}, headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text
    assert client.post("/api/materials/purchases", json={**base, "dept_id": depts["on"]},
                       headers=admin).status_code == 201


def test_物资采购的科室须属于申请机构(client, admin, world):
    """P1-103 把取科室改成取出变量后，P1-80 的捎带对象判据看见了它：甲院的采购单挂乙院的科室照收，
    按科室归集的成本从此算错机构。"""
    other = client.post("/api/organizations", json={"name": "停用目录·别家卫生院", "org_type": "township",
                                                    "level": "township"}, headers=admin).json()["id"]
    dept = client.post("/api/mgmt/departments", json={"org_id": other, "code": "P1103X", "name": "别家科室"},
                       headers=admin)
    assert dept.status_code == 201, dept.text
    r = client.post("/api/materials/purchases", headers=admin,
                    json={"org_id": world["org"], "dept_id": dept.json()["id"], "item_name": "输液泵"})
    assert r.status_code == 422 and "不属于该机构" in r.json()["detail"], r.text


def test_高值耗材不从停用供应商入库(client, admin, world):
    def supplier(name):
        r = client.post("/api/pharmacy/suppliers", json={"name": name}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    off, on = supplier("停用目录·停用供应商"), supplier("停用目录·在用供应商")
    _disable("Supplier", off)
    base = {"name": "心脏支架", "org_id": world["org"]}
    r = client.post("/api/materials/consumables", json={**base, "barcode": "P1103-OFF", "supplier_id": off},
                    headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text
    r = client.post("/api/materials/consumables", json={**base, "barcode": "P1103-ON", "supplier_id": on},
                    headers=admin)
    assert r.status_code == 201, r.text


def test_凭证分录不用停用科目(client, admin, world):
    r = client.post("/api/accounting/subjects", json={"code": "P1103X", "name": "停用目录·待停科目",
                                                      "category": "expense"}, headers=admin)
    assert r.status_code == 201, r.text
    _disable("AccountSubject", r.json()["id"])

    def voucher(no, code):
        return client.post("/api/accounting/vouchers", headers=admin, json={
            "org_id": world["org"], "voucher_no": no, "voucher_date": "2026-09-01",
            "entries": [{"subject_code": code, "debit": 100}, {"subject_code": "1001", "credit": 100}]})

    r = voucher("P1103-1", "P1103X")
    assert r.status_code == 422 and "科目不存在或已停用：P1103X" in r.json()["detail"], r.text
    assert voucher("P1103-2", "1002").status_code == 201


# ================================================================ 闸门：零基线
#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）
BASELINE = 0

#: 按设计不看启用标志的「处理函数:模型」→ 理由（只减不增，失效即红）
BY_DESIGN: dict[str, str] = {
    "routers/cost.py:upsert_department_cost:Department":
        "按期间归集科室直接成本：撤销的科室，往期成本照样要补录、月末反复调整（成本表按期间出，不按科室现状）",
    "routers/fund.py:create_pool:OrgGroup":
        "机构分组的启用标志只管清单筛选，可见性（visibility）与覆盖统计都不看它；基金池按年度建，停用分组补建往年的池要能建",
    "spd/routers/assess.py:create_plan:SpdIndicator":
        "考核方案是配置：计分时停用的指标逐项记「指标不存在或已停用」、不计分（run_scoring），结果里看得见",
    "spd/routers/assess.py:update_plan:SpdIndicator": "同上（改方案与建方案同一句 _check_plan_items）",
    "spd/routers/assess.py:run_scoring:SpdAssessPlan":
        "按周期重算、覆盖上次结果：方案停用后，补算停用前那些周期仍要能跑",
    "spd/routers/config/centers.py:create_center:SpdProgram":
        "病种配置可以先于启用：停用病种、配好中心与路径再启用是正常流程；业务入口（筛查 / 自动识别 / 建档）都拒停用病种（P1-89）",
    "spd/routers/config/paths.py:create_path_template:SpdProgram": "同上（路径模板也是病种配置）",
    "spd/routers/followup.py:generate_report:SpdReportTemplate":
        "手动「生成报告」当场出结果、看得见；模板停用管的是调度不再自动推送（jobs.spd_report_push 跳过停用模板）",
}

_FLAG_COLUMNS = ("active", "enabled")
_SKIP_ANNOTATIONS = {"Session", "Response", "Request", "BackgroundTasks", "User", "WebSocket"}
_TERMINALS = ("first", "one", "one_or_none", "scalar", "all")
#: 值里只有这些调用时，才算「原样取自入参」往下传（查库 / 新建对象 / 调别的函数都不算）
_PURE_METHODS = {"model_dump", "strip", "lower", "upper", "items", "values", "keys", "copy", "split", "replace"}
_PURE_FUNCS = {"int", "str", "float", "list", "set", "dict", "tuple", "sorted", "len", "bool", "min", "max"}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def flagged_models() -> tuple[dict[str, str], set[str]]:
    """(带启用标志的模型 → 标志列名, 全部 ORM 模型名)，从映射派生。"""
    import app.models  # noqa: F401  先 app.models 再 app.spd.models（P2-51）
    import app.spd.models  # noqa: F401
    from app.database import Base

    flagged, orm = {}, set()
    for m in Base.registry.mappers:
        orm.add(m.class_.__name__)
        cols = {c.name for c in m.columns}
        for flag in _FLAG_COLUMNS:
            if flag in cols:
                flagged[m.class_.__name__] = flag
                break
    return flagged, orm


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _fetches(value: ast.AST, orm: set[str]) -> bool:
    for c in ast.walk(value):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        if isinstance(f, ast.Attribute):
            if f.attr in _PURE_METHODS:
                continue
            if f.attr == "get" and (len(c.args) == 1 or (c.args and isinstance(c.args[0], ast.Constant))):
                continue  # 字典取值，不是 db.get(Model, id)
            return True
        if isinstance(f, ast.Name) and f.id in _PURE_FUNCS and f.id not in orm:
            continue
        return True
    return False


def _derived(fn: ast.AST, seeds: set[str], orm: set[str]) -> set[str]:
    """由入参原样派生的局部名（不穿过查库 / 新建对象 / 属性赋值）。"""
    local = set(seeds)
    changed = True
    while changed:
        changed = False
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                pairs = [(t, n.value) for t in n.targets]
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value is not None:
                pairs = [(n.target, n.value)]
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
                pairs = [(n.target, n.iter)]
            else:
                continue
            for target, value in pairs:
                if isinstance(target, (ast.Attribute, ast.Subscript)) or _fetches(value, orm):
                    continue
                if _names(value) & local:
                    new = _names(target) - local
                    if new:
                        local |= new
                        changed = True
    return local


def _table_models(fn: ast.AST, consts: dict[str, ast.AST]) -> dict[str, set[str]]:
    """`for 字段, (model, 名) in 常量.items()` 里循环变量 → 常量值同一位置上的模型名。"""
    out: dict[str, set[str]] = {}
    for n in ast.walk(fn):
        if not (isinstance(n, ast.For) and isinstance(n.iter, ast.Call) and isinstance(n.iter.func, ast.Attribute)
                and isinstance(n.iter.func.value, ast.Name) and isinstance(consts.get(n.iter.func.value.id), ast.Dict)):
            continue
        const = consts[n.iter.func.value.id]
        assert isinstance(const, ast.Dict)
        target = n.target
        if n.iter.func.attr == "items" and isinstance(target, ast.Tuple) and len(target.elts) == 2:
            target = target.elts[1]
        if not isinstance(target, ast.Tuple):
            continue
        for i, el in enumerate(target.elts):
            if isinstance(el, ast.Name):
                out[el.id] = {v.elts[i].id for v in const.values if isinstance(v, ast.Tuple)
                              and len(v.elts) > i and isinstance(v.elts[i], ast.Name)}
    return out


def _chain(call: ast.Call) -> list[ast.Call]:
    out: list[ast.Call] = []
    cur: ast.AST | None = call
    while isinstance(cur, ast.Call):
        out.append(cur)
        cur = cur.func.value if isinstance(cur.func, ast.Attribute) else None
    return out


def _lookups(fn, derived, consts, flagged):
    """(模型, 查找调用, 取它的那条查询里已带启用标志)"""
    table = _table_models(fn, consts)
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if n.func.attr == "get" and len(n.args) == 2 and isinstance(n.args[0], ast.Name) \
                and _names(n.args[1]) & derived:
            models = {n.args[0].id} & flagged.keys() or table.get(n.args[0].id, set()) & flagged.keys()
            for model in models:
                yield model, n, False
        if n.func.attr in _TERMINALS:
            chain = _chain(n)
            queries = [c for c in chain if isinstance(c.func, ast.Attribute) and c.func.attr == "query" and c.args]
            if not queries:
                continue
            head = queries[-1].args[0]
            model = head.id if isinstance(head, ast.Name) else (
                head.value.id if isinstance(head, ast.Attribute) and isinstance(head.value, ast.Name) else None)
            if model not in flagged:
                continue
            filters = [c for c in chain if isinstance(c.func, ast.Attribute) and c.func.attr in ("filter", "filter_by")]
            parts = [a for c in filters for a in c.args + [k.value for k in c.keywords]]
            if not any(_names(p) & derived for p in parts):
                continue
            flag = flagged[model]
            chained = any(isinstance(x, ast.Attribute) and x.attr == flag and isinstance(x.value, ast.Name)
                          and x.value.id == model for p in parts for x in ast.walk(p)) \
                or any(k.arg == flag for c in filters for k in c.keywords)
            yield model, n, chained


def _bound(fn: ast.AST, call: ast.AST) -> set[str]:
    for n in ast.walk(fn):
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and n.value is not None \
                and any(x is call for x in ast.walk(n.value)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            return {x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name)}
    return set()


def _returned(fn: ast.AST, call: ast.AST) -> bool:
    return any(isinstance(n, ast.Return) and n.value is not None and any(x is call for x in ast.walk(n.value))
               for n in ast.walk(fn))


def _flag_read(fn: ast.AST, names: set[str], flag: str) -> bool:
    for x in ast.walk(fn):
        if isinstance(x, ast.Attribute) and x.attr == flag and isinstance(x.value, ast.Name) and x.value.id in names:
            return True
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "getattr" and len(x.args) >= 2 \
                and isinstance(x.args[0], ast.Name) and x.args[0].id in names \
                and isinstance(x.args[1], ast.Constant) and x.args[1].value == flag:
            return True
    return False


def _inputs(fn, route: str) -> set[str]:
    """入参：请求体与查询参数。路径参数（被操作对象本身）、依赖注入、会话 / 当前用户不算。"""
    path_params = set(re.findall(r"{(\w+)", route))
    args = fn.args.args + fn.args.kwonlyargs
    defaults = [None] * (len(fn.args.args) - len(fn.args.defaults)) + list(fn.args.defaults) + list(fn.args.kw_defaults)
    out = set()
    for arg, default in zip(args, defaults):
        if arg.arg in path_params:
            continue
        if isinstance(default, ast.Call) and ast.unparse(default.func).split(".")[-1] in ("Depends", "Security"):
            continue
        if arg.annotation is not None and ast.unparse(arg.annotation).split(".")[-1] in _SKIP_ANNOTATIONS:
            continue
        out.add(arg.arg)
    return out


class _Walk:
    """从一个处理函数出发，沿「实参派生自入参」的同模块调用走两层，收集每一次目录查找。"""

    def __init__(self, funcs, consts, flagged, orm):
        self.funcs, self.consts, self.flagged, self.orm = funcs, consts, flagged, orm
        self.hits: list[tuple] = []   # (所在函数, 模型, 查找调用, 已带标志, 调用处)
        self.visited: list[ast.AST] = []
        self.seen: set[tuple] = set()

    def visit(self, fn, seeds: set[str], depth: int, site) -> None:
        key = (fn.name, frozenset(seeds))
        if key in self.seen:
            return
        self.seen.add(key)
        self.visited.append(fn)
        derived = _derived(fn, seeds, self.orm)
        for model, call, chained in _lookups(fn, derived, self.consts, self.flagged):
            self.hits.append((fn, model, call, chained, site))
        if depth >= 2:
            return
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in self.funcs:
                callee = self.funcs[call.func.id]
                params = [a.arg for a in callee.args.args]
                sub = {params[i] for i, a in enumerate(call.args) if i < len(params) and _names(a) & derived}
                sub |= {k.arg for k in call.keywords if k.arg and _names(k.value) & derived}
                if sub:
                    self.visit(callee, sub, depth + 1, (fn, call))


def unchecked_catalog_lookups(sources: dict[str, str] | None = None) -> list[str]:
    """写接口按入参取启用标志目录对象、却不看启用标志的「文件:处理函数:模型」。"""
    flagged, orm = flagged_models()
    files = {p.relative_to(ds.APP_DIR).as_posix(): p.read_text(encoding="utf-8")
             for base in ds._ROUTE_DIRS for p in sorted(base.rglob("*.py"))}
    files.update(sources or {})
    found: set[str] = set()
    for rel, text in files.items():
        tree = ast.parse(text)
        funcs = {f.name: f for f in tree.body if isinstance(f, _FUNCS)}
        consts = {t.id: node.value for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                  and node.value is not None
                  for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)}
        for handler in funcs.values():
            decorators = [d for d in handler.decorator_list if isinstance(d, ast.Call)
                          and isinstance(d.func, ast.Attribute) and d.func.attr in ("post", "put", "patch")]
            if not decorators:
                continue
            first = decorators[0].args[0] if decorators[0].args else None
            route = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""
            walk = _Walk(funcs, consts, flagged, orm)
            walk.visit(handler, _inputs(handler, route), 0, None)
            built = {c.func.id for f in walk.visited for c in ast.walk(f)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in orm}
            for fn, model, call, chained, site in walk.hits:
                if chained or model in built:
                    continue
                flag = flagged[model]
                names = _bound(fn, call)
                if names and _flag_read(fn, names, flag):
                    continue
                if not names and site is not None and _returned(fn, call):
                    caller, call_site = site
                    caller_names = _bound(caller, call_site)
                    if caller_names and _flag_read(caller, caller_names, flag):
                        continue  # 取数函数原样 return，由调用处看标志（chronic.get_disease_type）
                found.add(f"{rel}:{handler.name}:{model}")
    return sorted(found)


def test_写接口引用启用标志目录_取出来得看标志():
    bad = [k for k in unchecked_catalog_lookups() if k not in BY_DESIGN]
    assert len(bad) <= BASELINE, (
        "以下写接口按入参取了带启用标志的目录对象，却不看它停用没有：\n  " + "\n  ".join(bad)
        + "\n\n停用的意思是「不再用于新业务」：清单只列启用的、调度见停用就跳过，按编号直接调接口却照样引用得上。"
        "取出来看一眼：`if x is None or not x.active: raise HTTPException(404, \"X不存在或已停用\")`；"
        "按设计要引用停用对象的（历史期间补录、配置先于启用……）写进 BY_DESIGN 并写明理由。"
    )


def test_按设计名单只许变少_失效即红():
    stale = sorted(BY_DESIGN.keys() - set(unchecked_catalog_lookups()))
    assert stale == [], f"这些已经看了启用标志（或不再引用），请从 BY_DESIGN 划掉：{stale}"


SELF_PROOF = '''
class TeamRef(BaseModel):
    team_id: int
    code: str

@router.post("/a")
def unbound(body: TeamRef, db: Session = Depends(get_db)):
    if db.get(SpdTeam, body.team_id) is None:
        raise HTTPException(404)

@router.post("/b")
def bound_unread(body: TeamRef, db: Session = Depends(get_db)):
    team = db.get(SpdTeam, body.team_id)
    if team is None:
        raise HTTPException(404)

@router.post("/c")
async def bound_read(body: TeamRef, db: Session = Depends(get_db)):
    team = db.get(SpdTeam, body.team_id)
    if team is None or not team.active:
        raise HTTPException(404)

@router.post("/d")
def chain_filtered(body: TeamRef, db: Session = Depends(get_db)):
    return db.query(SpdProgram).filter(SpdProgram.code == body.code, SpdProgram.active.is_(True)).first()

def _get_program(db, code):
    return db.query(SpdProgram).filter(SpdProgram.code == code).first()

@router.post("/e")
def via_helper_read(body: TeamRef, db: Session = Depends(get_db)):
    program = _get_program(db, body.code)
    if program is None or not program.active:
        raise HTTPException(404)

@router.post("/f")
def via_helper_unread(body: TeamRef, db: Session = Depends(get_db)):
    if _get_program(db, body.code) is None:
        raise HTTPException(404)

_REFS = {"team_id": (SpdTeam, "团队")}

def _check_refs(db, values):
    for field, (model, label) in _REFS.items():
        if db.get(model, values.get(field)) is None:
            raise HTTPException(404)

@router.post("/g")
def table_driven(body: TeamRef, db: Session = Depends(get_db)):
    _check_refs(db, body.model_dump())

@router.patch("/h/{team_id}")
def path_param(team_id: int, db: Session = Depends(get_db)):
    team = db.get(SpdTeam, team_id)

@router.post("/i")
def dup_check(body: TeamRef, db: Session = Depends(get_db)):
    if db.query(SpdProgram).filter(SpdProgram.code == body.code).first():
        raise HTTPException(409)
    db.add(SpdProgram(code=body.code))

@router.post("/j")
def query_param(team_id: int, db: Session = Depends(get_db)):
    team = db.get(SpdTeam, team_id)
'''


def test_判据自证_未看标志的五种形状当场点名_看过的与操作对象本身不报():
    got = [k for k in unchecked_catalog_lookups({"自证.py": SELF_PROOF}) if k.startswith("自证.py")]
    assert got == [
        "自证.py:bound_unread:SpdTeam",
        "自证.py:query_param:SpdTeam",
        "自证.py:table_driven:SpdTeam",
        "自证.py:unbound:SpdTeam",
        "自证.py:via_helper_unread:SpdProgram",
    ], got
