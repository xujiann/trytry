"""押金预警把 gap 判定下推到 SQL 之后，结果集与改写前逐行相同（P1-54 最后一类）。

改写前的算法是：取前 500 个在院患者 → 逐个算 `deposit_balance - unsettled_amount`
→ 筛 `gap < threshold` → 按 gap 排序。这里把**那段算法原样写进用例当参照**
（两个聚合助手直接从 `app.routers.billing` 导入，不重抄），在同一份数据上跑一遍，
断言接口返回的 `admission_id` 序列与它**完全一致**——包括顺序。

参照数据刻意把边角都铺到：一分钱没交也没费用的（余额与未结都是 0）、
只预交没花的、预交+退费+冲抵三种流水都有的、费用已挂结算单的（不该算未结）、
以及 `0.3 - 0.1` 这种浮点上落在 `0.19999999999999998` 的金额——
取整没跟着一起下推的话，阈值 0.2 时 SQL 与 Python 会给出相反的答案。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Admission, BillDetail, Deposit, Organization, Patient
from app.routers.billing import deposit_balance, unsettled_amount


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


#: (押金流水, 未结费用, 已结费用) —— 每一行是一个在院患者的处境
CASES = [
    ([], [], []),                                   # 一分钱没交、也没花
    ([("prepay", 1000)], [], []),                   # 只预交
    ([("prepay", 1000)], [300], []),                # 交了够花
    ([("prepay", 100)], [300], []),                 # 交了不够花
    ([("prepay", 1000), ("refund", 900)], [50], []),        # 退过费
    ([("prepay", 1000), ("offset", 800)], [300], []),       # 结算冲抵过
    ([("prepay", 500)], [], [400]),                 # 费用已挂结算单，不算未结
    ([("prepay", 0.3)], [0.1], []),                 # 浮点边界：0.3 - 0.1
]


@pytest.fixture(scope="module")
def seeded(client, admin):
    db = SessionLocal()
    try:
        org = Organization(name="押金预警院", org_type="township", level="township")
        db.add(org)
        db.flush()
        for i, (deposits, unsettled, settled) in enumerate(CASES):
            patient = Patient(name=f"押金-{i}", id_card=f"32098119900303{i:04d}",
                              ehc_no=f"EHC-DEP-{i:05d}")
            db.add(patient)
            db.flush()
            adm = Admission(patient_id=patient.id, org_id=org.id, ward_id=0, bed_id=0,
                            status="admitted", created_by=1)
            db.add(adm)
            db.flush()
            for deposit_type, amount in deposits:
                db.add(Deposit(admission_id=adm.id, amount=amount, deposit_type=deposit_type,
                               method="cash", operator="test"))
            for amount in unsettled:
                db.add(BillDetail(patient_id=patient.id, admission_id=adm.id, item_code="x",
                                  item_name="项目", unit_price=amount, quantity=1,
                                  amount=amount, created_by=1))
            for amount in settled:
                db.add(BillDetail(patient_id=patient.id, admission_id=adm.id, item_code="y",
                                  item_name="已结项目", unit_price=amount, quantity=1,
                                  amount=amount, settlement_id=1, created_by=1))
        db.commit()
        return org.id
    finally:
        db.close()


def _reference(threshold: float) -> list[int]:
    """改写前那段算法的原样复刻：逐个在院患者算 gap、筛、按 gap 排序。

    **唯独不照抄那个 `.limit(500)`**：扫描上限正是本次要修的缺陷，参照系里留着它，
    参照系自己就会漏报。少了这一句，这几条用例还会**依赖执行顺序**——同模块里
    「第 501 位欠费」那条会铺 520 个在院患者，它先跑，参照系就只看得见前 500 个，
    与接口对不上（实测在打乱顺序时 5 条全红）。参照系要证的是**判定**
    （gap 怎么算、怎么取整、怎么排序）与改写前一致，不是把缺陷也一起复刻。
    """
    db = SessionLocal()
    try:
        alerts = []
        for adm in db.query(Admission).filter(Admission.status == "admitted").order_by(
            Admission.id
        ).all():
            gap = round(deposit_balance(db, adm.id) - unsettled_amount(db, adm.id), 2)
            if gap < threshold:
                alerts.append({"admission_id": adm.id, "gap": gap})
        alerts.sort(key=lambda a: a["gap"])
        return [a["admission_id"] for a in alerts]
    finally:
        db.close()


@pytest.mark.parametrize("threshold", [0, 0.2, 100, 500, 1000])
def test_下推后的预警集合与改写前逐行相同(client, admin, seeded, threshold):
    resp = client.get(f"/api/billing/deposits/alerts?threshold={threshold}", headers=admin)
    assert resp.status_code == 200, resp.text
    got = [a["admission_id"] for a in resp.json()]
    assert got == _reference(threshold), (
        f"阈值 {threshold} 下，SQL 判定与改写前的 Python 判定不是同一个集合/顺序"
    )
    # 返回的三个数也得自洽：gap 必须真的小于阈值
    assert all(a["gap"] < threshold for a in resp.json())
    assert all(a["gap"] == round(a["balance"] - a["unsettled"], 2) for a in resp.json())


def test_第500个之后欠费的在院患者不会被漏掉(client, admin, seeded):
    """原先 `.limit(500)` 限的是扫描范围：第 500 个之后的患者根本没被算过。"""
    db = SessionLocal()
    try:
        org_id = db.query(Organization).filter(Organization.name == "押金预警院").one().id
        base = db.query(Admission).filter(Admission.status == "admitted").count()
        # 先把在院患者填到 500 以上，这些人都交够了钱（不该进预警）
        for i in range(max(0, 520 - base)):
            patient = Patient(name=f"充足-{i}", id_card=f"32098119910101{i:04d}",
                              ehc_no=f"EHC-OK-{i:05d}")
            db.add(patient)
            db.flush()
            adm = Admission(patient_id=patient.id, org_id=org_id, ward_id=0, bed_id=0,
                            status="admitted", created_by=1)
            db.add(adm)
            db.flush()
            db.add(Deposit(admission_id=adm.id, amount=10000, deposit_type="prepay",
                           method="cash", operator="test"))
        db.commit()
        # 最后一个（id 最大，改写前扫描根本轮不到他）欠着 8000
        patient = Patient(name="第501位欠费", id_card="320981199202020001",
                          ehc_no="EHC-TAIL-00001")
        db.add(patient)
        db.flush()
        tail = Admission(patient_id=patient.id, org_id=org_id, ward_id=0, bed_id=0,
                         status="admitted", created_by=1)
        db.add(tail)
        db.flush()
        db.add(BillDetail(patient_id=patient.id, admission_id=tail.id, item_code="z",
                          item_name="住院费", unit_price=8000, quantity=1, amount=8000,
                          created_by=1))
        db.commit()
        tail_id = tail.id
        total_admitted = db.query(Admission).filter(Admission.status == "admitted").count()
    finally:
        db.close()
    assert total_admitted > 500, "在院患者没铺过 500，这条用例证明不了任何事"

    resp = client.get("/api/billing/deposits/alerts", headers=admin)
    assert resp.status_code == 200, resp.text
    alerts = resp.json()
    assert alerts[0]["admission_id"] == tail_id, (
        "第 500 个之后欠费最多的患者被漏掉了——gap 判定还没下推到 SQL"
    )
    assert alerts[0]["gap"] == -8000
    # 最缺钱的排最前，这条排序口径不能因为下推而变
    assert [a["gap"] for a in alerts] == sorted(a["gap"] for a in alerts)
