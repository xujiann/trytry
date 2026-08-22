"""档案调阅授权的有效期判定：把门的和报信的必须是同一份。

## 缺陷形状（与 ws 那份"第二拷贝"同形）

同一个问题——"这家机构对这位患者的调阅授权此刻还算数吗"——此前有两份答案：

| 实现 | 空 `expire_date` 怎么算 |
|---|---|
| `visibility._patient_basis_uncached`（**真正把门的**） | 不设到期日 → **有效** |
| `routers/patients.check_authorization`（**给对接方看的**） | SQL 里 `expire_date >= 今天`，空串比不过 → **无效** |

`archive_authorizations.expire_date` 是 `String(10) default=""` 的**非空列**，
空串是可达状态（模型默认值、历史数据、任何不填该列的写入）。于是同一条长期
授权：医生那边确实调得开档案，而校验接口告诉对接方 `allowed=false`。
两个答案各自都"能跑"，改任一处另一处都不会跟——这正是必须收敛的判据。

收敛后两侧共用 `visibility.active_authorization_grants`。本文件钉住这一点：
**同一条授权，两处必须给同一个答案**，而不是各自断言各自的预期值
（那样写的话，两边一起漂走也照样绿）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import ArchiveAuthorization, User
from app.visibility import clear_visibility_cache, patient_basis


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, username, password="pw123456"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client):
    """一位患者 + 一家与他**毫无业务关系**的机构，唯一的联系就是一条调阅授权。

    没有这个"毫无关系"的前提，可见性会因为就诊/签约等其它依据而成立，
    授权判定改坏了也测不出来——依据词必须恰好是 authorization。
    """
    admin = _login(client, "admin", "admin123")
    grantee = client.post(
        "/api/organizations",
        json={"name": "授权乙卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "auth_doc_b", "password": "pw123456", "full_name": "乙医生",
              "role": "doctor", "org_id": grantee["id"]},
        headers=admin,
    )
    patient = client.post(
        "/api/patients",
        json={"name": "授权患者", "id_card": "330000198803034321"},
        headers=admin,
    ).json()
    return {"admin": admin, "grantee": grantee, "patient": patient}


def _revoke_all() -> None:
    """先把此前的授权全部撤销。

    不做这一步的话，前一条用例留下的那条正常授权会替本条"顶上"——
    `check` 只要找到任意一条有效授权就 allowed=True，于是即便把有效期判定
    改回缺陷版本，本文件照样全绿。用例之间靠残留数据互相搭救，是这类
    模块级 fixture 套件最典型的失效方式（test_stage15_horizontal 的
    `stranger` fixture 记着同一条教训）。
    """
    db = SessionLocal()
    try:
        for row in db.query(ArchiveAuthorization).all():
            row.status = "revoked"
        db.commit()
    finally:
        db.close()
    clear_visibility_cache()


def _grant(client, world, expire_date: str) -> int:
    resp = client.post(
        f"/api/patients/{world['patient']['id']}/authorizations",
        json={"grantee_org_id": world["grantee"]["id"], "scope": "all",
              "expire_date": expire_date},
        headers=world["admin"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _set_expire_date(auth_id: int, value: str) -> None:
    """把有效期改成入参校验（`DateStr`）挡得住、而数据库拦不住的值。

    走库而不是走接口是**故意的**：这一列的默认值就是空串，接口只是恰好没有
    一条路径能写出它。缺陷不在"谁写进去的"，而在"写进去之后两处答得不一样"。
    """
    db = SessionLocal()
    try:
        row = db.get(ArchiveAuthorization, auth_id)
        row.expire_date = value
        db.commit()
    finally:
        db.close()
    clear_visibility_cache()  # 依据结论有 TTL 缓存，改了数据要让它重算


def _check(client, world, scope: str = "all") -> bool:
    resp = client.get(
        f"/api/patients/{world['patient']['id']}/authorizations/check",
        params={"org_id": world["grantee"]["id"], "scope": scope},
        headers=world["admin"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["allowed"]


def _basis(world) -> str | None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "auth_doc_b").first()
        return patient_basis(db, user, world["patient"]["id"])
    finally:
        db.close()


def test_未到期授权_两侧都说有效(client, world):
    """先证明这条链路本来就通——不然后面的"两侧一致"可能只是一起说 False。"""
    _revoke_all()
    _grant(client, world, "2099-12-31")
    clear_visibility_cache()
    assert _basis(world) == "authorization", "可见性未按授权放行，用例前提就不成立"
    assert _check(client, world) is True


def test_空到期日的长期授权_把门的与报信的口径一致(client, world):
    """回归点：`expire_date` 为空时，两处必须给同一个答案。

    修复前——可见性说 `authorization`（能调阅），校验接口说 `allowed=false`。
    断言写成"两者相等"而不是"都等于 True"：口径将来若真要改成"空=无效"，
    该改的是 `active_authorization_grants` 这一处，两侧仍然一起改，用例仍然绿；
    而任何一侧单独漂走，这条立刻红。
    """
    _revoke_all()
    auth_id = _grant(client, world, "2099-12-31")
    _set_expire_date(auth_id, "")

    gate_allows = _basis(world) == "authorization"
    api_allows = _check(client, world)
    assert gate_allows == api_allows, (
        f"空 expire_date 的授权：可见性判定 ={gate_allows}，校验接口 allowed={api_allows}"
        "——同一条授权两处给了不同答案，说明有效期判定又长出了第二份实现"
    )
    assert gate_allows is True, "空 expire_date 按'不设到期日'算有效（收敛口径以把门的一侧为准）"


def test_已过期授权_两侧都说无效(client, world):
    """另一头也钉住：收敛不能把判定放宽成"有授权就算数"。"""
    db = SessionLocal()
    try:
        for row in db.query(ArchiveAuthorization).all():
            row.expire_date = "2000-01-01"
        db.commit()
    finally:
        db.close()
    clear_visibility_cache()

    assert _basis(world) is None, "过期授权仍被当作可见依据"
    assert _check(client, world) is False


def test_范围仍由校验接口自己判(client, world):
    """`scope` **不**进共用判定——它是校验接口独有的问题，不是同一判定的第二份。

    可见性不分范围（拿到任一有效授权即构成调阅依据），而对接方问的是
    "我要的这个范围在不在授权里"。合进去会把两个不同的问题揉成一个。
    """
    _revoke_all()
    _grant(client, world, "2099-12-31")
    db = SessionLocal()
    try:
        row = (
            db.query(ArchiveAuthorization)
            .filter(ArchiveAuthorization.status == "active")
            .first()
        )
        row.scope = "encounter"
        db.commit()
    finally:
        db.close()
    clear_visibility_cache()

    assert _check(client, world, scope="encounter") is True
    assert _check(client, world, scope="exam") is False, "范围判定被误收敛掉了"
    assert _basis(world) == "authorization", "可见性不该因范围不匹配而拒绝"
