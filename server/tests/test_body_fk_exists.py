"""请求体里引用别的表的 id，写库之前先查它存在（P1-90）。

一族写接口把请求体里的外键字段原样写库（构造时 `**body.model_dump()`，或改档时 `setattr`），
函数里别处再不看它一眼。于是填错一个编号：

- **开发库（SQLite，不开外键约束）**：照写，存成一个悬空 id——列表里那一栏从此是空的或对不上人；
- **生产库（PostgreSQL，外键约束生效）**：撞外键抛 `IntegrityError`。没接住的直接 **500**；
  接住了的更糟——这些接口的 `except IntegrityError` 本是为唯一约束写的，于是被翻成
  「该患者已纳管此病种」「该中心编码已存在」这类 **409 误报**：用户照着提示去查重，查不出任何重复。

表单里这些字段多是手填的数字（「主管医生ID」「个案管理师ID」「负责人用户ID」），填错一位就走到这里。

修法：写库之前逐个 `db.get` 查存在，不存在 404、文案点名是哪一项——与这些文件里早就这么查的
兄弟字段同一个写法（建档的服务包、急救的目标医院）。回归按端点逐条：不存在的 id 404 且一行不写。

2026-09-24 实测量出 18 个端点（14 个创建 + 4 个 PATCH，spd 16 个、平台 2 个），全部修完。
文件末尾的闸门把「请求体外键原样写库、函数里别处不看它」钉成零基线，判据见 `unchecked_body_fks`。

**生产库那一半要在真 PG 上跑才算数**：默认跟 test-unit 跑 SQLite（验的是「不存在 404、一行不写」）；
`tests/test_postgres_real.py` 末尾有一条 integration 用例，用 `MEDPLAT_BODYFK_PG_URL` 把本文件换到
PG 上再跑一遍——外键约束生效时同样拿到 404，而不是 500 或「编码已存在」。接法照抄
`test_date_filter_pg_dialect.py`。
"""
import ast
import os
import pathlib

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前。
# 只有 test_postgres_real.py 起的那个子进程会带上这个变量。
_PG_URL = os.environ.get("MEDPLAT_BODYFK_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402

from app.database import SessionLocal, engine  # noqa: E402
# 先 `app.models` 再 `app.spd.models`：`app/models/__init__.py` 末尾星号导入 spd 模型，而 `app/spd/models.py`
# 又从 `app.models` 取列类型——谁先被导入决定结果。本文件若是进程里第一个碰 spd 模型的，循环里那句星号导入
# 只拿到半个 `app.spd.models`，`app.models` 从此缺全部 `Spd*` 名字（实测：本文件与
# `test_refactor_drift_guards.py` 一起跑即红，P2-51）。
import app.models  # noqa: E402,F401
from app.spd.models import SpdEnrollment  # noqa: E402

if _PG_URL:
    assert engine.dialect.name == "postgresql", (
        "MEDPLAT_BODYFK_PG_URL 已给出，引擎却不是 PostgreSQL——"
        f"实际 {engine.dialect.name}，多半是 app.database 在本模块之前就被导入了"
    )

B = "/api/spd"
MISSING = 987654321


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "外键校验卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    r = client.post(f"{B}/programs", json={"code": "p190_prog", "name": "外键校验病种", "category": "chronic"},
                    headers=admin)
    assert r.status_code == 201, r.text
    counter = iter(range(1, 1000))

    def patient():
        n = next(counter)
        r = client.post("/api/patients", json={"name": f"外键校验患者{n}", "id_card": f"33019019800101{n:04d}",
                                               "gender": "女", "birth_date": "1980-01-01"}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    return {"org": org, "patient": patient}


ENROLL_REFS = [
    ("team_id", "服务团队"),
    ("doctor_user_id", "主管医生"),
    ("manager_user_id", "个案管理师"),
    ("village_doctor_id", "村医"),
]


def _enrollments_of(patient_id):
    with SessionLocal() as db:
        return db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient_id).count()


@pytest.mark.parametrize("field,label", ENROLL_REFS)
def test_建档引用不存在的团队或人员_404且不建档(client, admin, world, field, label):
    pid = world["patient"]()
    r = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p190_prog",
                                              "org_id": world["org"], field: MISSING}, headers=admin)
    assert r.status_code == 404, (field, r.status_code, r.text[:200])
    assert label in r.json()["detail"]
    assert _enrollments_of(pid) == 0


@pytest.fixture(scope="module")
def enrollment(client, admin, world):
    pid = world["patient"]()
    r = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p190_prog",
                                              "org_id": world["org"]}, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.parametrize("field,label", ENROLL_REFS)
def test_改档引用不存在的团队或人员_404且不改(client, admin, enrollment, field, label):
    r = client.patch(f"{B}/enrollments/{enrollment}", json={field: MISSING}, headers=admin)
    assert r.status_code == 404, (field, r.status_code, r.text[:200])
    assert label in r.json()["detail"]
    with SessionLocal() as db:
        assert getattr(db.get(SpdEnrollment, enrollment), field) is None


def test_特征化_引用存在的人员照常建档与改档_清空照常(client, admin, world, enrollment):
    with SessionLocal() as db:
        from app.models import User
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"doctor_user_id": admin_id}, headers=admin)
    assert r.status_code == 200 and r.json()["doctor_user_id"] == admin_id, r.text
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"doctor_user_id": None}, headers=admin)
    assert r.status_code == 200, r.text
    pid = world["patient"]()
    r = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p190_prog",
                                              "org_id": world["org"], "manager_user_id": admin_id}, headers=admin)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- spd care：复诊、个案上报任务、个案上报
def test_复诊引用不存在的医生_404(client, admin, world):
    pid = world["patient"]()
    r = client.post(f"{B}/revisits", json={"patient_id": pid, "plan_date": "2031-06-01",
                                           "doctor_user_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "复诊医生" in r.json()["detail"], (r.status_code, r.text[:200])


def test_上报任务引用不存在的负责人_404而不是编码已存在(client, admin):
    r = client.post(f"{B}/case-report-tasks", json={"code": "p190_crt", "name": "外键校验上报任务",
                                                    "manager_user_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "负责人" in r.json()["detail"], (r.status_code, r.text[:200])
    # 编码并没有被占：换一个真实的负责人照常建
    r = client.post(f"{B}/case-report-tasks", json={"code": "p190_crt", "name": "外键校验上报任务"}, headers=admin)
    assert r.status_code == 201, r.text


def test_个案上报引用不存在的上报任务_404(client, admin, world):
    pid = world["patient"]()
    r = client.post(f"{B}/case-reports", json={"patient_id": pid, "task_id": MISSING, "content": "外键校验"},
                    headers=admin)
    assert r.status_code == 404 and "上报任务" in r.json()["detail"], (r.status_code, r.text[:200])


# ---------------------------------------------------------------- spd config：专病中心、数据源、团队、病种
@pytest.mark.parametrize("field,label", [("lead_org_id", "牵头机构"), ("leader_user_id", "负责人")])
def test_专病中心引用不存在的机构或负责人_404而不是编码已存在(client, admin, field, label):
    code = f"p190_ctr_{field}"
    r = client.post(f"{B}/centers", json={"code": code, "name": "外键校验中心", "program_code": "p190_prog",
                                          field: MISSING}, headers=admin)
    assert r.status_code == 404 and label in r.json()["detail"], (r.status_code, r.text[:200])
    r = client.post(f"{B}/centers", json={"code": code, "name": "外键校验中心", "program_code": "p190_prog"},
                    headers=admin)
    assert r.status_code == 201, r.text  # 编码并没有被占


def test_数据源引用不存在的机构_404而不是编码已存在(client, admin):
    r = client.post(f"{B}/data-sources", json={"code": "p190_ds", "name": "外键校验数据源", "source_type": "HIS",
                                               "org_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "所属机构" in r.json()["detail"], (r.status_code, r.text[:200])


def test_团队引用不存在的负责人_404(client, admin, world):
    r = client.post(f"{B}/teams", json={"name": "外键校验团队", "org_id": world["org"], "leader_user_id": MISSING},
                    headers=admin)
    assert r.status_code == 404 and "团队负责人" in r.json()["detail"], (r.status_code, r.text[:200])


def test_改病种的牵头机构为不存在的机构_404_与建病种同一句(client, admin):
    program = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "p190_prog")
    r = client.patch(f"{B}/programs/{program['id']}", json={"lead_org_id": MISSING}, headers=admin)
    assert r.status_code == 404 and r.json()["detail"] == "机构不存在", (r.status_code, r.text[:200])


# ---------------------------------------------------------------- spd 转诊规则、随访、路径
def test_转诊规则引用不存在的目标机构_404而不是编码已存在(client, admin):
    body = {"code": "p190_rr", "name": "外键校验转诊规则",
            "conditions": [{"field": "age", "op": ">=", "value": 60}]}
    r = client.post(f"{B}/referral-rules", json={**body, "target_org_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "目标机构" in r.json()["detail"], (r.status_code, r.text[:200])
    assert client.post(f"{B}/referral-rules", json=body, headers=admin).status_code == 201


@pytest.fixture(scope="module")
def followup_rule(client, admin):
    rules = client.get(f"{B}/followup-rules", headers=admin).json()
    return next(r for r in rules if r["code"] == "fr_discharge")["id"]


def test_生成随访计划引用不存在的执行人_404且一条不生成(client, admin, world, followup_rule):
    pid = world["patient"]()
    r = client.post(f"{B}/followup-plans", json={"patient_id": pid, "rule_id": followup_rule,
                                                 "org_id": world["org"], "executor_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "随访执行人" in r.json()["detail"], (r.status_code, r.text[:200])
    from app.spd.models import SpdFollowupRecord
    with SessionLocal() as db:
        assert db.query(SpdFollowupRecord).filter(SpdFollowupRecord.patient_id == pid).count() == 0


def test_改随访执行人为不存在的人_404(client, admin, world, followup_rule):
    pid = world["patient"]()
    r = client.post(f"{B}/followup-plans", json={"patient_id": pid, "rule_id": followup_rule,
                                                 "org_id": world["org"]}, headers=admin)
    assert r.status_code == 201 and r.json()["items"], r.text
    record_id = r.json()["items"][0]["id"]
    r = client.patch(f"{B}/followup-records/{record_id}", json={"executor_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "随访执行人" in r.json()["detail"], (r.status_code, r.text[:200])


def test_改路径负责人为不存在的人_404(client, admin, world):
    prog = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "p190_prog")
    tpl = client.post(f"{B}/path-templates", json={"program_id": prog["id"], "code": "P190", "name": "外键校验路径"},
                      headers=admin)
    assert tpl.status_code == 201, tpl.text
    client.post(f"{B}/path-templates/{tpl.json()['id']}/nodes", json={"key": "n1", "name": "首节点", "seq": 1},
                headers=admin)
    client.post(f"{B}/path-templates/{tpl.json()['id']}/status", json={"status": "published"}, headers=admin)
    pid = world["patient"]()
    enr = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p190_prog",
                                                "org_id": world["org"]}, headers=admin)
    assert enr.status_code == 201, enr.text
    inst = client.post(f"{B}/path-instances", json={"enrollment_id": enr.json()["id"],
                                                    "template_id": tpl.json()["id"]}, headers=admin)
    assert inst.status_code == 201, inst.text
    r = client.patch(f"{B}/path-instances/{inst.json()['id']}", json={"owner_user_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "路径负责人" in r.json()["detail"], (r.status_code, r.text[:200])


# ---------------------------------------------------------------- 平台：急救调度、儿童建档
def test_急救调度引用不存在的患者_404(client, admin):
    r = client.post("/api/emergency/cases", json={"location": "外键校验路口", "patient_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "患者档案" in r.json()["detail"], (r.status_code, r.text[:200])
    assert client.post("/api/emergency/cases", json={"location": "外键校验路口"}, headers=admin).status_code == 201


def test_儿童建档引用不存在的监护人_404(client, admin):
    r = client.post("/api/maternal/children", json={"name": "外键校验儿童", "birth_date": "2031-01-01",
                                                    "guardian_patient_id": MISSING}, headers=admin)
    assert r.status_code == 404 and "监护人" in r.json()["detail"], (r.status_code, r.text[:200])



# ================================================================ 闸门：零基线
APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）
BASELINE = 0


def _fk_columns() -> dict[str, set[str]]:
    import app.models  # noqa: F401
    import app.spd.models  # noqa: F401
    from app.database import Base
    return {m.class_.__name__: {c.name for c in m.columns if c.foreign_keys} for m in Base.registry.mappers}


def unchecked_body_fks(sources: dict[str, str] | None = None) -> list[str]:
    """写接口里「请求体的外键字段原样写库、却从不单独看它一眼」的位置。

    两种写库形状：构造 `Model(**body.model_dump(...))`（按模型的外键列认），以及改档的
    `setattr` 循环（按请求模型里名字是某张表外键列的字段认）；构造里显式写 `列=body.字段` 的同样算。
    **算看过**：函数里别处出现 `body.字段`，或字段被「按名字取过」（见 `checked_names`）——
    `changes.get("executor_id")`、`_check_enroll_refs` 查 `_ENROLL_REFS` 这两种写法由此认得出来。
    ⚠️ 判据是宽的：「别处用过」不等于「查过存在」（修之前建档的 `village_doctor_id` 只因拿去记了积分
    就被当成看过），所以它只防「一眼都没看」这一种形状，漏报可能，误报少。
    """
    fk_by_model = _fk_columns()
    fk_any = set().union(*fk_by_model.values())
    files = {str(p.relative_to(APP_DIR)): p.read_text(encoding="utf-8")
             for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers")
             for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    found = []
    for name, text in files.items():
        tree = ast.parse(text)
        body_models = {n.name: {s.target.id for s in n.body if isinstance(s, ast.AnnAssign)
                                and isinstance(s.target, ast.Name)}
                       for n in tree.body if isinstance(n, ast.ClassDef)}
        funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        consts = {t.id: node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                  for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
                  if isinstance(t, ast.Name)}

        def checked_names(fn, depth=2, seen=None) -> set[str]:
            """函数（连同它调用的同模块函数，两层）里「按名字取过」的字段：`x.get("f")` / `x["f"]`，
            以及它引用的模块级常量字典的键。**出参字典的键不算**（序列化里写着 `"team_id": e.team_id`
            不等于查过它存在——判据的第一版就是这样把 14 处漏看成查过的）。"""
            seen = set() if seen is None else seen
            names: set[str] = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                        and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                        and isinstance(node.slice.value, str):
                    names.add(node.slice.value)
                if isinstance(node, ast.Name) and node.id in consts and node.id not in seen:
                    seen.add(node.id)
                    value = consts[node.id].value
                    if isinstance(value, ast.Dict):
                        names |= {k.value for k in value.keys if isinstance(k, ast.Constant)}
                if depth and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id in funcs and node.func.id not in seen:
                    seen.add(node.func.id)
                    names |= checked_names(funcs[node.func.id], depth - 1, seen)
            return names

        for fn in funcs.values():
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("post", "put", "patch") for d in fn.decorator_list):
                continue
            params = {a.arg: ast.unparse(a.annotation) for a in fn.args.args
                      if a.annotation is not None and ast.unparse(a.annotation) in body_models}
            if not params:
                continue
            src, checked = ast.unparse(fn), checked_names(fn)
            fed: set[tuple[str, str, str]] = set()   # (请求体参数, 字段, 写库处)
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in fk_by_model:
                    explicit = {k.arg for k in node.keywords if k.arg}
                    for kw in node.keywords:
                        v = kw.value
                        # 显式 `executor_id=body.executor_id`
                        if kw.arg in fk_by_model[node.func.id] and isinstance(v, ast.Attribute) \
                                and isinstance(v.value, ast.Name) and v.value.id in params:
                            fed.add((v.value.id, v.attr, ast.unparse(node)))
                        if kw.arg is None and isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) \
                                and v.func.attr == "model_dump" and isinstance(v.func.value, ast.Name) \
                                and v.func.value.id in params:
                            excluded = {c.value for k2 in v.keywords if k2.arg == "exclude"
                                        for c in ast.walk(k2.value) if isinstance(c, ast.Constant)}
                            for f in body_models[params[v.func.value.id]] - excluded - explicit:
                                if f in fk_by_model[node.func.id]:
                                    fed.add((v.func.value.id, f, ast.unparse(node)))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr":
                    for bp, cls in params.items():
                        if f"{bp}.model_dump(exclude_unset=True" in src:
                            for f in body_models[cls] & fk_any:
                                fed.add((bp, f, ""))
            for bp, f, ctor in sorted(fed):
                ref = f"{bp}.{f}"
                if src.count(ref) - ctor.count(ref) > 0 or f in checked:
                    continue
                found.append(f"{name}:{fn.name}:{f}")
    return sorted(set(found))


def test_请求体外键原样写库之前得先查存在():
    bad = unchecked_body_fks()
    assert len(bad) <= BASELINE, (
        "以下写接口把请求体里的外键原样写库，函数里别处从不看它：\n  " + "\n  ".join(bad)
        + "\n\n开发库存成悬空 id，生产库撞外键——没接住 500，接住了多半被翻成「编码已存在」这类 409 误报。"
        "写库之前 `if body.x is not None and db.get(M, body.x) is None: raise HTTPException(404, ...)`。"
    )


def test_判据自证_修复前的两种形状当场点名_查过的不报():
    snippet = (
        "class EnrollIn(BaseModel):\n    patient_id: int\n    team_id: int | None = None\n"
        "class EnrollUpdate(BaseModel):\n    team_id: int | None = None\n"
        "_REFS = {'team_id': 1}\n"
        "def _check(db, values):\n    return _REFS\n"
        "@router.post('/a')\ndef create_a(body: EnrollIn, db=None):\n"
        "    db.add(SpdEnrollment(**body.model_dump()))\n"
        "@router.patch('/a/{i}')\ndef patch_a(i: int, body: EnrollUpdate, db=None):\n"
        "    e = db.get(SpdEnrollment, i)\n"
        "    for k, v in body.model_dump(exclude_unset=True).items():\n        setattr(e, k, v)\n"
        "@router.post('/b')\ndef create_b(body: EnrollIn, db=None):\n"
        "    assert_patient_visible(db, None, body.patient_id)\n"
        "    _check(db, body.model_dump())\n    db.add(SpdEnrollment(**body.model_dump()))\n"
    )
    got = [v for v in unchecked_body_fks({"自证.py": snippet}) if v.startswith("自证.py")]
    assert got == ["自证.py:create_a:patient_id", "自证.py:create_a:team_id", "自证.py:patch_a:team_id"]
