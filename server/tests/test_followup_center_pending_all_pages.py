"""随访中心「待随访任务」面板续页取全，标题印全量条数（P2-1546，第四十五批扫描 AI2-9）。

`renderFollowups` 原先只取 `/api/followups?status=pending` 的缺省一页（`limit=100`，按应随访日排）。扫描实测（修前代码）：
同一机构 101 条待随访，页面请求只回 100 行、`X-Total-Count` 101；标题「待随访任务（100）」，同页统计卡写着待随访 101；
应随访日最晚的第 101 条页面上没有行——「完成」「取消」都够不着。

修法照 P2-1333 / P2-456「待办按状态取全」：待随访改用 `shared.js` 的 `fetchAllPages` 续页取全，标题按取全后的条数印。
页面原样拿到 node 里跑、请求转给真接口（夹具见 `tests/chronic_followup_pages.py`）。
"""
import re
import shutil
from datetime import timedelta

import pytest

from chronic_followup_pages import run
from conftest import business_today, login

from app.database import SessionLocal
from app.models import FollowupTask

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def doctor(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21546 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21546_doc", "password": "passw0rd1", "full_name": "随访医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21546 郑一", "id_card": "330106195001011546"}).json()["id"]
    first = business_today() + timedelta(days=1)
    with SessionLocal() as db:   # 101 条待随访（逐条走接口太慢，直接落库），应随访日一天一条，第 101 条最晚
        db.add_all([FollowupTask(patient_id=patient, org_id=org, category="discharge", title=f"P21546 出院随访 {i + 1}",
                                 due_date=(first + timedelta(days=i)).isoformat()) for i in range(101)])
        db.commit()
    return login(client, "p21546_doc", "passw0rd1")


def test_待随访过100条_页面101行_标题101(client, doctor):
    first_page = client.get("/api/followups?status=pending", headers=doctor)
    assert (len(first_page.json()), first_page.headers["X-Total-Count"]) == (100, "101")   # 缺省一页只到第 100 条
    html = run(client, doctor, "doctor", "await renderFollowups(); return pageHtml();")
    panel = html[html.index("<h3>待随访任务"):]
    assert panel.startswith("<h3>待随访任务（101）</h3>"), panel[:60]   # 修前「待随访任务（100）」
    rows = re.findall(r'<td>P21546 出院随访 (\d+)</td>', panel)
    assert len(rows) == 101 and rows[-1] == "101", rows[-3:]   # 修前 100 行，第 101 条不在页面上
    assert len(re.findall(r'data-cancel="\d+"', panel)) == 101   # 每一行都能「取消」，同样能「完成」
