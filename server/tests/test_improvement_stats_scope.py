"""回归：整改任务汇总必须按可见范围过滤，不得泄露全县数字。

修的洞：`GET /api/performance/improvement-stats` 此前**连 `user` 参数都没有**，
任何登录账号拿到的都是全县汇总——村医、药师都能看到全县有多少条整改任务、
多少条超期、闭环率多少。与 `routers/jobs.py` T6.7 整改掉的是同一类问题：
"任务摘要里带着各类超期数量……属于运营管理信息，没有理由对医师、药师开放"。

更要紧的是它与紧挨着的 `GET /improvements` 对不上：那个按 `scope_org_list`
只给本机构明细，这个却给全县汇总。`pages-public.js` 把两者放在同一个
Promise.all 里同时取，于是同一屏上"列表 2 条"、"汇总 87 条"。

口径：用 `scope_org_list(..., stats=True)`——统计走医共体范围（牵头医院要看得到
片区汇总），明细走本机构，这个区分是 `visibility` 早就建好的。
全域角色（admin/director）响应与整改前一模一样。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import ImprovementTask, Organization, User


@pytest.fixture()
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def world(client):
    """两家互不相干的卫生院，各有自己的整改任务。"""
    admin = _login(client, "admin", "admin123")
    out = {"admin": admin}
    with SessionLocal() as db:
        creator = db.query(User).filter(User.username == "admin").first()
        creator_id = creator.id
    for tag, n_open, n_verified in (("甲", 2, 1), ("乙", 5, 0)):
        org = client.post(
            "/api/organizations",
            json={"name": f"整改{tag}院", "org_type": "township", "level": "township"},
            headers=admin,
        ).json()
        client.post(
            "/api/users",
            json={"username": f"op_{tag}", "password": "pass123456",
                  "role": "operator", "org_id": org["id"]},
            headers=admin,
        )
        with SessionLocal() as db:
            for i in range(n_open):
                db.add(ImprovementTask(
                    org_id=org["id"], problem=f"{tag}问题{i}", owner_name="某人",
                    due_date="2030-01-01", status="open", created_by=creator_id,
                ))
            for i in range(n_verified):
                db.add(ImprovementTask(
                    org_id=org["id"], problem=f"{tag}已闭环{i}", owner_name="某人",
                    due_date="2030-01-01", status="verified", created_by=creator_id,
                ))
            db.commit()
        out[tag] = {"org": org, "op": _login(client, f"op_{tag}", "pass123456")}
    return out


def test_经办只看到本机构的汇总而不是全县(client, world):
    """核心：甲院经办不该知道乙院有几条整改任务。"""
    stats = client.get("/api/performance/improvement-stats",
                       headers=world["甲"]["op"]).json()
    assert stats["total"] == 3, f"甲院共 3 条（2 open + 1 verified），实际 {stats['total']}"
    assert stats["by_status"]["open"]["count"] == 2
    assert stats["by_status"]["verified"]["count"] == 1

    other = client.get("/api/performance/improvement-stats",
                       headers=world["乙"]["op"]).json()
    assert other["total"] == 5, f"乙院共 5 条，实际 {other['total']}"
    assert "verified" not in other["by_status"], "乙院没有已闭环的，不该出现该状态"


def test_汇总与同屏的明细列表口径一致(client, world):
    """两者在 pages-public.js 里是同一个 Promise.all 取的，数字必须对得上。"""
    headers = world["甲"]["op"]
    listed = client.get("/api/performance/improvements", headers=headers).json()
    stats = client.get("/api/performance/improvement-stats", headers=headers).json()
    assert stats["total"] == len(listed), (
        f"列表 {len(listed)} 条、汇总说 {stats['total']} 条——同一屏上自相矛盾"
    )


def test_全域角色仍看全县_响应与整改前一致(client, world):
    """admin/director 的可见范围本就是全域，这次整改不该改变他们看到的东西。"""
    stats = client.get("/api/performance/improvement-stats",
                       headers=world["admin"]).json()
    assert stats["total"] == 8, "全县 3 + 5 = 8 条"
    assert stats["closed_rate_pct"] == round(1 * 100.0 / 8, 2)


def test_闭环率按各自范围算(client, world):
    """闭环率是从过滤后的集合里算的，不能拿全县分母套本机构分子。"""
    jia = client.get("/api/performance/improvement-stats",
                     headers=world["甲"]["op"]).json()
    assert jia["closed_rate_pct"] == round(1 * 100.0 / 3, 2), "甲院 1/3"
    yi = client.get("/api/performance/improvement-stats",
                    headers=world["乙"]["op"]).json()
    assert yi["closed_rate_pct"] == 0.0, "乙院一条都没闭环"
