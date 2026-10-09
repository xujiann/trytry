"""住院页医嘱「停止」先确认，写明医嘱内容与「停止后不可恢复，需重新开立」（P2-1695，第五十批扫描 AN4-3）。

修前：医嘱单上红色「停止」紧挨「登记执行」，点一下就 POST `/stop`（`if (d.stopOrder) { await api(…/stop…) }`）——停医嘱没有恢复
入口，点错只能另开一条新医嘱，执行记录从此分在两条医嘱上，医嘱单留下一条错误的「某医生某刻停止」；点完立刻整页重画、医嘱面板
随之关掉，点错了哪一行当场看不到。P2-43 立的规矩是点一下就生效的不可逆操作必须先确认，P2-1334 已给同页「出院」补了确认；
确认闸门（`test_frontend_destructive_confirm_guard.py`）的判据却不认 `/stop`，一直绿着（本条把 `stop` 补进判据）。

闸门管「确认在不在」；这里把住院页原样拿到 node 里跑（夹具见 `tests/inpatient_page.py`）：确认框写明是哪条医嘱（内容经
spdModal 转义）与后果，点「取消」不发停止请求，点「确定」才停。真浏览器里取消仍执行中、确定才停止，由端到端
`test_住院页停医嘱先确认_取消仍执行中_确定才停止` 按接口核对。
"""
import shutil

import pytest
from conftest import login
from inpatient_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

STEPS = """
  await renderInpatient();
  await click({ orders: String(ARGS.params.adm) });
  const pending = click({ stopOrder: String(ARGS.params.order) });
  await tick();
  const modal = lastModal();
  const html = modal ? modal.html : "";
  const before = posts.length;
  if (modal) { if (ARGS.params.confirm) await submitModal(modal); else await cancelModal(modal); }
  await pending;
  return { html, before, posts, routed: ROUTED };
"""


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21695 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21695_doc", "password": "passw0rd1", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    doctor = login(client, "p21695_doc", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21695 内科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21695-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21695 患者", "id_card": "330106196707071695", "gender": "女"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=doctor, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    order = client.post("/api/inpatient/orders", headers=doctor, json={
        "admission_id": adm.json()["id"], "order_type": "long", "content": "P21695 头孢呋辛 1.5g ivgtt <b>bid</b>"})
    assert order.status_code == 201, order.text
    return {"adm": adm.json()["id"], "order": order.json()["id"], "doctor": doctor}


def _status(client, world) -> str:
    rows = client.get(f"/api/inpatient/orders?admission_id={world['adm']}", headers=world["doctor"]).json()
    return next(o["status"] for o in rows if o["id"] == world["order"])


def test_点停止先弹确认_写明医嘱内容与不可恢复_取消即不停(client, world):
    got = run(client, world["doctor"], STEPS, {"adm": world["adm"], "order": world["order"], "confirm": False},
              send_writes=True)
    assert got["before"] == 0, got["posts"]   # 修前点一下就 POST /stop，还没轮到确认就停了
    assert "P21695 头孢呋辛 1.5g ivgtt &lt;b&gt;bid&lt;/b&gt;" in got["html"]   # 是哪一条医嘱：内容经转义原样写出
    assert "长期医嘱" in got["html"] and "停止后不可恢复" in got["html"] and "重新开立" in got["html"]
    assert got["posts"] == [] and got["routed"] == 0   # 点「取消」不停、不重画
    assert _status(client, world) == "active"


def test_点确定才停(client, world):
    got = run(client, world["doctor"], STEPS, {"adm": world["adm"], "order": world["order"], "confirm": True},
              send_writes=True)
    assert got["posts"] == [["POST", f"/api/inpatient/orders/{world['order']}/stop", None]]
    assert got["routed"] == 1
    assert _status(client, world) == "stopped"
