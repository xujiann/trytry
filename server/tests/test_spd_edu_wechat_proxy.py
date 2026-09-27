"""慢专病微信宣教只推本人绑定的账户，代管的家属一条都收不到（P2-502，第九批「通知承诺」扫描 W2-10）。

站内信（`notify.notify_patient`）的口径写得明白：本人绑定的账户与**代管该档案的家属账户**都收——「儿童与失能老人的消息本来
就该发给代管人，只发给本人账户等于发进黑洞」。宣教的「站内」渠道走它，家属收得到；「微信」渠道的 `send_wechat_edu` 却只查
本人绑定的账户：失能老人名下没有账户，推送记「尚无绑定微信」失败，绑了微信的子女收不到。

修法：「该发给哪些账户」抽成 `notify.patient_recipients`，站内信与微信宣教共用；微信那头再只取在用、绑了微信的。
"""
import pytest

from app import wechat
from app.database import SessionLocal

B = "/api/spd"


class _Recorder(wechat.MockWeChatProvider):
    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def send_template_message(self, openid, template_id, data, url=""):
        self.sent.append(openid)
        return True


@pytest.fixture
def recorder():
    provider = _Recorder()
    wechat.set_wechat_provider(provider)
    yield provider
    wechat.set_wechat_provider(None)


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import ResidentAccount, ResidentFamilyMember, SystemParam
    from app.spd.models import SpdEduMaterial

    elder = client.post("/api/patients", headers=admin, json={
        "name": "P2502 失能老人", "id_card": "330106193501012502"}).json()["id"]
    with SessionLocal() as db:
        child = ResidentAccount(phone="13900025021", wechat_openid="oP2502-CHILD", status="active")
        frozen = ResidentAccount(phone="13900025022", wechat_openid="oP2502-FROZEN", status="disabled")
        db.add_all([child, frozen])
        db.flush()
        db.add_all([ResidentFamilyMember(account_id=child.id, patient_id=elder, relation="child"),
                    ResidentFamilyMember(account_id=frozen.id, patient_id=elder, relation="child")])
        if db.query(SystemParam).filter(SystemParam.key == "wechat_template_spd_edu").first() is None:
            db.add(SystemParam(key="wechat_template_spd_edu", value="TPL-P2502"))
        material = SpdEduMaterial(code="P2502EDU", title="冬季血压管理", content="注意保暖，按时服药",
                                  media_type="text", active=True)
        db.add(material)
        db.commit()
        return {"elder": elder, "material": material.id}


def test_微信宣教推给代管的家属(client, admin, world, recorder):
    resp = client.post(f"{B}/edu-pushes", headers=admin, json={
        "material_id": world["material"], "patient_ids": [world["elder"]], "channel": "wechat"})
    assert resp.status_code in (200, 201), resp.text
    rows = client.get(f"{B}/edu-pushes", headers=admin, params={"patient_id": world["elder"]}).json()
    assert (rows[0]["channel"], rows[0]["status"], rows[0]["fail_reason"]) == ("wechat", "sent", ""), rows  # 修前 failed
    assert recorder.sent == ["oP2502-CHILD"]   # 停用的家属账户不推


def test_本人与家属都没绑微信_原因照实说(client, admin, recorder):
    lonely = client.post("/api/patients", headers=admin, json={
        "name": "P2502 独居老人", "id_card": "330106193502022502"}).json()["id"]
    from app.spd.platform import send_wechat_edu

    with SessionLocal() as db:
        assert send_wechat_edu(db, lonely, title="t", body="b") == (0, "该患者及代管家属尚无绑定微信的居民账号")
    assert recorder.sent == []
