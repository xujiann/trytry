"""父对象已经结束（离职 / 已出报告 / 已退回 / 死亡 / 结项）仍能挂上新的子行（P1-104）。

写接口取一个带 `status` 的父对象，随后新建外键指向它的子行，却一眼不看父对象的状态。
2026-09-24 开发库实测（修前代码）：

- **号源挂离职医师**：员工登记离职后，给他放号 201、患者挂他的号 201——约了一个没人坐诊的号；
  离职前放出的号源照样约得上（登记离职不回收号源）；
- **病理申请已出报告仍收标本**：出报告只收待诊断 / 诊断中的申请，已出报告（或互认了既往结果）的申请
  再送检 201，标本照样走核收→取材→阅片，报告却再也出不了（409「当前状态 reported 不可出报告」）；
- **县外就诊挂到已退回的转诊单**：201、`referred=True`——被退回的转诊没有转成，患者是自行外出，
  有序转诊率因此虚高（同一函数早就拦了「挂到别人的转诊单」，理由相同）；
- **死亡档案绑服务包**：慢专病档案登记死亡后绑包 201，同一档案改档却是 409「非在管状态的档案不可修改」；
- **离职员工签在期合同**：到期提醒只看合同状态，照样提醒续签一个已经走了的人（补录已到期的历史合同照收，
  与派驻 P1-102 同一口径）；
- **已完成 / 已中止的项目加里程碑**：项目本身的逾期判断早把这两态排除在外，新加的里程碑照样按到期日算逾期。

**闸门**（派生、零基线）：写接口里 `x = db.get(父, 入参)`（父模型带 status 列；入参含路径参数——父对象多半就在
路径里；取数函数原样 return 的由调用处接手），同一处理函数（连同同模块被调函数一层）新建了外键指向父表的
子行，就得看一眼 `x.status`（或把 x 交给看 status 的同模块函数）。按设计不看的写进 `BY_DESIGN`（逐条写理由，
只减不增，失效即红）。⚠️ 盲区：按查询（`query…filter(...).all()`）取的父对象、跨模块的取数函数不跟——
号源批量生成就是这么取医师的，已一并修上、由下面的行为回归钉住。
"""
from __future__ import annotations

import ast

import pytest
import test_datestr_single_source as ds

from app.database import SessionLocal

# ---------------------------------------------------------------- 行为回归


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "父对象结束卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    counter = iter(range(1, 1000))

    def patient():
        n = next(counter)
        r = client.post("/api/patients", json={"name": f"父对象结束患者{n}", "id_card": f"33019219830101{n:04d}",
                                               "gender": "女", "birth_date": "1983-01-01"}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def employee(name, leave=False):
        emp = client.post("/api/mgmt/employees", json={"org_id": org, "name": name, "position": "主治医师"},
                          headers=admin).json()["id"]
        if leave:
            r = client.post(f"/api/mgmt/employees/{emp}/changes", json={"change_type": "leave"}, headers=admin)
            assert r.status_code == 201 and r.json()["employee_status"] == "left", r.text
        return emp

    return {"org": org, "patient": patient, "employee": employee}


def _slot(client, admin, org, employee_id, day="2031-01-06"):
    return client.post("/api/appointments/slots", headers=admin, json={
        "org_id": org, "resource_type": "outpatient", "resource_name": "内科", "employee_id": employee_id,
        "slot_date": day, "slot_time": "09:00-10:00", "capacity": 5})


def test_离职医师不放号_批量也不放(client, admin, world):
    gone = world["employee"]("离职的内科医师", leave=True)
    r = _slot(client, admin, world["org"], gone)
    assert r.status_code == 409 and "已离职" in r.json()["detail"], r.text  # 修前 201
    r = client.post("/api/appointments/slots/batch", headers=admin, json={
        "org_id": world["org"], "date_from": "2031-01-06", "date_to": "2031-01-07",
        "templates": [{"resource_type": "outpatient", "resource_name": "内科", "employee_id": gone,
                       "slot_time": "10:00-11:00"}]})
    assert r.status_code == 409 and "已离职" in r.json()["detail"], r.text
    here = world["employee"]("在岗的内科医师")
    assert _slot(client, admin, world["org"], here).status_code == 201


def test_离职前放出的号源_离职后约不上(client, admin, world):
    doctor = world["employee"]("即将离职的医师")
    slot = _slot(client, admin, world["org"], doctor, day="2031-01-08")
    assert slot.status_code == 201, slot.text
    r = client.post(f"/api/mgmt/employees/{doctor}/changes", json={"change_type": "leave"}, headers=admin)
    assert r.status_code == 201, r.text
    book = client.post("/api/appointments", json={"slot_id": slot.json()["id"], "patient_id": world["patient"]()},
                       headers=admin)
    assert book.status_code == 409 and "已离职" in book.json()["detail"], book.text  # 修前 201


def test_病理申请出了报告就不再收标本(client, admin, world):
    pid = world["patient"]()

    def request():
        r = client.post("/api/exams", headers=admin, json={"patient_id": pid, "from_org_id": world["org"],
                                                           "center_type": "pathology", "item_code": "P1104",
                                                           "item_name": "活检病理"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    open_req = request()
    assert client.post("/api/pathology/specimens", json={"request_id": open_req, "site": "胃窦"},
                       headers=admin).status_code == 201  # 一张申请多部位取材照常
    done = request()
    assert client.post(f"/api/exams/{done}/report", json={"conclusion": "慢性炎症"}, headers=admin).status_code == 201
    r = client.post("/api/pathology/specimens", json={"request_id": done, "site": "胃体"}, headers=admin)
    assert r.status_code == 409 and "reported" in r.json()["detail"], r.text  # 修前 201，之后出报告 409


def test_县外就诊不挂已退回的转诊单(client, admin, world):
    other = client.post("/api/organizations", json={"name": "父对象结束县医院", "org_type": "township",
                                                    "level": "township"}, headers=admin).json()["id"]
    pid = world["patient"]()

    def referral():
        r = client.post("/api/referrals", json={"patient_id": pid, "from_org_id": world["org"], "to_org_id": other,
                                                "direction": "up"}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    rejected = referral()
    assert client.patch(f"/api/referrals/{rejected}/status", json={"status": "rejected"},
                        headers=admin).status_code == 200
    body = {"patient_id": pid, "visit_date": "2026-09-01", "external_org_name": "省人民医院"}
    r = client.post("/api/analytics/outbound-visits", json={**body, "referral_id": rejected}, headers=admin)
    assert r.status_code == 422 and "已退回" in r.json()["detail"], r.text  # 修前 201、referred=True
    r = client.post("/api/analytics/outbound-visits", json={**body, "referral_id": referral()}, headers=admin)
    assert r.status_code == 201 and r.json()["referred"] is True, r.text


def test_死亡档案不绑服务包(client, admin, world):
    from app.spd.models import SpdPackageBinding

    client.post("/api/spd/programs", json={"code": "p1104_prog", "name": "父对象结束病种", "category": "chronic"},
                headers=admin)
    enrollment = client.post("/api/spd/enrollments", json={"patient_id": world["patient"](),
                                                           "program_code": "p1104_prog", "org_id": world["org"]},
                             headers=admin).json()["id"]
    assert client.post(f"/api/spd/enrollments/{enrollment}/lifecycle", json={"event": "death", "reason": "病故"},
                       headers=admin).status_code == 200
    pkg = client.post("/api/spd/service-packages", headers=admin, json={
        "code": "p1104_pkg", "name": "父对象结束服务包", "items": [{"code": "visit", "times": 2}]}).json()["id"]
    r = client.post(f"/api/spd/enrollments/{enrollment}/packages", json={"package_id": pkg}, headers=admin)
    assert r.status_code == 409 and "非在管" in r.json()["detail"], r.text  # 修前 201
    with SessionLocal() as db:
        assert db.query(SpdPackageBinding).filter(SpdPackageBinding.enrollment_id == enrollment).count() == 0


def test_离职员工不签在期合同_补录已到期的照收(client, admin, world):
    gone = world["employee"]("离职的护士", leave=True)
    r = client.post("/api/mgmt/staff-contracts", headers=admin, json={
        "employee_id": gone, "contract_no": "P1104-C1", "start_date": "2026-01-01", "end_date": "2031-12-31"})
    assert r.status_code == 409 and "已离职" in r.json()["detail"], r.text
    r = client.post("/api/mgmt/staff-contracts", headers=admin, json={
        "employee_id": gone, "contract_no": "P1104-C2", "start_date": "2020-01-01", "end_date": "2022-12-31"})
    assert r.status_code == 201, r.text


def test_结项或中止的项目不再加里程碑(client, admin, world):
    def project(name):
        r = client.post("/api/projects", json={"org_id": world["org"], "name": name}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    done, stopped, running = project("已结项的项目"), project("已中止的项目"), project("进行中的项目")
    assert client.patch(f"/api/projects/{done}", json={"status": "done", "progress_pct": 100},
                        headers=admin).status_code == 200
    assert client.patch(f"/api/projects/{stopped}", json={"status": "suspended"}, headers=admin).status_code == 200
    for pid in (done, stopped):
        r = client.post(f"/api/projects/{pid}/milestones", json={"name": "补一个节点", "due_date": "2020-01-01"},
                        headers=admin)
        assert r.status_code == 409, r.text
    assert client.post(f"/api/projects/{running}/milestones", json={"name": "正常节点"},
                       headers=admin).status_code == 201


# ================================================================ 闸门：零基线
#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）
BASELINE = 0

#: 按设计不看父对象状态的「处理函数:父模型」→ 理由（只减不增，失效即红）
BY_DESIGN: dict[str, str] = {
    "routers/admin_mgmt.py:create_payroll:Employee": "离职当月工资、离职后补发都要录——薪酬是对已发生劳动的结算",
    "routers/billing.py:create_settlement:Admission":
        "出院前必须先结清（inpatient.discharge 查 _assert_billing_settled），一次住院一张结算单由唯一索引兜底",
    "routers/inpatient.py:create_case_summary:Admission": "出院前必须先填病案首页（discharge 查首页），出院后已有首页即 409",
    "routers/clinical_docs.py:create_nursing_record:InpatientOrder":
        "护理记录只是引用一条医嘱（记录停嘱后的观察也要引用它），不是执行医嘱；住院状态本函数已判",
    "routers/cssd.py:create_cost_item:SterilizationBatch": "灭菌失败 / 召回的批次，人工与耗材照样花了，成本照记",
    "routers/emergency.py:record_milestone:EmergencyCase":
        "绿道时间节点跨院前院内（到院、心电图、球囊扩张都在收治前后），急救事件没有「取消」态",
    "routers/maternal.py:create_screening:MaternalRecord":
        "筛查结果晚到（结案后才出结果）照样入档：结论是这次孕期的事实，结案不该把它挡在档案外",
    "routers/prescriptions.py:comment_prescription:Prescription": "处方点评本就是事后评价，已发药 / 已作废的处方都要点评",
    "routers/users.py:change_user_role:User": "角色变更留痕行（谁把谁改成了什么），不是挂在账号下的业务",
    "spd/routers/config/paths.py:copy_path_template:SpdPathTemplate": "复制出一份新草稿，已停用 / 已发布的模板都能当底本",
}

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SKIP = {"db", "user", "response", "request"}


def _schema() -> tuple[set[str], dict[str, str], dict[str, set[str]], set[str]]:
    """(带 status 列的模型, 模型 → 表名, 模型 → 外键指向的表, 全部 ORM 模型名)"""
    import app.models  # noqa: F401  先 app.models 再 app.spd.models（P2-51）
    import app.spd.models  # noqa: F401
    from app.database import Base

    with_status, table, fk_to, orm = set(), {}, {}, set()
    for m in Base.registry.mappers:
        name = m.class_.__name__
        orm.add(name)
        table[name] = m.local_table.name
        if "status" in {c.name for c in m.columns}:
            with_status.add(name)
        fk_to[name] = {fk.column.table.name for c in m.columns for fk in c.foreign_keys}
    return with_status, table, fk_to, orm


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _params(fn) -> set[str]:
    return {a.arg for a in fn.args.args + fn.args.kwonlyargs} - _SKIP


def _direct_parents(fn, with_status: set[str]) -> dict[str, str]:
    """`x = db.get(父, 入参…)` → {x: 父}。入参：本函数的参数（路径 / 请求体 / 被调函数的形参）。"""
    params, out = _params(fn), {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) \
                and n.value.func.attr == "get" and len(n.value.args) == 2 and isinstance(n.value.args[0], ast.Name) \
                and n.value.args[0].id in with_status and _names(n.value.args[1]) & params:
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n.value.args[0].id
    return out


def _returned_names(fn) -> set[str]:
    return {n.value.id for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)}


def _reads_status(fn, var: str, funcs: dict, model: str = "") -> bool:
    """本函数里看 `var.status`；或按状态做条件更新 / 查询（`Bed.status == "free"` 这种原子占床）；
    或把 var 交给同模块函数、那个函数看了对应形参的 `.status`。"""
    for x in ast.walk(fn):
        if isinstance(x, ast.Attribute) and x.attr == "status" and isinstance(x.value, ast.Name) \
                and x.value.id in (var, model):
            return True
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id in funcs:
            callee = funcs[x.func.id]
            names = [a.arg for a in callee.args.args]
            for i, arg in enumerate(x.args):
                if isinstance(arg, ast.Name) and arg.id == var and i < len(names) and _reads_status(callee, names[i], {}):
                    return True
    return False


def unchecked_parent_writes(sources: dict[str, str] | None = None) -> list[str]:
    """新建外键指向父表的子行、却不看父对象 status 的「文件:处理函数:父模型」。"""
    with_status, table, fk_to, orm = _schema()
    files = {p.relative_to(ds.APP_DIR).as_posix(): p.read_text(encoding="utf-8")
             for base in ds._ROUTE_DIRS for p in sorted(base.rglob("*.py"))}
    files.update(sources or {})
    found: set[str] = set()
    for rel, text in files.items():
        tree = ast.parse(text)
        funcs = {f.name: f for f in tree.body if isinstance(f, _FUNCS)}
        for handler in funcs.values():
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("post", "put", "patch") for d in handler.decorator_list):
                continue
            helpers = [funcs[c.func.id] for c in ast.walk(handler) if isinstance(c, ast.Call)
                       and isinstance(c.func, ast.Name) and c.func.id in funcs and funcs[c.func.id] is not handler]
            scope = [handler, *helpers]
            parents: list[tuple[ast.AST, str, str]] = []   # (变量所在函数, 变量, 父模型)
            bound_calls = {n.value.func.id for n in ast.walk(handler) if isinstance(n, ast.Assign)
                           and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)}
            for f in scope:
                # 取数函数 return 出去、调用处又接住了的，交给调用处的变量判；调用处把返回值丢掉的
                # （`_project(db, id, user)` 单独一行），仍按取数函数里的变量判——没人再看得到它
                handed = _returned_names(f) if f is not handler and f.name in bound_calls else set()
                parents += [(f, v, m) for v, m in _direct_parents(f, with_status).items() if v not in handed]
            # 取数函数原样 return 的父对象，由调用处接手：`x = _enrollment(db, id)`
            for n in ast.walk(handler):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) \
                        and n.value.func.id in funcs:
                    helper = funcs[n.value.func.id]
                    inner = _direct_parents(helper, with_status)
                    for v in _returned_names(helper) & inner.keys():
                        for t in n.targets:
                            if isinstance(t, ast.Name):
                                parents.append((handler, t.id, inner[v]))
            built = {c.func.id for f in scope for c in ast.walk(f)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in orm}
            for where, var, model in parents:
                children = {c for c in built if c != model and table[model] in fk_to.get(c, set())}
                if children and not _reads_status(where, var, funcs, model):
                    found.add(f"{rel}:{handler.name}:{model}")
    return sorted(found)


def test_父对象已结束_挂子行之前得看它的状态():
    bad = [k for k in unchecked_parent_writes() if k not in BY_DESIGN]
    assert len(bad) <= BASELINE, (
        "以下写接口取了带 status 的父对象、又新建挂在它下面的子行，却不看父对象的状态：\n  " + "\n  ".join(bad)
        + "\n\n离职的医师照样放号、出了报告的申请照样收标本、死亡的档案照样绑包——子行挂上去了，后面的流程却走不通"
        "或把统计带偏。挂之前看一眼 `parent.status`；按设计要挂的（事后补录、结算先于出院……）写进 BY_DESIGN 并写明理由。"
    )


def test_按设计名单只许变少_失效即红():
    stale = sorted(BY_DESIGN.keys() - set(unchecked_parent_writes()))
    assert stale == [], f"这些已经看了父对象状态（或不再挂子行），请从 BY_DESIGN 划掉：{stale}"


SELF_PROOF = '''
@router.post("/slots")
def unchecked(body: SlotCreate, db: Session = Depends(get_db)):
    employee = db.get(Employee, body.employee_id)
    db.add(AppointmentSlot(employee_id=employee.id))

@router.post("/slots2")
async def checked(body: SlotCreate, db: Session = Depends(get_db)):
    employee = db.get(Employee, body.employee_id)
    if employee.status == "left":
        raise HTTPException(409)
    db.add(AppointmentSlot(employee_id=employee.id))

def _ensure_open(emp):
    if emp.status == "left":
        raise HTTPException(409)

@router.post("/slots3")
def checked_via_helper(body: SlotCreate, db: Session = Depends(get_db)):
    employee = db.get(Employee, body.employee_id)
    _ensure_open(employee)
    db.add(AppointmentSlot(employee_id=employee.id))

def _enrollment(db, enrollment_id):
    row = db.get(SpdEnrollment, enrollment_id)
    return row

@router.post("/enrollments/{enrollment_id}/packages")
def returned_unchecked(enrollment_id: int, db: Session = Depends(get_db)):
    enrollment = _enrollment(db, enrollment_id)
    db.add(SpdPackageBinding(enrollment_id=enrollment.id))

@router.post("/enrollments/{enrollment_id}/packages2")
def returned_checked(enrollment_id: int, db: Session = Depends(get_db)):
    enrollment = _enrollment(db, enrollment_id)
    if enrollment.status != "active":
        raise HTTPException(409)
    db.add(SpdPackageBinding(enrollment_id=enrollment.id))

@router.post("/admissions")
def conditional_update(body: AdmissionCreate, db: Session = Depends(get_db)):
    bed = db.get(Bed, body.bed_id)
    db.query(Bed).filter(Bed.id == bed.id, Bed.status == "free").update({Bed.status: "occupied"})
    db.add(Admission(bed_id=bed.id))

@router.post("/enrollments/{enrollment_id}/packages3")
def returned_discarded(enrollment_id: int, db: Session = Depends(get_db)):
    _enrollment(db, enrollment_id)
    db.add(SpdPackageBinding(enrollment_id=enrollment_id))

@router.post("/employees/{employee_id}/rename")
def no_child(employee_id: int, db: Session = Depends(get_db)):
    employee = db.get(Employee, employee_id)
    employee.name = "x"
'''


def test_判据自证_不看状态的三种形状当场点名_看过的与不挂子行的不报():
    got = [k for k in unchecked_parent_writes({"自证.py": SELF_PROOF}) if k.startswith("自证.py")]
    assert got == ["自证.py:returned_discarded:SpdEnrollment", "自证.py:returned_unchecked:SpdEnrollment",
                   "自证.py:unchecked:Employee"], got
