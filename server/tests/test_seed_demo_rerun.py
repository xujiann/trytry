"""演示种子重跑：不再每启动一次就多灌一份，同一天的第二遍也不再中途崩（P2-1095，扫描 C2-8）。

`scripts/seed_demo.py` 由 `start.sh` 在 MEDPLAT_SEED_DEMO=1 时每次启动都跑一遍（失败被 `|| true` 吞掉）。它的前半段
（就诊、检查与危急值、处方、药房库存与调拨、慢病随访、转诊、传染病报卡、会诊、预约、医废）原先没有一处存在性判断：
第二遍跑下来就诊 7→11、检查申请 3→6、危急值 1→2、居民消息 3→4、处方 4→8、传染病报卡 6→11、会诊 1→2、上转 2→3，
镇卫生院的二甲双胍库存 130→260；同一天的第二遍还在约号那一行拿 409 的响应体取 id（`KeyError: 'id'`），末端自检从此不跑。

这里在进程内把脚本原样跑两遍，不起 uvicorn：脚本里每个 `httpx.Client(...)` 换成一个对着应用的 TestClient（各会话的
登录头互不相干）。同一手机号 60 秒内只发一条验证码——两遍之间把这道冷却置零，第二遍的居民会话照样登得上、居民侧那条
自检也跑得到，不靠睡够一分钟。第二遍之前另落 500 张不相干的处方与影像申请（seed_bulk 灌过仿真数据的演示库就是这样），
把种子自己那几张压到清单第一页之外：判重要按页取全，只看第一页照样再补一份。末了再补一遍"上一次号源建好、没约上
就断了"的现场：号源撞 409 时查回已有的再约。

后半段三处首跑即被拒、末端自检又看不见（P2-1096）：物资采购由 admin 自己审批，撞「申请人不得自批」403，合同与验收跟着
409，单子永远停在待审批；急救事件先推三步到「已收治」才回传车载体征，409「已收治，转由院内记录」；慢专病转诊照
ADR-0005 之前的三级链审三次，新单两步就到「已接收」，第三次 409。所以头一遍（空库）逐个响应记下来，一个被拒的都
不许有。门诊药费明细原先按刘洋「最近一次就诊」判重：第二遍之前给他另开一次门诊（演示站上有人接诊过他就是这样），
重跑就给这次再挂两行。

急救绿道那例演示胸痛病例原先按不带参数的急救事件清单判重（P2-1369，扫描 AD4-3）：那份清单不翻页、只回全县最新 200 起，
演示站上再来 200 起呼救，种子那例就在窗口之外，重跑再建一例。第二遍之前另落 500 起不相干的普通呼救，急救事件只许多出这 500 起。
"""
import runpy
import sys
from datetime import date
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import (
    Appointment,
    AppointmentSlot,
    BillDetail,
    ChronicPatient,
    Consultation,
    DrugStock,
    EmergencyCase,
    EmergencyVital,
    Encounter,
    ExamReport,
    ExamRequest,
    FollowUp,
    InfectiousCase,
    MaterialPurchase,
    MedicalWaste,
    Notification,
    Organization,
    Prescription,
    Patient,
    Referral,
    SpdReferralStep,
    StockTransfer,
    User,
)
from app.routers import portal

SEED_DEMO = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo.py"


def _count(db, model, *where) -> int:
    return db.query(func.count(model.id)).filter(*where).scalar()


def _metformin(db, org_name: str) -> int | None:
    return (db.query(DrugStock.quantity).join(Organization, Organization.id == DrugStock.org_id)
            .filter(Organization.name == org_name, DrugStock.drug_code == "METFORMIN").scalar())


def _snapshot() -> dict:
    """前半段灌的每一样各数一遍，外加后半段修过的三样（P2-1096）与急救事件（P2-1369）。都不随日期变——哪怕两遍之间
    跨了午夜、跨了月，该是同一个数还是同一个数。"""
    with SessionLocal() as db:
        return {
            "就诊": _count(db, Encounter),
            "检查申请": _count(db, ExamRequest),
            "危急值报告": _count(db, ExamReport, ExamReport.critical.is_(True)),
            "居民消息": _count(db, Notification, Notification.resident_account_id.isnot(None)),
            "工作人员消息": _count(db, Notification, Notification.user_id.isnot(None)),
            "处方": _count(db, Prescription),
            "传染病报卡": _count(db, InfectiousCase),
            "会诊": _count(db, Consultation),
            "上转": _count(db, Referral, Referral.direction == "up"),
            "慢病随访": _count(db, FollowUp),
            "调拨": _count(db, StockTransfer),
            "号源": _count(db, AppointmentSlot),
            "预约": _count(db, Appointment),
            "医废": _count(db, MedicalWaste),
            "镇卫生院二甲双胍库存": _metformin(db, "城东镇卫生院"),
            "县医院二甲双胍库存": _metformin(db, "县人民医院"),
            "物资采购": _count(db, MaterialPurchase),
            "急救事件": _count(db, EmergencyCase),
            "急救车载体征": _count(db, EmergencyVital),
            "门诊药费明细": _count(db, BillDetail, BillDetail.encounter_id.isnot(None)),
        }


def _run_seed() -> None:
    runpy.run_path(str(SEED_DEMO), run_name="__main__")


BURY = 500   # deps.paginate 一页的上限


def _bury_first_page() -> None:
    """再落 500 张不相干的处方与影像申请（像 seed_bulk 灌的仿真数据那样，比种子的新）：清单新的在前、一页至多 500 条，
    种子自己那几张就翻到了第一页之外——只看第一页的判重认不出它们，重跑照样再补一份。急救事件清单不翻页、只回全县
    最新 200 起（P2-1369）：再落 500 起不相干的普通呼救，种子那例胸痛病例就在不带参数的那一页之外。"""
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        patient_id = db.query(func.min(Patient.id)).scalar()
        org_id = db.query(func.min(Organization.id)).scalar()
        db.add_all([Prescription(patient_id=patient_id, org_id=org_id, created_by=admin_id) for _ in range(BURY)])
        db.add_all([ExamRequest(patient_id=patient_id, from_org_id=org_id, center_type="imaging",
                                item_code="BURY", item_name="压页占位", created_by=admin_id) for _ in range(BURY)])
        db.add_all([EmergencyCase(location="压页占位", dest_org_id=org_id) for _ in range(BURY)])
        db.commit()


def _open_another_visit() -> None:
    """演示站上有人给刘洋另开了一次门诊（比种子那次新）：药费明细按"最近一次就诊"判重的话，重启就给这次再挂两行。"""
    with SessionLocal() as db:
        patient_id = db.query(Patient.id).filter(Patient.name == "刘洋").scalar()
        org_id = db.query(Organization.id).filter(Organization.name == "河西镇卫生院").scalar()
        db.add(Encounter(patient_id=patient_id, org_id=org_id, diagnosis_name="复诊"))
        db.commit()


def test_演示种子同一天跑两遍_第二遍不崩也不多灌(monkeypatch, capsys):
    reset_database()
    # 只借 lifespan 种一遍启动数据（admin 账号、各类目录）就退出：调度循环不在场，两遍之间没有别的写入
    with TestClient(app):
        pass
    responses: list[tuple[str, str, int]] = []

    def _client(*args, **kwargs):
        # 与 start.sh 真起服务时一样，脚本只看得到状态码——服务端异常回 500，不抛进脚本；每个响应都记下来
        client = TestClient(app, raise_server_exceptions=False)
        client.event_hooks["response"].append(
            lambda r: responses.append((r.request.method, r.request.url.path, r.status_code)))
        return client

    monkeypatch.setattr(httpx, "Client", _client)
    monkeypatch.setattr(sys, "argv", ["seed_demo.py", "http://testserver"])
    monkeypatch.setattr(portal, "SEND_COOLDOWN_SECONDS", 0)

    _run_seed()
    # 空库头一遍一个请求都不该被拒：被拒就是某段演示流程静悄悄没走通，而脚本一路不看响应码（P2-1096）
    assert [r for r in responses if r[2] >= 400] == []
    first = _snapshot()
    # 头一遍每样都真灌上了，下面的"两遍一样"才不是两个空库在比
    assert all(first.values()), first
    assert first["急救车载体征"] == 1   # 转运途中回传的那一条真落了库
    with SessionLocal() as db:
        assert db.query(MaterialPurchase.status).filter(MaterialPurchase.item_name == "移动输液架").scalar() == "received"
        # 慢专病转诊的两级审核都落了库（ADR-0005：卫生院审核 → 县级医院接收）；多审的那一次修前是 409，上面那句已拦
        assert _count(db, SpdReferralStep, SpdReferralStep.action == "pass") == 2
        # 慢病超期名单、诊间提醒与驾驶舱下钻有东西可看：随访手填的下次随访日不得早于今天（P2-1545）之后，随访过的档案都
        # 不再超期，超期只能来自建档时补录的到期日——种子得自己建一份这样的档案
        assert _count(db, ChronicPatient, ChronicPatient.next_due != "",
                      ChronicPatient.next_due < date.today().isoformat()) >= 1
    capsys.readouterr()

    _bury_first_page()
    _open_another_visit()
    _run_seed()   # 修前：约号那一行 KeyError: 'id'，其后各段与末端自检都不跑
    assert _snapshot() == {**first, "处方": first["处方"] + BURY, "检查申请": first["检查申请"] + BURY,
                           "就诊": first["就诊"] + 1, "急救事件": first["急救事件"] + BURY}
    # 第二遍一直走到了末端自检，居民侧那条也在里头（验证码冷却没把居民会话挡在外面）；采购走没走到验收自检也看得见了
    out = capsys.readouterr().out
    assert "末端自检通过" in out
    assert "报告/手术已落居民消息" in out
    assert "物资采购已走到验收" in out

    # 上一遍号源建好、约号之前就断了：再跑时号源撞 409，要查回已有的那个补约，不拿 409 的响应体取 id。
    # 只断言预约补上了——两遍之间若跨了午夜，号源日期跟着变、走的是新建那条路，号源数就不该拿来比
    with SessionLocal() as db:
        db.query(Appointment).delete()
        db.query(AppointmentSlot).update({AppointmentSlot.booked: 0})
        db.commit()
    _run_seed()
    assert _snapshot()["预约"] == first["预约"]
