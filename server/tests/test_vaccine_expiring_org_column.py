"""临期面板列出全县批次，却不印机构（P2-1504，第四十四批「疫苗批次、冷链与 AEFI」扫描 AH1-10）。

`/api/vaccine-supply/expiring` 不收调用方、不按机构收口（端点收口属待裁定的 P1-49），列的是全县的临期与过期批次；同一页
的批次台账却只列本机构。修前临期表只有「疫苗 批号 厂家 效期 余量 状态」：扫描实测东镇公卫的页面上出现「麻腮风 MMR-W
2026-10-20 8/8 可用」，其实是西镇的批次，看着像自家的。

修后照 P2-1467：临期表加「机构」列，按页面取的机构清单映射名称、一律 `esc()`；映射不到（页面打开之后才建的机构）回显编号，
机构清单取不到也只是回显编号、不把整页掀掉。接口不动。这里把页面原样拿到 node 里跑（`vaccine_page.py`），临期清单取真接口。
"""
import re
import shutil
from datetime import timedelta

import pytest

from conftest import business_today, login
from vaccine_page import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")

B = "/api/vaccine-supply"
EAST, WEST = "P21504 东镇卫生院", "P21504 西镇<中心>卫生院"
STEPS = """
await goto(renderVaccineSupply);
return document.querySelector("#vx-list").innerHTML;
"""


@pytest.fixture(scope="module")
def ctx(client, admin):
    orgs = {name: client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"] for name in (EAST, WEST)}
    created = client.post("/api/users", headers=admin, json={
        "username": "p21504_ph", "password": "passw0rd1", "role": "public_health", "org_id": orgs[EAST]})
    assert created.status_code == 201, created.text
    today = business_today()
    for name, batch_no, days in ((WEST, "P21504-MMR-W", 10), (EAST, "P21504-MMR-E", 20)):
        resp = client.post(f"{B}/batches", headers=admin, json={
            "vaccine_code": "MMR", "vaccine_name": "麻腮风疫苗", "batch_no": batch_no,
            "expire_date": (today + timedelta(days=days)).isoformat(), "org_id": orgs[name], "quantity": 8})
        assert resp.status_code == 201, resp.text
    return {"orgs": orgs, "ph": login(client, "p21504_ph", "passw0rd1")}


def _org_by_batch(html: str) -> dict:
    head = re.findall(r"<th>([^<]*)</th>", html)
    assert head[0] == "机构", head   # 修前：疫苗 批号 厂家 效期 余量 状态
    rows = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in html.split("<tr>")[2:]]
    return {row[head.index("批号")]: row[0] for row in rows}


def test_临期面板印机构名_一律转义(client, ctx):
    html = run(client, ctx["ph"], STEPS)
    # 东镇公卫的页面上，西镇的临期批次写明是西镇的
    assert _org_by_batch(html) == {"P21504-MMR-W": "P21504 西镇&lt;中心&gt;卫生院", "P21504-MMR-E": EAST}


@pytest.mark.parametrize("reply", [(200, []), (503, {"detail": "机构清单暂不可用"})], ids=["映射不到", "取失败"])
def test_映射不到或机构清单取不到_回显编号(client, ctx, reply):
    html = run(client, ctx["ph"], STEPS, overrides={"/api/organizations": reply})
    assert _org_by_batch(html) == {"P21504-MMR-W": str(ctx["orgs"][WEST]), "P21504-MMR-E": str(ctx["orgs"][EAST])}
