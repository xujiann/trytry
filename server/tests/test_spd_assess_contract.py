"""慢专病考核域（`spd/assess`）的**响应契约**：24 个端点里的 23 个。

取证靠套件级字节捕获（前后各跑一遍全套件，1857 个组合逐项比对）。本文件补
捕获**证明不了**的部分，并钉住两处需要判断的地方。

## 有一个端点**刻意不加契约**：`GET /api/spd/scores-analysis`

它两个分支的键集合与**键顺序都不同**：

    空数据 : total, distribution, top_deductions, average          （4 键）
    有数据 : total, average, distribution, top_deductions, ranking （5 键）

Pydantic 按模型声明顺序序列化，**单个模型最多只能满足一个分支**（实测确认：
顺序对齐非空分支，空分支就变；反之亦然）。三条路里只有一条诚实：

- 用 `dict[str, Any]` → 那是治理文档点名的"拿宽字典逃避契约"，且这里没有任何
  东西自描述形状（不像 `metrics/drilldown` 有同响应的 `fields`）；
- 改 handler 让两分支一致 → **是行为变更**，不该夹在契约批次里；
- **留在欠账里并写明原因** ← 选这个。

它底下是个真问题：空数据时 `ranking` 键整个消失，而"这个周期还没打分"正是最
可能打开该页面的时刻，前端 `data.ranking.map(...)` 会 TypeError。目前无前端
消费方，故不算在燃的火，已单独登记进 ROADMAP。`test_scores_analysis_两分支形状不一致`
把这个事实钉住——哪天有人把两分支统一了，那条会红，提醒他顺手把契约补上。
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
        org = Organization(name="考核契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        user = User(username="assessct", password_hash=hash_password("Assess-ct-2026!"),
                    full_name="考核管理员", role="admin", org_id=org.id)
        db.add(user)
        db.flush()
        db.add(S.SpdPointRule(code="AC-SIGNIN", name="每日签到", event="signin",
                              points=5, daily_limit=1, active=True))
        db.add(S.SpdGoods(code="AC-CUP", name="保温杯", points=50, stock=10,
                          image_url="", active=True))
        acct = S.SpdPointAccount(user_id=user.id, org_id=org.id, balance=100,
                                 earned=100, used=0)
        db.add(acct)
        # workload 需要真任务才有行——空列表钉不住字段顺序，
        # 而字段顺序正是这个端点要守的东西（object_name/completion_rate 是后追加的）
        db.add_all([
            S.SpdTask(program_code="AC", patient_id=1, task_type="followup",
                      title="已完成任务", org_id=org.id, status="done",
                      due_date="2026-08-10"),
            S.SpdTask(program_code="AC", patient_id=1, task_type="report",
                      title="未完成任务", org_id=org.id, status="pending",
                      due_date="2026-08-20"),
        ])
        db.commit()
        return {"org": org.id, "user": user.id, "account": acct.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "assessct",
                              "password": "Assess-ct-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


B = "/api/spd"


# ------------------------------------------------- 捕获里零覆盖的四个
def test_四个从没被跑过的列表端点(client, auth, seeded):
    """`/goods`、`/point-rules`、`/point-accounts`、`/redeems` 在套件级捕获里
    **一次 key 都没有**——前后都没记录，比对显示"一致"不是证据，是没证据。
    这里逐个调起来并钉住键集合（都造了数据，空集钉不住字段）。"""
    goods = client.get(f"{B}/goods", headers=auth).json()
    assert goods and set(goods[0]) == {"id", "code", "name", "points", "stock",
                                       "image_url", "active"}

    rules = client.get(f"{B}/point-rules", headers=auth).json()
    assert rules and set(rules[0]) == {"id", "code", "name", "event", "points",
                                       "daily_limit", "active"}

    accounts = client.get(f"{B}/point-accounts", headers=auth).json()
    assert accounts and set(accounts[0]) == {"id", "user_id", "user_name", "org_id",
                                             "balance", "earned", "used"}
    assert accounts[0]["user_name"] == "考核管理员"

    redeemed = client.post(f"{B}/redeems", headers=auth,
                           json={"goods_id": goods[0]["id"]})
    assert redeemed.status_code == 201
    assert set(redeemed.json()) == {"id", "verify_code", "balance"}
    # 六位随机核销码——两次运行必然不同，逐字节比对时要归一化
    assert len(redeemed.json()["verify_code"]) == 6
    assert redeemed.json()["balance"] == 50   # 100 - 50

    rows = client.get(f"{B}/redeems", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "goods_id", "goods_name", "points",
                                     "verify_code", "status", "created_at",
                                     "verified_at"}
    # 未核销时是空串（handler 已折 None），不是 null
    assert rows[0]["verified_at"] == "" and rows[0]["status"] == "pending"


# ------------------------------------------------- JSON 列里的数字
def test_方案权重取自JSON列整数就该是整数(client, auth):
    """`IndicatorPlanRefOut.weight` 来自方案的 **JSON 列** `items`，不是数据库
    的 Float 列——里面存 int 还是 float 由写入方决定。声明成 float 会把存进去的
    整数 `100` 变成 `100.0`，逐字节比对当场抓到过。

    这是 Money 陷阱的同一形状，只是来源从 Numeric 列换成了 JSON 值：
    **判据仍是"实际存的是什么"，不是字段名看着像不像小数**。
    """
    ind = client.post(f"{B}/indicators", headers=auth,
                      json={"code": "ac_ratio", "name": "比例指标", "object_type": "org",
                            "data_source": "task", "formula": "done / total * 100",
                            "score_rule": {"type": "ratio", "full": 100, "target": 100},
                            "weight": 1})
    assert ind.status_code == 201, ind.text[:300]
    plan = client.post(f"{B}/assess-plans", headers=auth,
                       json={"code": "ac_plan", "name": "契约方案", "level": "township",
                             "items": [{"indicator_code": "ac_ratio", "weight": 100}]})
    assert plan.status_code == 201, plan.text[:300]

    usage = client.get(f"{B}/indicators/{ind.json()['id']}/usage", headers=auth).json()
    assert set(usage) == {"indicator", "plans", "used_by"}
    assert usage["used_by"] == 1
    w = usage["plans"][0]["weight"]
    assert w == 100 and isinstance(w, int), f"整数权重被改成了 {w!r}"


def test_未在方案里给权重时是null不是零(client, auth):
    """没设与设成 0 是两回事——方案 items 里没有 weight 键时应回 null。"""
    ind = client.post(f"{B}/indicators", headers=auth,
                      json={"code": "ac_noweight", "name": "无权重指标",
                            "object_type": "org", "data_source": "task",
                            "formula": "1", "weight": 1})
    client.post(f"{B}/assess-plans", headers=auth,
                json={"code": "ac_plan2", "name": "无权重方案", "level": "township",
                      "items": [{"indicator_code": "ac_noweight"}]})
    usage = client.get(f"{B}/indicators/{ind.json()['id']}/usage", headers=auth).json()
    assert usage["plans"][0]["weight"] is None


# ------------------------------------------------- 条件键
def test_没有积分账户时account_id整个键不出现(client, seeded, auth):
    """`/point-accounts/me` 的早返回分支只给 balance/earned/used/records 四个零值，
    没有 `account_id`。声明成可选字段会注入 `"account_id": null`——那会让客户端
    以为"有账户但 id 是空的"，而真实情况是根本没开户。"""
    with SessionLocal() as db:
        org = db.query(Organization).first()
        db.add(User(username="nopoints", password_hash=hash_password("Nopoint-2026!"),
                    full_name="无积分用户", role="doctor", org_id=org.id))
        db.commit()
    token = client.post("/api/auth/login",
                        json={"username": "nopoints",
                              "password": "Nopoint-2026!"}).json()["access_token"]
    body = client.get(f"{B}/point-accounts/me",
                      headers={"Authorization": f"Bearer {token}"}).json()
    # 用 list() 不用 set()：`account_id` 必须声明在**最前**，去掉它之后剩下的
    # 顺序才与早返回分支一致。只断言"键不在"是不够的——把 account_id 挪到中间，
    # 集合判等照样绿，字节却变了（变异验证实测到这一点，本条因此改成有序断言）。
    assert list(body) == ["balance", "earned", "used", "records"]
    assert "account_id" not in body
    assert body == {"balance": 0, "earned": 0, "used": 0, "records": []}

    # 有账户那条同样钉顺序：account_id 必须在最前
    # （`auth` 那个用户在 seeded 里签到过，已有账户）
    holder = client.get(f"{B}/point-accounts/me", headers=auth).json()
    assert list(holder) == ["account_id", "balance", "earned", "used", "records"]


# ------------------------------------------------- 刻意不加契约的那个
def test_scores_analysis_两分支形状不一致(client, auth):
    """把"为什么这个端点没有契约"钉成一条会红的事实。

    两个分支的键集合与顺序都不同，单个 Pydantic 模型无法同时匹配。哪天有人把
    两分支统一了（比如给空分支补上 `ranking: []` 并对齐顺序），这条会红——
    那正是提醒他"现在可以补契约了"的时机。
    """
    # 自建指标，不借别的用例造的数据——跨用例借数据会让本条单跑就红
    # （本仓库专门修过这一类，见 ROADMAP「测试隔离修复」）。
    client.post(f"{B}/indicators", headers=auth,
                json={"code": "ac_lonely", "name": "本条自用指标", "object_type": "org",
                      "data_source": "task", "formula": "1", "weight": 1})
    # 方案必须至少有一个指标，所以空分支要靠"该周期没打过分"来触发，
    # 而不是空方案——这恰恰是最常见的场景：方案刚配好、还没跑过评分。
    plan = client.post(f"{B}/assess-plans", headers=auth,
                       json={"code": "ac_empty", "name": "未评分方案", "level": "township",
                             "items": [{"indicator_code": "ac_lonely", "weight": 100}]})
    assert plan.status_code == 201, plan.text[:300]
    empty = client.get(f"{B}/scores-analysis", headers=auth,
                       params={"plan_id": plan.json()["id"], "period": "1999-01"}).json()
    assert list(empty) == ["total", "distribution", "top_deductions", "average"], (
        "空分支的键或顺序变了——若已与非空分支统一，请给该端点补上 response_model"
        "（见本文件 docstring）"
    )
    assert "ranking" not in empty


# ------------------------------------------------- 后追加键的排序陷阱
def test_工作量每行的字段顺序照handler实际出键排(client, auth):
    """handler 先建 `{object_id, total, done, by_type}`，之后才追加
    `object_name` 与 `completion_rate`——dict 是插入序，名字**不**紧跟 id。

    这条是补上来的：变异验证发现把三个字段排成"读起来顺眼"的顺序时，本文件
    原有的用例一条都没红（当时只有套件级字节比对能发现）。字节比对是一次性
    证明，用例才是长期守卫，两者都要有。
    """
    body = client.get(f"{B}/workload", headers=auth,
                      params={"period": "2026-08", "object_type": "org"}).json()
    assert list(body) == ["period", "object_type", "items"]
    # 不用 skip 兜底：seeded 里造了两条任务（一 done 一 pending），
    # 空列表在这里就是失败——永远 skip 的守卫等于没有守卫。
    assert body["items"], "seeded 应造出任务数据，空列表钉不住字段顺序"
    row = body["items"][0]
    assert list(row) == ["object_id", "total", "done", "by_type",
                         "object_name", "completion_rate"]
    assert row["total"] == 2 and row["done"] == 1
    assert row["completion_rate"] == 50.0
    # by_type 只在 done 分支累加，故只有已完成那一类
    assert row["by_type"] == {"followup": 1}
