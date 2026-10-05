"""满意度页「差评清单」标题写全量，截断时写明只列了多少（P2-1550 的满意度那半，第四十五批扫描 AI1-9）。

修前：`pages-mgmt.js` 的 `renderSurveys` 取差评清单 `max_score=2&limit=50`，标题印的是这 50 条的条数「差评清单（50）」，
而同页统计卡片是全量。扫描实测（`r6_truncation.py`）：60 条差评时卡片 60、清单 50、X-Total-Count 60，最早的 10 条在页面上
无处可看，标题照写「（50）」。

修法：标题用同页统计里的差评数（与清单同一判据：≤2 分，同样不分机构），列不全时写「差评清单（已列 50 / 共 60）」，
没截断的照旧写条数。
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _function_source(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21550 满意度卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21550_op", "password": "passw0rd1", "role": "operator", "org_id": org})
    assert resp.status_code == 201, resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21550 评价居民", "id_card": "330102195501011550"}).json()["id"]
    return {"operator": login(client, "p21550_op", "passw0rd1"), "patient": patient}


def _add_negatives(client, world, start, count):
    for i in range(start, start + count):
        resp = client.post("/api/surveys", headers=world["operator"], json={
            "target_type": "contract", "target_id": 1, "patient_id": world["patient"], "score": 1,
            "comment": f"差评{i + 1}"})
        assert resp.status_code == 201, resp.text


def _negative_title(client, world) -> str:
    """`renderSurveys` 原文放进 node 跑，页面的三个 GET 回放真接口（经办身份）的响应；返回差评清单那块的标题。"""
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = _function_source(mgmt, "async function renderSurveys(")
    data = {}
    for path in re.findall(r'\bapi\("([^"]+)"\)', page):
        resp = client.get(path, headers=world["operator"])
        assert resp.status_code == 200, (path, resp.text)
        data[path] = resp.json()
    script = (
        "const els = {};\n"
        "globalThis.document = { addEventListener() {}, cookie: '', querySelector(sel) { return (els[sel] ||= {}); } };\n"
        + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "const DATA = JSON.parse(process.argv[1]);\nasync function api(path) { return DATA[path]; }\n"
        + "".join(_function_source(core, f"function {name}(") for name in ("table", "panel", "barChart"))
        + re.search(r"^const SURVEY_TARGETS = .*$", mgmt, re.M).group(0) + "\n" + page
        + "renderSurveys().then(() => process.stdout.write(JSON.stringify(\n"
        + "  [...els['#page-body'].innerHTML.matchAll(/<h3>([^<]*)<\\/h3>/g)].map((m) => m[1]))));\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    (title,) = [t for t in json.loads(done.stdout) if t.startswith("差评清单")]
    return title


def test_差评超过一页时标题写全量与已列条数_没超过照旧(client, world):
    _add_negatives(client, world, 0, 3)
    assert _negative_title(client, world) == "差评清单（3）"   # 没截断：照旧写条数
    _add_negatives(client, world, 3, 57)
    assert _negative_title(client, world) == "差评清单（已列 50 / 共 60）"   # 修前「差评清单（50）」
