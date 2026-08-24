"""慢专病人群域（`spd/population`）的**响应契约**：28 个端点。

这一批与前四批不同：**套件级字节捕获对半个模块证明不了任何事**。
治理前的基线捕获里，29 个端点有 **14 个一次 2xx 都没有**（只有 403 记录）——
整片 groups / candidates / recalls / lifecycle-events 的正常路径在全套件里
从没跑过。对这些端点，"前后一致"是没证据，不是有证据：

    POST   /candidates/distribute        POST   /candidates/{id}/claim
    POST   /candidates/{id}/status       PATCH  /enrollments/{id}
    POST   /groups                       GET    /groups
    POST   /groups/{id}/members          GET    /groups/{id}/members
    DELETE /groups/{id}/members/{pid}    GET    /lifecycle-events
    POST   /recalls/{id}/progress        GET    /recalls
    POST   /package-bindings/{id}/unbind GET    /package-bindings/{id}/usages

所以本文件是这个模块的**主要**取证手段，不是捕获的补充。

## 两个真出过的错（都是"猜形状"）

1. `closed` 我按名字猜成计数 `int`，实际是 `close_open_work()` 的**四项明细
   字典**。加上契约当场 500。
2. `AutoScreenOut.rule_version` 我猜成 int，实际 `SpdProgram.version` 是
   `String(16)`（`"v1"`）——这条在写用例前查列类型时抓到，没等到跑挂。

两次都是同一个毛病：**按字段名猜，不查写入方**。判据只有列类型与实际返回值。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Encounter, Organization, Patient, User
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
        org = Organization(name="人群契约院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        user = User(username="popct", password_hash=hash_password("Pop-ct-2026!"),
                    full_name="人群主任", role="director", org_id=org.id)
        patient = Patient(ehc_no="EHC-POP-001", name="人群患者", gender="male",
                          birth_date="1968-05-06", id_card="330102196805060022",
                          phone="13700000022")
        db.add_all([user, patient])
        db.flush()
        # 没有就诊关系，涉患者的端点会被 visibility 挡成 403（§8），不是接口缺陷
        db.add(Encounter(patient_id=patient.id, org_id=org.id, doctor_name="人群主任",
                         encounter_type="outpatient", diagnosis_code="I10",
                         diagnosis_name="高血压", summary="首诊"))
        db.add(S.SpdProgram(code="POP", name="人群契约病种", version="v1",
                            include_rules=[], exclude_rules=[]))
        # 服务包配置里的次数键是 **times**，`_bind_package` 才把它映射成绑定的
        # `total`（见该函数）。这里写成 total 会让绑定的次数变 0，剩余也是 0。
        db.add(S.SpdServicePackage(code="POP-PKG", name="基础包", price=50,
                                   items=[{"code": "bp", "name": "血压测量",
                                           "times": 4, "price": 5}]))
        db.commit()
        return {"org": org.id, "user": user.id, "patient": patient.id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "popct",
                              "password": "Pop-ct-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


B = "/api/spd"


# ------------------------------------------------- 真出过的错之一
def test_生命周期的closed是四项明细不是计数(client, auth, seeded):
    """`closed` 来自 `service.close_open_work()`，是
    `{tasks, instances, interventions, revisits}` 四项**明细**，不是一个计数。
    声明成 int 会让这个端点直接 500（已实测），不是字节漂移。

    同时钉住**两条分支的键顺序**：`resume` 走早返回只有两个键，其余事件四个键。
    `event_id`/`pending_confirm` 是夹在**中间**的条件键——去掉它们剩下的顺序
    恰好是早返回分支，所以一个模型 + `exclude_unset` 能同时满足。
    把 `closed` 声明到 `enrollment` 后面（而不是最末），早返回分支就会变字节。
    """
    enroll = client.post(f"{B}/enrollments", headers=auth,
                         json={"patient_id": seeded["patient"], "program_code": "POP",
                               "org_id": seeded["org"], "risk_level": "mid",
                               "sign_date": "2026-08-01"})
    assert enroll.status_code == 201, enroll.text[:400]
    eid = enroll.json()["id"]

    # 建一条未完成任务，让 close_open_work 真的有东西可关——全零钉不住形状
    with SessionLocal() as db:
        db.add(S.SpdTask(program_code="POP", patient_id=seeded["patient"],
                         enrollment_id=eid, task_type="followup", title="待办",
                         org_id=seeded["org"], status="pending", due_date="2026-09-01"))
        db.commit()

    dead = client.post(f"{B}/enrollments/{eid}/lifecycle", headers=auth,
                       json={"event": "death", "reason": "登记死亡"})
    assert dead.status_code == 200, dead.text[:400]
    body = dead.json()
    assert list(body) == ["enrollment", "event_id", "pending_confirm", "closed"]
    assert body["closed"] == {"tasks": 1, "instances": 0,
                              "interventions": 0, "revisits": 0}, (
        f"closed 是四项明细字典，不是计数：{body['closed']!r}"
    )
    assert isinstance(body["event_id"], int) and body["pending_confirm"] is False

    # resume 分支：只有两个键，closed 恒为空字典。
    # 换一条 **recalled** 的档案——死亡档案不可恢复（409「已登记死亡的档案不可
    # 恢复管理」），拿死亡那条测 resume 测不到早返回分支。
    rid = _active_enrollment(client, auth, seeded, "2026-08-04", status="recalled")
    resumed = client.post(f"{B}/enrollments/{rid}/lifecycle", headers=auth,
                          json={"event": "resume", "reason": "恢复管理"})
    assert resumed.status_code == 200, resumed.text[:400]
    assert list(resumed.json()) == ["enrollment", "closed"]
    assert resumed.json()["closed"] == {}
    assert "event_id" not in resumed.json()


# ------------------------------------------------- 真出过的错之二
def test_自动识别回的版本号是字符串(client, auth, seeded):
    """`rule_version` 取自 `SpdProgram.version`，是 `String(16)`（如 `"v1"`），
    **不是数字**。按字段名猜成 int 就错了——判据是列类型。"""
    with SessionLocal() as db:
        prog = db.query(S.SpdProgram).filter(S.SpdProgram.code == "POP").first()
        prog.include_rules = [{"field": "diagnosis", "op": "contains", "value": "I10"}]
        db.commit()
    run = client.post(f"{B}/screenings/auto-run", headers=auth,
                      json={"program_code": "POP", "org_id": seeded["org"]})
    assert run.status_code == 200, run.text[:400]
    assert list(run.json()) == ["scanned", "suspect", "excluded", "normal", "rule_version"]
    v = run.json()["rule_version"]
    assert v == "v1" and isinstance(v, str), f"版本号被当成数字了：{v!r}"


# ------------------------------------------------- 三组条件键的键名各不相同
def test_三组患者摘要键名不同不能共用模型(client, auth, seeded):
    """本模块有三种把患者摘要拼进响应的写法，**落进响应的键名不一样**：

        `_candidate_out`  → patient_name, gender, birth_date, phone
        `_enroll_out`     → patient_name, gender, birth_date, phone, ehc_no
        `**brief` 字典展开 → name, gender, birth_date, ehc_no, phone   ← 是 name

    第三种出现在 `/groups/{id}/members` 与 `/service-applies`：handler 直接
    `**(briefs.get(pid) or {})`，展开的是 `_patient_brief` 的原始键名。
    共用一个模型会让其中两组的键名整个错掉。
    """
    cid, pid = _candidate(seeded)
    cands = [c for c in client.get(f"{B}/candidates", headers=auth).json()
             if c["id"] == cid]
    assert cands, "自造的目标池记录应列得出来"
    assert list(cands[0])[-4:] == ["patient_name", "gender", "birth_date", "phone"]
    assert "name" not in cands[0] and "ehc_no" not in cands[0]

    eid = _active_enrollment(client, auth, seeded, "2026-08-06")
    enrolls = [e for e in client.get(f"{B}/enrollments", headers=auth,
                                     params={"status": ""}).json() if e["id"] == eid]
    assert enrolls
    assert list(enrolls[0])[-5:] == ["patient_name", "gender", "birth_date",
                                     "phone", "ehc_no"]

    group = client.post(f"{B}/groups", headers=auth,
                        json={"name": "契约分组", "scope": "team"})
    assert group.status_code == 201, group.text[:400]
    assert list(group.json()) == ["id", "name", "scope", "member_count"]
    gid = group.json()["id"]

    added = client.post(f"{B}/groups/{gid}/members", headers=auth,
                        json={"patient_ids": [seeded["patient"]]})
    assert added.status_code == 200, added.text[:400]
    assert added.json() == {"added": 1, "total": 1}

    members = client.get(f"{B}/groups/{gid}/members", headers=auth).json()
    assert members, "刚加的成员应列得出来——空表钉不住字典展开的那五个键"
    assert list(members[0]) == ["id", "patient_id", "added_at",
                                "name", "gender", "birth_date", "ehc_no", "phone"], (
        "字典展开的是 _patient_brief 的原始键名 name，不是 patient_name"
    )
    assert members[0]["name"] == "人群患者"

    gone = client.delete(f"{B}/groups/{gid}/members/{seeded['patient']}", headers=auth)
    assert gone.status_code == 204 and gone.content == b""
    assert client.get(f"{B}/groups/{gid}/members", headers=auth).json() == []


# ------------------------------------------------- 分组列表
def test_分组列表的成员数与更新时间(client, auth, seeded):
    made = client.post(f"{B}/groups", headers=auth,
                       json={"name": "列表用分组", "scope": "dept", "dept": "全科"})
    assert made.status_code == 201, made.text[:400]
    gid = made.json()["id"]
    rows = [g for g in client.get(f"{B}/groups", headers=auth).json() if g["id"] == gid]
    assert rows
    assert list(rows[0]) == ["id", "name", "scope", "dept", "owner_user_id",
                             "auto_rule", "member_count", "updated_at"]
    # owner_user_id 是非空外键，不该声明成可空
    assert rows[0]["owner_user_id"] == seeded["user"]
    assert rows[0]["member_count"] == 0
    assert rows[0]["auto_rule"] == [] and rows[0]["dept"] == "全科"


# ------------------------------------------------- 目标池分发 / 认领 / 改状态
def test_目标池三个写端点(client, auth, seeded):
    """分发、认领、改状态在捕获里都只有 403，正常路径一次没跑过。"""
    cid, _pid = _candidate(seeded)

    dist = client.post(f"{B}/candidates/distribute", headers=auth,
                       json={"candidate_ids": [cid, 999999],
                             "assigned_user_id": seeded["user"]})
    assert dist.status_code == 200, dist.text[:400]
    # 不存在的 id 计入 not_found 而不是报错——批量分发不该被一条脏数据打断
    assert dist.json() == {"distributed": 1, "not_found": 1}

    claimed = client.post(f"{B}/candidates/{cid}/claim", headers=auth)
    assert claimed.status_code == 200, claimed.text[:400]
    body = claimed.json()
    assert list(body)[:13] == [
        "id", "patient_id", "program_code", "status", "source", "org_id", "team_id",
        "assigned_user_id", "risk_level", "reason", "matched_rules",
        "claimed_at", "created_at",
    ]
    # 认领后 claimed_at 才有值；单条端点不带 brief，四个条件键整个不出现
    assert body["claimed_at"] and "patient_name" not in body

    changed = client.post(f"{B}/candidates/{cid}/status", headers=auth,
                          json={"status": "excluded", "reason": "复核排除"})
    assert changed.status_code == 200, changed.text[:400]
    assert changed.json()["status"] == "excluded"
    assert changed.json()["reason"] == "复核排除"


# ------------------------------------------------- 服务包：Money 与 float 同处一行
def test_服务包价格是Money用量率是float(client, auth, seeded):
    """同一个响应里两个数值字段，**类型判据相反**：

    - `price` 取自 `SpdServicePackage.price`（**Money** 列），整数价读回来是
      `int`；包查不到时兜底字面量也是 int `0`。声明 float 会把「50 元」
      变「50.0 元」。
    - `usage_rate` 是 `round(used/total*100, 1)`，兜底 `0.0`，两边都是
      **float**——这里声明 float 才是原样。

    Python 里 `50 == 50.0` 为真，所以两条都要 `isinstance` 才咬得住。
    """
    eid = _active_enrollment(client, auth, seeded, "2026-08-02")
    bound = client.post(f"{B}/enrollments/{eid}/packages", headers=auth,
                        json={"package_id": _package_id()})
    assert bound.status_code == 201, bound.text[:400]
    body = bound.json()
    assert list(body) == ["id", "enrollment_id", "package_id", "package_name",
                          "price", "items", "status", "period_end", "bound_at",
                          "usage_rate", "remaining"]
    assert body["price"] == 50 and isinstance(body["price"], int), (
        f"Money 列的整数价被改成了 {body['price']!r}"
    )
    assert body["usage_rate"] == 0.0 and isinstance(body["usage_rate"], float), (
        f"用量率应是 float：{body['usage_rate']!r}"
    )
    assert body["remaining"] == 4

    bid = body["id"]
    used = client.post(f"{B}/package-bindings/{bid}/usages", headers=auth,
                       json={"item_code": "bp", "qty": 1})
    assert used.status_code == 201, used.text[:400]
    assert list(used.json()) == ["usage_id", "binding"]
    assert used.json()["binding"]["usage_rate"] == 25.0
    assert used.json()["binding"]["remaining"] == 3

    usages = client.get(f"{B}/package-bindings/{bid}/usages", headers=auth).json()
    assert usages
    assert list(usages[0]) == ["id", "item_code", "item_name", "qty", "price",
                               "note", "used_at"]
    # 流水的 price 同样是 Money 列
    assert usages[0]["price"] == 5 and isinstance(usages[0]["price"], int)

    unbound = client.post(f"{B}/package-bindings/{bid}/unbind", headers=auth)
    assert unbound.status_code == 200, unbound.text[:400]
    assert unbound.json()["status"] == "unbound"


def _package_id() -> int:
    with SessionLocal() as db:
        return db.query(S.SpdServicePackage).first().id


_SEQ = iter(range(100, 300))


def _candidate(seeded, status: str = "suspect") -> tuple[int, int]:
    """造一条目标池记录，连患者带就诊关系一起造。返回 (candidate_id, patient_id)。

    不借 auto-run 跑出来的那批——那要求本条排在 auto-run 之后，单跑就红。
    """
    n = next(_SEQ)
    with SessionLocal() as db:
        patient = Patient(ehc_no=f"EHC-POP-{n}", name=f"人群患者{n}", gender="male",
                          birth_date="1970-02-02", id_card=f"3301021970020200{n}",
                          phone=f"137000002{n}")
        db.add(patient)
        db.flush()
        db.add(Encounter(patient_id=patient.id, org_id=seeded["org"],
                         doctor_name="人群主任", encounter_type="outpatient",
                         diagnosis_code="I10", diagnosis_name="高血压", summary="首诊"))
        cand = S.SpdCandidate(patient_id=patient.id, program_code="POP", status=status,
                              source="screening", org_id=seeded["org"],
                              risk_level="mid", matched_rules=[], reason="用例造数")
        db.add(cand)
        db.commit()
        return cand.id, patient.id


def _active_enrollment(client, auth, seeded, sign_date: str,
                       status: str = "active") -> int:
    """造一条**确定是指定状态**的档案，并且**每次换一个患者**。

    两条约束逼出这个写法：
    1. `spd_enrollments` 在 `(patient_id, program_code)` 上有唯一键——同一个人
       在同一病种下只能有一条，复用 seeded 患者第二次就撞 IntegrityError。
    2. 前面的用例会把档案 death/recall 掉，复用别人造的那条会撞 409。
       用 `pytest.skip` 兜底是不行的：永远 skip 的守卫等于没有守卫。

    顺带补 `Encounter`——没有就诊关系，涉患者的端点会被 visibility 挡成 403。
    """
    n = next(_SEQ)
    with SessionLocal() as db:
        patient = Patient(ehc_no=f"EHC-POP-{n}", name=f"人群患者{n}", gender="female",
                          birth_date="1975-01-01", id_card=f"3301021975010100{n}",
                          phone=f"137000001{n}")
        db.add(patient)
        db.flush()
        db.add(Encounter(patient_id=patient.id, org_id=seeded["org"],
                         doctor_name="人群主任", encounter_type="outpatient",
                         diagnosis_code="I10", diagnosis_name="高血压", summary="首诊"))
        enrollment = S.SpdEnrollment(
            patient_id=patient.id, program_code="POP", org_id=seeded["org"],
            status=status, stage="stable", risk_level="mid", sign_date=sign_date,
        )
        db.add(enrollment)
        db.commit()
        return enrollment.id


# ------------------------------------------------- 生命周期事件列表与召回
def test_生命周期事件列表与召回流水(client, auth, seeded):
    """两个 GET 在捕获里零记录。`patient_id` 声明可空是有依据的——
    档案被物理删除时 `enrollments.get()` 落空，handler 回 None。

    本条**自己造事件**，不借前面用例留下的——跨用例借数据会让它单跑就红
    （本仓库专门修过这一类，见 ROADMAP「测试隔离修复」；这份文件初稿又犯了
    一次，实测单跑 `events == []`）。
    """
    eid = _active_enrollment(client, auth, seeded, "2026-08-05")
    recalled = client.post(f"{B}/enrollments/{eid}/lifecycle", headers=auth,
                           json={"event": "recall", "reason": "失联"})
    assert recalled.status_code == 200, recalled.text[:400]

    events = client.get(f"{B}/lifecycle-events", headers=auth).json()
    assert events, "刚登记的 recall 应该列得出来"
    mine = next(e for e in events if e["enrollment_id"] == eid)
    assert list(mine) == ["id", "enrollment_id", "event", "reason", "detail",
                          "target_org_id", "confirmed", "occurred_at",
                          "program_code", "patient_id", "patient_name",
                          "created_at"]
    assert mine["event"] == "recall" and mine["reason"] == "失联"
    assert mine["patient_name"].startswith("人群患者")
    # `confirmed = not cross_org`：只有**跨机构迁出**才要目标机构确认，
    # 本地事件（recall/death/exclude）落库即生效，恒 True。
    assert mine["confirmed"] is True

    # 跨机构迁出那条才是 False——两条分支都钉，否则这个字段等于没测
    other = Organization(name="迁入院", org_type="hospital", level="township")
    with SessionLocal() as db:
        db.add(other)
        db.commit()
        target_org = other.id
    mid = _active_enrollment(client, auth, seeded, "2026-08-07")
    moved = client.post(f"{B}/enrollments/{mid}/lifecycle", headers=auth,
                        json={"event": "migrate", "reason": "迁出",
                              "target_org_id": target_org})
    assert moved.status_code == 200, moved.text[:400]
    assert moved.json()["pending_confirm"] is True
    cross = next(e for e in client.get(f"{B}/lifecycle-events", headers=auth).json()
                 if e["enrollment_id"] == mid)
    assert cross["confirmed"] is False and cross["target_org_id"] == target_org

    recalls = client.get(f"{B}/recalls", headers=auth).json()
    assert recalls, "recall 事件应产生召回记录"
    assert list(recalls[0]) == ["id", "enrollment_id", "reason", "status",
                                "result", "contacts", "created_at"]
    rid = next(r["id"] for r in recalls if r["enrollment_id"] == eid)

    prog = client.post(f"{B}/recalls/{rid}/progress", headers=auth,
                       json={"status": "contacted", "contact_note": "已电话联系"})
    assert prog.status_code == 200, prog.text[:400]
    assert list(prog.json()) == ["id", "status", "result"]
    assert prog.json()["status"] == "contacted"


# ------------------------------------------------- 档案详情继承是对的
def test_档案详情把packages与paths排在摘要键之后(client, auth, seeded):
    """`EnrollmentDetailOut` **继承** `EnrollmentOut` 是对的：handler 先调
    `_enroll_out(带 brief)`，之后才追加 `packages`/`paths`，所以它们确实排在
    五个条件键之后——与继承把子类字段排到末尾一致。

    （`spd/followup` 的报表详情正相反：新字段插在中间，那里继承就会改字节。
    继承对不对，取决于**新字段在 handler 里是不是真的在最后**。）
    """
    eid = _active_enrollment(client, auth, seeded, "2026-08-03")
    client.post(f"{B}/enrollments/{eid}/packages", headers=auth,
                json={"package_id": _package_id()})
    detail = client.get(f"{B}/enrollments/{eid}", headers=auth)
    assert detail.status_code == 200, detail.text[:400]
    keys = list(detail.json())
    assert keys[-7:] == ["patient_name", "gender", "birth_date", "phone", "ehc_no",
                         "packages", "paths"]

    patched = client.patch(f"{B}/enrollments/{eid}", headers=auth,
                           json={"risk_level": "high", "tags": ["契约"]})
    assert patched.status_code == 200, patched.text[:400]
    # PATCH 走 `_enroll_out(enrollment)` 不带 brief，五个条件键整个不出现
    assert "patient_name" not in patched.json()
    assert patched.json()["risk_level"] == "high"
    assert patched.json()["tags"] == ["契约"]
