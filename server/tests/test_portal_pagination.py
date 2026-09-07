"""P2-8 第二批：居民端两个 portal 模块 23 个列表端点切 `deps.paginate`。

**切法与第一批（billing）一致，是兼容增强**：每个端点的 `limit` 默认值取它原来
那个硬编码值，所以不带参数调用时第一页与切之前逐条相同；新增的只有 `offset`/`limit`
两个可选查询参数与一个 `X-Total-Count` 响应头。既有 146 条 portal 用例一条没改就全绿，
这是"没改行为"的第一重证据；本文件补的是那些**切之前根本做不到**的事。

这一批有四件事不是照抄第一批就能过的，各由本文件的用例钉住：

1. **四处排序不是全序，补尾键是切分页的前提**（`/me/slots`、spd 的 tasks /
   followups / revisits）。它们按 `String(10)` 的日期串排，其中三处 `default=""`
   ——同日/空串并列是常态。OFFSET/LIMIT 在并列行上会重复+漏行，等于拿「静默少返回」
   换「静默重复+漏行」，是往回走。**这条 SQLite 照不出来**（按 rowid 稳定返回），
   所以判据做成了静态闸门 `test_pagination_sort_stability.py`，本文件只钉"补了"。
2. **空名单那条早退要自己补 `X-Total-Count: 0`**。`/me/appointments` 在账户既没
   实名绑定又没代管成员时 `return []`，这条路径不经过 `paginate`；不补就成了
   "同一个端点有时带头、有时不带"，调用方没法照一种写法处理。
3. **两个免登录端点的上限压回原值**（`/health-articles` 与 spd 的 `/scales`，
   `max_limit=50`）。`paginate` 默认 `max_limit=500`，照默认切等于把匿名调用者
   单次可取的量抬高十倍——翻页给的是可达性，不是吞吐。
4. **`/me/admissions/{id}/bill` 修的是另一种缺陷**：它把明细 `.limit(500)` 之后拿
   这批被截断的行去算 `total_amount`/`by_category`，**账单合计会少算**。
   少显示几行只是难用，给患者看的合计算错是另一回事。现在汇总对全部明细算，
   明细列表另走分页。下面 `test_住院费用合计不再被明细上限截断` 造了 520 条明细，
   在修之前的实现上会算成 500 条的和。
"""
import pytest

from app.database import SessionLocal
from app.models import (
    Admission,
    Appointment,
    AppointmentSlot,
    Bed,
    BillDetail,
    ChargeItem,
    HealthArticle,
    Organization,
    Patient,
    ResidentAccount,
    Settlement,
    Ward,
)


@pytest.fixture(scope="module")
def seeded(client):
    """一份专门照分页的种子：号源 7 条**同日同时段**（并列行），账单 6 条，
    住院明细 520 条（超过原来 500 的硬上限），健康宣教 60 篇（超过原来 50 的上限）。"""
    with SessionLocal() as db:
        org = Organization(name="分页居民端医院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        me = Patient(ehc_no="EHC-PG-001", name="分页居民", id_card="330188199003031234",
                     gender="male", birth_date="1990-03-03", phone="13911203001")
        lonely = Patient(ehc_no="EHC-PG-002", name="无预约居民",
                         id_card="330188199004041234", gender="female",
                         birth_date="1990-04-04", phone="13911203002")
        db.add_all([me, lonely])
        db.flush()
        # wechat_openid 是可空唯一列，两个账户都留 None（空串会撞唯一索引）
        db.add(ResidentAccount(phone="13911203001", patient_id=me.id, nickname="分页",
                               status="active"))
        db.add(ResidentAccount(phone="13911203002", patient_id=lonely.id, nickname="无",
                               status="active"))

        # 号源：7 条**排序键完全并列**（同一天同一时段，只有资源名不同）——
        # 正是补 id 尾键要防的那种形状。
        for i in range(7):
            db.add(AppointmentSlot(org_id=org.id, resource_type="doctor",
                                   resource_name=f"并列科室{i}", slot_date="2026-10-01",
                                   slot_time="09:00", capacity=5, booked=0))
        # 账单 6 条
        for i in range(6):
            db.add(Settlement(patient_id=me.id, org_id=org.id, bill_type="outpatient",
                              total_amount=100 + i, insurance_pay=60, self_pay=40 + i,
                              created_by=1))
        # 住院 + 520 条明细：每条 1.00 元，合计应当是 520.00
        ward = Ward(org_id=org.id, name="分页病区")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="PG-01", status="occupied")
        db.add(bed)
        db.flush()
        adm = Admission(patient_id=me.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id,
                        doctor_name="分页医生", diagnosis_name="观察",
                        status="in_hospital", created_by=1)
        db.add(adm)
        db.flush()
        db.add(ChargeItem(code="PG-DRUG", name="分页药", category="drug",
                          price=1, active=True))
        db.add_all([
            BillDetail(patient_id=me.id, admission_id=adm.id, item_code="PG-DRUG",
                       item_name="分页药", unit_price=1, quantity=1, amount=1, created_by=1)
            for _ in range(520)
        ])
        # 健康宣教 60 篇（免登录端点，原上限 50）
        db.add_all([
            HealthArticle(title=f"分页宣教{i}", category="chronic", content="正文",
                          status="published")
            for i in range(60)
        ])
        db.commit()
        return {"admission": adm.id, "org": org.id, "me": me.id}


def _token(client, phone: str) -> dict:
    code = client.post("/api/portal/auth/sms/code",
                       json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login",
                        json={"phone": phone, "code": code}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def auth(client, seeded):
    return _token(client, "13911203001")


@pytest.fixture(scope="module")
def lonely_auth(client, seeded):
    """既没有额外预约、也没有代管成员的账户——用来走 `/me/appointments` 的早退分支。"""
    return _token(client, "13911203002")


# ---------------------------------------------------------------- 总数头
MIGRATED_PATHS = [
    "/api/portal/me/consents",
    "/api/portal/me/corrections",
    "/api/portal/me/slots",
    "/api/portal/me/appointments",
    "/api/portal/me/bills",
    "/api/portal/me/referrals",
    "/api/portal/me/admissions",
    "/api/portal/me/surgeries",
    "/api/portal/spd/service-applies",
    "/api/portal/spd/tasks",
    "/api/portal/spd/followups",
    "/api/portal/spd/interventions",
    "/api/portal/spd/edu",
    "/api/portal/spd/revisits",
    "/api/portal/spd/assessments",
    "/api/portal/spd/referrals",
    "/api/portal/spd/consults",
]


@pytest.mark.parametrize("path", MIGRATED_PATHS)
def test_切过的端点都带上了总数头(client, auth, seeded, path):
    """一个都不许漏——漏掉的那个就是下一次"列表少了一半没人发现"。"""
    resp = client.get(path, headers=auth)
    assert resp.status_code == 200, resp.text
    assert "X-Total-Count" in resp.headers, f"{path} 没带 X-Total-Count"


@pytest.mark.parametrize("path", ["/api/portal/health-articles", "/api/portal/price-list",
                                  "/api/portal/spd/scales"])
def test_三个免登录端点也带总数头(client, seeded, path):
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    assert "X-Total-Count" in resp.headers


def test_信封型响应的总数说的是items(client, auth, seeded):
    """押金与住院费用清单的 response_model 是信封对象，头描述的是里面那个列表。"""
    for path in (f"/api/portal/me/admissions/{seeded['admission']}/bill",
                 f"/api/portal/me/deposits?admission_id={seeded['admission']}"):
        resp = client.get(path, headers=auth)
        assert resp.status_code == 200, resp.text
        assert "X-Total-Count" in resp.headers, path


# ---------------------------------------------------------------- 早退分支
def test_空名单早退也要带总数头(client, lonely_auth, seeded):
    """`/me/appointments` 在没有可见患者时直接 return []，不经过 paginate。

    不补头就成了"同一个端点有时带头、有时不带"——调用方要么写两套分支，
    要么把"没有头"误当成"这个接口不支持分页"。
    """
    resp = client.get("/api/portal/me/appointments", headers=lonely_auth)
    assert resp.status_code == 200
    assert resp.json() == []
    assert resp.headers.get("X-Total-Count") == "0"


# ---------------------------------------------------------------- 翻页
def test_翻页不重不漏(client, auth, seeded):
    total = int(client.get("/api/portal/me/bills", headers=auth)
                .headers["X-Total-Count"])
    assert total >= 6
    seen, offset = [], 0
    while offset < total:
        rows = client.get("/api/portal/me/bills", headers=auth,
                          params={"offset": offset, "limit": 2}).json()
        assert rows, "翻页翻到空页说明 offset 没生效"
        seen.extend(r["id"] for r in rows)
        offset += 2
    assert len(seen) == total == len(set(seen)), "翻页结果有重复或缺漏"


def test_排序键并列时翻页仍不重不漏(client, auth, seeded):
    """号源那 7 条 `(slot_date, slot_time)` 完全相同——正是补 id 尾键要防的形状。

    诚实边界：**SQLite 上这条即使不补尾键也会绿**（并列行按 rowid 稳定返回），
    真正把这件事钉死的是静态闸门 `test_pagination_sort_stability.py`。
    这条留在这里是行为侧的下限：至少在能跑的环境里，翻页确实不重不漏。
    """
    total = int(client.get("/api/portal/me/slots", headers=auth)
                .headers["X-Total-Count"])
    seen, offset = [], 0
    while offset < total:
        rows = client.get("/api/portal/me/slots", headers=auth,
                          params={"offset": offset, "limit": 3}).json()
        assert rows
        seen.extend(r["id"] for r in rows)
        offset += 3
    assert len(seen) == total == len(set(seen))


def test_不带参数时第一页与切之前一样(client, auth, seeded):
    """`limit` 默认值取的就是原来那个硬编码值，所以默认调用的结果不该变。"""
    rows = client.get("/api/portal/me/bills", headers=auth).json()
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True), "原实现是 Settlement.id.desc()，顺序不该变"
    assert len(rows) <= 50, "原硬编码上限是 50，默认调用不该一次给出更多"


# ---------------------------------------------------------------- 免登录端点的上限
def test_免登录端点的单次上限压回原值(client, seeded):
    """`paginate` 默认 max_limit=500；这两个端点显式压回 50。

    切分页的目的是让第 51 篇之后**翻得到**，不是把匿名调用者单次可取的量抬高十倍。
    宣教文章的响应体还带全文 `content`。
    """
    resp = client.get("/api/portal/health-articles", params={"limit": 500})
    assert len(resp.json()) == 50, "免登录端点的单次上限被抬高了"
    assert int(resp.headers["X-Total-Count"]) >= 60, "总数应当照实说，压的是单页大小"
    # 翻得到第 51 篇：这才是切分页换来的东西
    page2 = client.get("/api/portal/health-articles", params={"offset": 50, "limit": 50})
    assert page2.json(), "第 51 篇之后翻不到，等于没切"
    assert client.get("/api/portal/spd/scales", params={"limit": 500}).status_code == 200


# ---------------------------------------------------------------- 合计不再被截断
def test_住院费用合计不再被明细上限截断(client, auth, seeded):
    """本批修的真缺陷：原实现拿被 `.limit(500)` 截断的明细去算合计。

    种子造了 **520** 条每条 1.00 元的明细，正确合计是 520.00。
    修之前那条 `round(sum(d.amount for d in details), 2)` 吃的是截断后的 500 条，
    会算成 500.00——**页面上"合计 ¥500.00"和真的就是 500 长得一模一样**，
    患者与收费员都发现不了。分类小计 `by_category` 同理。
    """
    resp = client.get(f"/api/portal/me/admissions/{seeded['admission']}/bill", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_amount"] == 520, f"合计被明细上限截断了：{body['total_amount']}"
    assert body["by_category"]["drug"] == 520, body["by_category"]
    # 明细列表本身仍按原上限 500 分页，且总数照实说
    assert len(body["items"]) == 500
    assert int(resp.headers["X-Total-Count"]) == 520
    # 翻得到第 501 条——这是切分页换来的
    page2 = client.get(f"/api/portal/me/admissions/{seeded['admission']}/bill",
                       headers=auth, params={"offset": 500, "limit": 500})
    assert len(page2.json()["items"]) == 20
    assert page2.json()["total_amount"] == 520, "翻页不该改变合计——合计说的是整次住院"
