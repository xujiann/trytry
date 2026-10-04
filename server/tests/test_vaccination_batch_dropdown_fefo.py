"""疫苗接种的批次下拉：同一疫苗先到期的在前（P2-1359，第四十批扫描 AD1-10）。

接种登记表单的 `#vac-batch` 原样按 `/api/vaccine-supply/batches?usable_only=true` 的顺序列（接口按登记倒序、最新在前）：
扫描实测乙肝疫苗 HB-OLD（25 天后到期）排在 HB-NEW（700 天后到期）后面，也没有任何「先用这批」的提示——新批次先打掉、
旧批次放到过期。药品侧发药按 FEFO（`dispense` 模块说明第 2 条）。

修法只改页面：下拉按（疫苗、效期升序、批号）排，下拉旁注明「同一疫苗先到期的在前」；接口顺序不动（批次台账照旧按登记
倒序）。这里把页面取批次、画下拉的那一段原样拿到 node 里跑，喂给它的是真接口的响应。
"""
import json
import os
import re
import shutil
import subprocess
from datetime import timedelta

import pytest

from conftest import business_today

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
USABLE = "/api/vaccine-supply/batches?usable_only=true"


def _render_vaccination() -> str:
    with open(os.path.join(STATIC, "pages-clinical.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderVaccination()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_下拉旁注明同一疫苗先到期的在前():
    body = _render_vaccination()
    form = body[body.index('id="vac-form"'):]
    form = form[:form.index('id="contra-form"')]   # 接种登记表单与它下面那段说明
    assert "同一疫苗先到期的在前" in form          # 修前没有任何「先用这批」的提示


@pytest.fixture(scope="module")
def batches(client, admin):
    """按登记先后：HB-OLD、MMR-A、HB-NEW、MMR-B、BCG-1——接口按登记倒序回。MMR 两批同一天到期、后登记的 MMR-B 在接口里
    排前面，批号这一级排序才看得出来。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21359 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    today = business_today()
    ids = {}
    for code, name, batch_no, days in (("HepB", "乙肝疫苗", "HB-OLD", 25), ("MMR", "麻腮风疫苗", "MMR-A", 200),
                                       ("HepB", "乙肝疫苗", "HB-NEW", 700), ("MMR", "麻腮风疫苗", "MMR-B", 200),
                                       ("BCG", "卡介苗", "BCG-1", 400)):
        resp = client.post("/api/vaccine-supply/batches", headers=admin, json={
            "vaccine_code": code, "vaccine_name": name, "batch_no": batch_no,
            "expire_date": (today + timedelta(days=days)).isoformat(), "org_id": org, "quantity": 20})
        assert resp.status_code == 201, resp.text
        ids[batch_no] = resp.json()["id"]
    return ids


def _run_vaccination_fetch(response: list) -> dict:
    """在 node 里原样执行页面末尾取批次、画下拉的那一段（`// 取数放最后` 到函数结尾），`api` 回给定的响应。"""
    body = _render_vaccination()
    start = body.index("// 取数放最后")
    block = body[start:body.index("\n}\n", start)]   # 函数在顶格的 `}` 处结束
    with open(os.path.join(STATIC, "shared.js"), encoding="utf-8") as fh:
        shared = fh.read()
    script = (
        "const elements = {};\n"
        "globalThis.document = { addEventListener() {}, querySelector(sel) { return (elements[sel] ||= {}); },"
        " cookie: '' };\n"
        + shared
        + f"\nconst RESPONSE = {json.dumps(response, ensure_ascii=False)};\n"
        "async function api(path) {\n"
        f"  if (path !== {json.dumps(USABLE)}) throw new Error(`没料到的请求：${{path}}`);\n"
        "  return JSON.parse(JSON.stringify(RESPONSE));\n"
        "}\n"
        "let usableBatches = [];\n"
        f"(async () => {{\n{block}\n"
        "  process.stdout.write(JSON.stringify({ order: usableBatches.map((b) => b.batch_no),"
        " html: elements['#vac-batch'].innerHTML }));\n})();\n"
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的取数")
def test_下拉按疫苗_效期升序_批号排_接口顺序不动(client, admin, batches):
    response = client.get(USABLE, headers=admin).json()
    # 接口顺序不动：照旧按登记倒序（批次台账页同样按这个顺序列）
    assert [b["batch_no"] for b in response] == ["BCG-1", "MMR-B", "HB-NEW", "MMR-A", "HB-OLD"]
    got = _run_vaccination_fetch(response)
    expected = ["BCG-1", "HB-OLD", "HB-NEW", "MMR-A", "MMR-B"]
    assert got["order"] == expected                # 修前原样：HB-NEW（700 天后到期）排在 HB-OLD（25 天后到期）前面
    options = [int(v) for v in re.findall(r'<option value="(\d+)"', got["html"])]
    assert options == [batches[no] for no in expected]   # 画出来的下拉同序（空白的「请选择」项不算）
