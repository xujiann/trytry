"""呼叫任务派发没受理，页面上看不出来；派发失败的原因还会盖掉网关回调已回写的结果（P2-367）。

模块文档说「派发失败不抛异常：任务留在 pending，result 里记失败原因」，培训手册也说「原因记在结果里——通道抖动不会丢任务」。
可「转呼叫」走的是丢掉回执、整页重画的 `postAction`，呼叫任务表又不显示结果列：用 http 网关的县，网关不通时这条任务
看着与普通待呼叫一模一样，网关永远不会拨，也没人知道该人工外呼。另一头，记原因是无条件写：网关超时（5 秒）之后其实
已受理、回调先一步回写了结果的，沟通结果被盖成「呼叫网关异常」。

修法：原因只在仍待呼叫时记（与回写结果「只从待呼叫翻」同一口径）；表里显示结果列、派发没受理的标出来，
「转呼叫」把回执说出来。自动重派 / 补派要不要做另行待裁定。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    return {"n": 0}


def _patient(client, admin, world):
    world["n"] += 1
    resp = client.post("/api/patients", headers=admin, json={
        "name": f"P2367 患者{world['n']}", "id_card": f"33010219700101{world['n']:03d}7", "phone": "13800002367"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class _Provider:
    name = "test"

    def __init__(self, dispatch):
        self._dispatch = dispatch

    def dispatch(self, task_id, phone, ref_type):
        return self._dispatch(task_id)


@pytest.fixture
def provider():
    from app.spd.callcenter import set_call_provider

    yield lambda fn: set_call_provider(_Provider(fn))
    set_call_provider(None)


def _task(task_id):
    from app.spd.models import SpdCallTask

    with SessionLocal() as db:
        row = db.get(SpdCallTask, task_id)
        return row.status, row.result


def test_派发没受理_原因记进结果_列表里看得到(client, admin, world, provider):
    provider(lambda task_id: (False, "呼叫网关返回 503"))
    patient = _patient(client, admin, world)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient})
    assert resp.status_code == 201, resp.text
    assert resp.json()["dispatch"] == {"accepted": False, "note": "呼叫网关返回 503"}
    assert _task(resp.json()["id"]) == ("pending", "呼叫网关返回 503")
    rows = client.get(f"{B}/call-tasks", headers=admin, params={"status": "pending"}).json()
    assert {"id": resp.json()["id"], "result": "呼叫网关返回 503"}.items() <= next(
        r for r in rows if r["id"] == resp.json()["id"]).items()


def test_网关超时前回调已回写_派发失败的原因不盖掉沟通结果(client, admin, world, provider):
    from app.spd.models import SpdCallTask

    def late_timeout(task_id):
        # 网关其实已受理、回调先一步把结果回写好了，这边才等到超时
        with SessionLocal() as db:
            row = db.get(SpdCallTask, task_id)
            row.status, row.result, row.duration_s = "connected", "已接通：血压平稳，按时服药", 95
            db.commit()
        return False, "呼叫网关异常：timed out"

    provider(late_timeout)
    patient = _patient(client, admin, world)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient})
    assert resp.status_code == 201, resp.text
    assert _task(resp.json()["id"]) == ("connected", "已接通：血压平稳，按时服药")   # 修前结果被盖成「呼叫网关异常」
    assert resp.json()["status"] == "connected"


def test_页面显示结果列_转呼叫把回执说出来():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
    panel = src[src.index('panel("呼叫任务与录音"'):]
    panel = panel[:panel.index("</tr>`)}")]
    assert '"结果"' in panel and "c.result" in panel, panel   # 修前表里没有结果列
    handler = src[src.index("if (call) {"):]
    handler = handler[:handler.index("\n    }\n")]
    assert "return postAction(" not in handler and "r.dispatch.accepted" in handler, handler   # 修前 postAction 丢掉回执
