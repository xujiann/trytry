"""一人两病在桌面端录随访：指标按病种归各自的档案、各自定级（P2-1541，第四十五批扫描 AI2-1）。

桌面端慢病页的随访表单对任何档案都摆收缩压、舒张压、空腹血糖三格，`chronic.add_followup` 原先把请求里的读数全记进所选
档案、只按这份档案的病种定级。扫描实测（修前代码）：钱七有高血压、糖尿病两份档案，在高血压档案上一次录 150/95 与空腹
血糖 16.7——201、「2 级、不建议上转」；血糖存进了高血压那条随访，糖尿病档案仍是 1 级、随访史为空，照样在超期名单里，
诊间提醒「2型糖尿病 随访已超期」。同一组读数走 FHIR 入站（P2-848 已按指标归病种拆开），糖尿病档案定 3 级。

修法照 P2-848 的拆法（指标 → 病种的映射挪进 `chronic.py` 作唯一一份，FHIR 入站改为引用它）：属于另一病种、且本档案分级
规则用不到的标准指标（收缩压、舒张压、空腹血糖），该患者有那个病种的档案，就拆成那份档案的一条随访、按那份档案的规则定级，
下次到期按那份档案的病种周期自动建议（不套用本次手填的到期日）；没有那份档案的照旧记在本条。回执末尾只增 `others`
（拆出去的各条：档案号、病种、指标、级别），没拆就是空列表。同一事务提交，收缩压须高于舒张压等既有校验照旧。
"""
import shutil
from datetime import timedelta

import pytest

from chronic_followup_pages import run
from conftest import business_today

from app.database import SessionLocal
from app.models import ChronicPatient

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 两份档案建档时都填已过的到期日：修前糖尿病档案一直挂在超期名单里
STALE_DUE = "2026-09-01"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21541 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    gdm = client.post("/api/chronic/disease-types", headers=admin, json={
        "code": "p21541_gdm", "name": "P21541 妊娠期糖尿病", "followup_interval_days": 30,
        "level_rules": {"require_all": True, "metrics": [
            {"key": "glucose", "name": "空腹血糖", "unit": "mmol/L", "direction": "high", "level3": 7.0, "level2": 5.1}]}})
    assert gdm.status_code == 201, gdm.text
    made = {}
    for n, (tag, diseases) in enumerate((("两病", ["hypertension", "diabetes"]), ("两病反向", ["hypertension", "diabetes"]),
                                         ("只有高血压", ["hypertension"]), ("两病其他指标", ["hypertension", "diabetes"]),
                                         ("规则用得到", ["p21541_gdm", "diabetes"]), ("录反", ["hypertension", "diabetes"]),
                                         ("弹窗", ["hypertension", "diabetes"]))):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21541 {tag}", "id_card": f"33010219650101541{n}", "gender": "女",
            "birth_date": "1965-01-01"}).json()["id"]
        chronic = {}
        for disease in diseases:
            resp = client.post("/api/chronic", headers=admin, json={
                "patient_id": patient, "disease": disease, "managed_by_org_id": org, "next_due": STALE_DUE})
            assert resp.status_code == 201, resp.text
            chronic[disease] = resp.json()["id"]
        made[tag] = {"patient": patient, "chronic": chronic}
    return made


def _followup(client, admin, chronic_id, body):
    return client.post(f"/api/chronic/{chronic_id}/followups", headers=admin, json=body)


def _history(client, admin, chronic_id):
    return [(f["sbp"], f["dbp"], f["glucose"], f["metrics"], f["guidance"], f["next_due"])
            for f in client.get(f"/api/chronic/{chronic_id}/followups", headers=admin).json()]


def _archive(chronic_id):
    with SessionLocal() as db:
        row = db.get(ChronicPatient, chronic_id)
        return row.level, row.next_due


def _overdue(client, admin):
    return {c["id"] for c in client.get("/api/chronic/overdue", headers=admin).json()}


def test_高血压档案上录血压加血糖_血糖拆进糖尿病档案_各自定级(client, admin, world):
    htn, dm = world["两病"]["chronic"]["hypertension"], world["两病"]["chronic"]["diabetes"]
    assert {htn, dm} <= _overdue(client, admin)
    manual = (business_today() + timedelta(days=30)).isoformat()
    suggested = (business_today() + timedelta(days=90)).isoformat()   # 糖尿病的病种周期
    resp = _followup(client, admin, htn, {"sbp": 150, "dbp": 95, "glucose": 16.7, "metrics": {},
                                          "next_due": manual, "guidance": "限盐，控制主食"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["level"], body["next_due"], body["refer_up_suggested"]) == (2, manual, False), body
    # 修前回执没有这个键：血糖 16.7 记在高血压档案里，糖尿病档案不定级
    assert body["others"] == [{"chronic_id": dm, "disease": "diabetes", "values": {"glucose": 16.7}, "level": 3}], body
    assert list(body)[-1] == "others"   # 回执末尾只增这一个键
    assert body["followup"]["glucose"] is None
    assert _history(client, admin, htn) == [(150.0, 95.0, None, {}, "限盐，控制主食", manual)]
    # 修前 []；手填的到期日只管所选档案，拆过去的按糖尿病的病种周期自动建议
    assert _history(client, admin, dm) == [(None, None, 16.7, {}, "限盐，控制主食", suggested)]
    assert _archive(htn) == (2, manual)
    assert _archive(dm) == (3, suggested)   # 修前 (1, "2026-09-01")
    assert not {htn, dm} & _overdue(client, admin)   # 修前糖尿病档案照样超期


def test_糖尿病档案上录血压加血糖_血压拆进高血压档案(client, admin, world):
    htn, dm = world["两病反向"]["chronic"]["hypertension"], world["两病反向"]["chronic"]["diabetes"]
    resp = _followup(client, admin, dm, {"sbp": 150, "dbp": 95, "glucose": 6.1})
    assert resp.status_code == 201, resp.text
    assert resp.json()["level"] == 1, resp.json()
    assert resp.json()["others"] == [{"chronic_id": htn, "disease": "hypertension",
                                      "values": {"sbp": 150.0, "dbp": 95.0}, "level": 2}], resp.json()
    assert [row[:3] for row in _history(client, admin, dm)] == [(None, None, 6.1)]
    assert [row[:3] for row in _history(client, admin, htn)] == [(150.0, 95.0, None)]   # 修前 []
    assert _archive(htn)[0] == 2


def test_没有另一病种的档案_照旧记在本条(client, admin, world):
    htn = world["只有高血压"]["chronic"]["hypertension"]
    resp = _followup(client, admin, htn, {"sbp": 150, "dbp": 95, "glucose": 16.7})
    assert resp.status_code == 201, resp.text
    assert (resp.json()["level"], resp.json()["others"]) == (2, []), resp.json()
    assert [row[:3] for row in _history(client, admin, htn)] == [(150.0, 95.0, 16.7)]   # 不报错、不变行为


def test_写在其他指标里的血糖_同列一样拆(client, admin, world):
    """分级取值「列为空就取 metrics 同名键」（`_metric_value`）：其他指标框里写 glucose=16.7 的，与空腹血糖格同一口径。"""
    htn, dm = world["两病其他指标"]["chronic"]["hypertension"], world["两病其他指标"]["chronic"]["diabetes"]
    resp = _followup(client, admin, htn, {"sbp": 135, "dbp": 85, "metrics": {"glucose": 16.7, "bmi": 27.5}})
    assert resp.status_code == 201, resp.text
    assert resp.json()["others"] == [{"chronic_id": dm, "disease": "diabetes", "values": {"glucose": 16.7},
                                      "level": 3}], resp.json()
    assert [row[:4] for row in _history(client, admin, htn)] == [(135.0, 85.0, None, {"bmi": 27.5})]
    assert [row[:4] for row in _history(client, admin, dm)] == [(None, None, None, {"glucose": 16.7})]
    assert _archive(dm)[0] == 3


def test_本档案分级规则用得到的指标不拆(client, admin, world):
    gdm, dm = world["规则用得到"]["chronic"]["p21541_gdm"], world["规则用得到"]["chronic"]["diabetes"]
    resp = _followup(client, admin, gdm, {"glucose": 7.5})
    assert resp.status_code == 201, resp.text
    assert (resp.json()["level"], resp.json()["others"]) == (3, []), resp.json()
    assert [row[:3] for row in _history(client, admin, gdm)] == [(None, None, 7.5)]
    assert _history(client, admin, dm) == []
    assert _archive(dm) == (1, STALE_DUE)


def test_血压录反照旧422_两份档案都不动(client, admin, world):
    htn, dm = world["录反"]["chronic"]["hypertension"], world["录反"]["chronic"]["diabetes"]
    resp = _followup(client, admin, htn, {"sbp": 85, "dbp": 135, "glucose": 16.7})
    assert resp.status_code == 422, resp.text
    assert _history(client, admin, htn) == _history(client, admin, dm) == []
    assert _archive(htn) == _archive(dm) == (1, STALE_DUE)


@needs_node
def test_桌面端随访回执弹窗说出拆到了哪份档案(client, admin, world):
    """慢病页「随访录入」原样跑在 node 里、请求转给真接口：回执弹窗末尾逐条写出拆到了哪份档案、记了什么、定了几级。"""
    htn, dm = world["弹窗"]["chronic"]["hypertension"], world["弹窗"]["chronic"]["diabetes"]
    got = run(client, admin, "admin", r"""
await renderChronic();
await submitForm("#fu-form", { chronic_id: String(ARGS.params.htn), sbp: "150", dbp: "95", glucose: "16.7",
  metrics: "", next_due: "", guidance: "" });
return { alerts, msg: msgOf("#chronic-msg") };
""", {"htn": htn}, send_writes=True)
    assert got["msg"] == "", got
    (text,) = got["alerts"]
    assert text.startswith("分级：2 级\n下次随访："), text
    # 修前弹窗只说「2 级」：糖尿病档案定了 3 级、该上转，页面上一个字没有
    assert text.endswith(f"\n另记入档案 {dm}（2型糖尿病）：空腹血糖 16.7，分级 3 级（建议上转！）"), text
