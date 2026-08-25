"""`GET /api/users/selectable`：可选人员清单的最小披露与可见范围收窄（ADR-0015）。

这个接口是为"配置团队成员/村医时得能选人"开的：平台唯一的用户清单
`GET /api/users` 是 `require_admin`，而慢专病配置域的写操作放给
`CONFIG_ROLES=("director","doctor")`——那两个角色能建团队却列不出可选的人。

**它的全部价值在于"少回一点"**，所以守卫必须直接盯住少回的那部分，而不是
只验"能不能读到"：一个把 `UserOut` 原样回出去的实现，功能测试全过、
ADR 的理由全废。故本文件第一条就是"响应里不许出现 username / status"。

**这条守卫守的是契约，不是 handler——写清楚免得下一个人误判它的覆盖面。**
变异验证时发现的：往 handler 的 dict 里塞 `"username": u.username`，本文件
**15 条全绿**——因为 `response_model` 会把契约外的键过滤掉，那个键根本出不去。
真正会泄露的改法是动契约（换成 `UserOut` / 往 `SelectableUserOut` 里加字段），
两者各让 11 条转红。剩下的第三种改法——**干脆不声明 `response_model`**——
本文件同样咬不住（今天 dict 里就只有那 5 个键），它由兄弟棘轮
`test_api_contract_governance::test_响应契约欠账不许变大` 接住：实测欠账
450 → 451 当场变红。两道合起来才闭环：那条管"必须有契约"，这里管"契约不许变宽"。

守的五件事，每件都对着一个"改坏了功能照常、只是把不该给的给出去了"的形态：

1. **字段最小披露**：只回 id / name / role / org_id / org_name。
   `username` 是登录句柄——枚举给全体在册账号等于送出撞库字典的一半；
   `status` 会暴露"谁被停用了"。
2. **可见范围收窄**：全域角色（admin/director）看全部，其余角色只看本机构；
   未挂机构的账号看不到任何人。口径与明细数据同档（`visibility.visible_org_ids`）。
3. **只回在册账号**：停用的人不该还能被加进团队。
4. **跨机构显式拒绝**：传别家 `org_id` 回 403，不是静默返回空表——
   空表会让调用方以为"那边确实没人"。
5. **关键词不是登录名探测口**：只匹配显示名。`full_name` 非空时只匹配它，
   为空时才匹配 `username`。无条件放开按登录名搜，就等于给了一个
   "输猜测值、看有没有人跳出来"的口子，与本接口不外泄登录名的前提自相矛盾。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

#: 接口承诺回的**全部**字段。多一个少一个都要在这里显式改，改的时候会看见 ADR-0015。
EXPECTED_FIELDS = {"id", "name", "role", "org_id", "org_name"}

#: 绝不能出现在响应里的字段。这是本接口存在的理由，单独列出来而不是靠上面那条反推——
#: `EXPECTED_FIELDS` 将来可能因为业务需要加字段，这一条不该跟着松。
FORBIDDEN_FIELDS = {"username", "status", "password_hash", "totp_secret",
                    "token_valid_from", "password_updated_at", "must_change_password"}

PASSWORD = "selectpass12345"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client, h):
    """两家卫生院各配人：甲院一名 doctor + 一名 director + 一名停用账号，乙院一名 doctor。"""
    county = client.post(
        "/api/organizations",
        json={"name": "选人县医院", "org_type": "lead_hospital", "level": "county"},
        headers=h,
    ).json()
    orgs = {}
    for key, name in (("a", "选人甲卫生院"), ("b", "选人乙卫生院")):
        orgs[key] = client.post(
            "/api/organizations",
            json={"name": name, "org_type": "township", "level": "township",
                  "parent_id": county["id"]},
            headers=h,
        ).json()

    made = {}
    for username, org_key, role, full_name in (
        ("sel_doc_a", "a", "doctor", "甲院张医师"),
        ("sel_dir_a", "a", "director", "甲院钱院长"),
        ("sel_doc_b", "b", "doctor", "乙院李医师"),
        # 刻意留空 full_name：显示名回落到 username，第 5 条守卫要用它
        ("sel_noname_a", "a", "operator", ""),
        ("sel_off_a", "a", "operator", "甲院已停用的人"),
    ):
        resp = client.post(
            "/api/users",
            json={"username": username, "password": PASSWORD, "role": role,
                  "org_id": orgs[org_key]["id"], "full_name": full_name},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        made[username] = resp.json()

    client.patch(f"/api/users/{made['sel_off_a']['id']}/status",
                 json={"status": "disabled"}, headers=h)

    def headers_for(username):
        token = client.post(
            "/api/auth/login", json={"username": username, "password": PASSWORD}
        ).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return {"orgs": orgs, "users": made, "headers_for": headers_for}


def _names(resp):
    return {row["name"] for row in resp.json()}


# ------------------------------------------------------------ 1. 字段最小披露


def test_只回约定的五个字段(client, world):
    resp = client.get("/api/users/selectable", headers=world["headers_for"]("sel_doc_a"))
    assert resp.status_code == 200
    assert resp.json(), "本机构至少有几个人，空表说明范围收窄写错了"
    for row in resp.json():
        assert set(row) == EXPECTED_FIELDS, (
            f"字段集合与约定不符：多出 {set(row) - EXPECTED_FIELDS}、"
            f"缺少 {EXPECTED_FIELDS - set(row)}（改字段请同步 ADR-0015）"
        )


def test_响应里不得出现登录名与账号状态(client, world):
    """本接口存在的**全部理由**。把 UserOut 原样回出去时这条转红，其余用例照样绿。"""
    resp = client.get("/api/users/selectable", headers=world["headers_for"]("sel_doc_a"))
    leaked = FORBIDDEN_FIELDS & set().union(*(set(r) for r in resp.json()))
    assert not leaked, (
        f"响应泄露了不该给的字段：{sorted(leaked)}。"
        "username 是登录句柄（撞库字典的一半），status 会暴露谁被停用——"
        "少回这些正是 ADR-0015 选方案 C 而不是放宽 GET /api/users 的理由。"
    )
    # 连值都不能对得上：换个键名把 username 塞进 name 同样算泄露
    assert "sel_doc_a" not in {r["name"] for r in resp.json()}, (
        "有 full_name 的账号，显示名不该回落到 username"
    )


def test_没有全名时显示名回落到登录名(client, world):
    """回落是全仓库既有惯例（`u.full_name or u.username`），不是泄露——
    那个人的显示名本来就只有登录名可用。"""
    resp = client.get("/api/users/selectable", headers=world["headers_for"]("sel_doc_a"))
    assert "sel_noname_a" in _names(resp)


# ------------------------------------------------------------ 2. 可见范围收窄


def test_非全域角色只看得到本机构(client, world):
    names = _names(client.get("/api/users/selectable",
                              headers=world["headers_for"]("sel_doc_a")))
    assert "甲院张医师" in names
    assert "乙院李医师" not in names, "甲院医师看到了乙院的人——范围收窄没生效"


def test_全域角色看得到全部(client, world, h):
    """director/admin 在 visibility.GLOBAL_ROLES 里，本来就能跨机构配团队。

    这条同时是反面用例：把收窄写成"一律只看本机构"时它会转红。
    """
    for headers, who in ((h, "admin"), (world["headers_for"]("sel_dir_a"), "director")):
        names = _names(client.get("/api/users/selectable", headers=headers))
        assert {"甲院张医师", "乙院李医师"} <= names, f"{who} 应当看得到两家机构的人"


def test_未挂机构的账号看不到任何人(client, h):
    """`visible_org_ids` 对 org_id 为空的业务账号回 `[]`——宁可什么都看不到，
    也不默认放行。这条钉住 `in_([])` 的行为没有退化成"不加过滤"。"""
    resp = client.post(
        "/api/users",
        json={"username": "sel_orgless", "password": PASSWORD, "role": "operator",
              "full_name": "没挂机构的人"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    token = client.post(
        "/api/auth/login", json={"username": "sel_orgless", "password": PASSWORD}
    ).json()["access_token"]
    listed = client.get("/api/users/selectable",
                        headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200
    assert listed.json() == [], "没挂机构的账号不该看到任何人"


# ------------------------------------------------------------ 3. 只回在册账号


def test_停用账号不出现在可选清单里(client, world, h):
    assert "甲院已停用的人" not in _names(
        client.get("/api/users/selectable", headers=h)
    ), "停用的人不该还能被选进团队"
    # 对照：管理清单里它还在（停用是状态不是删除），证明拦的是本接口而不是数据没了
    assert any(u["username"] == "sel_off_a" for u in client.get("/api/users", headers=h).json())


# ------------------------------------------------------- 4. 跨机构显式拒绝


def test_传别家机构id回403而不是空表(client, world):
    resp = client.get(
        f"/api/users/selectable?org_id={world['orgs']['b']['id']}",
        headers=world["headers_for"]("sel_doc_a"),
    )
    assert resp.status_code == 403, (
        f"跨机构查询回了 {resp.status_code}。静默返回空表会让调用方以为"
        "「那边确实没人」，而不是「你没权限看」"
    )


def test_传本机构id照常放行(client, world):
    """反面：别把守卫写成"带 org_id 一律拒"。"""
    resp = client.get(
        f"/api/users/selectable?org_id={world['orgs']['a']['id']}",
        headers=world["headers_for"]("sel_doc_a"),
    )
    assert resp.status_code == 200
    assert "甲院张医师" in _names(resp)


# ------------------------------------------- 5. 关键词不是登录名探测口


def test_关键词按显示名匹配(client, h):
    assert _names(client.get("/api/users/selectable?keyword=张", headers=h)) == {"甲院张医师"}


def test_有全名的账号不能按登录名搜出来(client, h):
    """否则就是个用户名探测口：输猜测值、看有没有人跳出来。

    `sel_doc_a` 是真实存在的登录名，但那个账号有 full_name，
    所以按登录名搜必须搜不到——搜得到就说明匹配面放开到了 username。
    """
    assert _names(client.get("/api/users/selectable?keyword=sel_doc_a", headers=h)) == set()


def test_没有全名的账号才按登录名匹配(client, h):
    """回落规则的另一半：显示名就是登录名时，按它搜是理所当然的。"""
    assert "sel_noname_a" in _names(
        client.get("/api/users/selectable?keyword=sel_noname", headers=h)
    )


# --------------------------------------------------------------- 其它约定


def test_按角色筛选(client, h):
    names = _names(client.get("/api/users/selectable?role=doctor", headers=h))
    assert {"甲院张医师", "乙院李医师"} <= names
    assert "甲院钱院长" not in names


def test_走统一分页并回总数头(client, h):
    """§11：列表一律走 deps.paginate。缺 X-Total-Count 说明绕过了它。"""
    resp = client.get("/api/users/selectable?limit=2", headers=h)
    assert resp.status_code == 200
    assert len(resp.json()) <= 2
    assert resp.headers.get("X-Total-Count"), "没有 X-Total-Count——没走 paginate"


def test_管理清单本身没被顺手放宽(client, world):
    """ADR-0015 的另一半承诺：新开接口是为了**不动** `GET /api/users` 的守卫。

    哪天有人觉得"既然都放开了不如直接改那个"，这条会转红。
    """
    resp = client.get("/api/users", headers=world["headers_for"]("sel_doc_a"))
    assert resp.status_code == 403, (
        f"GET /api/users 对非管理员回了 {resp.status_code}——"
        "它应当仍是 require_admin，ADR-0015 选的是另开接口而不是放宽它"
    )
