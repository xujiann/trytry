"""物资页「采购流程」表只取最新 50 张：更早的待审批、待签合同、待验收单子在页面上办不了；本人提的申请照样摆「审批」
（P2-1506，第四十四批扫描 AH3-2）。

- `renderMaterials` 原先只取 `/api/materials/purchases` 的缺省一页（最新 50 张、按编号倒序），审批、签合同、验收三个按钮只
  挂在这张表上：第 51 张起，最早一批还没办完的单子从表里消失，标题还写「采购流程（50）」，看不出被截断。扫描实测 52 张：
  页面 50 行、X-Total-Count 52，最早那张（已签合同、待验收）与第二张（待审批）都不在页面数据里。
- 清单行不带申请人：管理层本人提的申请照样摆「审批 / 驳回」，点下去 403「不得审批本人提出的采购申请」。

修法：照同页耗材台账的 P2-1358——三种待办状态 `fetchAllPages` 续页取全、`actionableFirst` 排在最前，已办结的照旧取最新一页；
标题按「最新一页取没取满」写明截断（同 P1-250）。清单出参末尾补 `requested_by`（申请人账号）与 `requested_by_me`（申请人是不是
当前账号：页面不知道自己是谁，由后端按审批接口同一判据现算，同 P2-799），本人提的申请行上不摆审批按钮。
页面取数那一段与行模板都原样拿到 node 里跑（取数的管道复用 `test_materials_cssd_page_reach` 的夹具，不另造一份）。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import login
from test_materials_cssd_page_reach import _fetch_block, _run_page_fetch

from app.database import SessionLocal

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PAGE = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
#: 物资采购清单出参原有的 13 个键（次序即响应次序）；P2-1506 只在末尾增两项
ORIGINAL_KEYS = ["id", "org_id", "dept_id", "item_name", "spec", "unit", "quantity", "estimated_price", "status",
                 "supplier_id", "contract_no", "contract_amount", "received_quantity"]

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的取数")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21506 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ids = {}
    for username, role in (("p21506_op", "operator"), ("p21506_dir", "director"), ("p21506_dir2", "director")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": org})
        assert created.status_code == 201, created.text
        ids[username] = created.json()["id"]
    return {"org": org, "ids": ids, **{name: login(client, name, "pw123456") for name in ids}}


@needs_node
def test_采购满50张后_更早没办完的单子仍在页面数据里_标题写明截断(client, world):
    from app.models import MaterialPurchase

    op = world["ids"]["p21506_op"]
    block = _fetch_block("pages-mgmt.js", "renderMaterials", "\n  const canApprove")
    result = "{ rows: purchases.map((p) => [p.id, p.status]), count: purchaseCount }"
    with SessionLocal() as db:   # 最早三张各停在一种待办状态（逐张走接口太慢，直接落库）
        early = [MaterialPurchase(org_id=world["org"], item_name=name, quantity=1, status=status, requested_by=op)
                 for name, status in (("P21506 病床", "contracted"), ("P21506 轮椅", "requested"),
                                      ("P21506 担架", "approved"))]
        db.add_all(early)
        db.commit()
        early_ids = {p.id: p.status for p in early}
    got, requested = _run_page_fetch(client, world["p21506_dir"], block, result)
    assert (dict(got["rows"]), got["count"]) == (early_ids, "3"), requested   # 最新一页没取满：标题照旧是行数

    with SessionLocal() as db:   # 之后又办结了 50 张
        db.add_all([MaterialPurchase(org_id=world["org"], item_name=f"P21506 办公用品{i}", quantity=1,
                                     status="received" if i % 2 else "cancelled", requested_by=op)
                    for i in range(50)])
        db.commit()
    first_page = client.get("/api/materials/purchases", headers=world["p21506_dir"])
    assert first_page.headers["X-Total-Count"] == "53"
    assert not set(early_ids) & {p["id"] for p in first_page.json()}   # 缺省一页取不到这三张

    got, requested = _run_page_fetch(client, world["p21506_dir"], block, result)
    rows = dict(got["rows"])
    # 修前页面 50 行、这三张都不在：待验收的「验收」、待审批的「审批」、待签合同的「签合同」都够不着
    assert {i: rows.get(i) for i in early_ids} == early_ids, requested
    assert {i for i, _ in got["rows"][:3]} == set(early_ids), requested   # 待办的排在最前
    assert len(got["rows"]) == len(rows) == 53, requested                 # 按 id 去重，已办结的照旧是最新一页
    # 修前标题写「采购流程（50）」，看不出被截断
    assert got["count"] == "待审批 / 待签合同 / 待验收 3 张排在最前、其余只列最新 50 张", requested


def test_清单与新建回执末尾补申请人与是否本人_原键次序不动(client, world):
    org = world["org"]
    mine = client.post("/api/materials/purchases", headers=world["p21506_dir"], json={
        "org_id": org, "item_name": "P21506 冰箱"})
    assert mine.status_code == 201, mine.text
    assert list(mine.json()) == ORIGINAL_KEYS + ["requested_by", "requested_by_me"]   # 修前只有原 13 个键
    assert (mine.json()["requested_by"], mine.json()["requested_by_me"]) == (world["ids"]["p21506_dir"], True)
    other = client.post("/api/materials/purchases", headers=world["p21506_op"], json={
        "org_id": org, "item_name": "P21506 推车"}).json()

    rows = {r["id"]: r for r in client.get("/api/materials/purchases", headers=world["p21506_dir"],
                                           params={"status": "requested"}).json()}
    assert list(rows[mine.json()["id"]]) == ORIGINAL_KEYS + ["requested_by", "requested_by_me"]
    assert (rows[mine.json()["id"]]["requested_by"], rows[mine.json()["id"]]["requested_by_me"]) == (
        world["ids"]["p21506_dir"], True)
    assert (rows[other["id"]]["requested_by"], rows[other["id"]]["requested_by_me"]) == (
        world["ids"]["p21506_op"], False)
    # 同一张单子换个人看，「是不是本人」跟着看的人走
    by_op = {r["id"]: r for r in client.get("/api/materials/purchases", headers=world["p21506_op"],
                                            params={"status": "requested"}).json()}
    assert (by_op[mine.json()["id"]]["requested_by_me"], by_op[other["id"]]["requested_by_me"]) == (False, True)
    # 判据与审批接口同一句：本人 403、别人 200
    denied = client.post(f"/api/materials/purchases/{mine.json()['id']}/approve", headers=world["p21506_dir"],
                         json={"approved": True})
    assert (denied.status_code, denied.json()["detail"]) == (403, "不得审批本人提出的采购申请")
    assert client.post(f"/api/materials/purchases/{mine.json()['id']}/approve", headers=world["p21506_dir2"],
                       json={"approved": True}).status_code == 200


def _row_renderer() -> str:
    """「采购流程」表的行模板：`purchases, (p) => {` 到行尾的 `</tr>` 那一句。"""
    start = PAGE.index("(p) => {", PAGE.index("panel(`采购流程（"))
    end = PAGE.index("</tr>`;\n      }", start) + len("</tr>`;\n      }")
    return PAGE[start:end]


def _render_rows(rows: list[dict], can_approve: bool) -> list[str]:
    """在 node 里原样跑行模板（先加载 shared.js 的 esc / statusTag 与本页的状态表、金额帮手），逐行回 HTML。"""
    status = re.search(r"\nconst PURCHASE_STATUS = \{.*?\};\n", PAGE, re.S).group(0)
    amounts = PAGE[PAGE.index("function materialPurchaseAmounts("):]
    amounts = amounts[:amounts.index("\n}\n") + 2]
    script = ("globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
              + (STATIC / "shared.js").read_text(encoding="utf-8") + status + amounts
              + f"\nconst canApprove = {json.dumps(can_approve)};\nconst row = {_row_renderer()};\n"
              "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(row)));")
    out = subprocess.run(["node", "-e", script, json.dumps(rows)], capture_output=True, text=True, check=True,
                         timeout=60).stdout
    return json.loads(out)


@needs_node
def test_本人提的申请行上不摆审批与驳回_别人提的照旧摆(client, world):
    org = world["org"]
    mine = client.post("/api/materials/purchases", headers=world["p21506_dir"], json={
        "org_id": org, "item_name": "P21506 监护仪"}).json()["id"]
    other = client.post("/api/materials/purchases", headers=world["p21506_op"], json={
        "org_id": org, "item_name": "P21506 输液架"}).json()["id"]
    listed = {r["id"]: r for r in client.get("/api/materials/purchases", headers=world["p21506_dir"],
                                             params={"status": "requested"}).json()}
    own_html, other_html = _render_rows([listed[mine], listed[other]], can_approve=True)
    # 修前本人的行照样有这两个按钮，点下去 403「不得审批本人提出的采购申请」
    assert "data-approve" not in own_html and "data-reject" not in own_html, own_html
    assert "本人提出，待其他管理层审批" in own_html
    assert f'data-approve="{other}"' in other_html and f'data-reject="{other}"' in other_html, other_html
    # 不是管理层的照旧只写「待管理层审批」（P2-425），本人的也一样
    assert all("待管理层审批" in html and "data-approve" not in html
               for html in _render_rows([listed[mine], listed[other]], can_approve=False))
