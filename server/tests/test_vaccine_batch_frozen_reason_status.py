"""批次台账与临期面板的状态列，封存的批次写出封存原因（P2-1502，第四十四批「疫苗批次、冷链与 AEFI」扫描 AH1-6）。

封存框写着「封存原因（会印在批次状态列上）」，可两张表的状态列只印后端 `unusable_reason` 的「已封存」：扫描实测召回封存的
MMR-01 一行是「麻腮风疫苗 MMR-01 — 2027-06-30 5/5 已封存 受种者 解除封存」，召回原因整页都看不到——旁边就是一点即发的
「解除封存」（P2-1237），看的人分不清这批是超温待评估还是被召回。

修后状态列写「已封存：{frozen_reason}」（经 `esc()`），原因为空照旧「已封存」；过期优先的既有顺序不变（过期又封存的照旧写
「已过效期」）；后端 `unusable_reason` 不动。这里把页面原样拿到 node 里跑（`vaccine_page.py`），批次台账与临期清单取真接口。
"""
import re
import shutil
from datetime import timedelta

import pytest

from app.database import SessionLocal
from app.models import VaccineBatch
from conftest import business_today
from vaccine_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")

B = "/api/vaccine-supply"
REASON = "厂家召回：效价不合格 <禁止使用>"


@pytest.fixture(scope="module")
def tables(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21502 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    today = business_today()
    ids = {}
    for batch_no, days in (("P21502-RECALL", 10), ("P21502-EXPIRED", -1), ("P21502-NOREASON", 20), ("P21502-OK", 25)):
        resp = client.post(f"{B}/batches", headers=admin, json={
            "vaccine_code": "MMR", "vaccine_name": "麻腮风疫苗", "batch_no": batch_no,
            "expire_date": (today + timedelta(days=days)).isoformat(), "org_id": org, "quantity": 5})
        assert resp.status_code == 201, resp.text
        ids[batch_no] = resp.json()["id"]
    for batch_no, reason in (("P21502-RECALL", REASON), ("P21502-EXPIRED", "冷链超温 12℃ 待评估")):
        frozen = client.post(f"{B}/batches/{ids[batch_no]}/freeze", headers=admin, json={"frozen_reason": reason})
        assert frozen.status_code == 200, frozen.text
    with SessionLocal() as db:   # 原因为空的封存只会是存量（封存接口必填原因）：直接落库
        db.get(VaccineBatch, ids["P21502-NOREASON"]).status = "frozen"
        db.commit()
    return run(client, admin, """
await goto(renderVaccineSupply);
return { ledger: document.querySelector("#vb-list").innerHTML, expiring: document.querySelector("#vx-list").innerHTML };
""")


def _status_by_batch(html: str) -> dict:
    """表格里每个批号那一行的「状态」格（按表头找列，别的列增减不影响）。"""
    head = re.findall(r"<th>([^<]*)</th>", html)
    rows = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in html.split("<tr>")[2:]]
    return {row[head.index("批号")]: row[head.index("状态")].strip() for row in rows}


EXPECTED = {
    "P21502-RECALL": "已封存：厂家召回：效价不合格 &lt;禁止使用&gt;",   # 修前只有「已封存」
    "P21502-EXPIRED": "已过效期",                                       # 过期优先照旧，不带封存原因
    "P21502-NOREASON": "已封存",                                        # 原因为空照旧
    "P21502-OK": '<span class="tag ok">可用</span>',
}


def test_批次台账的状态列_封存的写出原因_经转义(tables):
    assert _status_by_batch(tables["ledger"]) == EXPECTED


def test_临期面板的状态列_同一句(tables):
    assert _status_by_batch(tables["expiring"]) == EXPECTED
