"""检查申请单清单带上报告号（第十六批「导出 / 打印 vs 页面」扫描 T1-6）。

报告打印、附件、修订史都按报告号取；检查页的申请单清单原先只给申请单号，两套编号各自递增——先出报告的未必是先开的单。
实测（修前）：张三开单 #1、李四开单 #2，中心先出李四的报告（报告 1）、再出张三的（报告 2）；照着张三的申请单号 1
填进「报告ID」打印，打出来是「姓名 李四｜结论 李四：脑梗死」。修后清单行带 `report_id`，页面在这一行直接给
「打印报告」「修订史」。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "T1-6 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    reqs = {}
    for name, card in (("张三", "330281197806062214"), ("李四", "330281197907072212")):
        patient = client.post("/api/patients", headers=admin, json={"name": f"T1-6 {name}", "id_card": card}).json()["id"]
        req = client.post("/api/exams", headers=admin, json={
            "patient_id": patient, "from_org_id": org, "center_type": "imaging",
            "item_code": "CT-T16", "item_name": "头颅CT(T1-6)"})
        assert req.status_code in (200, 201), req.text
        reqs[name] = req.json()["id"]
    reports = {}
    for name in ("李四", "张三"):   # 后开的单先出报告
        assert client.post(f"/api/exams/{reqs[name]}/claim", headers=admin).status_code == 200
        rep = client.post(f"/api/exams/{reqs[name]}/report", headers=admin, json={
            "finding": "", "conclusion": f"T1-6 {name}：结论", "critical": False, "reported_by": "影像科"})
        assert rep.status_code in (200, 201), rep.text
        reports[name] = rep.json()["id"]
    pending = client.post("/api/exams", headers=admin, json={
        "patient_id": client.post("/api/patients", headers=admin, json={
            "name": "T1-6 王五", "id_card": "330281198008082210"}).json()["id"],
        "from_org_id": org, "center_type": "imaging", "item_code": "CT-T16", "item_name": "头颅CT(T1-6)"}).json()["id"]
    return {"reqs": reqs, "reports": reports, "pending": pending}


def test_申请单清单每行带自己的报告号(client, admin, world):
    rows = {r["id"]: r for r in client.get("/api/exams?limit=500", headers=admin).json()}
    for name in ("张三", "李四"):
        assert rows[world["reqs"][name]]["report_id"] == world["reports"][name]   # 修前没有这一列
    assert world["reports"]["张三"] != world["reqs"]["张三"] or world["reports"]["李四"] != world["reqs"]["李四"]
    assert rows[world["pending"]]["report_id"] is None                            # 没出报告的不给


def test_按清单给的报告号打印_打的是这位患者的报告(client, admin, world):
    rows = {r["id"]: r for r in client.get("/api/exams?limit=500", headers=admin).json()}
    printed = client.get(f"/api/print/exam-reports/{rows[world['reqs']['张三']]['report_id']}", headers=admin)
    assert printed.status_code == 200 and "T1-6 张三：结论" in printed.text and "T1-6 李四" not in printed.text
