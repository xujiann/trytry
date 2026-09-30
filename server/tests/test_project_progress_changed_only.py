"""项目「报进度」恒送进度和状态两列：别人刚结项的，旧页面上报个进度就把项目悄悄改回「进行中」（P2-969，第二十七批
「丢失更新：编辑时把页面载入的整行值写回」扫描 G1-8）。

P2-416 专门为并发改档加了条件：只改其中一列时，按库里的另一列判（已结项的单改进度 422、另一列刚被改过的 409）。页面的
「报进度」框却把两列都按载入值预填、恒送——两列都送时后端不设条件，于是甲结项（完成、100%），乙在旧页面上报进度 85，
项目被改回「进行中 85%」，两路都 200，这道条件从页面上永远不生效。

修法：页面只送和预填值不同的列。只改了进度的，后端按库里现在的状态判：已结项的 422「项目已完成，进度须为 100%」。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _dialog() -> str:
    start = PAGE.index("spdModal(`报进度：")
    return PAGE[start:PAGE.index('"#pj-msg", "PATCH")', start)]


def test_报进度只送改过的列():
    dialog = _dialog()
    assert "{ progress_pct: picked.progress_pct, status: picked.status }" not in dialog   # 修前两列恒送
    assert "const body = {};" in dialog
    assert "String(picked.progress_pct) !== String(p.progress_pct)" in dialog
    assert "picked.status !== p.status" in dialog


def test_别人刚结项_旧页面只送进度_不再改回进行中(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2969 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = client.post("/api/projects", headers=admin, json={"org_id": org, "name": "P2969 慢病信息化改造"})
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    assert client.patch(f"/api/projects/{pid}", headers=admin, json={"status": "ongoing", "progress_pct": 60}
                        ).status_code == 200   # 乙载入时：进行中 60%
    closed = client.patch(f"/api/projects/{pid}", headers=admin, json={"status": "done", "progress_pct": 100})
    assert closed.status_code == 200, closed.text   # 甲结项
    stale = client.patch(f"/api/projects/{pid}", headers=admin, json={"progress_pct": 85})   # 乙：只改了进度
    assert stale.status_code == 422 and "项目已完成" in stale.json()["detail"], stale.text   # 修前两列都送，200 改回进行中
    row = client.get(f"/api/projects/{pid}", headers=admin).json()
    assert (row["status"], row["progress_pct"]) == ("done", 100)
