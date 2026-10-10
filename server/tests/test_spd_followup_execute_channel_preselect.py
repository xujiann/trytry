"""随访执行框的渠道预选这一条的计划渠道，不再固定预选「电话」（P2-1794，第五十三批扫描 AQ3-2，只做页面这一半）。

修前：执行框的渠道下拉恒为 `value: "phone"`。计划渠道已「调整」成面访的随访，医生没动下拉就执行成电话随访——随访看板的
`by_channel` 与居民端随访记录都显示成电话（扫描实测：PATCH channel=visit → 执行 → 渠道 phone，看板 `{'phone': 1}`）。
P2-988（住院护理级别）定过同一形状：表单预选现值，不用固定缺省盖掉。修后预选前置资料里这一条的计划渠道（点「执行」时现取，
比清单那一行新）；计划渠道不在下拉里的（自填）照旧预选电话。后端 `ExecuteIn.channel` 的缺省值不动（不传即沿用计划渠道是接口
语义变更，另登记待裁定）。页面原样拿到 node 里跑（夹具 `tests/page_race.py`），请求转给真接口。
"""
import shutil

import pytest

from page_race import run, spd_page_js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面脚本")

B = "/api/spd"

STEPS = """
  await renderSpdFollowup(); await idle();
  click({ fuExec: String(ARGS.params.record) });
  await idle();
  const [modal] = openModals();
  const channel = selectsIn(modal.html).channel;
  await submitModal(modal, { result: "P21794 按计划渠道执行" });
  return { preselected: channel.filter((o) => o.selected).map((o) => o.value),
           sent: posts.filter((p) => p[1].endsWith("/execute")).map((p) => p[2].channel) };
"""


@pytest.fixture(scope="module")
def plan(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21794 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P21794_R", "name": "P21794 随访方案", "points": [0, 30, 60]})
    assert rule.status_code == 201, rule.text

    def records(card):
        patient = client.post("/api/patients", headers=admin, json={"name": f"P21794 患者{card[-2:]}", "id_card": card})
        assert patient.status_code == 201, patient.text
        resp = client.post(f"{B}/followup-plans", headers=admin, json={
            "patient_id": patient.json()["id"], "rule_id": rule.json()["id"], "org_id": org})
        assert resp.status_code == 201, resp.text
        return resp.json()["items"]

    return records


def _execute(client, admin, record_id):
    return run(client, admin, spd_page_js(), STEPS, params={"record": record_id})


def _channel(client, admin, record_id):
    rows = client.get(f"{B}/followup-records", headers=admin, params={"limit": 200}).json()
    return next(r for r in rows if r["id"] == record_id)["channel"]


def test_计划渠道调成面访的_执行框预选面访_交上去是面访(client, admin, plan):
    record = plan("330102196001011794")[0]
    assert client.patch(f"{B}/followup-records/{record['id']}", headers=admin, json={"channel": "visit"}).status_code == 200
    out = _execute(client, admin, record["id"])
    assert out["preselected"] == ["visit"], out   # 修前恒预选 phone
    assert out["sent"] == ["visit"], out
    assert _channel(client, admin, record["id"]) == "visit"   # 修前执行后成了 phone


def test_计划渠道是电话的照旧预选电话_自填的不在下拉里预选电话(client, admin, plan):
    phone, selfie = plan("330102196002021794")[:2]
    assert phone["channel"] == "phone"
    out = _execute(client, admin, phone["id"])
    assert out["preselected"] == ["phone"] and out["sent"] == ["phone"], out
    assert client.patch(f"{B}/followup-records/{selfie['id']}", headers=admin, json={"channel": "self"}).status_code == 200
    out = _execute(client, admin, selfie["id"])
    assert out["preselected"] == ["phone"] and out["sent"] == ["phone"], out   # 执行框没有「自填」这一项
