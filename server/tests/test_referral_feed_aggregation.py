"""ADR-0003 方案 B：居民端转诊读侧聚合。

背景：居民端此前有两份互不相交的"我的转诊"——`/api/portal/me/referrals` 读平台
`referrals`，`/api/portal/spd/referrals` 读 `spd_referral_cases`。同一个居民在 App 里
看到什么，取决于他点了哪个入口，这就是 ADR-0003 说的数据孤岛。

本文件分两半：

1. **特征化网**（ADR 要求"落地前先给各套写特征化网"）：钉住两个老接口**当前**的
   响应形状。聚合是叠加上去的只读层，老接口一个字节都不该动。
2. **聚合行为**：并集、按日期倒序、带 source 与中文标签、子系统关掉时降级。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.database import SessionLocal
from app.models import Organization, Patient, Referral, ResidentAccount, User


@pytest.fixture()
def client():
    """函数级干净库。

    同时清掉居民端的发码限流：`SEND_COOLDOWN_SECONDS` 的计数存在进程内状态里，
    `reset_database()` 冲不掉它——不清的话，第二条用例走同一手机号发码会吃 429，
    响应里自然也就没有 `debug_code`。
    """
    from app.routers.portal import _reset_portal_failures

    reset_database()
    _reset_portal_failures()
    with TestClient(app) as c:
        yield c


def _admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def resident(client):
    """一个绑定了患者档案的居民账号，外加两条转诊：平台一条、慢专病一条。"""
    admin = _admin(client)
    org_a = client.post(
        "/api/organizations",
        json={"name": "聚合甲卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    org_b = client.post(
        "/api/organizations",
        json={"name": "聚合乙县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "聚合居民", "id_card": "330281199203044519", "phone": "13800001111"},
        headers=admin,
    ).json()

    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        # 平台转诊一条（2026-01-02）
        db.add(Referral(
            patient_id=patient["id"], from_org_id=org_a["id"], to_org_id=org_b["id"],
            direction="up", reason="血压控制不佳", status="pending", created_by=doctor.id,
        ))
        # 居民账号：直接落库，绕开短信验证码流程
        db.add(ResidentAccount(
            phone="13800001111", patient_id=patient["id"], status="active",
        ))
        db.commit()

    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": "13800001111"}
    ).json()["debug_code"]
    token = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13800001111", "code": code}
    )
    assert token.status_code == 200, token.text
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    return {"headers": headers, "patient": patient, "org_a": org_a, "org_b": org_b}


# ---------------------------------------------------------------- 特征化网


def test_特征化_平台单源接口形状不变(client, resident):
    """老接口是既有契约，聚合层不得改动它一个字段。"""
    rows = client.get("/api/portal/me/referrals", headers=resident["headers"])
    assert rows.status_code == 200, rows.text
    data = rows.json()
    assert len(data) == 1
    assert set(data[0]) == {"id", "direction", "from_org", "to_org", "reason", "status", "date"}
    assert data[0]["from_org"] == "聚合甲卫生院"
    assert data[0]["to_org"] == "聚合乙县医院"
    assert data[0]["status"] == "pending"      # 平台原始码，无标签


def test_特征化_慢专病单源接口形状不变(client, resident):
    """spd 那个老接口同样是既有契约，聚合层不得改动它。

    （原先这里只数了条数，形状能悄悄漂走——本文件开头声称"钉住两个老接口"，
    只钉一个就是名不副实。）
    """
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        db.add(SpdReferralCase(
            patient_id=resident["patient"]["id"], direction="up",
            initiator_org_id=resident["org_a"]["id"],
            target_org_id=resident["org_b"]["id"],
            current_level="village", status="submitted", reason="特征化用例",
        ))
        db.commit()

    rows = client.get("/api/portal/spd/referrals", headers=resident["headers"])
    assert rows.status_code == 200, rows.text
    data = rows.json()
    assert len(data) == 1
    assert set(data[0]) == {
        "id", "direction", "status", "current_level",
        "reason", "trigger_evidence", "created_at",
    }
    assert data[0]["status"] == "submitted"    # spd 原始码，无标签
    assert data[0]["current_level"] == "village"


# ---------------------------------------------------------------- 聚合行为


def test_聚合接口包含平台源且带source与中文标签(client, resident):
    resp = client.get("/api/portal/me/referrals/all", headers=resident["headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) >= 1
    platform = [i for i in items if i["source"] == "platform"]
    assert len(platform) == 1
    item = platform[0]
    assert item["status"] == "pending"
    assert item["status_label"] == "待接收", "不标中文，两套同名不同义的状态码读不懂"
    assert item["from_org"] == "聚合甲卫生院"
    assert set(item) == {
        "source", "id", "direction", "status", "status_label",
        "reason", "from_org", "to_org", "created_at", "date", "detail_path",
    }
    assert item["date"] == item["created_at"][:10]


def test_两套accepted同名不同义_靠source分辨():
    """平台 `accepted` 是"已接诊"，spd `accepted` 是"县级已接收"。

    这是 ADR-0003 所说的口径分叉。聚合层只能如实呈现、分源映射标签，
    不能假装它们是一回事——所以标签表必须是两张，且每条都带 source。
    """
    from app.routers.portal import _PLATFORM_REFERRAL_STATUS
    from app.spd.service import _STATUS_LABELS

    assert _PLATFORM_REFERRAL_STATUS["accepted"] == "已接收"
    assert _STATUS_LABELS["accepted"] == "县级医院已接收"
    assert _PLATFORM_REFERRAL_STATUS["accepted"] != _STATUS_LABELS["accepted"]


def test_两个源真的并到了一起(client, resident):
    """本功能的全部意义所在：一份列表里同时看到平台转诊与慢专病转诊。

    此前居民看到哪一份取决于他点了哪个入口（ADR-0003 的数据孤岛）。
    """
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        db.add(SpdReferralCase(
            patient_id=resident["patient"]["id"],
            program_code="hypertension",
            direction="up",
            initiator_org_id=resident["org_a"]["id"],
            target_org_id=resident["org_b"]["id"],
            current_level="village",
            status="township_reviewed",
            reason="血压持续偏高，需上级复诊",
        ))
        db.commit()

    items = client.get("/api/portal/me/referrals/all",
                       headers=resident["headers"]).json()
    by_source = {i["source"] for i in items}
    assert by_source == {"platform", "spd"}, f"两个源都要在，实际 {by_source}"

    spd_item = next(i for i in items if i["source"] == "spd")
    assert spd_item["status"] == "township_reviewed"
    assert spd_item["status_label"] == "卫生院已审核，待县级医院接收"
    assert spd_item["from_org"] == "聚合甲卫生院"
    assert spd_item["to_org"] == "聚合乙县医院"
    detail = spd_item["detail_path"]
    assert detail.startswith(f"/api/portal/spd/referrals/{spd_item['id']}"), \
        "要能点回各自的详情页，否则聚合列表是个死胡同"
    assert "patient_id=" in detail, \
        "详情端点按 patient_id 决定看谁的档案，不带就会拿默认患者去查（代管家属直接 404）"
    followed = client.get(detail, headers=resident["headers"])
    assert followed.status_code == 200, f"聚合列表给出的链接必须真能打开：{followed.text}"

    # 两个老接口仍各自只看得到自己那一份——聚合是叠加，不是改写
    platform_only = client.get("/api/portal/me/referrals", headers=resident["headers"]).json()
    spd_only = client.get("/api/portal/spd/referrals", headers=resident["headers"]).json()
    assert len(platform_only) == 1 and len(spd_only) == 1


def test_目标机构未定时留空而不是编一个(client, resident):
    """逐级审核中的单子还没确定目标机构，`to_org` 应为空串。"""
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        db.add(SpdReferralCase(
            patient_id=resident["patient"]["id"], direction="up",
            initiator_org_id=resident["org_a"]["id"], target_org_id=None,
            current_level="village", status="submitted", reason="待定",
        ))
        db.commit()
    items = client.get("/api/portal/me/referrals/all", headers=resident["headers"]).json()
    pending = next(i for i in items if i["source"] == "spd" and i["status"] == "submitted")
    assert pending["to_org"] == ""


def test_聚合按日期倒序且顺序稳定(client, resident):
    """同日多条要有确定顺序，否则前端分页与快照测试都会飘。"""
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        for reason in ("较早一条", "较晚一条"):
            db.add(Referral(
                patient_id=resident["patient"]["id"],
                from_org_id=resident["org_a"]["id"], to_org_id=resident["org_b"]["id"],
                direction="up", reason=reason, status="pending", created_by=doctor.id,
            ))
        db.commit()
    items = client.get("/api/portal/me/referrals/all",
                       headers=resident["headers"]).json()
    dates = [i["date"] for i in items]
    assert dates == sorted(dates, reverse=True), "必须按日期倒序"
    same_day = [i for i in items if i["date"] == dates[0] and i["source"] == "platform"]
    ids = [i["id"] for i in same_day]
    assert ids == sorted(ids, reverse=True), "同日同源按 id 倒序，保证顺序确定"


def test_同日内按时刻排序而不是按源名(client, resident):
    """只按日期排序，同一天里两个源会按**源名**交错——"我的转诊"是条时间线。

    造一条 23:59 的平台单与一条 00:01 的 spd 单，同一天。按源名排会把
    "platform" 排到 "spd" 后面，于是当天最晚发生的反而垫底。
    """
    from datetime import datetime

    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        db.query(Referral).delete()
        doctor = db.query(User).filter(User.username == "admin").first()
        db.add(Referral(
            patient_id=resident["patient"]["id"],
            from_org_id=resident["org_a"]["id"], to_org_id=resident["org_b"]["id"],
            direction="up", reason="当天最晚", status="pending", created_by=doctor.id,
            created_at=datetime(2026, 3, 1, 23, 59),
        ))
        db.add(SpdReferralCase(
            patient_id=resident["patient"]["id"], direction="up",
            initiator_org_id=resident["org_a"]["id"],
            current_level="village", status="submitted", reason="当天最早",
            created_at=datetime(2026, 3, 1, 0, 1),
        ))
        db.commit()

    items = [i for i in client.get("/api/portal/me/referrals/all",
                                   headers=resident["headers"]).json()
             if i["date"] == "2026-03-01"]
    assert [i["reason"] for i in items] == ["当天最晚", "当天最早"], \
        f"同日应按时刻倒序，实际 {[i['reason'] for i in items]}"


def test_子系统未装载时聚合降级为平台单源(client, resident, monkeypatch):
    """spd 是可装卸子系统：关掉之后聚合接口不能炸，只是少一段数据。"""
    from app.routers import portal as portal_mod

    kept = dict(portal_mod._REFERRAL_SOURCES)
    try:
        portal_mod._REFERRAL_SOURCES.pop("spd", None)
        resp = client.get("/api/portal/me/referrals/all", headers=resident["headers"])
        assert resp.status_code == 200, resp.text
        assert {i["source"] for i in resp.json()} == {"platform"}
    finally:
        portal_mod._REFERRAL_SOURCES.clear()
        portal_mod._REFERRAL_SOURCES.update(kept)


def test_source参数在服务端收窄_不被另一源挤出窗口(client, resident):
    """条数上限是**合并之后**才截的，所以收窄必须在服务端做。

    造 `REFERRAL_FEED_LIMIT` 条更新的平台转诊，把唯一一条慢专病单子压在窗口之外：
    不带 `source` 时它确实看不见（这正是客户端 filter 会踩的坑——慢专病页会显示
    "暂无转诊记录"而居民其实有在办的单子）；带上 `source=spd` 就必须能看见。
    """
    from datetime import datetime

    from app.routers.portal import REFERRAL_FEED_LIMIT
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        db.query(Referral).delete()
        doctor = db.query(User).filter(User.username == "admin").first()
        # 一条很早的慢专病单
        db.add(SpdReferralCase(
            patient_id=resident["patient"]["id"], direction="up",
            initiator_org_id=resident["org_a"]["id"], current_level="village",
            status="submitted", reason="被挤出去的那条",
            created_at=datetime(2020, 1, 1, 0, 0),
        ))
        # 满窗口的、更新的平台转诊
        for i in range(REFERRAL_FEED_LIMIT):
            db.add(Referral(
                patient_id=resident["patient"]["id"],
                from_org_id=resident["org_a"]["id"], to_org_id=resident["org_b"]["id"],
                direction="up", reason=f"平台{i}", status="pending", created_by=doctor.id,
                created_at=datetime(2026, 6, 1, 12, 0),
            ))
        db.commit()

    merged = client.get("/api/portal/me/referrals/all",
                        headers=resident["headers"]).json()
    assert {i["source"] for i in merged} == {"platform"}, \
        "前提：慢专病那条确实被挤出了合并后的窗口"

    scoped = client.get("/api/portal/me/referrals/all?source=spd",
                        headers=resident["headers"]).json()
    assert [i["reason"] for i in scoped] == ["被挤出去的那条"], \
        "服务端收窄后，慢专病那条必须回来——客户端 filter 做不到这件事"


def test_未知source拒绝而不是静默返回空(client, resident):
    resp = client.get("/api/portal/me/referrals/all?source=nope",
                      headers=resident["headers"])
    assert resp.status_code == 422, resp.text


def test_聚合源注册是幂等的():
    """装卸开关反复开关时不该越积越多（与附件业务域注册同一约定）。"""
    from app.routers.portal import _REFERRAL_SOURCES, register_referral_source

    before = len(_REFERRAL_SOURCES)
    loader = _REFERRAL_SOURCES["platform"]
    register_referral_source("platform", loader)
    register_referral_source("platform", loader)
    assert len(_REFERRAL_SOURCES) == before


def _spd_reachable_statuses() -> set[str]:
    """慢专病转诊单**真的可能出现**的状态码，从代码现算。

    三个来源合起来：模型默认值（起始格）、`_NEXT` 状态机表的键与目标
    （键里含 `station_reviewed` 这类只进不出的存量格——存量单据仍停在那儿，
    标签不能少），以及 `referral.py` 里所有 `case.status = "<字面量>"` 赋值
    （到院/下转/闭环/退回/撤回这几格不走 `_NEXT`，是直接写的）。
    """
    import ast
    import pathlib

    from app.spd.models import SpdReferralCase
    from app.spd.routers.referral import _NEXT

    src = pathlib.Path(__file__).resolve().parent.parent / "app" / "spd" / "routers" / "referral.py"
    assigned = set()
    for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute) and target.attr == "status"
                    and isinstance(node.value.value, str)):
                assigned.add(node.value.value)
    assert assigned, "一个 `case.status = \"...\"` 都没扫到，判据已失效（不是真没有）"
    return (
        {SpdReferralCase.__table__.c.status.default.arg}
        | set(_NEXT)
        | {target for target, *_ in _NEXT.values()}
        | assigned
    )


def test_慢专病转诊文案表没有状态机产生不了的格():
    """多一格不报错，但它是**假信息**：读代码的人会以为流程里真有这一步。

    实测过一次：`followup_received`（"下转随访已接收"）在文案表与模型列注释里
    都写着，像是 `down_referred` 与 `closed` 之间的一格——而随访接收那一步
    `case.status = "closed"` 是直接闭环的，全仓库没有任何一处写过这个码。
    前端 `SPD_REF_STATUS` 因此也没有它（前端是对的），于是这一格既无人写入、
    也无人显示，只在两处注释里误导读者。
    """
    from app.spd.service import _STATUS_LABELS

    reachable = _spd_reachable_statuses()
    assert set(_STATUS_LABELS) == reachable, (
        f"状态机产生不了、却有文案的：{set(_STATUS_LABELS) - reachable}；"
        f"能产生、却没文案（界面会露出英文码）：{reachable - set(_STATUS_LABELS)}"
    )


def test_慢专病转诊前端标签表与后端同码集():
    """前端措辞**刻意更短**（表格里一格放不下"卫生院已审核，待县级医院接收"），

    但**码集必须一致**：少一个码，那张单子在列表里显示的就是英文原始码
    （`statusTag` 查不到时回落成 key 本身）。所以只比键、不比词。
    """
    import pathlib
    import re

    from app.spd.service import _STATUS_LABELS

    js = (pathlib.Path(__file__).resolve().parent.parent
          / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
    block = re.search(r"const SPD_REF_STATUS = \{(.*?)\n\};", js, re.S)
    assert block, "前端 SPD_REF_STATUS 表没找到（改名了就把这条一起改）"
    front = set(re.findall(r"(\w+):\s*\[", block.group(1)))
    assert front == set(_STATUS_LABELS), (
        f"前端缺（会显示英文码）：{set(_STATUS_LABELS) - front}；"
        f"前端多（后端已经产生不了）：{front - set(_STATUS_LABELS)}"
    )
