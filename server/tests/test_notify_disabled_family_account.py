"""停用的代管家属账户不再收站内信与微信模板消息（P2-765，第二十批「停用 / 注销 / 作废的对象仍在被用」扫描 M3-6）。

`notify.patient_recipients` 的本人一支只认在用账户，代管家属一支原先不看账户状态：停用的账户（停用后令牌校验即失败、
登不上）照收站内信，报告出具、宣教照往它的 openid 推微信模板消息（带检查项目名）。慢专病自己的微信宣教通道挡住了
（`send_wechat_edu` 只取在用的，P2-502），站内宣教走 `notify_patient` 又从旁路推了出去。
"""
import pytest

from app import wechat
from app.database import SessionLocal
from app.models import Notification, ResidentAccount, ResidentFamilyMember, SystemParam
from app.notify import notify_patient, patient_recipients


class _Recorder(wechat.MockWeChatProvider):
    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str]] = []

    def send_template_message(self, openid, template_id, data, url=""):
        self.sent.append((openid, template_id))
        return True


@pytest.fixture
def recorder():
    provider = _Recorder()
    wechat.set_wechat_provider(provider)
    yield provider
    wechat.set_wechat_provider(None)


@pytest.fixture(scope="module")
def world(client, admin):
    elder = client.post("/api/patients", headers=admin, json={
        "name": "P2765 失能老人", "id_card": "330106193501012765"}).json()["id"]
    with SessionLocal() as db:
        child = ResidentAccount(phone="13900027651", wechat_openid="oP2765-CHILD", status="active")
        frozen = ResidentAccount(phone="13900027652", wechat_openid="oP2765-FROZEN", status="disabled")
        db.add_all([child, frozen])
        db.flush()
        db.add_all([ResidentFamilyMember(account_id=child.id, patient_id=elder, relation="child"),
                    ResidentFamilyMember(account_id=frozen.id, patient_id=elder, relation="child")])
        db.add(SystemParam(key="wechat_template_p2765_report", value="TPL-P2765"))
        db.commit()
        return {"elder": elder, "child": child.id, "frozen": frozen.id}


def test_收件人只有在用的家属账户(world):
    with SessionLocal() as db:
        assert patient_recipients(db, world["elder"]) == {world["child"]}   # 修前连停用的也在


def test_停用的家属账户不收站内信_不推微信(world, recorder):
    with SessionLocal() as db:
        assert notify_patient(db, world["elder"], category="p2765_report", title="空腹血糖 报告已出具") == 1
        db.commit()
        inbox = sorted(n.resident_account_id for n in db.query(Notification).filter_by(category="p2765_report"))
    assert inbox == [world["child"]]                         # 修前停用的家属也收一条
    assert recorder.sent == [("oP2765-CHILD", "TPL-P2765")]   # 修前也往停用账户的 openid 推
