"""写接口把新业务挂给已停用的账号（P1-106）：转派任务、随访执行人、主管医生照样挂得上，之后没人办。

账号停用（`users.status = disabled`）即时生效：`deps.get_current_user` 每请求校验，停用的人登录不了、
旧令牌下一次请求就被拒。可慢专病一族写接口按请求里的账号编号挂人，只查了存在（P1-90）——
2026-09-24 实测（修前代码）：

- **转派任务 / 批量分配**给停用的账号 200，任务进了一个没人登得上的待办箱，到期只会被扫成超期；
- **纳管建档**的主管医生、个案管理师挂停用账号 201，**随访方案**的执行人 201，一整条随访计划派给没人办的账号；
- **团队**负责人、成员，**上报任务**负责人，**复诊**医生同样照收——成员名单里多一个永远不接活的人；
- 另有三处**连存在都没查**：新建任务的责任人与服务团队、批量分配的责任人、分发目标患者的指派人，
  填错编号时开发库存成悬空 id，生产库撞外键 500（请求体外键闸门 P1-90 看不见：它只认构造函数与 setattr 循环
  写库、「别处提到过字段」就算查过，这三处是属性赋值 / 交给 `spawn_task` 写库）。

修法：统一经 `platform.unusable_user` 查——不存在的报「X不存在」（原文案一字不改），停用的报「X已停用」，
都是 404，与停用目录对象（P1-103）同一口径；改档与现值相同的不再查（团队后来停用了负责人，只改团队名的
调用不该被它挡住）。新建任务的服务团队一并查存在与启用。

**闸门**（派生、零基线）：写接口请求模型里「指向账号」的字段（名字与某张表外键到 `users.id` 的列相同）
都得经 `unusable_user` 解析；按设计不查的逐条写进 `BY_DESIGN`（只减不增，失效即红）。
"""
from __future__ import annotations

import ast
import re

import pytest
import test_datestr_single_source as ds
from conftest import login

from app.database import SessionLocal

B = "/api/spd"
MISSING = 987654321


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "停用账号卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    r = client.post(f"{B}/programs", json={"code": "p1106_prog", "name": "停用账号病种", "category": "chronic"},
                    headers=admin)
    assert r.status_code == 201, r.text
    users = {}
    for name in ("on", "off", "later"):
        r = client.post("/api/users", headers=admin, json={
            "username": f"p1106_{name}", "password": "pass123456", "role": "doctor", "org_id": org})
        assert r.status_code == 201, r.text
        users[name] = r.json()["id"]
    login(client, "p1106_on", "pass123456")  # 在用的账号能登录：对照组
    _disable(client, admin, users["off"])
    counter = iter(range(1, 1000))

    def patient():
        n = next(counter)
        r = client.post("/api/patients", json={"name": f"停用账号患者{n}", "id_card": f"33019219820101{n:04d}",
                                               "gender": "男", "birth_date": "1982-01-01"}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def task():
        r = client.post(f"{B}/tasks", json={"patient_id": patient(), "title": "停用账号待办", "org_id": org},
                        headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    return {"org": org, "users": users, "patient": patient, "task": task}


def _disable(client, admin, user_id):
    r = client.patch(f"/api/users/{user_id}/status", json={"status": "disabled"}, headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "disabled", r.text


def test_转派任务不给停用账号_在用的照常(client, admin, world):
    task = world["task"]()
    r = client.post(f"{B}/tasks/{task}/assign", json={"assignee_id": world["users"]["off"]}, headers=admin)
    assert r.status_code == 404 and r.json()["detail"] == "责任人已停用", r.text  # 修前 200
    r = client.post(f"{B}/tasks/{task}/assign", json={"assignee_id": MISSING}, headers=admin)
    assert r.status_code == 404 and r.json()["detail"] == "责任人不存在", r.text  # 原文案不变
    r = client.post(f"{B}/tasks/{task}/assign", json={"assignee_id": world["users"]["on"]}, headers=admin)
    assert r.status_code == 200 and r.json()["assignee_id"] == world["users"]["on"], r.text


def test_批量分配不给停用或不存在的账号(client, admin, world):
    task = world["task"]()

    def batch(assignee):
        return client.post(f"{B}/tasks/batch", json={"task_ids": [task], "action": "assign",
                                                     "assignee_id": assignee}, headers=admin)

    r = batch(world["users"]["off"])
    assert r.status_code == 404 and r.json()["detail"] == "责任人已停用", r.text  # 修前 200、processed 1
    r = batch(MISSING)
    assert r.status_code == 404 and r.json()["detail"] == "责任人不存在", r.text  # 修前 200：存成悬空 id
    r = batch(world["users"]["on"])
    assert r.status_code == 200 and r.json()["processed"] == 1, r.text


def test_新建任务的责任人与服务团队先查(client, admin, world):
    def create(**extra):
        return client.post(f"{B}/tasks", json={"patient_id": world["patient"](), "title": "停用账号新任务",
                                               "org_id": world["org"], **extra}, headers=admin)

    r = create(assignee_id=world["users"]["off"])
    assert r.status_code == 404 and r.json()["detail"] == "责任人已停用", r.text  # 修前 201
    r = create(assignee_id=MISSING)
    assert r.status_code == 404 and r.json()["detail"] == "责任人不存在", r.text  # 修前 201：悬空 id
    r = create(team_id=MISSING)
    assert r.status_code == 404 and r.json()["detail"] == "服务团队不存在或已停用", r.text  # 修前 201
    team = client.post(f"{B}/teams", json={"name": "停用账号·停用团队", "org_id": world["org"]}, headers=admin)
    assert team.status_code == 201, team.text
    assert client.patch(f"{B}/teams/{team.json()['id']}", json={"active": False}, headers=admin).status_code == 200
    r = create(team_id=team.json()["id"])
    assert r.status_code == 404 and r.json()["detail"] == "服务团队不存在或已停用", r.text  # 修前 201
    r = create(assignee_id=world["users"]["on"])
    assert r.status_code == 201 and r.json()["assignee_id"] == world["users"]["on"], r.text


def test_分发目标患者不指派给停用或不存在的账号(client, admin, world):
    def distribute(assignee):
        return client.post(f"{B}/candidates/distribute", json={"candidate_ids": [MISSING],
                                                               "assigned_user_id": assignee}, headers=admin)

    r = distribute(world["users"]["off"])
    assert r.status_code == 404 and r.json()["detail"] == "指派人已停用", r.text  # 修前 200
    r = distribute(MISSING)
    assert r.status_code == 404 and r.json()["detail"] == "指派人不存在", r.text  # 修前 200
    r = distribute(world["users"]["on"])
    assert r.status_code == 200 and r.json() == {"distributed": 0, "not_found": 1}, r.text


def test_纳管档案不挂停用账号_改档原样带回的照常(client, admin, world):
    users = world["users"]
    r = client.post(f"{B}/enrollments", json={"patient_id": world["patient"](), "program_code": "p1106_prog",
                                              "org_id": world["org"], "doctor_user_id": users["off"]},
                    headers=admin)
    assert r.status_code == 404 and r.json()["detail"] == f"主管医生已停用（doctor_user_id={users['off']}）", \
        r.text  # 修前 201
    r = client.post(f"{B}/enrollments", json={"patient_id": world["patient"](), "program_code": "p1106_prog",
                                              "org_id": world["org"], "doctor_user_id": users["later"]},
                    headers=admin)
    assert r.status_code == 201, r.text
    enrollment = r.json()["id"]
    _disable(client, admin, users["later"])  # 建档之后才停用
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"manager_user_id": users["off"]}, headers=admin)
    assert r.status_code == 404 and "个案管理师已停用" in r.json()["detail"], r.text  # 修前 200
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"doctor_user_id": users["later"], "risk_level": "high"},
                     headers=admin)
    assert r.status_code == 200 and r.json()["risk_level"] == "high", r.text  # 原样带回现值，不挡


def test_随访方案执行人不派给停用账号(client, admin, world):
    rule = client.post(f"{B}/followup-rules", json={"code": "p1106_rule", "name": "停用账号随访方案",
                                                    "points": [7, 30]}, headers=admin)
    assert rule.status_code == 201, rule.text
    body = {"patient_id": world["patient"](), "rule_id": rule.json()["id"], "org_id": world["org"]}
    r = client.post(f"{B}/followup-plans", json={**body, "executor_id": world["users"]["off"]}, headers=admin)
    assert r.status_code == 404 and "随访执行人已停用" in r.json()["detail"], r.text  # 修前 201，两条随访都挂给他
    r = client.post(f"{B}/followup-plans", json={**body, "executor_id": world["users"]["on"]}, headers=admin)
    assert r.status_code == 201, r.text


def test_团队负责人与成员不收停用账号(client, admin, world):
    off = world["users"]["off"]
    r = client.post(f"{B}/teams", json={"name": "停用账号·负责人", "org_id": world["org"], "leader_user_id": off},
                    headers=admin)
    assert r.status_code == 404 and "团队负责人已停用" in r.json()["detail"], r.text  # 修前 201
    team = client.post(f"{B}/teams", json={"name": "停用账号·成员", "org_id": world["org"]}, headers=admin)
    assert team.status_code == 201, team.text
    tid = team.json()["id"]
    r = client.post(f"{B}/teams/{tid}/members", json={"user_id": off}, headers=admin)
    assert r.status_code == 404 and r.json()["detail"] == "用户已停用", r.text  # 修前 201
    r = client.patch(f"{B}/teams/{tid}", json={"leader_user_id": off}, headers=admin)
    assert r.status_code == 404 and "团队负责人已停用" in r.json()["detail"], r.text  # 修前 200
    r = client.post(f"{B}/teams/{tid}/members", json={"user_id": world["users"]["on"]}, headers=admin)
    assert r.status_code == 201, r.text


def test_批量开通村医跳过停用账号(client, admin, world):
    r = client.post(f"{B}/village-doctors/batch", json={"items": [
        {"user_id": world["users"]["off"], "org_id": world["org"]}, {"user_id": MISSING, "org_id": world["org"]}]},
        headers=admin)
    assert r.status_code == 200, r.text
    reasons = {s["user_id"]: s["reason"] for s in r.json()["skipped"]}
    assert reasons == {world["users"]["off"]: "用户已停用", MISSING: "用户不存在"}, reasons  # 修前停用的照建


def test_上报任务负责人与复诊医生不挂停用账号(client, admin, world):
    off = world["users"]["off"]
    r = client.post(f"{B}/case-report-tasks", json={"code": "p1106_crt", "name": "停用账号上报",
                                                    "manager_user_id": off}, headers=admin)
    assert r.status_code == 404 and "负责人已停用" in r.json()["detail"], r.text  # 修前 201
    r = client.post(f"{B}/revisits", json={"patient_id": world["patient"](), "plan_date": "2026-10-01",
                                           "doctor_user_id": off}, headers=admin)
    assert r.status_code == 404 and "复诊医生已停用" in r.json()["detail"], r.text  # 修前 201


def test_停用账号确实登录不了(client, world):
    r = client.post("/api/auth/login", json={"username": "p1106_off", "password": "pass123456"})
    assert r.status_code == 403, r.text
    with SessionLocal() as db:
        from app.models import User

        assert db.get(User, world["users"]["off"]).status == "disabled"


# ---------------------------------------------------------------- 闸门
#
# 判据：写接口（POST / PUT / PATCH）的请求模型（本模块定义的，或 `app/schemas.py` 的，含一层 `list[子模型]`）
# 里，名字与某张表外键到 `users.id` 的列相同、注解为整数的字段，得在处理函数（连同同模块被调函数两层）里
# 交给 `unusable_user(...)` 解析：调用参数里点到这个字段（`body.x` / `changes["x"]` / `item.x`），或调用位于
# 遍历「以这个字段为键的模块级常量字典」的循环里。只查存在（`db.get(User, …)`）不算——停用的照样挂得上。

BASELINE = 0

#: 按设计不经 unusable_user 的「文件:处理函数:字段」→ 理由（只减不增，失效即红）
BY_DESIGN: dict[str, str] = {
    "routers/education.py:create_assessment:user_id":
        "给已报名的学员录考核成绩：学员须在本次实训的报名表里（按 user_id 查过），录的是已经结束的培训的"
        "结果——学员后来停用了，补录成绩是留痕，不是派新活。",
}

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _user_fk_columns() -> set[str]:
    import app.models  # noqa: F401  先 app.models 再 app.spd.models（P2-51）
    import app.spd.models  # noqa: F401
    from app.database import Base

    return {c.name for m in Base.registry.mappers for c in m.columns
            if any(fk.column.table.name == "users" for fk in c.foreign_keys)}


def _model_fields(tree: ast.Module) -> dict[str, dict[str, str]]:
    """请求模型 → {字段: 注解}；`list[子模型]` 的字段展开一层（批量接口的 items）。"""
    raw = {n.name: {s.target.id: ast.unparse(s.annotation) for s in n.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
           for n in tree.body if isinstance(n, ast.ClassDef)}
    out = {}
    for name, fields in raw.items():
        merged = dict(fields)
        for ann in fields.values():
            inner = re.fullmatch(r"list\[(\w+)\]", ann)
            if inner and inner.group(1) in raw:
                merged.update(raw[inner.group(1)])
        out[name] = merged
    return out


def _resolved_fields(fn: ast.AST, funcs: dict[str, ast.AST], consts: dict[str, ast.AST],
                     depth: int = 2, seen: set[str] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    names: set[str] = set()
    parents = {child: node for node in ast.walk(fn) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "unusable_user":
            for arg in node.args[1:]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Attribute):
                        names.add(sub.attr)
                    elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        names.add(sub.value)
            loop = parents.get(node)
            while loop is not None and not isinstance(loop, ast.For):
                loop = parents.get(loop)
            if loop is not None:
                for sub in ast.walk(loop.iter):
                    if isinstance(sub, ast.Name) and isinstance(consts.get(sub.id), ast.Dict):
                        names |= {k.value for k in consts[sub.id].keys if isinstance(k, ast.Constant)}
        if depth and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in funcs and node.func.id not in seen:
            seen.add(node.func.id)
            names |= _resolved_fields(funcs[node.func.id], funcs, consts, depth - 1, seen)
    return names


def unresolved_user_fields(sources: dict[str, str] | None = None) -> list[str]:
    """写接口请求体里指向账号、却没经 unusable_user 解析的「文件:处理函数:字段」。"""
    user_cols = _user_fk_columns()
    shared = _model_fields(ast.parse((ds.APP_DIR / "schemas.py").read_text(encoding="utf-8")))
    files = {p.relative_to(ds.APP_DIR).as_posix(): p.read_text(encoding="utf-8")
             for base in ds._ROUTE_DIRS for p in sorted(base.rglob("*.py"))}
    files.update(sources or {})
    found: set[str] = set()
    for rel, text in files.items():
        tree = ast.parse(text)
        models = {**shared, **_model_fields(tree)}
        funcs = {n.name: n for n in tree.body if isinstance(n, _FUNCS)}
        consts = {t.id: node.value for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                  and node.value is not None
                  for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
                  if isinstance(t, ast.Name)}
        for fn in funcs.values():
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("post", "put", "patch") for d in fn.decorator_list):
                continue
            fields = {field for a in fn.args.args if a.annotation is not None
                      for field, ann in models.get(ast.unparse(a.annotation), {}).items()
                      if field in user_cols and re.search(r"\bint\b", ann)}
            if not fields:
                continue
            resolved = _resolved_fields(fn, funcs, consts)
            found |= {f"{rel}:{fn.name}:{field}" for field in fields - resolved}
    return sorted(found)


def test_写接口里指向账号的字段都经_unusable_user():
    bad = [k for k in unresolved_user_fields() if k not in BY_DESIGN]
    assert len(bad) <= BASELINE, (
        "以下写接口的请求体指向账号，却没经 `unusable_user` 查——不存在的会存成悬空 id / 生产库撞外键 500，"
        "停用的照样挂得上、之后没人办：\n  " + "\n  ".join(bad)
        + "\n\n写法：`state = unusable_user(db, body.x)`，非空就 404 `f\"<名称>{state}\"`；改档与现值相同的可以不查。"
        "按设计不需要在用账号的（给已发生的事补录），写进 BY_DESIGN 并写明理由。"
    )


def test_按设计名单只许变少_失效即红():
    stale = sorted(BY_DESIGN.keys() - set(unresolved_user_fields()))
    assert stale == [], f"这些已经经 unusable_user 查了（或字段已不在），请从 BY_DESIGN 划掉：{stale}"


def test_判据自证_转派任务退回只查存在当场点名():
    rel = "spd/routers/tasks.py"
    text = (ds.APP_DIR / rel).read_text(encoding="utf-8")
    fixed = "    state = unusable_user(db, body.assignee_id)  # 停用的账号登录不了，转过去就没人办（P1-106）\n"
    assert fixed in text, "转派任务里找不到 P1-106 的修法，自证前提变了"
    reverted = text.replace(fixed, "    state = \"\" if db.get(User, body.assignee_id) else \"不存在\"\n", 1)
    assert "spd/routers/tasks.py:assign_task:assignee_id" in unresolved_user_fields({rel: reverted})
    assert "spd/routers/tasks.py:assign_task:assignee_id" not in unresolved_user_fields()


SELF_PROOF = '''
class OneIn(BaseModel):
    assignee_id: int | None = None
    executor_id: int
    reported_by: str = ""

class Row(BaseModel):
    user_id: int

class ManyIn(BaseModel):
    items: list[Row]

_REFS = {"executor_id": "执行人"}

@router.post("/a")
def only_exists(body: OneIn, db: Session = Depends(get_db)):
    if db.get(User, body.assignee_id) is None:
        raise HTTPException(404)
    _check(db, body.model_dump())

def _check(db, values):
    for field, label in _REFS.items():
        if unusable_user(db, values[field]):
            raise HTTPException(404)

@router.patch("/b")
def patched(body: OneIn, db: Session = Depends(get_db)):
    changes = body.model_dump(exclude_unset=True)
    if unusable_user(db, changes["assignee_id"]) or unusable_user(db, body.executor_id):
        raise HTTPException(404)

@router.post("/c")
def batch(body: ManyIn, db: Session = Depends(get_db)):
    for item in body.items:
        db.add(Thing(user_id=item.user_id))
'''


def test_判据自证_只查存在与一眼不看的当场点名_查过的与名字字段不报():
    got = [k for k in unresolved_user_fields({"自证.py": SELF_PROOF}) if k.startswith("自证.py")]
    assert got == ["自证.py:batch:user_id", "自证.py:only_exists:assignee_id"], got
