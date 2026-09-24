"""同一份统计里，一部分数字按机构范围收口、另一部分还是全县口径（P2-61）。

统计 / 工作台端点先算出这次要看的机构范围（`_scope` / `resolve_org_scope`），各个数字再按它收口。
2026-09-24 按「函数里已有范围变量、却对带机构列的表另起一条查询不套范围」扫了一遍（49 处逐条判，
其余是名字查表、按本人过滤、先按机构分组再按范围取行），有三处数字没跟上同一份响应里的其他数字：

1. 全程管理中心端工作台「待确认迁入」数的是**全县**未确认的跨机构迁出。乡镇的个案管理师看到
   「待确认迁入 1」，却没有一条是迁到本院的：确认只能由迁入机构做，`confirm_migration` 判的是
   `assert_org_writable(target_org_id)`。同一个面板里的已排除 / 已迁出 / 已死亡都只数本机构。
2. 应急资源保障 `?group_id=` 选了片区：按机构列出的缺口 / 过期只有片区内的，按类型的计数却是全县的。
3. 卫健管理端工作台 `?org_id=`：纳管、筛查、待办只数这一家，「服务人数 / 服务人次」和「县乡村三级
   服务能力」里的机构数、团队数却还是全县的。

修法：三处都用同一份范围收口。不给范围时（全域角色、不带参数）结果与原先一致，下面每条都有断言。

**闸门**（派生、零基线）：函数里已有范围变量，对带机构列的表另起计数 / 求和却不套范围的，逐条写进
`BY_DESIGN`（先按机构分组再按范围取行 / 平台管理端 / 按本人收口，只减不增，失效即红）。判据见闸门一节。
"""
from __future__ import annotations

import ast
import re

import pytest
import test_datestr_single_source as ds
from conftest import login

from app.database import SessionLocal
from app.models import EmergencyResource, Organization, OrgGroup, OrgGroupMember, Patient
from app.spd.models import SpdEnrollment, SpdLifecycleEvent, SpdTask, SpdTeam


@pytest.fixture(scope="module")
def world(client, admin):
    with SessionLocal() as db:
        src = Organization(name="口径迁出院", org_type="township", level="township")
        dst = Organization(name="口径迁入院", org_type="township", level="township")
        other = Organization(name="口径旁观院", org_type="township", level="township")
        county = Organization(name="口径县医院", org_type="lead_hospital", level="county")
        db.add_all([src, dst, other, county])
        db.flush()
        patients = [Patient(ehc_no=f"EHC-SC-{i}", name=f"口径患者{i}", id_card=f"SC{i:016d}",
                            gender="女", birth_date="1960-01-01") for i in range(4)]
        db.add_all(patients)
        db.flush()
        leaving = SpdEnrollment(patient_id=patients[0].id, program_code="sc_prog", org_id=src.id,
                                status="active")
        db.add(leaving)
        db.flush()
        # 甲 → 乙 的跨机构迁出，等乙确认
        db.add(SpdLifecycleEvent(enrollment_id=leaving.id, event="migrate", target_org_id=dst.id,
                                 confirmed=False))
        # 服务人次：迁入院办结 1 条，县医院办结 2 条（两个人）
        db.add_all([
            SpdTask(patient_id=patients[1].id, title="口径随访", org_id=dst.id, status="done"),
            SpdTask(patient_id=patients[2].id, title="口径随访", org_id=county.id, status="done"),
            SpdTask(patient_id=patients[3].id, title="口径随访", org_id=county.id, status="done"),
        ])
        db.add_all([
            SpdTeam(name="口径乡镇团队", org_id=dst.id, active=True),
            SpdTeam(name="口径县级团队", org_id=county.id, active=True),
        ])
        # 应急资源：片区里只有迁入院；县医院的不在片区里
        group = OrgGroup(name="口径片区")
        db.add(group)
        db.flush()
        db.add(OrgGroupMember(group_id=group.id, org_id=dst.id))
        db.add_all([
            EmergencyResource(org_id=dst.id, name="口径口罩", resource_type="material"),
            EmergencyResource(org_id=county.id, name="口径急救队", resource_type="team"),
            EmergencyResource(org_id=county.id, name="口径呼吸机", resource_type="equipment"),
        ])
        db.commit()
        ids = {"src": src.id, "dst": dst.id, "other": other.id, "county": county.id,
               "group": group.id}
    for key in ("dst", "other"):
        r = client.post("/api/users", headers=admin, json={
            "username": f"sc_{key}", "password": "pass123456", "role": "doctor",
            "org_id": ids[key]})
        assert r.status_code == 201, r.text
    ids["h_dst"] = login(client, "sc_dst", "pass123456")
    ids["h_other"] = login(client, "sc_other", "pass123456")
    return ids


def _center(client, headers):
    r = client.get("/api/spd/workbench/center", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["lifecycle"]["pending_migrations"]


def test_管理中心待确认迁入只数迁到本机构的(client, admin, world):
    assert _center(client, world["h_other"]) == 0  # 修前 1：别人家的迁入也算进本院待办
    assert _center(client, world["h_dst"]) == 1
    assert _center(client, admin) == 1  # 全域角色照旧数全县


def test_应急资源按类型计数跟着片区筛选(client, admin, world):
    def by_type(**params):
        r = client.get("/api/surveillance/resources/readiness", params=params, headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        return {k: v["count"] for k, v in body["by_type"].items()}, {o["org_id"] for o in body["orgs"]}

    counts, orgs = by_type(group_id=world["group"])
    assert orgs == {world["dst"]}
    assert counts == {"material": 1}  # 修前还带着片区外县医院的 team / equipment
    counts, _ = by_type()
    assert counts == {"material": 1, "team": 1, "equipment": 1}  # 不筛片区照旧全县


def _commission(client, admin, **params):
    r = client.get("/api/spd/workbench/health-commission", params=params, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def test_卫健工作台指定机构时服务人次与三级能力跟着收口(client, admin, world):
    body = _commission(client, admin, org_id=world["dst"])
    core = body["core"]
    assert (core["service_persons"], core["service_times"]) == (1, 1)  # 修前 (3, 3)：全县办结
    assert body["by_level"]["乡级"] == {"orgs": 1, "enrolled": 0, "teams": 1}
    assert body["by_level"]["县级"] == {"orgs": 0, "enrolled": 0, "teams": 0}  # 修前县医院与其团队照算

    whole = _commission(client, admin)
    assert (whole["core"]["service_persons"], whole["core"]["service_times"]) == (3, 3)
    assert whole["by_level"]["县级"]["teams"] >= 1 and whole["by_level"]["乡级"]["orgs"] >= 3


# ---------------------------------------------------------------- 闸门
#
# 判据：处理函数（含帮手函数）里已有范围变量——`_scope` / `visible_org_ids` / `stats_org_ids` /
# `scope_stats_orgs` / `resolve_org_scope` 的返回值，或名为 `orgs` / `scope` 的形参——却对**带机构列**的表
# 另起一条 `db.query(...)` 做计数 / 求和（链尾 `.count()`，或查询里有 `func.count / sum / avg`），这条链
# 既没用到范围变量、也没交给收口包装（`_apply_scope` / `scope_org_list` / 本函数里用到范围的局部闭包…），
# 赋给的变量之后也没有和范围变量同句出现；链里按本人（`user.id`）收口的不算。
# 登记粒度是「文件:函数:模型」，同一函数里给别的表新加一个不收口的数照样会红。

BASELINE = 0

_SCOPE_FUNCS = {"_scope", "visible_org_ids", "stats_org_ids", "scope_stats_orgs", "resolve_org_scope"}
_SCOPE_WRAPPERS = {"_apply_scope", "scope_org_list", "scope_patient_list", "scoped", "in_scope",
                   "_scope_instances"}
_AGGREGATE = re.compile(r"\bfunc\.(count|sum|avg)\(")
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)

_PER_ORG = ("先按机构分组聚合成「机构 → 数」，结果行只给范围内的机构出（{how}）——范围外机构的数留在字典里、"
            "不进响应，同一行的各个数口径一致。")
_PLATFORM = ("平台管理端（运行中枢）看的是全平台的配置健康度与运营告警：路径模板、服务团队、村医是全县共用的配置，"
             "待确认迁出是平台级告警。内置角色里只有 director / admin 进得来，二者都是全域，`_scope` 恒为 None。")
_MINE = "按本人名下患者（专家看本人团队、成员看本人签约、个案管理师看本人管理）收口，比机构范围还窄。"

#: 按设计不套机构范围的「文件:函数:模型」→ 理由（只减不增，失效即红）
BY_DESIGN: dict[str, str] = {
    "routers/analytics.py:_efficiency_rows:Admission": _PER_ORG.format(how="`candidates &= set(scope)`"),
    "routers/analytics.py:_efficiency_rows:Employee": _PER_ORG.format(how="`candidates &= set(scope)`"),
    "routers/analytics.py:_efficiency_rows:Encounter": _PER_ORG.format(how="`candidates &= set(scope)`"),
    "routers/analytics.py:_efficiency_rows:Ward": _PER_ORG.format(how="`candidates &= set(scope)`"),
    "routers/analytics.py:drug_use:Admission": _PER_ORG.format(how="`org_ids = sorted(scope)`"),
    "routers/analytics.py:drug_use:Encounter": _PER_ORG.format(how="`org_ids = sorted(scope)`"),
    "routers/analytics.py:drug_use:Prescription": _PER_ORG.format(how="`org_ids = sorted(scope)`"),
    "routers/inpatient.py:inpatient_stats:Admission": _PER_ORG.format(how="行取自已按范围过滤的床位统计"),
    "spd/routers/workbench.py:admin_workbench:SpdLifecycleEvent": _PLATFORM,
    "spd/routers/workbench.py:admin_workbench:SpdPathTemplate": _PLATFORM,
    "spd/routers/workbench.py:admin_workbench:SpdTeam": _PLATFORM,
    "spd/routers/workbench.py:admin_workbench:SpdVillageDoctor": _PLATFORM,
    "spd/routers/workbench.py:center_workbench:SpdTeam":
        "中心端的 `teams` 是可分发的团队数：服务团队按设计跨机构，县级团队下沉服务乡镇的目标患者"
        "（分发时可选别家机构的团队，见 `test_secondary_body_id_guard.BY_DESIGN`）；界面也不显示这个数。",
    "spd/routers/workbench.py:team_workbench:SpdEnrollment": _MINE,
    "spd/routers/workbench.py:team_workbench:SpdFollowupRecord": _MINE,
    "spd/routers/workbench.py:team_workbench:SpdReferralCase": _MINE,
}


def _org_models() -> set[str]:
    import app.models  # noqa: F401  先 app.models 再 app.spd.models（P2-51）
    from app.models import Base

    return {m.class_.__name__ for m in Base.registry.mappers
            if any(c.name.endswith("org_id") for c in m.class_.__table__.columns)}


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _chain_top(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """从 `db.query(...)` 往上走完方法链：`.filter(...)`、`.group_by(...)`、`.count()`……"""
    while True:
        parent = parents.get(node)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            node = parent
        elif isinstance(parent, ast.Call) and parent.func is node:
            node = parent
        else:
            return node


def unscoped_aggregates(sources: dict[str, str] | None = None) -> list[str]:
    """有范围变量的函数里，对带机构列的表计数 / 求和却不套范围的「文件:函数:模型」。"""
    org_models = _org_models()
    files = {p.relative_to(ds.APP_DIR).as_posix(): p.read_text(encoding="utf-8")
             for base in ds._ROUTE_DIRS for p in sorted(base.rglob("*.py"))}
    files.update(sources or {})
    found: set[str] = set()
    for rel, text in files.items():
        tree = ast.parse(text)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for fn in [n for n in ast.walk(tree) if isinstance(n, _FUNCS)]:
            scope_vars = {a.arg for a in fn.args.args if a.arg in ("orgs", "scope")}
            for node in ast.walk(fn):
                if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                        and ast.unparse(node.value.func).split(".")[-1] in _SCOPE_FUNCS):
                    scope_vars |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            if not scope_vars:
                continue
            uses_scope = re.compile(r"\b(" + "|".join(sorted(map(re.escape, scope_vars))) + r")\b")
            closures = {n.name for n in ast.walk(fn) if isinstance(n, _FUNCS) and n is not fn
                        and uses_scope.search(ast.unparse(n))}
            statements = [s for s in ast.walk(fn) if isinstance(s, ast.stmt) and not isinstance(s, _FUNCS)
                          and not isinstance(s, (ast.If, ast.For, ast.While, ast.With, ast.Try))
                          and not _is_docstring(s)]
            for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call) and ast.unparse(c.func) == "db.query"]:
                model = next((n.id for arg in call.args for n in ast.walk(arg)
                              if isinstance(n, ast.Name) and n.id in org_models), None)
                if model is None:
                    continue
                top = _chain_top(call, parents)
                counted = isinstance(top, ast.Call) and isinstance(top.func, ast.Attribute) and top.func.attr == "count"
                if not (counted or _AGGREGATE.search(ast.unparse(call))):
                    continue
                chain = ast.unparse(top)
                if uses_scope.search(chain) or "user.id" in chain:
                    continue
                parent = parents.get(top)
                if isinstance(parent, ast.Call) and (
                        uses_scope.search(ast.unparse(parent))
                        or ast.unparse(parent.func).split(".")[-1] in _SCOPE_WRAPPERS | closures):
                    continue
                holder = top  # 顺着包装调用（row_dict / dict …）找到赋值目标，看它之后有没有按范围收
                while isinstance(parents.get(holder), (ast.Call, ast.keyword)):
                    holder = parents[holder]
                target = parents.get(holder)
                if (isinstance(target, ast.Assign) and len(target.targets) == 1
                        and isinstance(target.targets[0], ast.Name)):
                    var = re.compile(rf"\b{re.escape(target.targets[0].id)}\b")
                    if any(s is not target and var.search(ast.unparse(s)) and uses_scope.search(ast.unparse(s))
                           for s in statements):
                        continue
                found.add(f"{rel}:{fn.name}:{model}")
    return sorted(found)


def test_有范围的统计里不另起不收口的计数():
    bad = [k for k in unscoped_aggregates() if k not in BY_DESIGN]
    assert len(bad) <= BASELINE, (
        "以下函数已经算出了机构范围，却对带机构列的表另起一条计数 / 求和、不套范围：\n  " + "\n  ".join(bad)
        + "\n\n同一份响应里其他数字按范围收口、这个数却是全县的，界面上并排摆着就对不上（P2-61）。"
        "套上同一个范围：`_apply_scope(db.query(X), X.org_id, orgs)`，或在已收口的 query 上 `with_entities(...)`；"
        "按设计就是全县口径的，写进 BY_DESIGN 并写明理由。"
    )


def test_按设计名单只许变少_失效即红():
    stale = sorted(BY_DESIGN.keys() - set(unscoped_aggregates()))
    assert stale == [], f"这些已经套上范围（或不再计数），请从 BY_DESIGN 划掉：{stale}"


def _reverted(rel: str, fixed: str, broken: str) -> dict[str, str]:
    text = (ds.APP_DIR / rel).read_text(encoding="utf-8")
    assert fixed in text, f"{rel} 里找不到 P2-61 的修法，自证前提变了"
    return {rel: text.replace(fixed, broken, 1)}


def test_判据自证_拿掉三处修法当场点名():
    center = _reverted(
        "spd/routers/workbench.py",
        "_apply_scope(\n                db.query(SpdLifecycleEvent), SpdLifecycleEvent.target_org_id, orgs\n            )",
        "db.query(SpdLifecycleEvent)")
    assert "spd/routers/workbench.py:center_workbench:SpdLifecycleEvent" in unscoped_aggregates(center)
    service = _reverted(
        "spd/routers/workbench.py",
        '"service_times": _apply_scope(\n                db.query(SpdTask), SpdTask.org_id, orgs\n            )',
        '"service_times": db.query(SpdTask)')
    assert "spd/routers/workbench.py:health_commission_workbench:SpdTask" in unscoped_aggregates(service)
    readiness = _reverted(
        "routers/surveillance.py",
        "query.with_entities(EmergencyResource.resource_type, func.count(EmergencyResource.id))",
        "db.query(EmergencyResource.resource_type, func.count(EmergencyResource.id))")
    assert "routers/surveillance.py:readiness:EmergencyResource" in unscoped_aggregates(readiness)


SELF_PROOF = '''
def raw_count(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    orgs = _scope(db, user, org_id)
    return {"enrolled": _apply_scope(db.query(SpdEnrollment), SpdEnrollment.org_id, orgs).count(),
            "tasks": db.query(SpdTask).filter(SpdTask.status == "done").count()}

def grouped(group_id: int | None = None, db: Session = Depends(get_db)):
    scope = resolve_org_scope(db, group_id, None)
    return row_dict(db.query(EmergencyResource.resource_type, func.count(EmergencyResource.id))
                    .group_by(EmergencyResource.resource_type).all())

def fine(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """docstring 里写 orgs 与 q 不算收口。"""
    orgs = _scope(db, user, org_id)
    a = db.query(SpdTask).filter(SpdTask.org_id.in_(orgs or [0])).count()
    q = db.query(SpdTask)
    if orgs is not None:
        q = q.filter(SpdTask.org_id.in_(orgs))
    b = q.count()

    def by_org(query):
        return query.filter(SpdTask.org_id.in_(orgs)).all()

    c = by_org(db.query(SpdTask.org_id, func.count(SpdTask.id)))
    d = db.query(SpdTask).filter(SpdTask.assignee_id == user.id).count()
    e = db.query(SpdServiceApply).count()
    return a, b, c, d, e

def later_assigned(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """orgs 与 n 在 docstring 里同句出现。"""
    orgs = _scope(db, user, org_id)
    n = db.query(SpdTask).count()
    return n
'''


def test_判据自证_不收口的两种形状当场点名_收过的与本人口径不报():
    got = [k for k in unscoped_aggregates({"自证.py": SELF_PROOF}) if k.startswith("自证.py")]
    assert got == [
        "自证.py:grouped:EmergencyResource",
        "自证.py:later_assigned:SpdTask",
        "自证.py:raw_count:SpdTask",
    ], got
