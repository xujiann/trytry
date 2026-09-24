"""「已完成」与「进度 100%」是一条不变式，改档要与存量合并后再判（P2-58）。

`update_project` 的结项校验原先只在本次请求带 `status=done` 时才跑。2026-09-24 开发库实测（修前代码）：
项目结项（进度 100）200 → 再单独 PATCH `progress_pct=60` 200，得到一个「已完成但进度 60%」的项目——
正是这个函数的 docstring 说不允许的那种（那份进度数就再也没人信了）；同一请求里结项且进度 60 却是 422。

修法：状态与进度跟存量合并后再判；只在状态或进度这次真要改时才判，改负责人不受存量脏数据牵连。
"""
from app.database import SessionLocal
from app.models import AdminProject


def _project(client, admin, name):
    org = client.post("/api/organizations", json={"name": f"{name}院", "org_type": "township", "level": "township"},
                      headers=admin).json()["id"]
    r = client.post("/api/projects", json={"org_id": org, "name": name}, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_已结项的项目单独把进度改低_422且原样不动(client, admin):
    pid = _project(client, admin, "结项后改进度")
    assert client.patch(f"/api/projects/{pid}", json={"status": "done", "progress_pct": 100},
                        headers=admin).status_code == 200
    r = client.patch(f"/api/projects/{pid}", json={"progress_pct": 60}, headers=admin)
    assert r.status_code == 422 and "改回「进行中」" in r.json()["detail"], r.text  # 修前 200，done + 60%
    with SessionLocal() as db:
        project = db.get(AdminProject, pid)
        assert (project.status, project.progress_pct) == ("done", 100)


def test_先改回进行中再改进度照常_同一请求结项且进度不满照旧422(client, admin):
    pid = _project(client, admin, "重开项目")
    assert client.patch(f"/api/projects/{pid}", json={"status": "done", "progress_pct": 100},
                        headers=admin).status_code == 200
    r = client.patch(f"/api/projects/{pid}", json={"status": "ongoing", "progress_pct": 60}, headers=admin)
    assert r.status_code == 200 and (r.json()["status"], r.json()["progress_pct"]) == ("ongoing", 60), r.text
    r = client.patch(f"/api/projects/{pid}", json={"status": "done", "progress_pct": 90}, headers=admin)
    assert r.status_code == 422 and "结项须同时" in r.json()["detail"], r.text


def test_存量脏数据不牵连不改状态进度的改档(client, admin):
    """库里早有「已完成 + 进度 60」的老数据（修前写进去的）：只改负责人照常，不逼着顺手改进度。"""
    pid = _project(client, admin, "存量结项")
    with SessionLocal() as db:
        project = db.get(AdminProject, pid)
        project.status, project.progress_pct = "done", 60
        db.commit()
    r = client.patch(f"/api/projects/{pid}", json={"owner_name": "新负责人"}, headers=admin)
    assert r.status_code == 200 and r.json()["owner_name"] == "新负责人", r.text
