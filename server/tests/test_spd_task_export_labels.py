"""慢专病任务中心「导出 CSV」与清单同一套文案、创建时间写本地时刻（P2-567，第十一批「导出 / 打印 vs 清单」扫描 Y1-2）。

导出行里的病种、类型、状态、优先级原先是编码（hypertension / followup / pending / 2），前端照原样拼进 CSV；同一筛选下的
清单显示的是病种名、「随访 / 待接收 / 紧急」。创建时间写的是落库的 naive UTC——`clock.to_local` 的 docstring 写明供人工
誊录的导出要写本地时刻。修后：后端把创建时间换成本地，前端拼 CSV 前按清单那几张表换成中文。
"""
import re
import time
from datetime import datetime
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.spd.models import SpdTask

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def test_导出的创建时间写本地时刻(client, admin, east_eight):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2567 患者", "id_card": "330127196001012567"}).json()["id"]
    with SessionLocal() as db:
        # 落库 UTC 09-26 23:30 = 东八区 09-27 07:30
        task = SpdTask(patient_id=patient, title="P2567 随访", task_type="followup", status="pending",
                       priority=2, due_date="2026-10-04", created_at=datetime(2026, 9, 26, 23, 30))
        db.add(task)
        db.commit()
        task_id = task.id
    body = client.get("/api/spd/tasks-export", headers=admin, params={"limit": 5000}).json()
    row = next(r for r in body["rows"] if r[0] == task_id)
    assert row[-1] == "2026-09-27 07:30"   # 修前 2026-09-26 23:30


def test_前端拼CSV前换成清单同一套文案():
    source = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    handler = source[source.index("const d = await api(`/api/spd/tasks-export?${qs}`);"):]
    handler = handler[:handler.index("spdDownloadCsv(")]
    # 与清单行（drawTasks）同一套表：类型、状态、优先级；病种按目录换成名称——换算函数 programOf 提到了页面函数里，
    # 任务详情与导出共用（P2-684），这里核它确实按病种目录换算
    for piece in ("SPD_TASK_TYPES[r[3]]", "SPD_TASK_STATUS[r[5]]", "spdPriorityLabel(r[6])", "programOf(r[2])"):
        assert piece in handler, piece
    assert "const programOf = (code) => (catalog.programs.find((p) => p.code === code) || {}).name || code;" in source
    call = source[source.index("spdDownloadCsv(`spd_tasks_"):]
    assert re.match(r"spdDownloadCsv\(`spd_tasks_\$\{localToday\(\)\}\.csv`, d\.columns, rows\)", call)   # 修前传 d.rows
