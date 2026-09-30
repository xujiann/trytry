"""医护端「上传佐证」是页面侧读改写：两位医护交错上传只剩后写的那个附件，还把别人刚保存的办理结果改回旧值（P2-972，
第二十七批「丢失更新：编辑时把页面载入的整行值写回」扫描 G1-6）。

管理端任务中心与医生移动端的「上传佐证」原先：传成附件 → 取任务 → 旧清单加新附件 → 连同取到的办理结果以草稿整体提交
（`submit_task` 整体替换结果与佐证）。甲、乙交错上传，清单里只剩后写的那个；丙刚保存的办理结果被上传佐证的草稿改回
取任务时的旧值。居民端早按 P2-303 改成锁住任务行、重读、再追加。

修法：医护端改走服务端追加（`POST /api/spd/tasks/{id}/evidence`，与居民端同一个写法），不回传办理结果；与原先的保存草稿
一样把待接收 / 已接收的翻成办理中，待审核、已结束的 409，不属于这个任务的附件 422。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2972 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2972 患者", "id_card": "330106196503030071", "birth_date": "1965-03-03"}).json()["id"]
    return {"org": org, "patient": patient}


def _task(client, admin, world, title):
    made = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "title": title, "task_type": "report",
        "require_evidence": True})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _upload(client, admin, task_id, tag):
    upload = client.post("/api/attachments", headers=admin,
                         files={"file": (f"{tag}.png", b"\x89PNG\r\n\x1a\n" + tag.encode(), "image/png")},
                         data={"owner_type": "spd_task", "owner_id": str(task_id)})
    assert upload.status_code == 201, upload.text
    return upload.json()["id"]


def test_两路交错上传_两个附件都在_办理结果不动(client, admin, world):
    task_id = _task(client, admin, world, "P2972 上门测压留照")
    first, second = _upload(client, admin, task_id, "p2972-a"), _upload(client, admin, task_id, "p2972-b")
    saved = client.post(f"{B}/tasks/{task_id}/submit", headers=admin, json={
        "result": {"note": "丙刚保存：血压 150/95"}, "draft": True})
    assert saved.status_code == 200, saved.text
    for attachment_id in (second, first):   # 乙先写完、甲后写完：修前页面各自带着取任务时的旧清单整体写回
        added = client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": attachment_id})
        assert added.status_code == 200, added.text
    task = client.get(f"{B}/tasks/{task_id}", headers=admin).json()
    assert sorted(task["evidence"]) == sorted([first, second])
    assert task["result"] == {"note": "丙刚保存：血压 150/95"}   # 修前被上传佐证的草稿改回取任务时的旧值
    assert task["status"] == "doing"   # 与原先的保存草稿一样翻成办理中
    again = client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": first})
    assert again.status_code == 200 and sorted(again.json()["evidence"]) == sorted([first, second])   # 不重复记


def test_别的任务的附件422_待审核的409(client, admin, world):
    task_id = _task(client, admin, world, "P2972 甲任务")
    other_id = _task(client, admin, world, "P2972 乙任务")
    foreign = _upload(client, admin, other_id, "p2972-c")
    wrong = client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": foreign})
    assert wrong.status_code == 422 and "不属于该任务" in wrong.json()["detail"], wrong.text
    own = _upload(client, admin, task_id, "p2972-d")
    assert client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": own}).status_code == 200
    submitted = client.post(f"{B}/tasks/{task_id}/submit", headers=admin, json={"result": {"note": "已测"}})
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted", submitted.text
    late = _upload(client, admin, task_id, "p2972-e")
    blocked = client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": late})
    assert blocked.status_code == 409 and "待审核" in blocked.json()["detail"], blocked.text
    assert client.get(f"{B}/tasks/{task_id}", headers=admin).json()["evidence"] == [own]   # 审核人看的清单不变


@pytest.mark.parametrize("path", ["pages-spd.js", "m/doctor.js"])
def test_两个页面都走服务端追加_不再回传办理结果(path):
    src = (STATIC / path).read_text(encoding="utf-8")
    start = src.index('uploadAttachment("spd_task"')
    handler = src[start:start + 900]
    assert "/evidence`" in handler and "attachment_id: att.id" in handler
    assert "result: t.result" not in handler and "/submit`" not in handler   # 修前取任务、带着旧结果整体提交草稿
