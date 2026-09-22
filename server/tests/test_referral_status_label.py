"""转诊状态文案的权威收归后端，且**两个读者的措辞并排放在一张表里**。

背景：同一份 status→中文 的映射，此前在**前端各存一份**：
`static/core.js` 的 `REF_STATUS` 与 `static/m/m.js` 的 `REFERRAL_STATUS`。
两份都已删掉，改用后端 `status_label`；前端只留配色。

注意**不是**要求两个界面用同一套词：居民端说"待接收"（面向患者），
业务端说"待接诊"（面向医师），同一个状态、两个读者、两套措辞是对的。
不对的是同一套措辞在前后端各存一份——那种复制迟早改一处漏一处。

**P1-38 收账（本轮）**：两套措辞原先各存一处（`referrals.STATUS_LABELS` 与
`portal._PLATFORM_REFERRAL_STATUS`），各自演化。危险不在"用词不一样"（那是对的），
而在**加一个状态码时没有任何东西提醒你居民端也要给一句话**——居民端的兜底是
`.get(status, status)`，漏了不报错，居民看到的是英文状态码。现在两列并排长在
`referrals.STATUS_WORDING` 的同一张表里，一行一个状态，想漏也漏不掉。

余下的风险是**状态码集合本身散在四处**：模型默认值、`_ALLOWED_TRANSITIONS`、
`schemas.ReferralStatusUpdate` 的 pattern、文案表。下面几条把四处**现算**着对上，
任何一处加了/删了状态码而其余三处没跟上，都会当场变红。
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers.referrals import RESIDENT_STATUS_LABELS, STATUS_LABELS

CORE_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "core.js"


@pytest.fixture()
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_三个端点都返回status_label(client):
    """创建 / 列表 / 状态流转都要带上——少一个前端就得自己补一张表。"""
    admin = _admin(client)
    orgs = []
    for tag in ("甲", "乙"):
        orgs.append(client.post(
            "/api/organizations",
            json={"name": f"{tag}标签院", "org_type": "lead_hospital", "level": "county"},
            headers=admin,
        ).json())
    client.post(
        "/api/users",
        json={"username": "lbl_doc", "password": "pass123456", "role": "doctor",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    doc = client.post(
        "/api/auth/login", json={"username": "lbl_doc", "password": "pass123456"}
    ).json()
    doc = {"Authorization": f"Bearer {doc['access_token']}"}
    # 推进状态要用**接收方**机构的医师：转诊状态流转已收归接收方
    # （见 tests/test_referral_status_authority.py）。本用例考的是 status_label，
    # 不是权限，所以老老实实按新规则准备一个接收方账号，而不是改用 admin 绕过去
    # ——admin 走的是全域放行分支，那样就测不到机构账号这条正路了。
    client.post(
        "/api/users",
        json={"username": "lbl_doc_recv", "password": "pass123456", "role": "doctor",
              "org_id": orgs[1]["id"]},
        headers=admin,
    )
    recv = client.post(
        "/api/auth/login", json={"username": "lbl_doc_recv", "password": "pass123456"}
    ).json()
    recv = {"Authorization": f"Bearer {recv['access_token']}"}
    patient = client.post(
        "/api/patients", json={"name": "标签患者", "id_card": "330281199101016006"},
        headers=admin,
    ).json()

    created = client.post(
        "/api/referrals",
        json={"patient_id": patient["id"], "from_org_id": orgs[0]["id"],
              "to_org_id": orgs[1]["id"], "direction": "up", "reason": "上转"},
        headers=doc,
    )
    assert created.status_code == 201, created.text
    assert created.json()["status_label"] == "待接诊"

    listed = client.get("/api/referrals", headers=doc).json()
    assert listed[0]["status_label"] == "待接诊"

    moved = client.patch(
        f"/api/referrals/{created.json()['id']}/status",
        json={"status": "accepted"}, headers=recv,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status_label"] == "已接诊"


def test_业务端与居民端措辞刻意不同():
    """两个读者、两套措辞——但只有一张表。

    这里仍然把两边的词**写死**着断言，而不只是"两个字典键集相同"：
    键集相同现在由 `STATUS_WORDING` 的形状保证，本来就推不翻；真正要钉住的是
    **这两句话是刻意不同的**——将来有人图省事把居民端也改成"待接诊"，
    结构上毫无破绽，得靠这一条拦。
    """
    from app.routers.portal import _PLATFORM_REFERRAL_STATUS

    assert STATUS_LABELS["pending"] == "待接诊"              # 面向医师：这单等我接
    assert RESIDENT_STATUS_LABELS["pending"] == "待接收"      # 面向患者：医院还没接
    assert STATUS_LABELS["completed"] == "已结案"
    assert RESIDENT_STATUS_LABELS["completed"] == "已完成"
    # 居民端聚合用的就是这一份，不是另抄的一份
    assert _PLATFORM_REFERRAL_STATUS is RESIDENT_STATUS_LABELS


def test_状态码集合四处对得上():
    """状态码散在四处：模型默认值 / 状态机 / 入参 pattern / 文案表。

    加一个状态而漏掉其中一处的后果各不相同，但都不报错：漏 pattern → 接口 422
    退回一个业务上合法的流转；漏文案表 → 界面露出英文原始码。所以四处都**现算**
    比对，不手写清单。
    """
    import re

    from app.models import Referral
    from app.routers.referrals import _ALLOWED_TRANSITIONS, STATUS_WORDING
    from app.schemas import ReferralStatusUpdate

    initial = Referral.__table__.c.status.default.arg
    reachable = {initial} | set(_ALLOWED_TRANSITIONS) | {
        s for targets in _ALLOWED_TRANSITIONS.values() for s in targets
    }
    assert reachable == set(STATUS_WORDING), (
        "状态机能产生的状态码与文案表对不上："
        f"没文案的 {reachable - set(STATUS_WORDING)}；"
        f"文案表里多出来、状态机产生不了的 {set(STATUS_WORDING) - reachable}"
    )

    # 入参 pattern 应当恰好是"可以被流转到"的那些（初始状态不由入参设置）
    pattern = next(
        m.pattern for m in ReferralStatusUpdate.model_fields["status"].metadata
        if hasattr(m, "pattern")
    )
    accepts = {s for s in reachable if re.fullmatch(pattern, s)}
    targets = {s for ts in _ALLOWED_TRANSITIONS.values() for s in ts}
    assert accepts == targets, (
        f"入参 pattern 收的状态 {accepts} 与状态机的流转目标 {targets} 对不上："
        "多收 = 接口放进一个状态机不认的状态；少收 = 合法流转被 422 挡掉"
    )


def test_没有哪个状态把英文码当文案():
    """兜底是 `.get(status, status)`：漏一个不报错，只是界面上冒出英文。"""
    from app.routers.referrals import STATUS_WORDING

    raw = sorted(
        code for code, words in STATUS_WORDING.items()
        if any(not w.strip() or w == code for w in words)
    )
    assert raw == [], f"这些状态的文案是空的或就是状态码本身：{raw}"


def test_两个读者的措辞自证(capsys):
    """把两列并排打出来——闸门要让人一眼看见"两套措辞"长什么样。"""
    from app.routers.referrals import STATUS_WORDING

    with capsys.disabled():
        print("\n  状态码            业务端（医师）   居民端（患者）")
        for code, (business, resident) in STATUS_WORDING.items():
            same = "  ← 两边同一句话" if business == resident else ""
            print(f"    {code:<14} {business:<12} {resident}{same}")
    assert STATUS_WORDING


def test_业务端前端不再自带文案表():
    source = CORE_JS.read_text(encoding="utf-8")
    assert "REF_STATUS = {" not in source, "REF_STATUS 文案表应已删除，改用后端 status_label"
    assert "r.status_label" in source, "应从后端取文案"
    # 配色留在前端是对的：配色是展示，文案是口径
    assert "REF_STATUS_COLOR" in source


def test_后端映射覆盖全部可达状态():
    """漏一个状态，界面上就会露出英文原始码。"""
    from app.routers.referrals import _ALLOWED_TRANSITIONS

    reachable = set(_ALLOWED_TRANSITIONS) | {
        s for targets in _ALLOWED_TRANSITIONS.values() for s in targets
    }
    missing = reachable - set(STATUS_LABELS)
    assert not missing, f"这些状态没有中文文案：{missing}"
