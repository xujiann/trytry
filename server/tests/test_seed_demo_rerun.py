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
"""
import runpy
import sys
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
    Consultation,
    DrugStock,
    Encounter,
    ExamReport,
    ExamRequest,
    FollowUp,
    InfectiousCase,
    MedicalWaste,
    Notification,
    Organization,
    Prescription,
    Patient,
    Referral,
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
    """前半段灌的每一样各数一遍。都不随日期变——哪怕两遍之间跨了午夜、跨了月，该是同一个数还是同一个数。"""
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
        }


def _run_seed() -> None:
    runpy.run_path(str(SEED_DEMO), run_name="__main__")


BURY = 500   # deps.paginate 一页的上限


def _bury_first_page() -> None:
    """再落 500 张不相干的处方与影像申请（像 seed_bulk 灌的仿真数据那样，比种子的新）：清单新的在前、一页至多 500 条，
    种子自己那几张就翻到了第一页之外——只看第一页的判重认不出它们，重跑照样再补一份。"""
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        patient_id = db.query(func.min(Patient.id)).scalar()
        org_id = db.query(func.min(Organization.id)).scalar()
        db.add_all([Prescription(patient_id=patient_id, org_id=org_id, created_by=admin_id) for _ in range(BURY)])
        db.add_all([ExamRequest(patient_id=patient_id, from_org_id=org_id, center_type="imaging",
                                item_code="BURY", item_name="压页占位", created_by=admin_id) for _ in range(BURY)])
        db.commit()


def test_演示种子同一天跑两遍_第二遍不崩也不多灌(monkeypatch, capsys):
    reset_database()
    # 只借 lifespan 种一遍启动数据（admin 账号、各类目录）就退出：调度循环不在场，两遍之间没有别的写入
    with TestClient(app):
        pass
    # 与 start.sh 真起服务时一样，脚本只看得到状态码——服务端异常回 500，不抛进脚本
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: TestClient(app, raise_server_exceptions=False))
    monkeypatch.setattr(sys, "argv", ["seed_demo.py", "http://testserver"])
    monkeypatch.setattr(portal, "SEND_COOLDOWN_SECONDS", 0)

    _run_seed()
    first = _snapshot()
    # 头一遍每样都真灌上了，下面的"两遍一样"才不是两个空库在比
    assert all(first.values()), first
    capsys.readouterr()

    _bury_first_page()
    _run_seed()   # 修前：约号那一行 KeyError: 'id'，其后各段与末端自检都不跑
    assert _snapshot() == {**first, "处方": first["处方"] + BURY, "检查申请": first["检查申请"] + BURY}
    # 第二遍一直走到了末端自检，居民侧那条也在里头（验证码冷却没把居民会话挡在外面）
    out = capsys.readouterr().out
    assert "末端自检通过" in out
    assert "报告/手术已落居民消息" in out

    # 上一遍号源建好、约号之前就断了：再跑时号源撞 409，要查回已有的那个补约，不拿 409 的响应体取 id。
    # 只断言预约补上了——两遍之间若跨了午夜，号源日期跟着变、走的是新建那条路，号源数就不该拿来比
    with SessionLocal() as db:
        db.query(Appointment).delete()
        db.query(AppointmentSlot).update({AppointmentSlot.booked: 0})
        db.commit()
    _run_seed()
    assert _snapshot()["预约"] == first["预约"]
