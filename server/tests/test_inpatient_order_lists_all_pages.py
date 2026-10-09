"""医嘱单、护理记录「关联医嘱」下拉续页取全，执行记录列不全时写明「已列 N / 共 M」（P2-1693，第五十批扫描 AN4-1）。

住院管理页的医嘱单（`/api/inpatient/orders?admission_id=…`）、住院临床文书护理记录的「关联医嘱」下拉（同一接口带
`&status=active`）原先只取接口缺省的第一页 200 条（按医嘱号倒序）。修前实测：入院当天开 1 条长期医嘱、之后陆续开 200 条临时
医嘱（临时医嘱执行过也一直「执行中」，P2-281），两处都只回 200 行、X-Total-Count 201——被挤出去的正是入院当天那条长期医嘱：
医嘱单上没有它的「登记执行」「停止」按钮，护理记录的关联医嘱下拉里选不到它，页面也不读总数、不提示截断。

修法照 P2-1333（住院清单）：两处改用 shared.js 的 `fetchAllPages` 续页取全（一次住院的医嘱以住院天数封顶）；执行记录只看最近的，
照旧取一页，按 X-Total-Count 在标题上写明「已列 N / 共 M」（`api(…, { withTotal: true })`，P2-1547）。后端不动。
页面原样拿到 node 里跑、请求转给真接口（夹具见 `tests/inpatient_page.py`）。
"""
import re
import shutil
from pathlib import Path

import pytest
from conftest import login
from inpatient_page import run

from app.database import SessionLocal

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import InpatientOrder, OrderExecution, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21693 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for username, role in (("p21693_doc", "doctor"), ("p21693_nurse", "operator")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org})
        assert created.status_code in (200, 201), created.text
    doctor, nurse = login(client, "p21693_doc", "passw0rd1"), login(client, "p21693_nurse", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21693 神经内科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21693-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21693 患者", "id_card": "330106195505051693", "gender": "男"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=doctor, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "脑梗死"})
    assert adm.status_code == 201, adm.text
    adm = adm.json()["id"]
    long_order = client.post("/api/inpatient/orders", headers=doctor, json={
        "admission_id": adm, "order_type": "long", "content": "P21693 阿司匹林肠溶片 100mg po qd"})
    assert long_order.status_code == 201, long_order.text
    long_order = long_order.json()["id"]
    with SessionLocal() as db:   # 住院期间陆续开的 200 条临时医嘱（逐条走接口太慢，直接落库），都还「执行中」（P2-281）
        db.add_all([InpatientOrder(admission_id=adm, order_type="temp", content=f"P21693 临时医嘱 {i:03d}",
                                   created_by_name="P21693 医生") for i in range(200)])
        executor = db.query(User).filter(User.username == "p21693_nurse").one().id
        # 入院以来每天两次的执行登记：201 笔，比执行记录一页（200）多一笔
        db.add_all([OrderExecution(inpatient_order_id=long_order, executed_by=executor, note=f"第 {i + 1} 次")
                    for i in range(201)])
        db.commit()
    return {"adm": adm, "long": long_order, "doctor": doctor, "nurse": nurse}


def test_接口缺省一页只到200条_入院长期医嘱在第二页(client, world):
    for query in ("", "&status=active"):
        first = client.get(f"/api/inpatient/orders?admission_id={world['adm']}{query}", headers=world["doctor"])
        assert (len(first.json()), first.headers["X-Total-Count"]) == (200, "201")
        assert world["long"] not in [o["id"] for o in first.json()]   # 修前页面就是这么取的：他不在


def test_医嘱单取全_入院长期医嘱有登记执行与停止按钮(client, world):
    steps = """
      await renderInpatient();
      await click({ orders: String(ARGS.params.adm) });
      return htmlOf("#inp-orders");
    """
    html = run(client, world["doctor"], steps, {"adm": world["adm"]})
    rows = re.findall(r'data-exec-list="(\d+)"', html)
    assert len(rows) == 201, len(rows)   # 修前 200 行
    assert rows[-1] == str(world["long"])   # 与接口同序（医嘱号倒序），入院那条在最后
    assert f'data-exec-add="{world["long"]}"' in html and f'data-stop-order="{world["long"]}"' in html   # 修前两个按钮都没有


def _active_orders_fetch() -> str:
    """住院临床文书取「关联医嘱」下拉的那一句：直接 `api(…)`（修前，只有一页）或 `fetchAllPages(api, …)`。"""
    src = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    found = re.search(r"(?:fetchAllPages\(api, |\bapi\()`/api/inpatient/orders\?admission_id=\$\{current\}&status=active`\)",
                      src)
    assert found, "pages-mgmt.js 里找不到取在用医嘱的那一句"
    return found.group(0)


def test_护理记录关联医嘱下拉取全_入院长期医嘱选得到(client, world):
    expression = _active_orders_fetch()
    steps = f"const current = ARGS.params.adm; const rows = await {expression}; return rows.map((o) => o.id);"
    ids = run(client, world["nurse"], steps, {"adm": world["adm"]})
    assert len(ids) == 201 and world["long"] in ids, len(ids)   # 修前 200 条、他不在
    assert expression.startswith("fetchAllPages(api, "), expression   # 修前 api(`…&status=active`)


def test_执行记录只看最近一页_列不全时标题写已列与共几条(client, world):
    steps = """
      await renderInpatient();
      await click({ execList: String(ARGS.params.order) });
      return htmlOf("#inp-exec");
    """
    html = run(client, world["nurse"], steps, {"order": world["long"]})
    title = re.search(r"<h3[^>]*>([^<]*)</h3>", html).group(1)
    assert title == f"医嘱 {world['long']} 的执行记录（已列 200 / 共 201）", title   # 修前没有总数，看不出截断
    assert html.count("<tr><td>") == 200
    assert "<td>第 201 次</td>" in html and "<td>第 1 次</td>" not in html   # 最近的在前


def test_三处取数形状_医嘱清单不再只取一页():
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    assert "fetchAllPages(api, `/api/inpatient/orders?admission_id=${d.orders}`)" in clinical
    for name, src in (("pages-clinical.js", clinical), ("pages-mgmt.js", mgmt)):
        assert not re.search(r"\bapi\(`/api/inpatient/orders\?admission_id=", src), name   # 只取一页的写法不许回来
    assert re.search(r"api\(`/api/inpatient/orders/\$\{encodeURIComponent\(orderId\)\}/executions`, \{ withTotal: true \}\)",
                     clinical)
