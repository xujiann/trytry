"""同一档案同一模板「只一条在途」的判定是先查后建：两个人同时点启动，两路都查不到在途的、各建一条（P2-269）。

`POST /api/spd/path-instances` 的 docstring：「同一纳管档案下同一模板只允许有一个在跑的实例：重复启动会生成两份并行任务，
办完一份另一份还挂着」——判定却是先查有没有在途实例、再建，两次请求交错时两路都查不到。修法：判定与建实例圈进档案
这一行的临界区（`serialized_on`）。

这里用两个线程把交错钉成确定的时序：第一路查完「没有在途」、建实例之前停住；第二路整个跑一遍；再放第一路。
修前第二路建成（201），第一路随后也建成——两条在途；修后第二路在临界区外等第一路提交，进来查到在途即 409。
"""
import threading
import time

B = "/api/spd"


def test_同时启动同一路径_只建成一条(client, admin, monkeypatch):
    from app.database import SessionLocal
    from app.spd import service
    from app.spd.models import SpdPathInstance, SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2269 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2269 患者", "id_card": "330127197612122269"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P2269_PATH", name="P2269 路径", status="published")
        db.add(template)
        db.flush()
        db.add(SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1))
        db.commit()
        template_id = template.id

    real_start = service.start_path
    first_checked, release_first = threading.Event(), threading.Event()

    def paused_start(*args, **kwargs):
        if not first_checked.is_set():   # 第一路：查完「没有在途」、建实例之前停住
            first_checked.set()
            release_first.wait(timeout=10)
        return real_start(*args, **kwargs)

    monkeypatch.setattr(service, "start_path", paused_start)
    results = {}

    def start(tag):
        results[tag] = client.post(f"{B}/path-instances", headers=admin,
                                   json={"enrollment_id": enrollment, "template_id": template_id}).status_code

    first = threading.Thread(target=start, args=("first",))
    first.start()
    assert first_checked.wait(timeout=10)
    second = threading.Thread(target=start, args=("second",))
    second.start()
    second.join(timeout=1.5)   # 修前第二路此刻已经建成；修后它在临界区外等
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert sorted(results.values()) == [201, 409], results   # 修前 [201, 201]
    with SessionLocal() as db:
        assert db.query(SpdPathInstance).filter_by(enrollment_id=enrollment, template_id=template_id).count() == 1
    time.sleep(0)   # 线程都已收尾
