"""按批号反查受种者整表列在页内，不再用 alert 只列前 20 条（P2-1501，第四十四批「疫苗批次、冷链与 AEFI」扫描 AH1-4）。

`batch_recipients` 的 docstring：「召回时唯一有用的那个查询」；接口最多回 1000 行，`total` 如实说出这一批共接种多少人次。修前
页面点「受种者」弹 `alert`，只列 `recipients.slice(0, 20)`：扫描实测 25 人次的批次，弹窗 20 行、末行「受种者06」，最早接种的
5 位只体现在「共接种 25 人次」这个数里，召回时界面上无从知道是谁；弹窗也不给记录号。药品侧「发给了谁」早就整表列出。

修后：写进批次台账下面的受种者面板，整表列出记录号 / 受种者 / 剂次 / 接种日期（一律 `esc()`）；接口截断（返回行数少于
`total`）时面板标题写「已列 N / 共 total」。接口不动（端点收口与调阅留痕属待裁定的 P1-49）。这里把页面原样拿到 node 里跑
（`vaccine_page.py`），受种者请求转给真接口。
"""
import re
import shutil

import pytest

from app.database import SessionLocal
from app.models import VaccinationRecord
from vaccine_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")

B = "/api/vaccine-supply"
STEPS = """
await goto(renderVaccineSupply);
await document.querySelector("#page-body").onclick({ target: { dataset: { recipients: String(ARGS.params.batch) } } });
return { html: document.querySelector("#vb-recipients").innerHTML, alerts };
"""


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21501 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _batch(client, admin, org, batch_no, quantity):
    resp = client.post(f"{B}/batches", headers=admin, json={
        "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗", "batch_no": batch_no, "expire_date": "2099-12-31",
        "org_id": org, "quantity": quantity})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _panel(client, admin, batch):
    out = run(client, admin, STEPS, {"batch": batch})
    head = re.search(r"<h3>([^<]*)</h3>", out["html"])
    rows = [re.findall(r"<td>(.*?)</td>", row) for row in out["html"].split("<tr>")[2:]]
    return (head.group(1) if head else None), rows, out["alerts"]


def test_25人次的批次_面板整表列出25行_记录号受种者剂次日期(client, admin, org):
    batch = _batch(client, admin, org, "P21501-HB", 30)
    records = []
    for i in range(1, 26):
        name = f"P21501 受种者{i:02d}" if i != 1 else "P21501 <i>受种者01</i>"   # 最早接种的那位，名字带标签看转义
        patient = client.post("/api/patients", headers=admin, json={
            "name": name, "id_card": f"33010220250101{i:04d}", "birth_date": "2025-01-01"}).json()["id"]
        resp = client.post("/api/vaccination/records", headers=admin, json={
            "patient_id": patient, "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗", "org_id": org,
            "batch_id": batch, "vaccinated_date": "2026-09-01"})
        assert resp.status_code == 201, resp.text
        records.append((resp.json()["id"], patient, name))
    title, rows, alerts = _panel(client, admin, batch)
    assert alerts == []                                    # 修前弹 alert，只列最近 20 位
    assert title == "批号 P21501-HB（乙肝疫苗）共接种 25 人次"   # 全列出来了，不写「已列」
    assert len(rows) == 25
    expected = [[str(rid), f"{name.replace('<', '&lt;').replace('>', '&gt;')}（{pid}）", "第1剂", "2026-09-01"]
                for rid, pid, name in reversed(records)]   # 接口按登记倒序
    assert rows == expected
    assert rows[-1][1].startswith("P21501 &lt;i&gt;受种者01")   # 修前：最早接种的这位界面上看不到


def test_接口截到1000行_面板标题写已列N共total(client, admin, org):
    batch = _batch(client, admin, org, "P21501-BIG", 2000)
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21501 截断受种者", "id_card": "330102202501019999", "birth_date": "2025-01-01"}).json()["id"]
    with SessionLocal() as db:   # 1001 剂一条条走接口太慢：直接落库，接口照常按 1000 行截、total 照实数
        db.add_all([VaccinationRecord(patient_id=patient, vaccine_code="HepB", vaccine_name="乙肝疫苗", dose_no=i,
                                      vaccinated_date="2026-09-01", org_id=org, batch_id=batch, batch_no="P21501-BIG")
                    for i in range(1, 1002)])
        db.commit()
    got = client.get(f"{B}/batches/{batch}/recipients", headers=admin).json()
    assert (got["total"], len(got["recipients"])) == (1001, 1000)   # 判据自证：接口确实截断了
    title, rows, _ = _panel(client, admin, batch)
    assert title == "批号 P21501-BIG（乙肝疫苗）共接种 1001 人次，已列 1000 / 共 1001"
    assert len(rows) == 1000
