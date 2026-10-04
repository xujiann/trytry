"""智慧急救页、绿道页只拿全县最新 200 起事件：被挤出窗口的「已到院待判转归」「已调度 / 转运中」事件与要补录节点的通道病例，
页面上再没有一行能办（P2-1369，第四十批扫描 AD4-3）。

`emergency.list_cases` 原先只收 `status`、固定取最新 200 起、不收翻页参数；急救页（`renderEmergency`）与绿道页
（`renderEmTimeline`）都只调一次不带参数的清单。实测：事件 #1（胸痛通道）推到「已到院」等医师判转归，之后全县再来
200 起呼救——清单里没有 #1（`?offset=200` 被忽略，`?status=arrived` 才取得到），「判定转归」「录节点」都够不着，
按编号直接判转归接口照回 200。演示种子靠同一份清单判重，再来 200 起之后重跑再建一例演示胸痛病例（那半由
`test_seed_demo_rerun.py` 盯着）。

修法照 P2-408 / P2-456（core.js `actionableFirst`）：急救页另取待流转（已调度、转运中）与待判转归（已到院 / 已收治而
转归未判定，接口新收 `rescue_outcome=pending`），绿道页另取胸痛 / 卒中 / 创伤三种通道（接口新收 `channel_type`），
都排在最前、按 id 去重。接口只增可选筛选，不带参数的调用逐字节不变；按机构收口（P1-39）与整表翻页（P1-49）不在本条。
"""
import json
import os
import re
from urllib.parse import parse_qsl, urlsplit

import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import EmergencyCase
from app.routers import emergency
from app.routers.emergency import _FLOW, CASE_STATUS_NAMES, CaseCreate

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
LIST = "/api/emergency/cases"
PAGES = {"急救页": ("pages-clinical.js", "renderEmergency"), "绿道页": ("pages-public.js", "renderEmTimeline")}
BURY = 200   # 清单的窗口：全县最新 200 起


def _function(filename: str, name: str) -> str:
    with open(os.path.join(STATIC, filename), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"async function {name}()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


def _actionable_urls(page: str) -> list[str]:
    """页面单独取待办的地址：`api("…?…")` 字面量，加上 `["a", "b"].map((x) => api(`…${x}`))` 逐项展开的每一条。"""
    body = _function(*PAGES[page])
    urls = re.findall(r'api\("(/api/emergency/cases\?[^"]+)"\)', body)
    for items, var, template in re.findall(
            r'\[([^\[\]]*)\]\.map\(\((\w+)\) => api\(`(/api/emergency/cases\?[^`]+)`\)\)', body):
        urls += [template.replace("${" + var + "}", item) for item in re.findall(r'"(\w+)"', items)]
    return urls


def _query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


@pytest.mark.parametrize("page, fetches, merged", [
    ("急救页", ['api("/api/emergency/cases")', 'api("/api/emergency/cases?status=dispatched")',
               'api("/api/emergency/cases?status=en_route")', 'api("/api/emergency/cases?rescue_outcome=pending")'],
     "const cases = actionableFirst(recent, dispatched, enRoute, unjudged);"),
    ("绿道页", ['api("/api/emergency/cases")',
               '["chest_pain", "stroke", "trauma"].map((ch) => api(`/api/emergency/cases?channel_type=${ch}`))'],
     "const cases = actionableFirst(recent, ...channels);"),
])
def test_两页的待办单独取_排在最前(page, fetches, merged):
    body = _function(*PAGES[page])
    for fetch in fetches:
        assert fetch in body, fetch   # 修前两页都只取不带参数的最新 200 起
    assert merged in body


def test_单独取的正好是页面上还有事可办的那几种():
    """急救页：车还没到院、`_FLOW` 里还有下一步的每种状态按 `?status=` 取全（「流转」），到院之后的按 `rescue_outcome=pending`
    取（「判定转归」只对这两种状态开放）；绿道页：建单收的每种通道都取。后端加一个状态 / 通道而页面没跟，这里先红。"""
    from app.routers.emergency import _OUTCOME_STATUSES

    urls = _actionable_urls("急救页")
    assert {_query(u)["status"] for u in urls if "status" in _query(u)} == set(_FLOW) - set(_OUTCOME_STATUSES)
    assert f"{LIST}?rescue_outcome=pending" in urls
    channel_pattern = CaseCreate.model_json_schema()["properties"]["channel_type"]["pattern"]
    assert {_query(u)["channel_type"] for u in _actionable_urls("绿道页")} == set(re.findall(r"\w+", channel_pattern))


def test_待办取数用的查询参数_后端都认():
    """FastAPI 对不认识的查询参数一声不吭、照返全量——修前 `channel_type` / `rescue_outcome` 就是这样被吞掉的，
    「待办」其实是没筛过的最新一页，上面两条静态钉看不出来。"""
    route = next(r for r in emergency.router.routes if getattr(r, "path", "") == LIST and "GET" in r.methods)
    known = {param.alias for param in route.dependant.query_params}
    urls = _actionable_urls("急救页") + _actionable_urls("绿道页")
    assert len(urls) == 6, urls   # 判据自证：两页的六个待办取数都认得出
    for url in urls:
        unknown = [name for name in _query(url) if name not in known]
        assert not unknown, (url, unknown)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21369 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]

    def case(steps: int, outcome: str = "", **body) -> int:
        created = client.post(LIST, headers=admin, json={"location": "P21369 事发地", "dest_org_id": org, **body})
        assert created.status_code == 201, created.text
        case_id = created.json()["id"]
        for _ in range(steps):
            assert client.post(f"{LIST}/{case_id}/advance", headers=admin).status_code == 200
        if outcome:
            judged = client.post(f"{LIST}/{case_id}/rescue-outcome", headers=admin, json={"rescue_outcome": outcome})
            assert judged.status_code == 200, judged.text
        return case_id

    rows = {
        "arrived": case(2, symptom="胸痛", channel_type="chest_pain"),   # 已到院、等医师判转归，节点打算事后补录
        "admitted": case(3, channel_type="stroke"),                      # 已收治、转归还没判
        "en_route": case(1),                                             # 普通呼救，转运中
        "dispatched": case(0),                                           # 普通呼救，刚调度
        "trauma_done": case(3, "success", channel_type="trauma"),        # 急救页上办完了，绿道节点还要补
        "judged": case(2, "failed"),                                     # 已到院、已判：不是待判转归
    }
    with SessionLocal() as db:   # 之后全县又来了 200 起，都已收治、已判（县域 120 一天 15~30 起，约一到两周）
        db.execute(insert(EmergencyCase), [{
            "location": f"P21369 后来的呼救{i}", "dest_org_id": org, "status": "admitted",
            "rescue_outcome": "success"} for i in range(BURY)])
        db.commit()
    return rows


def _page_rows(client, headers, page: str) -> list[int]:
    """照页面取数：不带参数的最新一页，加上单独取的待办按 `actionableFirst` 合并（待办在前、按 id 去重）。"""
    first = list(dict.fromkeys(
        row["id"] for url in _actionable_urls(page) for row in client.get(url, headers=headers).json()))
    seen = set(first)
    return first + [row["id"] for row in client.get(LIST, headers=headers).json() if row["id"] not in seen]


def test_再来200起之后_待办仍在两页取到的数据里_排在最前(client, admin, world):
    recent = [row["id"] for row in client.get(LIST, headers=admin).json()]
    assert len(recent) == BURY and not set(world.values()) & set(recent)   # 判据自证：都挤出了窗口，修前两页上一行都没有
    rows = _page_rows(client, admin, "急救页")
    for key in ("arrived", "admitted", "en_route", "dispatched"):
        assert world[key] in rows, key                                     # 还能「流转」或「判定转归」
        assert rows.index(world[key]) < rows.index(recent[0]), key         # 排在后来那 200 起之前
    rows = _page_rows(client, admin, "绿道页")
    for key in ("arrived", "admitted", "trauma_done"):
        assert world[key] in rows, key                                     # 还能「录节点 / 时间轴」
        assert rows.index(world[key]) < rows.index(recent[0]), key


def test_两个新筛选只筛出该筛的(client, admin, world):
    pending = client.get(LIST, headers=admin, params={"rescue_outcome": "pending"}).json()
    # 待判转归 = 已到院 / 已收治而转归未判定：车还在路上的（判了 409）、已判的都不算
    assert {(row["status"], row["rescue_outcome"]) for row in pending} <= {("arrived", ""), ("admitted", "")}
    assert [row["id"] for row in pending] == [world["admitted"], world["arrived"]]
    failed = client.get(LIST, headers=admin, params={"rescue_outcome": "failed"}).json()
    assert [row["id"] for row in failed] == [world["judged"]]
    for channel, key in (("chest_pain", "arrived"), ("stroke", "admitted"), ("trauma", "trauma_done")):
        got = client.get(LIST, headers=admin, params={"channel_type": channel}).json()
        assert [row["id"] for row in got] == [world[key]], channel
    both = client.get(LIST, headers=admin, params={"channel_type": "stroke", "rescue_outcome": "pending"}).json()
    assert [row["id"] for row in both] == [world["admitted"]]   # 几个筛选同时给是「且」


def test_不带参数的调用逐字节不变(client, admin, world):
    """缺省仍是全县最新 200 起、按编号倒序、不带翻页头；响应字节与按原先写法现算的一致。"""
    resp = client.get(LIST, headers=admin)
    with SessionLocal() as db:
        expected = [{
            "caller_phone": c.caller_phone, "location": c.location, "symptom": c.symptom, "ambulance_no": c.ambulance_no,
            "dest_org_id": c.dest_org_id, "patient_id": c.patient_id, "channel_type": c.channel_type, "id": c.id,
            "status": c.status, "status_name": CASE_STATUS_NAMES[c.status], "rescue_outcome": c.rescue_outcome,
        } for c in db.query(EmergencyCase).order_by(EmergencyCase.id.desc()).limit(BURY)]
    assert len(expected) == BURY
    assert resp.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    assert "x-total-count" not in resp.headers
    assert client.get(LIST, headers=admin, params={"channel_type": "", "rescue_outcome": ""}).content == resp.content
