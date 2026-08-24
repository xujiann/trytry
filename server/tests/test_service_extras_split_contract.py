"""ADR-0006 收官批的**响应契约**：`service_extras` 拆解后落到七个模块的 20 个端点。

取证主要靠套件级字节捕获（`tests/capture_plugin.py`）：加契约前后各跑一遍全套件，
逐 (方法,路径,状态) 比对，**落在这 20 个端点内的差异 0 处**。

本文件补的是捕获**证明不了**的两类：

1. `GET /api/surveys` 一次都没被任何用例跑到——前后都没记录，比对显示"一致"
   只是因为没证据。
2. `GET /api/cssd/requests` 跑到了，但那一次是**空列表**——空集钉不住任何字段。

另外把两处真正需要判断的地方钉死，它们都是"照读起来顺眼的写法就会改字节"：

- `survey_stats` 的**字段顺序**：handler 建的是
  `{target_type, count, total, distribution, negative}`，随后 `pop("count")`、
  `pop("total")`、再重新赋 `count`——`count` 因此被挪到了 `distribution` 与
  `negative` **之后**。序列化按模型声明顺序走，排成"顺眼"的顺序就是改字节。
- `ExamResource.price` 是 `Money`（Numeric）列：整数价读回来是 int。捕获里实测
  到的就是 `"price":240`，声明成 float 会让公示页的「240 元」变「240.0 元」。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Organization, Patient, User
from app.security import hash_password


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        org = Organization(name="拆解契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        db.add(User(username="splitadmin", password_hash=hash_password("Split-adm-2026!"),
                    full_name="拆解管理员", role="admin", org_id=org.id))
        p1 = Patient(ehc_no="EHC-SP-001", name="评价患者甲", id_card="330166199001011234",
                     gender="male", birth_date="1990-01-01")
        p2 = Patient(ehc_no="EHC-SP-002", name="评价患者乙", id_card="330166199202022345",
                     gender="female", birth_date="1992-02-02")
        db.add_all([p1, p2])
        db.commit()
        return {"org": org.id, "p1": p1.id, "p2": p2.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "splitadmin",
                              "password": "Split-adm-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------- 捕获盖不到的两处
def test_满意度明细列表的键集合(client, auth, seeded):
    """`GET /api/surveys` 此前**一次都没被任何用例调过**——套件级捕获对它
    前后都没有记录，比对显示"一致"不是证据，是没证据。"""
    for score, patient, comment in [(5, seeded["p1"], "很满意"), (2, seeded["p2"], "等太久")]:
        r = client.post("/api/surveys", headers=auth,
                        json={"target_type": "encounter", "target_id": 1,
                              "patient_id": patient, "score": score, "comment": comment})
        assert r.status_code == 201 and set(r.json()) == {"id"}

    rows = client.get("/api/surveys", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "target_type", "target_id", "patient_id",
                                     "patient_name", "score", "comment", "date"}
    # patient_name 是 handler 一次查全部姓名回填的，不是列——空串意味着患者被删
    assert {r["patient_name"] for r in rows} == {"评价患者甲", "评价患者乙"}
    # max_score=2 即差评清单
    bad = client.get("/api/surveys", headers=auth, params={"max_score": 2}).json()
    assert [r["score"] for r in bad] == [2]


def test_消毒申领列表有数据时的键集合(client, auth, seeded):
    """捕获里这条只跑到过空列表。空集钉不住任何字段——`[]` 在任何契约下都合法。"""
    created = client.post("/api/cssd/requests", headers=auth,
                          json={"org_id": seeded["org"], "item_name": "手术器械包",
                                "quantity": 3})
    assert created.status_code == 201 and set(created.json()) == {"id", "status"}

    rows = client.get("/api/cssd/requests", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "org_id", "item_name", "quantity", "status",
                                     "batch_id"}
    # 未响应前没有批次：null，不是 0
    assert rows[0]["batch_id"] is None and rows[0]["status"] == "requested"


# ------------------------------------------------- 两处会改字节的判断
def test_满意度统计的字段顺序照handler实际出键排(client, auth, seeded):
    """本文件最要紧的一条。

    handler 先建 `{target_type, count, total, distribution, negative}`，再
    `pop("count")` / `pop("total")`、然后重新赋 `count` —— dict 是插入序，
    `count` 于是跑到了 `distribution` 与 `negative` 后面。模型字段顺序若按
    "读起来顺眼"排（target_type, count, ...），序列化出来就与原字节不同。
    """
    rows = client.get("/api/surveys/stats", headers=auth,
                      params={"target_type": "encounter"}).json()
    assert len(rows) == 1
    assert list(rows[0]) == ["target_type", "distribution", "negative", "count",
                             "avg_score", "negative_rate_pct"]
    stats = rows[0]
    # 分布恒五档，缺档补 0——统计要的是分布形状，缺档就画不出直方图
    assert list(stats["distribution"]) == ["1", "2", "3", "4", "5"]
    assert stats["distribution"] == {"1": 0, "2": 1, "3": 0, "4": 0, "5": 1}
    assert stats["count"] == 2 and stats["negative"] == 1
    assert stats["avg_score"] == 3.5 and stats["negative_rate_pct"] == 50.0


def test_检查资源的整数价仍是int(client, auth, seeded):
    """`ExamResource.price` 是 Money（Numeric）列。整数价读回来是 int——
    声明成 float 会把公示页的「240 元」变成「240.0 元」。"""
    created = client.post("/api/exams/resources", headers=auth,
                          json={"org_id": seeded["org"], "center_type": "imaging",
                                "item_name": "胸部CT", "device": "64排", "price": 240,
                                "duration_min": 10, "notes": "去除金属物品"})
    assert created.status_code == 201
    # 新建只回三个键，与列表的八个键不同形
    assert set(created.json()) == {"id", "center_type", "item_name"}

    client.post("/api/exams/resources", headers=auth,
                json={"org_id": seeded["org"], "center_type": "lab",
                      "item_name": "血常规", "price": 12.5})
    rows = {r["item_name"]: r for r in
            client.get("/api/exams/resources", headers=auth).json()}
    assert set(rows["胸部CT"]) == {"id", "org_id", "center_type", "item_name", "device",
                                   "price", "duration_min", "notes"}
    assert rows["胸部CT"]["price"] == 240 and isinstance(rows["胸部CT"]["price"], int)
    assert rows["血常规"]["price"] == 12.5


# ------------------------------------------------- 其余端点的键集合
def test_报告模板与专家与黑名单与宣教的形状(client, auth, seeded):
    tpl = client.post("/api/exams/templates", headers=auth,
                      json={"center_type": "imaging", "name": "CT模板", "content": "正文"})
    assert tpl.status_code == 201 and set(tpl.json()) == {"id"}
    templates = client.get("/api/exams/templates", headers=auth).json()
    assert set(templates[0]) == {"id", "center_type", "name", "content"}

    expert = client.post("/api/consultations/experts", headers=auth,
                         json={"name": "王专家", "org_id": seeded["org"],
                               "specialty": "心内"})
    assert expert.status_code == 201 and set(expert.json()) == {"id"}
    experts = client.get("/api/consultations/experts", headers=auth).json()
    assert set(experts[0]) == {"id", "name", "org_id", "specialty", "available"}

    added = client.post("/api/appointments/blacklist", headers=auth,
                        json={"patient_id": seeded["p1"], "reason": "两次爽约"})
    assert added.status_code == 201 and set(added.json()) == {"id", "domain"}
    listed = client.get("/api/appointments/blacklist", headers=auth).json()
    assert set(listed[0]) == {"id", "domain", "domain_name", "patient_id", "reason"}
    assert listed[0]["domain_name"] == "预约爽约"   # 中文名服务端折算
    removed = client.request("DELETE", f"/api/appointments/blacklist/{seeded['p1']}",
                             headers=auth)
    assert removed.status_code == 200 and removed.json() == {"removed": True,
                                                             "domain": "appointment"}

    article = client.post("/api/education/articles", headers=auth,
                          json={"title": "高血压防治", "content": "少盐"})
    assert article.status_code == 201 and set(article.json()) == {"id", "status"}
    published = client.post(f"/api/education/articles/{article.json()['id']}/publish",
                            headers=auth)
    # 建稿与发布同形，共用一个模型
    assert set(published.json()) == set(article.json())
    assert published.json()["status"] == "published"


def test_导诊无命中时回落全科门诊(client, auth):
    """一条都没匹配上时不返回空列表——让居民永远拿得到一个可去的地方。"""
    hit = client.post("/api/triage/suggest", headers=auth, json=["胸痛", "心悸"]).json()
    assert set(hit) == {"recommendations", "emergency_hint"}
    assert set(hit["recommendations"][0]) == {"department", "matched", "urgent"}
    assert hit["recommendations"][0]["department"] == "心血管内科"
    assert hit["emergency_hint"] is True

    miss = client.post("/api/triage/suggest", headers=auth, json=["查无此症状"]).json()
    assert miss["recommendations"] == [{"department": "全科门诊", "matched": [],
                                        "urgent": False}]
    assert miss["emergency_hint"] is False
