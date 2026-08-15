"""越权矩阵：**每一个**带角色守卫的写接口都要拒绝无权角色（P1-1）。

上一轮审阅报的是"越权用例只覆盖 64%，259 个写接口路径中 91 个没有 403 用例"。
补法有两条路：手写 91 条用例，或者把这件事做成生成的。选后者，理由是——
手写的清单今天补到 100%，下周新增一个接口就又不是 100% 了，而且没人会发现。
这里从 FastAPI 路由表里现算：有几个带守卫的写接口，就跑几条断言。
**新增接口忘了配用例这件事，从此不可能发生。**

口径三条：

1. 只测**被守卫的写接口**。无守卫的（登录、居民端自助、任意登录用户可做的
   自助操作）另有 `test_未守卫写接口清单必须是审阅过的白名单` 盯着，
   防的是"新接口忘了加守卫"。
2. 用**空请求体**打。守卫写在 `dependencies=[]` 里时先于请求体校验执行，
   所以合法的响应只有 403。若返回 422，说明该接口把守卫写成了函数参数，
   非授权角色能拿到一份字段清单——既是信息泄露，也让口径不成立，同样算失败。
3. `admin` 永远放行，不参与本测试；挑的是**明确不在允许清单里**的那个角色。
"""
import inspect

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from conftest import reset_database

from app.deps import ROLE_NAMES
from app.main import app

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 无角色守卫的写接口白名单。每一条都是审阅过的：要么是登录/注册这类天然开放的，
# 要么是居民端（自带 scope="portal" 令牌校验），要么是任意登录用户的自助操作
# （读自己的通知、自己报名培训、试算体质辨识）。
# **新接口默认不在这里**——漏配守卫会让下面那条用例直接红。
UNGUARDED_WHITELIST = {
    # 登录与令牌
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/change-password"),
    # 居民端：另有 portal 令牌 scope 校验，不走员工角色体系
    ("POST", "/api/portal/auth/sms/code"),
    ("POST", "/api/portal/auth/sms/login"),
    ("POST", "/api/portal/auth/wechat/login"),
    ("POST", "/api/portal/auth/logout"),
    ("POST", "/api/portal/auth/realname"),
    ("POST", "/api/portal/auth/bind-phone"),
    ("POST", "/api/portal/auth/bind-wechat"),
    ("POST", "/api/portal/me/family"),
    ("DELETE", "/api/portal/me/family/{member_id}"),
    ("POST", "/api/portal/me/appointments"),
    ("POST", "/api/portal/me/appointments/{appointment_id}/cancel"),
    ("POST", "/api/portal/me/notifications/{notification_id}/read"),
    ("POST", "/api/portal/me/surveys"),
    ("POST", "/api/portal/my-archive"),
    ("POST", "/api/portal/surveys"),
    # 慢专病居民端：同上，走 portal 令牌 + accessible_patient 判定"这次能看谁的档案"，
    # 不进员工角色体系。每个接口都只写调用者本人或其代管家人的数据。
    ("POST", "/api/portal/spd/measurements"),
    ("POST", "/api/portal/spd/screenings"),
    ("POST", "/api/portal/spd/service-applies"),
    ("POST", "/api/portal/spd/tasks/{task_id}/submit"),
    # 佐证照片上传：portal 令牌 + accessible_patient + 任务归属校验（见 portal.py）
    ("POST", "/api/portal/spd/tasks/{task_id}/attachments"),
    ("POST", "/api/portal/spd/followups/{record_id}/self-answer"),
    ("POST", "/api/portal/spd/interventions/{intervention_id}/feedback"),
    ("POST", "/api/portal/spd/edu/{push_id}/read"),
    ("POST", "/api/portal/spd/consults"),
    # 任意登录用户的自助操作
    ("POST", "/api/notifications/{notification_id}/read"),
    ("POST", "/api/notifications/read-all"),
    ("POST", "/api/education/courses/{course_id}/exam"),
    ("POST", "/api/education/materials/{material_id}/play"),
    ("POST", "/api/education/training-plans/{plan_id}/enroll"),
    ("POST", "/api/education/training-plans/{plan_id}/cancel-enroll"),
    # 直播反馈：学员给自己听过的课打分，与培训报名同类。
    # 一人一场一条由 (session_id, user_id) 唯一约束保证，替不了别人打分。
    ("POST", "/api/education/live-sessions/{session_id}/feedback"),
    # 模拟诊疗作答：学员自己练习，评分只写自己的记录（user_id 从令牌取，
    # 不从请求体取），替不了别人作答。
    ("POST", "/api/tcm-heritage/simulations/{case_id}/attempts"),
    # 一码通核验：窗口、药房、检查科都要扫码，凡是登录用户就该能扫。
    # 出码（/one-code）另有角色守卫——能核验不等于能替人出码。
    ("POST", "/api/credentials/one-code/resolve"),
    ("POST", "/api/attachments"),
    # 只做计算不落业务数据的试算类接口
    ("POST", "/api/tcm/constitution"),
    ("POST", "/api/tcm/assist-diagnosis"),
    ("POST", "/api/triage/suggest"),
    ("POST", "/api/rules/evaluate"),
    # DRG 事前提示：按拟诊断试算候选组，不落任何业务数据。
    ("POST", "/api/drgs/pre-check"),
    # 流程引擎与总线：实例推进由流程定义自身的角色约束控制
    ("POST", "/api/workflows/instances"),
    ("POST", "/api/workflows/instances/{instance_id}/advance"),
    ("POST", "/api/workflows/instances/{instance_id}/cancel"),
    ("POST", "/api/esb/messages"),
    # 慢专病村医积分：签到与兑换写的都是**调用者本人**的账户（user_id 从令牌取，
    # 不从请求体取），按角色卡人反而会挡住真正要用它的村医。
    # 核销（/api/spd/redeems/verify）另有 director/operator 守卫——
    # 能兑换不等于能核销。
    ("POST", "/api/spd/point-accounts/signin"),
    ("POST", "/api/spd/redeems"),
}


# 刻意对全部非 admin 角色开放的写接口。**不是漏配守卫**——它确实带着守卫，
# 只是允许清单囊括了所有角色，因而没有"无权角色"可测。
# 不良事件上报：谁发现谁报，按角色卡人只会让该报的不报。
ALL_ROLES_BY_DESIGN = {
    ("POST", "/api/quality/adverse-events"),
}


def _iter_api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _iter_api_routes(route.routes)


def _guard_of(route) -> tuple[set[str], bool]:
    """从依赖树里挖出该路由要求的角色。返回 (角色集合, 是否 require_admin)。"""
    roles: set[str] = set()
    admin = False

    def walk(dependencies):
        nonlocal admin
        for sub in dependencies:
            name = getattr(sub.call, "__name__", "")
            if name == "checker":  # require_roles 返回的闭包
                try:
                    declared = inspect.getclosurevars(sub.call).nonlocals.get("roles")
                except TypeError:  # pragma: no cover - 理论上不会走到
                    declared = None
                if declared:
                    roles.update(declared)
            elif name == "require_admin":
                admin = True
            walk(sub.dependencies)

    walk(route.dependant.dependencies)
    return roles, admin


def _collect():
    guarded, unguarded = [], []
    for route in _iter_api_routes(app.routes):
        methods = route.methods & WRITE_METHODS
        if not methods:
            continue
        method = sorted(methods)[0]
        roles, admin = _guard_of(route)
        if admin:
            guarded.append((method, route.path, frozenset({"admin"})))
        elif roles:
            guarded.append((method, route.path, frozenset(roles)))
        else:
            unguarded.append((method, route.path))
    return guarded, unguarded


GUARDED, UNGUARDED = _collect()
# 六个内置角色，admin 除外（admin 始终放行，测它没有意义）
NON_ADMIN_ROLES = [r for r in ROLE_NAMES if r != "admin"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def tokens(client):
    """给每个非 admin 角色各建一个账号，返回角色 → 请求头。"""
    admin = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {admin}"}
    out = {}
    for role in NON_ADMIN_ROLES:
        username = f"authz_{role}"
        client.post(
            "/api/users",
            json={"username": username, "password": "passw0rd1", "role": role},
            headers=headers,
        )
        token = client.post(
            "/api/auth/login", json={"username": username, "password": "passw0rd1"}
        ).json()["access_token"]
        out[role] = {"Authorization": f"Bearer {token}"}
    return out


def _fill_path(path: str) -> str:
    """路径参数一律填 1 / x。反正应当在 403 处就被挡下，取不到对象也没关系；
    真取到了对象反而说明守卫没生效——那正是这条用例要抓的。"""
    out = []
    for segment in path.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            out.append("x" if segment.strip("{}").endswith(("code", "key", "no")) else "1")
        else:
            out.append(segment)
    return "/".join(out)


def test_有可用于越权测试的角色():
    assert len(NON_ADMIN_ROLES) >= 2, NON_ADMIN_ROLES
    assert GUARDED, "一个带守卫的写接口都没找到，说明路由遍历写错了"


@pytest.mark.parametrize(
    "method,path,allowed",
    GUARDED,
    ids=[f"{m} {p} <- {'/'.join(sorted(a))}" for m, p, a in GUARDED],
)
def test_无权角色调用写接口一律403(client, tokens, method, path, allowed):
    outsiders = [r for r in NON_ADMIN_ROLES if r not in allowed]
    if not outsiders:
        pytest.skip("该接口对全部非 admin 角色开放，无越权可测")
    role = outsiders[0]
    resp = client.request(method, _fill_path(path), json={}, headers=tokens[role])
    assert resp.status_code == 403, (
        f"{method} {path} 允许 {sorted(allowed)}，但 {role} 得到 "
        f"{resp.status_code}：{resp.text[:200]}\n"
        "若为 422，说明守卫写成了函数参数、在请求体校验之后才执行，"
        "应改到 dependencies=[] 里。"
    )


def test_未守卫写接口清单必须是审阅过的白名单():
    """守卫漏配的保险丝。

    上面那条用例只测"有守卫的守得住"，守不住的漏洞是"根本没配守卫"——
    新加一个写接口忘了加 `dependencies=[Depends(require_roles(...))]`，
    上面那条永远不会红。这条会。
    """
    actual = {(m, p) for m, p in UNGUARDED}
    unexpected = actual - UNGUARDED_WHITELIST
    assert unexpected == set(), (
        f"这些写接口没有角色守卫，且不在审阅过的白名单里：{sorted(unexpected)}\n"
        "要么补 dependencies=[Depends(require_roles(...))]，"
        "要么在确认可开放后加进 UNGUARDED_WHITELIST 并写明理由。"
    )
    stale = UNGUARDED_WHITELIST - actual
    assert stale == set(), f"白名单里这些接口已加上守卫或已删除，应从白名单移除：{sorted(stale)}"


def test_越权覆盖率是100(capsys):
    """把覆盖率本身断言住：可测越权的接口条数 == 生成的用例条数。

    上一轮报的 64% 是"同一测试块里出现该路径且出现 403 断言即算覆盖"的粗口径，
    会高估。这里是精确口径：分母是带守卫的写接口，分子是真的跑了一次越权调用的。
    """
    testable = [
        (m, p, a) for m, p, a in GUARDED if any(r not in a for r in NON_ADMIN_ROLES)
    ]
    by_design = [(m, p, a) for m, p, a in GUARDED if (m, p) in ALL_ROLES_BY_DESIGN]
    coverage = len(testable) * 100 / (len(GUARDED) - len(by_design))
    with capsys.disabled():
        print(
            f"\n越权矩阵：写接口 {len(GUARDED) + len(UNGUARDED)} 个 = "
            f"带守卫 {len(GUARDED)} + 白名单开放 {len(UNGUARDED)}；"
            f"带守卫的里面 {len(by_design)} 个按设计对全角色开放，"
            f"其余 {len(testable)} 条逐条跑了越权调用，覆盖率 {coverage:.1f}%"
        )
    untestable = [
        (m, p) for m, p, a in GUARDED
        if not any(r not in a for r in NON_ADMIN_ROLES) and (m, p) not in ALL_ROLES_BY_DESIGN
    ]
    assert untestable == [], (
        f"这些接口对全部非 admin 角色开放，无越权可测：{untestable}\n"
        "若确属有意（如不良事件人人可报），加进 ALL_ROLES_BY_DESIGN 并写明理由。"
    )
    assert coverage == 100.0, f"覆盖率 {coverage:.1f}%"
