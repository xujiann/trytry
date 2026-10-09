"""PID-13 的号码放在 XTN-12 / XTN-7 时也取得到，邮件类的重复不当电话（P2-1761，第五十二批扫描 AP2-9）。

XTN-1 是旧写法，v2.5 起号码多放在 XTN-12（未格式化号码）或 XTN-6 区号 + XTN-7 本地号码。`_pid13_phone` 原先只取每个
重复的第 1 组件（P2-724 只处理了 XTN-1）：修前实测 `^PRN^CP^^86^^13987654321`、`^PRN^CP^^^^^^^^^13987654321` 建档都回 201、
电话为空，A08 也改不了；邮箱写在 XTN-1（`x@example.com^NET^Internet`）时邮箱当电话落库。

修法：XTN-1 为空时依次取 XTN-12、XTN-7（前面拼 XTN-6 区号，写成「区号-号码」）；邮件类的重复（XTN-2 为 NET 或 XTN-3 为
Internet / X.400）跳过；挑「像手机号的那一项」的既有规则照旧。
"""
import pytest

from app.database import SessionLocal
from app.models import Patient

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_card(body17: str) -> str:
    return body17 + "10X98765432"[sum(int(c) * w for c, w in zip(body17, _WEIGHTS)) % 11]


def _adt(client, admin, event, control_id, id_card, pid13):
    message = "\r".join([f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20261009090000||ADT^{event}|{control_id}|P|2.4",
                         f"PID|1||{id_card}^^^CN^ID||P21761 患者||19900307|F|||北京||{pid13}"])
    resp = client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        patients = db.query(Patient).filter(Patient.name == "P21761 患者").all()
        return next(p.phone for p in patients if p.id_card == id_card)


@pytest.mark.parametrize(("body17", "pid13", "phone"), [
    ("11010119900307171", "^PRN^CP^^86^^13987654321", "13987654321"),          # XTN-7（XTN-5 国家码不拼），修前为空
    ("11010119900307172", "^PRN^CP^^^^^^^^^13987654321", "13987654321"),       # XTN-12，修前为空
    ("11010119900307173", "^WPN^PH^^86^0571^88886666", "0571-88886666"),        # 区号 + 本地号码
    ("11010119900307174", "p1761@example.com^NET^Internet~^PRN^CP^^^^13900001761", "13900001761"),    # 修前存成邮箱
    ("11010119900307175", "^WPN^PH^^^0571^88886666~^PRN^CP^^^^^^^^^13987654322", "13987654322"),     # 照旧挑手机号
    ("11010119900307176", "0571-88888888~13912345678", "13912345678"),            # XTN-1 照旧
], ids=["XTN-7", "XTN-12", "区号拼本地号码", "邮件类跳过", "照旧挑手机号", "XTN-1照旧"])
def test_A04建档取得到号码(client, admin, body17, pid13, phone):
    assert _adt(client, admin, "A04", f"P21761-{body17[-1]}", _id_card(body17), pid13) == phone


def test_A08按XTN12改得了电话(client, admin):
    id_card = _id_card("11010119900307177")
    assert _adt(client, admin, "A04", "P21761-A", id_card, "13800001761") == "13800001761"
    assert _adt(client, admin, "A08", "P21761-B", id_card, "^PRN^CP^^^^^^^^^13987654323") == "13987654323"   # 修前改不了
