"""慢专病配置域 · 量表/宣教/服务包/标签 与 团队/村医的**响应契约**
（scales 15 + teams 12）。`spd/config` 58 个端点自此清零。

四处判断：

1. **服务包 `price` 是 `Money`（Numeric）列**——整数价读回来是 int，声明成
   float 会把「200 元」变成「200.0 元」。这是本仓库反复出现的同一个陷阱。
2. **标签新建与列表不是同一形状**：列表**没有** `active`（它本身已按 active
   过滤，再回一个恒为 true 的字段没意义）。两个模型，不能继承合并。
3. **`_team_out` 出三种形状**（改团队只有基础字段 / 列表与新建多
   `member_count` / 详情再多 `members`），用 `exclude_unset`；`member_count`
   必须声明在 `members` 之前——序列化按声明顺序走。
4. **两个二维码端点用 `SvgResponse`**（`_base` 里，与 `reports.CsvResponse`
   同一写法）：声明与实际返回是同一个类。

两条"不由字节决定"的记录，写在这里免得日后有人当成守卫失效：

- 去掉 `response_class=SvgResponse` **不改响应字节**（handler 返回的
  `SvgResponse` 自带 media_type，content-type 照样是 image/svg+xml）。变的是
  **OpenAPI 声明**——会回落成 `application/json`，即规格书上写了假话。
  故本文件断言的是 OpenAPI 里的媒体类型，不是响应字节。
- 二维码 SVG 的字节数**每次运行都不同**：`token_urlsafe` 生成的令牌不同，
  二维码内容随之不同。做逐字节比对时对照过——同一份代码跑两次，`scale-qr`
  一样会变，而令牌写死的 `vd-qr` 前后完全一致。不能拿随机项当比对依据。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.security import hash_password
from app.spd import models as S


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        org = Organization(name="量表契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        admin = User(username="scalect", password_hash=hash_password("Scale-ct-2026!"),
                     full_name="量表管理员", role="admin", org_id=org.id)
        doc = User(username="scalectdoc", password_hash=hash_password("Scale-doc-2026!"),
                   full_name="张医生", role="doctor", org_id=org.id)
        db.add_all([admin, doc])
        db.flush()
        draft = S.SpdScale(code="ST-D", name="草稿量表", category="risk",
                           program_code="HTN", version="v1", status="draft",
                           items=[{"key": "q1", "type": "single",
                                   "options": [{"label": "是", "score": 1}]}],
                           scoring={"ranges": []}, qr_token="")
        pub = S.SpdScale(code="ST-P", name="已发布量表", category="screen", version="v1",
                         status="published", items=[{"key": "a"}], scoring={},
                         qr_token="FIXEDTOKEN12")
        db.add_all([draft, pub])
        edu = S.SpdEduMaterial(code="ST-EDU", title="怎么吃", program_code="HTN",
                               media_type="text", content="少盐", dept="心内科")
        db.add(edu)
        # 服务包价格：一个整数、一个小数（Money 陷阱的两条分支）
        pkg = S.SpdServicePackage(code="ST-INT", name="整数价包", program_code="HTN",
                                  price=200, period_days=365,
                                  items=[{"code": "fu", "times": 4}])
        db.add(pkg)
        db.add(S.SpdServicePackage(code="ST-DEC", name="小数价包", price=88.5,
                                   period_days=180, items=[]))
        db.add(S.SpdTag(code="ST-TAG", name="高危", category="patient", color="#f00"))
        team = S.SpdTeam(name="契约团队", org_id=org.id, level="township",
                         program_codes=["HTN"], leader_user_id=admin.id, dept="全科",
                         service_area="城关", data_scope="org", active=True)
        db.add(team)
        db.flush()
        member = S.SpdTeamMember(team_id=team.id, user_id=doc.id, member_role="doctor",
                                 program_codes=["HTN"], patient_scope="team")
        db.add(member)
        vd = S.SpdVillageDoctor(user_id=doc.id, org_id=org.id, township="城关镇",
                                village="东村", license_no="LIC-1",
                                license_valid_to="2028-01-01", phone="13900000001",
                                bind_token="VDFIXEDTOKEN")
        db.add(vd)
        db.commit()
        return {"draft": draft.id, "pub": pub.id, "org": org.id, "team": team.id,
                "member": member.id, "vd": vd.id, "doc": doc.id, "admin": admin.id,
                "edu": edu.id, "pkg": pkg.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "scalect",
                              "password": "Scale-ct-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


B = "/api/spd"
PACKAGE_KEYS = {"id", "code", "name", "program_code", "price", "period_days", "items",
                "active"}
TEAM_BASE = {"id", "name", "org_id", "level", "program_codes", "leader_user_id", "dept",
             "service_area", "data_scope", "active"}


# ------------------------------------------------- Money 陷阱
def test_服务包整数价仍是int小数价仍是float(client, auth):
    rows = {p["code"]: p for p in client.get(f"{B}/service-packages", headers=auth).json()}
    assert set(rows["ST-INT"]) == PACKAGE_KEYS
    assert rows["ST-INT"]["price"] == 200 and isinstance(rows["ST-INT"]["price"], int)
    assert rows["ST-DEC"]["price"] == 88.5
    assert rows["ST-INT"]["items"] == [{"code": "fu", "times": 4}]


# ------------------------------------------------- 两个形状不同的标签模型
def test_标签列表没有active字段而新建有(client, auth):
    created = client.post(f"{B}/tags", headers=auth, json={"code": "ST-TAG2", "name": "中危"})
    assert created.status_code == 201
    assert set(created.json()) == {"id", "code", "name", "category", "color", "active"}
    listed = client.get(f"{B}/tags", headers=auth).json()
    assert all(set(t) == {"id", "code", "name", "category", "color"} for t in listed)
    assert all("active" not in t for t in listed)


# ------------------------------------------------- 团队三形状
def test_团队三种形状的键集合(client, auth, seeded):
    listed = {t["name"]: t for t in client.get(f"{B}/teams", headers=auth).json()}
    row = listed["契约团队"]
    assert set(row) == TEAM_BASE | {"member_count"}
    assert row["member_count"] == 1 and "members" not in row

    detail = client.get(f"{B}/teams/{seeded['team']}", headers=auth).json()
    assert set(detail) == TEAM_BASE | {"member_count", "members"}
    # 顺序：member_count 在 members 之前（_team_out 先塞前者，get_team 再追加后者）
    assert list(detail)[-2:] == ["member_count", "members"]
    assert set(detail["members"][0]) == {
        "id", "user_id", "user_name", "member_role", "program_codes", "stage_scope",
        "patient_scope", "can_view", "can_followup", "can_referral", "can_audit",
        "can_assess", "active"}
    assert detail["members"][0]["user_name"] == "张医生"

    patched = client.patch(f"{B}/teams/{seeded['team']}", headers=auth,
                           json={"dept": "心内科"})
    assert patched.status_code == 200
    # 改团队两个键都不出现，不是 null
    assert set(patched.json()) == TEAM_BASE
    assert "member_count" not in patched.json() and "members" not in patched.json()


def test_成员新增与修改是两组不同的键(client, auth, seeded):
    added = client.post(f"{B}/teams/{seeded['team']}/members", headers=auth,
                        json={"user_id": seeded["admin"], "member_role": "expert"})
    assert added.status_code == 201
    assert set(added.json()) == {"id", "team_id", "user_id", "member_role"}

    patched = client.patch(f"{B}/team-members/{seeded['member']}", headers=auth,
                           json={"member_role": "nurse"})
    assert patched.status_code == 200
    # 没有 team_id/user_id，多了 active——两组键不同，故是两个模型
    assert set(patched.json()) == {"id", "member_role", "active"}

    removed = client.delete(f"{B}/team-members/{added.json()['id']}", headers=auth)
    assert removed.status_code == 204 and removed.content == b""


# ------------------------------------------------- 量表
SCALE_KEYS = {"id", "code", "name", "category", "program_code", "version", "status",
              "items", "scoring", "qr_token", "owner_team_id"}


def test_量表的键集合与未发布时的空令牌(client, auth, seeded):
    body = client.get(f"{B}/scales/{seeded['draft']}", headers=auth).json()
    assert set(body) == SCALE_KEYS
    assert body["qr_token"] == ""          # 未发布：空串，不是 null
    assert body["owner_team_id"] is None
    assert body["scoring"] == {"ranges": []}


def test_发布后才出二维码且未发布时409(client, auth, seeded):
    early = client.get(f"{B}/scales/{seeded['draft']}/qr.svg", headers=auth)
    assert early.status_code == 409 and set(early.json()) == {"detail"}

    published = client.post(f"{B}/scales/{seeded['draft']}/publish", headers=auth)
    assert published.status_code == 200 and set(published.json()) == SCALE_KEYS
    assert published.json()["status"] == "published"
    assert len(published.json()["qr_token"]) > 0

    qr = client.get(f"{B}/scales/{seeded['draft']}/qr.svg", headers=auth)
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg+xml")
    assert qr.content.startswith(b"<?xml")


def test_二维码端点在OpenAPI里声明的是svg而不是json(client):
    """这条盯的**不是响应字节**——去掉 `response_class=SvgResponse` 后
    content-type 照样是 image/svg+xml（handler 返回的类自带媒体类型）。
    变的是规格书：OpenAPI 会回落成 `application/json`，写了假话，
    契约棘轮也就认不出这是一个已声明契约的端点。
    """
    paths = app.openapi()["paths"]
    for path in ("/api/spd/scales/{scale_id}/qr.svg",
                 "/api/spd/village-doctors/{vd_id}/qr.svg"):
        content = paths[path]["get"]["responses"]["200"]["content"]
        assert list(content) == ["image/svg+xml"], (path, list(content))


def test_已发布量表不许改题目(client, auth, seeded):
    blocked = client.patch(f"{B}/scales/{seeded['pub']}", headers=auth,
                           json={"items": [{"key": "z"}]})
    assert blocked.status_code == 409 and set(blocked.json()) == {"detail"}
    dup = client.post(f"{B}/scales", headers=auth,
                      json={"code": "ST-DUP", "name": "重复key",
                            "items": [{"key": "a"}, {"key": "a"}]})
    assert dup.status_code == 422 and set(dup.json()) == {"detail"}


# ------------------------------------------------- 宣教与村医
def test_宣教素材的键集合(client, auth, seeded):
    rows = client.get(f"{B}/edu-materials", headers=auth).json()
    keys = {"id", "code", "title", "program_code", "media_type", "content", "media_url",
            "dept", "active"}
    assert rows and set(rows[0]) == keys
    patched = client.patch(f"{B}/edu-materials/{seeded['edu']}", headers=auth,
                           json={"title": "改标题"})
    assert patched.status_code == 200 and set(patched.json()) == keys


VD_KEYS = {"id", "user_id", "user_name", "org_id", "township", "village", "license_no",
           "license_valid_to", "phone", "bind_token", "active"}


def test_村医档案单条与列表同形但user_name来源不同(client, auth, seeded):
    listed = client.get(f"{B}/village-doctors", headers=auth).json()
    assert set(listed[0]) == VD_KEYS
    assert listed[0]["user_name"] == "张医生"        # 列表会查用户名

    created = client.post(f"{B}/village-doctors", headers=auth,
                          json={"user_id": seeded["admin"], "org_id": seeded["org"],
                                "township": "城关镇"})
    assert created.status_code == 201 and set(created.json()) == VD_KEYS
    # 单条新建不查用户名，回空串——形状一样，取值来源不同
    assert created.json()["user_name"] == ""
    assert len(created.json()["bind_token"]) > 0


def test_批量开通回逐行结果而不是一个成败(client, auth, seeded):
    body = client.post(f"{B}/village-doctors/batch", headers=auth,
                       json={"items": [{"user_id": seeded["doc"], "org_id": seeded["org"]},
                                       {"user_id": 999999, "org_id": seeded["org"]}]})
    assert body.status_code == 200
    assert set(body.json()) == {"created", "skipped"}
    assert body.json()["created"] == 0
    reasons = {s["user_id"]: s["reason"] for s in body.json()["skipped"]}
    assert set(body.json()["skipped"][0]) == {"user_id", "reason"}
    assert reasons[seeded["doc"]] == "已建档"
    assert reasons[999999] == "用户不存在"


# ------------------------------------------------- 顺手修：绑定二维码指向 404
def test_两个二维码编的地址都能真的打开(client, auth, seeded):
    """村医绑定码原本编的是 `/m/doctor.html`，而那个地址**没有路由**——
    `main.py` 只挂了 `/static`，医生端入口是显式的 `/m/doctor`。印出去的码
    扫开是一张 404，村医绑不上账号，还看不出是码的问题。

    这条不比对字符串——只比字符串的话，写错成另一个同样不存在的路径照样绿。
    改成把 URL 里的 path 抠出来真的请求一次，能打开才算数。
    """
    import pathlib
    import re

    # 反面先立住：曾经被编进码里的那个地址确实是 404，别让它悄悄回来
    assert client.get("/m/doctor.html").status_code == 404

    config_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "spd" / "routers" / "config"
    encoded = []
    for module in ("teams.py", "scales.py"):
        encoded += re.findall(r'base_url\)\.rstrip\(.\/.\)\}(/[^#"]*)#',
                              (config_dir / module).read_text())
    assert len(encoded) == 2, f"没能从源码里抠出两个二维码路径，用例失效了：{encoded}"
    for path in encoded:
        assert client.get(path).status_code == 200, f"二维码编的 {path} 打不开"
