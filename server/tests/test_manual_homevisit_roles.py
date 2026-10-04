"""用户手册公卫一章写「申请 → 派单 → 完成；取消工单」，可接口和页面都不给公卫派单、取消；真正能派单的医师、经办两章一句
没写（P2-1406，第四十一批扫描 AE3-7）。

派单、取消只收经办、医师（`homevisits.dispatch_visit` / `cancel_visit` 的 `require_roles("operator", "doctor")`），申请与
完成另收公卫；页面按钮照这个给（P2-429）。公卫照手册派单、取消都是 403「需要以下角色之一：经办人员、医师」。修后公卫一章
只写公卫能做的（提交申请；已派单的工单可登记完成），派单与取消写进医师、经办两章。末一条按接口走一遍，钉住手册写的分工。
"""
from pathlib import Path

import pytest

from conftest import login

MANUAL = (Path(__file__).resolve().parents[2] / "docs" / "用户手册.md").read_text(encoding="utf-8")


def _chapter(title: str) -> str:
    start = MANUAL.index(title)
    return MANUAL[start:MANUAL.index("\n## ", start + 1)]


def test_公卫一章只写公卫能做的():
    chapter = _chapter("## 第五章 公卫人员（public_health）")
    start = chapter.index("同页「送医送护上门」")
    step = chapter[start:chapter.index("\n7. **", start)]
    assert "申请 → 派单" not in step and "派单（填上门人员）" not in step, step   # 修前「申请 → 派单 → 完成；取消工单」
    assert "提交上门申请" in step and "已派单的工单可登记完成" in step, step
    assert "派单与取消工单由医师或" in step and "经办人员办理" in step, step


@pytest.mark.parametrize("title, item", [("## 第三章 医师（doctor）", "16. **上门服务**"),
                                         ("## 第六章 经办人员（operator）", "10. **上门服务**")])
def test_派单与取消写在医师与经办两章(title, item):
    chapter = _chapter(title)
    assert item in chapter, f"{title} 没写上门服务（修前这两章一句没写）"
    step = chapter[chapter.index(item):]
    assert "家医签约页「送医送护上门」" in step and "派单（填上门人员）" in step, step
    assert "待派单、已派单的\n    工单都可取消" in step, step                     # 已派单可取消（P2-595）


def test_经办的页面清单列上家医签约():
    chapter = _chapter("## 第六章 经办人员（operator）")
    pages = chapter[chapter.index("### 页面清单"):chapter.index("### 关键操作")]
    assert "家医签约（送医送护上门的派单与取消）" in pages


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21406 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21406 居民", "id_card": "330127194303031406"}).json()["id"]
    heads = {}
    for username, role in (("p21406_ph", "public_health"), ("p21406_doc", "doctor"), ("p21406_op", "operator")):
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pass123456", "role": role, "org_id": org})
        assert made.status_code in (200, 201), made.text
        heads[role] = login(client, username, "pass123456")
    return {"org": org, "patient": patient, **heads}


def test_手册写的分工与接口一致(client, world):
    ph, doc, op = world["public_health"], world["doctor"], world["operator"]

    def apply():
        resp = client.post("/api/homevisits", headers=ph, json={
            "patient_id": world["patient"], "org_id": world["org"], "service_type": "nursing"})
        assert resp.status_code == 201, resp.text                  # 公卫：提交申请
        return resp.json()["id"]

    first = apply()
    for action, payload in (("dispatch", {"assignee_name": "公卫自己"}), ("cancel", None)):
        resp = client.post(f"/api/homevisits/{first}/{action}", headers=ph, json=payload)
        assert resp.status_code == 403, (action, resp.text)         # 修前手册让公卫派单、取消
    assert client.post(f"/api/homevisits/{first}/dispatch", headers=op, json={"assignee_name": "上门护士甲"}).status_code == 200
    done = client.post(f"/api/homevisits/{first}/complete", headers=ph, json={"service_note": "已更换导尿管"})
    assert done.status_code == 200, done.text                       # 公卫：已派单的工单登记完成
    second = apply()
    assert client.post(f"/api/homevisits/{second}/dispatch", headers=doc, json={"assignee_name": "上门医生乙"}).status_code == 200
    assert client.post(f"/api/homevisits/{second}/cancel", headers=doc).status_code == 200   # 医师：已派单的也能取消
