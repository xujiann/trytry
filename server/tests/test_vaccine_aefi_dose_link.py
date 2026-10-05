"""从界面上报的 AEFI 关联得到剂次：接种史表印记录号与批号、每一剂可直接上报，AEFI 表单的剂次改成下拉（P2-1499，
第四十四批「疫苗批次、冷链与 AEFI」扫描 AH1-1）。

模块口径 3「AEFI 关联到剂次」（`vaccine_supply` 模块说明），`report_aefi` 给了接种记录就从记录带出疫苗与批号。修前 AEFI 表单
要人手填「接种记录ID」，可全站没有一处页面显示记录号：接种史表只印疫苗、剂次、日期、机构，记录号藏在打印按钮的 data
属性里。扫描实测从界面只能不带剂次上报——批号恒空：按批号查不到这一例，「发病不得早于接种」（P2-884）不拦，统计按上报
机构而不是接种机构归（P2-942 退回原样），接种单位所在片区的 AEFI 数为 0。

修后：接种史表加「记录号」「批号」两列，每行一个「上报 AEFI」，带着这一剂去「疫苗批次与冷链」页（AEFI 表单在那一页，
照 P2-1316 的跨页带条件：只放内存、取用一次即清），患者号与剂次预填好；AEFI 表单的剂次是下拉——填好患者号按
`/api/vaccination/records` 取他的各剂次，选项写「疫苗名 第N剂 日期 批号」，首项「不关联」；取不到写进表单的消息行。
这里把两页原样拿到 node 里跑（`vaccine_page.py`），页面的请求转给真接口。
"""
import re
import shutil

import pytest

from conftest import login
from vaccine_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")

B = "/api/vaccine-supply"
OPTION = re.compile(r'<option value="([^"]*)">([^<]*)</option>')


@pytest.fixture(scope="module")
def data(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21499 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    other = client.post("/api/organizations", headers=admin, json={
        "name": "P21499 西镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21499_doc_w", "password": "passw0rd1", "role": "doctor", "org_id": other})
    assert created.status_code == 201, created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21499 受种者", "id_card": "330102202501011499", "birth_date": "2025-01-01"}).json()["id"]
    batch = client.post(f"{B}/batches", headers=admin, json={
        "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗", "batch_no": "P21499-HB<1>", "expire_date": "2099-12-31",
        "org_id": org, "quantity": 10}).json()["id"]
    doses = []
    for body in ({"batch_id": batch, "vaccinated_date": "2026-09-01"},   # 挂了批次的一剂
                 {"vaccinated_date": "2026-09-20"}):                    # 存量那样没挂批次的一剂
        resp = client.post("/api/vaccination/records", headers=admin, json={
            "patient_id": patient, "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗", "org_id": org, **body})
        assert resp.status_code == 201, resp.text
        doses.append(resp.json())
    return {"org": org, "patient": patient, "doses": doses, "outsider": login(client, "p21499_doc_w", "passw0rd1")}


def _history(client, admin, data):
    return run(client, admin, """
await goto(renderVaccination);
await document.querySelector("#vac-hist").onsubmit({ preventDefault() {}, target: form({ patient_id: String(ARGS.params.pid) }) });
return document.querySelector("#vac-hist-result").innerHTML;
""", {"pid": data["patient"]})


def test_接种史表印记录号与批号_每一剂可上报AEFI(client, admin, data):
    html = _history(client, admin, data)
    head = re.findall(r"<th>([^<]*)</th>", html)
    assert head == ["记录号", "疫苗", "剂次", "日期", "批号", "机构", "操作"], head   # 修前：疫苗 剂次 日期 机构 操作
    first, second = data["doses"]
    rows = [re.sub(r"<[^>]+>", " ", row) for row in html.split("<tr>")[2:]]
    # 接口按登记倒序：没挂批次的第 2 剂在前，批号写「—」；批号经 esc()
    assert re.sub(r"\s+", " ", rows[0]).split()[:5] == [str(second["id"]), "乙肝疫苗", "第2剂", "2026-09-20", "—"]
    assert re.sub(r"\s+", " ", rows[1]).split()[:5] == [str(first["id"]), "乙肝疫苗", "第1剂", "2026-09-01",
                                                         "P21499-HB&lt;1&gt;"]
    for dose in data["doses"]:
        assert f'data-aefi-dose="{dose["id"]}" data-aefi-pid="{data["patient"]}">上报 AEFI</button>' in html


def test_AEFI表单的剂次是下拉_按患者号列出各剂次_首项不关联(client, admin, data):
    out = run(client, admin, """
await goto(renderVaccineSupply);
await document.querySelector("#aefi-pid").onchange({ target: { value: String(ARGS.params.pid) } });
const dose = document.querySelector("#aefi-dose");
return { options: dose.innerHTML, value: dose.value, msg: document.querySelector("#aefi-msg").textContent,
         form: document.querySelector("#page-body").innerHTML };
""", {"pid": data["patient"]})
    first, second = data["doses"]
    assert OPTION.findall(out["options"]) == [
        ("", "不关联"),
        (str(second["id"]), "乙肝疫苗 第2剂 2026-09-20 无批号"),
        (str(first["id"]), "乙肝疫苗 第1剂 2026-09-01 批号 P21499-HB&lt;1&gt;"),
    ]
    assert out["value"] == "" and out["msg"] == ""   # 缺省不关联，不替人选
    assert 'placeholder="接种记录ID' not in out["form"]   # 修前：手填记录号的数字框
    assert '<select name="record_id" id="aefi-dose">' in out["form"]


def test_取不到这位患者的接种记录_原因写进表单的消息行(client, data):
    out = run(client, data["outsider"], """
await goto(renderVaccineSupply);
await document.querySelector("#aefi-pid").onchange({ target: { value: String(ARGS.params.pid) } });
const msg = document.querySelector("#aefi-msg");
return { options: document.querySelector("#aefi-dose").innerHTML, msg: [msg.textContent, msg.className] };
""", {"pid": data["patient"]})
    assert OPTION.findall(out["options"]) == [("", "不关联")]
    assert out["msg"][1] == "msg err" and out["msg"][0].startswith("无权调阅该患者档案"), out["msg"]


def test_从接种史行点上报AEFI_表单预填患者号与这一剂_交上去带出批号(client, admin, data):
    first = data["doses"][0]
    out = run(client, admin, """
await goto(renderVaccination);
await document.querySelector("#vac-hist").onsubmit({ preventDefault() {}, target: form({ patient_id: String(ARGS.params.pid) }) });
const html = document.querySelector("#vac-hist-result").innerHTML;
const [, aefiDose, aefiPid] = html.match(new RegExp(`data-aefi-dose="(${ARGS.params.rid})" data-aefi-pid="(\\\\d+)"`));
await document.querySelector("#vac-hist-result").onclick({ target: { dataset: { aefiDose, aefiPid } } });
const went = navs.slice();
await goto(renderVaccineSupply);
const prefilled = { pid: document.querySelector("#aefi-pid").value, dose: document.querySelector("#aefi-dose").value,
                    scrolled: document.querySelector("#aefi-form").scrolled };
document.querySelector("#aefi-form").onsubmit({ preventDefault() {}, target: form({
  patient_id: String(prefilled.pid), record_id: prefilled.dose, reaction_type: "severe", symptom: "P21499 高热",
  onset_date: "2026-09-02", org_id: String(ARGS.params.org) }) });
await settled();
await goto(renderVaccineSupply);   // 再从导航进来：带来的条件只用一次
return { went, prefilled, routed, again: { pid: document.querySelector("#aefi-pid").value,
                                           dose: document.querySelector("#aefi-dose").value } };
""", {"pid": data["patient"], "rid": first["id"], "org": data["org"]})
    assert out["went"] == ["vaccinesupply"]   # AEFI 表单在「疫苗批次与冷链」页
    assert out["prefilled"] == {"pid": data["patient"], "dose": str(first["id"]), "scrolled": True}
    assert out["routed"] == 1   # 上报成功照旧整页重画
    assert out["again"] == {"pid": "", "dose": ""}
    reports = client.get(f"{B}/aefi", params={"patient_id": data["patient"]}, headers=admin).json()
    assert [(r["record_id"], r["batch_no"], r["symptom"]) for r in reports] == [(first["id"], "P21499-HB<1>", "P21499 高热")]
    # 带上批号，按批号就查得到这一例（修前从界面上报只能不带剂次，批号恒空）
    assert [r["id"] for r in client.get(f"{B}/aefi", params={"batch_no": "P21499-HB<1>"}, headers=admin).json()] == \
        [reports[0]["id"]]
