"""任务中心首屏先列未结束的、待受理申请先到先办且不截在 30 条（P2-827，第二十二批「页面查询参数 vs 后端」扫描 X4-3；
P2-782 / P2-783 同形漏网）。

任务中心 `drawTasks()` 不带任何筛选取 `tasks?limit=30`，清单按优先级高、截止早排、不分状态——办结、取消的截止日早，
攒够 30 条之后首屏看不到一条要办的，中心工作台却报「全部待办 N」。中心端「居民服务申请（待受理）」取最新 30 条（按
提交倒序），待受理超过 30 条时等得最久的被截掉，页面也不说。修后没筛选时未结束的单独取一遍（上限 200）排最前；待受理
申请按 `oldest_first=true` 先到先办、上限 200。
"""
from pathlib import Path

from app.database import SessionLocal
from app.spd.models import SpdServiceApply

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_待受理申请可按先到先办取(client, admin):
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2827 申请人{n}", "id_card": f"33010219500101{2827 + n:04d}"}).json()["id"] for n in range(3)]
    with SessionLocal() as db:
        rows = [SpdServiceApply(patient_id=p, program_code="hypertension", note="想加入", status="pending")
                for p in patients]
        db.add_all(rows)
        db.commit()
        ids = [r.id for r in rows]
    newest = client.get(f"{B}/service-applies", headers=admin, params={"status": "pending", "limit": 500}).json()
    oldest = client.get(f"{B}/service-applies", headers=admin, params={
        "status": "pending", "oldest_first": "true", "limit": 500}).json()
    assert [r["id"] for r in newest] == sorted((r["id"] for r in newest), reverse=True)   # 缺省照旧：新的在前
    assert [r["id"] for r in oldest] == sorted(r["id"] for r in oldest)   # 修前参数不认，照样新的在前
    assert [i for i in (r["id"] for r in oldest) if i in ids] == ids


def test_页面_任务中心没筛选时未结束的排前_待受理先到先办():
    start = PAGE.index("const drawTasks = async (query) => {")
    draw = PAGE[start:PAGE.index("\n  };\n", start)]
    assert 'api("/api/spd/tasks?open_only=true&limit=200")' in draw   # 修前只取 tasks?limit=30
    assert "actionableFirst(" in draw and "Object.keys(lastTaskQuery).length ?" in draw
    assert 'api("/api/spd/service-applies?status=pending&oldest_first=true&limit=200")' in PAGE
    assert 'service-applies?status=pending&limit=30"' not in PAGE
