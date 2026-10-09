"""中心药房页「近效期批次」被永不离账的已过期批次占满：20 天后到期的批次不在页面数据里（P2-1672，第四十九批扫描 AM2-2
的页面与查询一半）。

- 页面原先 `api("/api/pharmacy/batches/expiring")`：缺省一页 500 条、按效期升序、含已过期，不读总数。已过期（或召回封存）
  而仍有余量的「死批次」没有出口（报废另议，见 P1-159），只增不减，又永远排在最前。扫描实测 520 个已过期仍有余量的批次 +
  1 个 20 天后到期的胰岛素：页面那一句 500 行、X-Total-Count 522、500 行全是已过期，胰岛素不在页面数据里——和端点 docstring
  里写的「最该预警的批次一条都出不来」是同一个后果。

修法：`expiring_drug_batches` 加可选 `expired`——缺省不传，返回集合、排序与字节都不变；`false` 只取未过期（效期 ≥ 今天）、
`true` 只取已过期，判据与出参 `expired` 同一句、同一个「今天」，下推到 SQL（X-Total-Count 与体同一批行）。页面分两段：
窗口内未过期的（`expired=false`）在前；已过期仍有余量的另列一段（`expired=true`），读总数，列不全时标题写明，提示按报废流程处理。
"""
import json
import re
import shutil
import subprocess
from datetime import timedelta

import pytest

from conftest import business_today, login
from test_dispense_reverse_row_button import PRELUDE, STATIC, _function_source, _page_gets

from app.database import SessionLocal

#: 已过期仍有余量的批次数：比近效期一页（500）多
N_EXPIRED = 510


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import DrugBatch, DrugStock

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21672 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    made = client.post("/api/users", headers=admin, json={
        "username": "p21672_ph", "password": "pw123456", "role": "pharmacist", "org_id": org})
    assert made.status_code == 201, made.text
    today = business_today()
    dates = {"P21672-TODAY": today, "P21672-S1": today + timedelta(days=20),
             "P21672-FAR": today + timedelta(days=200)}   # 最后一个在 90 天窗口外，两段都不该有
    with SessionLocal() as db:   # 几年累积的已过期批次逐个走接口太慢，直接落库
        db.add_all([DrugStock(org_id=org, drug_code="P21672-OLD", drug_name="P21672 阿莫西林", quantity=0, threshold=0),
                    DrugStock(org_id=org, drug_code="P21672-INS", drug_name="P21672 胰岛素", quantity=0, threshold=0)])
        db.add_all([DrugBatch(org_id=org, drug_code="P21672-OLD", batch_no=f"P21672-E{i:03d}",
                              expire_date=(today - timedelta(days=1 + i % 700)).isoformat(), quantity=5, used_quantity=0)
                    for i in range(N_EXPIRED)])
        db.add_all([DrugBatch(org_id=org, drug_code="P21672-INS", batch_no=batch_no, expire_date=day.isoformat(),
                              quantity=30, used_quantity=0) for batch_no, day in dates.items()])
        db.commit()
    return {"org": org, "ph": login(client, "p21672_ph", "pw123456"), "dates": dates}


def _walk(client, headers, params: dict) -> tuple[list[dict], int]:
    """按 offset 一页页取全，回 (全部行, X-Total-Count)。"""
    rows: list[dict] = []
    while True:
        resp = client.get("/api/pharmacy/batches/expiring", headers=headers, params={**params, "offset": len(rows)})
        assert resp.status_code == 200, resp.text
        page = resp.json()
        rows += page
        if len(page) < 500:
            return rows, int(resp.headers["X-Total-Count"])


def test_已过期的过了500条_expired_false第一页仍有20天后到期的那批_总数对(client, world):
    ph = world["ph"]
    # 修前那一句：500 行全是已过期，20 天后到期的 S1 不在里面
    before = client.get("/api/pharmacy/batches/expiring", headers=ph)
    assert before.headers["X-Total-Count"] == str(N_EXPIRED + 2)
    assert all(b["expired"] for b in before.json()) and "P21672-S1" not in {b["batch_no"] for b in before.json()}

    fresh = client.get("/api/pharmacy/batches/expiring", headers=ph, params={"expired": "false"})
    assert fresh.status_code == 200, fresh.text
    # 效期就是今天的不算过期（与出参 `expired` 同一句判据）；窗口外的 FAR 照旧不列
    assert [(b["batch_no"], b["expired"], b["remaining_days"]) for b in fresh.json()] == [
        ("P21672-TODAY", False, 0), ("P21672-S1", False, 20)]
    assert fresh.headers["X-Total-Count"] == "2"   # 条件在 SQL 里：总数与体同一批行

    dead = client.get("/api/pharmacy/batches/expiring", headers=ph, params={"expired": "true"})
    assert dead.headers["X-Total-Count"] == str(N_EXPIRED)
    assert len(dead.json()) == 500 and all(b["expired"] and b["remaining_days"] < 0 for b in dead.json())

    # 「今天」与算剩余天数同一个基准：把业务日挪到 S1 到期的次日（窗口放到一年，FAR 进窗口），S1 就归到已过期那一段
    shifted = (world["dates"]["P21672-S1"] + timedelta(days=1)).isoformat()
    moved = client.get("/api/pharmacy/batches/expiring", headers=ph,
                       params={"expired": "false", "today": shifted, "days": 365})
    assert [b["batch_no"] for b in moved.json()] == ["P21672-FAR"]
    moved_dead, total = _walk(client, ph, {"expired": "true", "today": shifted, "days": 365})
    assert total == N_EXPIRED + 2 and {"P21672-TODAY", "P21672-S1"} <= {b["batch_no"] for b in moved_dead}


def test_缺省不传_与修前同一批行同一顺序(client, world):
    from app.models import DrugBatch

    ph = world["ph"]
    rows, total = _walk(client, ph, {})
    limit_date = (business_today() + timedelta(days=90)).isoformat()
    with SessionLocal() as db:   # 修前的谓词与排序原样写一遍
        expected = [b.id for b in db.query(DrugBatch).filter(
            DrugBatch.org_id == world["org"], DrugBatch.expire_date <= limit_date,
            DrugBatch.quantity - DrugBatch.used_quantity > 0).order_by(DrugBatch.expire_date, DrugBatch.id)]
    assert [b["id"] for b in rows] == expected and total == len(expected) == N_EXPIRED + 2
    # 已过期的效期都早于未过期的：两段按顺序拼起来，逐字节就是缺省那一串
    dead, dead_total = _walk(client, ph, {"expired": "true"})
    fresh, fresh_total = _walk(client, ph, {"expired": "false"})
    assert json.dumps(dead + fresh, ensure_ascii=False) == json.dumps(rows, ensure_ascii=False)
    assert dead_total + fresh_total == total


def _render_page(role: str, gets: dict) -> str:
    """药房页原样拿到 node 里渲染一遍，回 `#page-body` 的 innerHTML。夹具照抄 `test_dispense_reverse_row_button._run`，
    只是回放数据走 stdin：已过期那一段 500 行，塞进命令行参数会超过单个参数的长度上限。"""
    prelude = PRELUDE.replace("JSON.parse(process.argv[1])", 'JSON.parse(require("fs").readFileSync(0, "utf8"))')
    assert prelude != PRELUDE, "夹具的入参写法变了"
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    script = (
        prelude + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + _function_source(core, "function table(") + _function_source(core, "function panel(")
        + _function_source(core, "function setMsg(")
        + re.search(r"^function currentRole\(\).*$", core, re.M).group(0) + "\n"
        + _function_source(core, "async function renderPharmacy(")
        + "\n(async () => { await renderPharmacy(); return document.querySelector('#page-body').innerHTML; })()"
        + ".then((r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n"
    )
    payload = json.dumps({"role": role, "get": gets, "modalResult": None}, ensure_ascii=False)
    done = subprocess.run(["node", "-e", script], input=payload, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面")
def test_页面近效期分两段_未过期在前_已过期另列并写明截断(client, world):
    gets = _page_gets(client, world["ph"])
    html = _render_page("pharmacist", gets)
    section = html[html.index(">近效期批次（90 天）</h3>"):html.index(">发药记录</h3>")]
    split = section.find(">已过期仍有余量（")
    near, dead = (section[:split], section[split:]) if split >= 0 else (section, "")
    # 修前只有这一段，500 行全是已过期，S1 不在
    assert "P21672-S1" in near and "P21672-TODAY" in near and "20 天" in near
    assert "P21672-E" not in near and "已过期" not in near
    assert dead.startswith(f">已过期仍有余量（已列 500 / 共 {N_EXPIRED}）</h3>"), dead[:80]
    assert "按报废流程处理" in dead
    assert dead.count("P21672-E") == 500 and "P21672-S1" not in dead
    assert "/api/pharmacy/batches/expiring" not in gets   # 修前那一句不分过没过期
