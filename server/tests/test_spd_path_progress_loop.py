"""专病路径绕环一圈进度就显示 100%，还能按办结条数越数越多（P2-1121，第三十二批「规则与配置的求值口径」扫描 B4-8 的
进度一半）。

`advance_path` 的进度 = 已办结的路径任务条数 ÷ 节点数。路径可以按 `next_key` 回到走过的节点（回环是有意支持的，
`tasks._resume_paused` 写明了），同一个节点每走一趟派一条任务、办结一条就多算一条。修前实测（b4/r4）：复诊 → 评估 →
复诊成环、结案节点在环外，逐次办结后进度依次为 33 → 66 → 100 → 100 → 100，状态始终是执行中——结案节点一次没走到，
页面上却是满格。

修法：进度按「办结过任务的不同节点数 ÷ 节点数」算。环合不合法、发布时拦不拦待裁定（与 P2-1033 一并定），不在此列。
"""
B = "/api/spd"


def test_回环逐次办结_进度只数办结过的不同节点_不到100(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1121 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ht = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1121 患者", "id_card": "330127197001011121"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    template = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": ht["id"], "code": "P1121-LOOP", "name": "P1121 复诊评估循环"}).json()["id"]
    for key, seq, nxt in (("revisit", 1, "assess"), ("assess", 2, "revisit"), ("close", 3, "")):
        node = client.post(f"{B}/path-templates/{template}/nodes", headers=admin, json={
            "key": key, "name": key, "seq": seq, "next_key": nxt})
        assert node.status_code == 201, node.text
    assert client.post(f"{B}/path-templates/{template}/status", headers=admin,
                       json={"status": "published"}).status_code == 200
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment.json()["id"], "template_id": template})
    assert started.status_code == 201, started.text
    instance = started.json()["id"]

    done_nodes, seen = set(), []
    for _ in range(5):
        (task,) = [t for t in client.get(f"{B}/tasks", headers=admin, params={"instance_id": instance, "limit": 50}).json()
                   if t["status"] not in ("done", "cancelled")]
        assert client.post(f"{B}/tasks/{task['id']}/complete", headers=admin,
                           json={"result": {"note": "已办"}}).status_code == 200
        done_nodes.add(task["node_key"])
        state = client.get(f"{B}/path-instances/{instance}", headers=admin).json()
        assert state["status"] == "running"
        assert state["progress"] == int(len(done_nodes) / 3 * 100), (state["progress"], done_nodes)
        seen.append(state["progress"])
    assert seen == [33, 66, 66, 66, 66]   # 修前 [33, 66, 100, 100, 100]：结案节点一次没走到就满格
