"""成员端「监测记录」「看趋势」按患者号查：只画最后一次查的那一位，结果区写明是谁（P2-1795，第五十三批扫描 AQ2-8）。

修前：两处都是「先清空 → await → 写」，不比对请求序号——先查甲、立刻改查乙，甲的回包晚到就画在乙的号下（扫描实测：甲收缩压
182、乙 124，乙的回包先到时显示 124，甲的晚到后框里是乙、结果区成了 182），结果区与表格里都不写是谁（P2-1009 的注释自己写着
「清单里不写是谁」）；甲查失败的报错晚到，同样写在乙的结果旁边。修法照 P2-1012：取数前记下序号，成功与出错两支回来都先比对、
过期就丢；结果区头写上患者号（监测出参不带姓名）。同形的一组只读面板（健康日历、新生儿筛查史、诊间医防提醒、危急值留痕、
住院执行记录、共享诊断修订史、流程流转记录）由 `test_panel_fetch_latest_wins.py` 的静态钉盯着。

页面原样拿到 node 里跑（夹具 `tests/page_race.py`，可扣住个别回包），请求转给真接口。
"""
import shutil

import pytest

from page_race import run, spd_page_js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面脚本")

B = "/api/spd"

#: 先查甲（回包扣住）、立刻改查乙，乙画完再放行甲；`ARGS.params.trend` 为真时点「看趋势」，否则交「查记录」
STEPS = """
  await renderSpdMember(); await idle();
  const form = $("#spd-meas-query");
  const ask = () => (ARGS.params.trend ? $("#spd-meas-trend-btn").onclick() : form.onsubmit({ preventDefault() {} }));
  hold(ARGS.params.held);
  form.patient_id = String(ARGS.params.a); form.metric = "bp_sys";
  ask();
  await idle();
  form.patient_id = String(ARGS.params.b);
  ask();
  await idle();
  const afterB = htmlOf("#spd-meas-result");
  const released = await release(ARGS.params.held);
  return { afterB, released, final: htmlOf("#spd-meas-result"), msg: textOf("#spd-meas-msg") };
"""


@pytest.fixture(scope="module")
def pair(client, admin):
    ids = []
    for name, card, value in (("P21795钱甲", "330102194801011795", 182), ("P21795冯乙", "330102194902021795", 124)):
        patient = client.post("/api/patients", headers=admin, json={"name": name, "id_card": card})
        assert patient.status_code == 201, patient.text
        resp = client.post(f"{B}/measurements", headers=admin, json={
            "patient_id": patient.json()["id"], "metric": "bp_sys", "value": value, "unit": "mmHg"})
        assert resp.status_code == 201, resp.text
        ids.append(patient.json()["id"])
    return ids


def test_监测记录_甲的回包晚到不盖乙_结果区写是乙(client, admin, pair):
    a, b = pair
    out = run(client, admin, spd_page_js(), STEPS,
              params={"a": a, "b": b, "held": f"patient_id={a}&limit=30&metric=bp_sys", "trend": False})
    assert "<td>124mmHg</td>" in out["afterB"] and out["released"] == 1, out
    assert "<td>124mmHg</td>" in out["final"] and "182" not in out["final"], out   # 修前甲的 182 盖在乙的号下
    assert f"患者 {b} 的监测记录" in out["final"] and f"患者 {a} " not in out["final"], out   # 修前结果区不写是谁


def test_看趋势_甲的回包晚到不盖乙_结果区写是乙(client, admin, pair):
    a, b = pair
    out = run(client, admin, spd_page_js(), STEPS,
              params={"a": a, "b": b, "held": f"trend?patient_id={a}&metric=bp_sys", "trend": True})
    assert "<td>124</td>" in out["afterB"] and out["released"] == 1, out
    assert "<td>124</td>" in out["final"] and "182" not in out["final"], out
    assert f"患者 {b} · bp_sys 的趋势" in out["final"], out


def test_甲查失败的报错晚到_不写在乙的结果旁(client, admin, pair):
    a, b = pair
    held = f"patient_id={a}&limit=30&metric=bp_sys"

    def refuse_a(method, path, body):   # 甲这一查被拒（如换成了别家的患者号）；其余照转真接口
        return (403, {"detail": "P21795 无权调阅甲"}) if path.endswith(held) else None

    out = run(client, admin, spd_page_js(), STEPS, params={"a": a, "b": b, "held": held, "trend": False},
              responder=refuse_a)
    assert out["released"] == 1, out
    assert "<td>124mmHg</td>" in out["final"] and f"患者 {b} 的监测记录" in out["final"], out
    assert out["msg"] == "", out   # 修前甲的报错写进消息行，看着像乙查失败了
