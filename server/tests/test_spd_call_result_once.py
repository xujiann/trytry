"""呼叫任务的结果回写不限次数：已接通的通话随后能被改成「已取消」、录音地址与沟通结果被整个换掉（P2-87）。

`record_call_result` 把请求里的状态、时长、录音地址、沟通结果原样写上去，不看这条任务是否已有结果。页面上「回写结果」
只对待呼叫的任务给出；呼叫中心网关对每条任务回调一次（呼叫失败后重派是新建一行）。所以已有结果的再写一次只有两种来路：
重复回调，或有人按任务号直接改——随访质量抽查要回听的录音地址、接通时同步进随访记录的沟通结果，就此可以事后改写。
与 P2-75（终态对象再操作一次照样 200）同形，那一轮按「`x.status = "字面量"`」扫，这里是 `task.status = body.status`，没扫到。

修法：只有待呼叫的任务能回写结果，已有结果的 409，原结果不动。（任务号对应的患者 / 机构归属不查，随呼叫中心网关的
调用身份一起待裁定，见待裁定清单 P1-71 一节。）
"""
B = "/api/spd"


def test_已有结果的呼叫任务_再回写409_原结果不动(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P287 患者", "id_card": "330127197107070287", "phone": "13928700287"}).json()["id"]
    task = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "edu"})
    assert task.status_code == 201, task.text
    url = f"{B}/call-tasks/{task.json()['id']}/result"
    first = client.post(url, headers=admin, json={
        "status": "connected", "duration_s": 95, "record_url": "https://rec.example/287.mp3", "result": "已讲解限盐"})
    assert first.status_code == 200, first.text
    again = client.post(url, headers=admin, json={
        "status": "cancelled", "duration_s": 0, "record_url": "https://other.example/x.mp3", "result": "改掉"})
    assert again.status_code == 409 and again.json() == {"detail": "该呼叫任务已回写过结果"}, again.text   # 修前 200
    rows = client.get(f"{B}/call-tasks?phone=13928700287", headers=admin).json()
    assert [(r["status"], r["record_url"], r["result"]) for r in rows] == [
        ("connected", "https://rec.example/287.mp3", "已讲解限盐")]   # 修前被改成 cancelled、录音地址换掉
