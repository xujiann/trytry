"""查「谁看过这个人」的留痕：落库失败原先悄无声息，查一个不存在的患者编号照回 []（P2-471）。

`GET /api/access-logs?patient_id=` 本身也留痕（查谁看过这个人，本身就在看这个人的隐私）。这笔留痕原先在
`access_logs._log_view` 里另写了一份：落库失败只回滚、一行日志都不打；共用底座（`visibility._write_access_row`）
同样的失败会记「调阅留痕写入失败……本条留痕丢失」的错误日志。患者编号不存在时外键落不了库，同样被吞掉，
接口照回 200 []——同文件的 `/stats` 对同一个编号是 404。

修法：留痕走共用底座；查不存在的患者 404「患者不存在」，与 `/stats` 同一句。另：用户手册两处写着「可按患者/调阅人/
机构/依据/时间段筛选」，接口也收 `org_id`，查询表单却没有机构这一格——补上。
"""
import logging
import os

from app import visibility


def test_查不存在的患者编号是404_与统计同一句(client, admin):
    resp = client.get("/api/access-logs", headers=admin, params={"patient_id": 999999})
    assert resp.status_code == 404, resp.text   # 修前 200 []，留痕因外键落不了库、被吞掉
    assert resp.json() == {"detail": "患者不存在"}
    stats = client.get("/api/access-logs/stats", headers=admin, params={"patient_id": 999999})
    assert stats.status_code == 404 and stats.json() == resp.json()


class _BrokenSession:
    """落库必失败的会话：模拟库满 / 断连。"""

    def add(self, row):
        pass

    def commit(self):
        raise RuntimeError("模拟：库写不进")

    def rollback(self):
        pass

    def close(self):
        pass


def test_留痕落库失败记错误日志_查询照常(client, admin, monkeypatch, caplog):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2471 患者", "id_card": "330106197003032471"}).json()["id"]
    monkeypatch.setattr(visibility, "SessionLocal", _BrokenSession)
    with caplog.at_level(logging.ERROR):
        resp = client.get("/api/access-logs", headers=admin, params={"patient_id": patient})
    monkeypatch.undo()
    assert resp.status_code == 200, resp.text   # 留痕失败不挡查询
    lost = [r for r in caplog.records if "调阅留痕写入失败" in r.getMessage() and "access_log_view" in r.getMessage()]
    assert lost, "修前：落库失败只回滚、一行日志都不打，丢了哪一笔无从查起"


def test_查询表单有机构这一格_与手册和接口对上():
    path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "pages-clinical.js")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderAccessLogs()")
    body = source[start:source.index("\nasync function ", start + 1)]
    form = body[body.index('<form class="inline" id="al-search">'):body.index("</form>", body.index('id="al-search"'))]
    assert 'name="org_id"' in form   # 修前没有：手册说能按机构筛，页面上筛不了
    for name in ("patient_id", "username", "basis", "start", "end"):
        assert f'name="{name}"' in form
