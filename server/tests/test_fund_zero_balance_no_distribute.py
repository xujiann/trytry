"""收支相抵（结余 0）的基金池，页面照样摆公式框和「按公式分配」，点了必 409（P2-1483，第四十三批扫描 AG3-9，补充 P2-261）。

`fund.distribute` 结余为 0 一律 409「本池收支相抵、结余为 0，无结余可分配」（P2-261 只修了报错文案）。修前页面的
`renderSettlement` 只按 `is_overrun` 分两支：超支的写明无结余可分，其余一律画公式框与按钮——扫描实测清算时填发生额 = 筹资
总额，`settle zero: 201 0 False`，页面照样给「按公式分配」，点下去 409。修法：结余为 0 不画公式框与按钮，直接写后端那一句；
结余为正的照旧。这里把 `renderSettlement` 原样拿到 node 里跑，喂真接口的清算单与公式变量。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _render_settlement(settlement: dict, variables: dict) -> str:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    script = ('globalThis.document = { addEventListener() {}, cookie: "" };\n'
              + (STATIC / "shared.js").read_text(encoding="utf-8") + _top_level(core, "function table(")
              + _top_level(page, "function renderSettlement(")
              + "const [s, vars] = JSON.parse(process.argv[1]);\n"
                "process.stdout.write(renderSettlement(s, vars));\n")
    out = subprocess.run(["node", "-e", script, json.dumps([settlement, variables], ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21483 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21483_dir", "password": "passw0rd1", "role": "director", "org_id": org})
    assert created.status_code == 201, created.text
    director = login(client, "p21483_dir", "passw0rd1")
    pools = {}
    for name, year, expense in (("zero", 2081, 500000), ("positive", 2082, 300000), ("overrun", 2083, 800000)):
        pool = client.post("/api/fund/pools", headers=director, json={
            "year": year, "insurance_type": "employee", "total_amount": 500000, "prepay_ratio_pct": 0}).json()["id"]
        settled = client.post(f"/api/fund/pools/{pool}/settle", headers=director, json={"total_expense": expense})
        assert settled.status_code == 201, settled.text
        pools[name] = pool
    variables = client.get("/api/fund/formula-variables", headers=director).json()
    return {"director": director, "pools": pools, "vars": variables}


def _page(client, world, name: str) -> str:
    settlement = client.get(f"/api/fund/pools/{world['pools'][name]}/settlement", headers=world["director"]).json()
    return _render_settlement(settlement, world["vars"])


def test_结余为0_不给公式框与按钮_直接写后端那一句(client, world):
    pool = world["pools"]["zero"]
    settlement = client.get(f"/api/fund/pools/{pool}/settlement", headers=world["director"]).json()
    assert (settlement["balance"], settlement["is_overrun"]) == (0, False)
    refused = client.post(f"/api/fund/pools/{pool}/distribute", headers=world["director"], json={"formula_expr": "1"})
    assert refused.status_code == 409, refused.text
    html = _page(client, world, "zero")
    assert f'<p class="desc">{refused.json()["detail"]}</p>' in html   # 与后端同一句，不另造
    # 修前照样画公式框与「按公式分配」，点了必 409
    assert 'id="fd-formula"' not in html and 'id="fd-distribute"' not in html


def test_结余为正_照旧有公式框与按钮(client, world):
    html = _page(client, world, "positive")
    assert 'id="fd-formula"' in html and 'id="fd-distribute"' in html
    assert "收支相抵" not in html


def test_超支_照旧写明无结余可分配(client, world):
    html = _page(client, world, "overrun")
    assert "本池超支，无结余可分配" in html
    assert 'id="fd-distribute"' not in html and "收支相抵" not in html
