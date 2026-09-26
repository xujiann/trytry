"""手术申请清单只取最新 100 条再挑状态：提前申请的择期手术排到 100 条之外，审批 / 排班 / 术中记录的入口跟着消失（P2-361）。

`GET /api/surgery/requests` 默认 `limit=100`、按编号倒序，另支持 `?status=`。医生移动端「待填术中记录」取最新 100 条再挑
已排班的；管理端手术页同样只取最新 100 条，按状态给「审批 / 排班 / 术中记录」按钮——提前两周申请的择期手术，到手术日
早已排在 100 条之外，手机上术中记录填不了，管理端也点不到。与已修的 P2-154（按状态筛放到截断之前）同一形状。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_医生移动端按状态取已排班的申请():
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    start = doctor.index("async function loadSurgery(")
    body = doctor[start:doctor.index("\n}\n", start)]   # 到函数自己的收尾
    assert 'api("/api/surgery/requests?status=scheduled")' in body
    assert 'api("/api/surgery/requests")' not in body   # 修前：取最新 100 条再在页面里挑


def test_管理端手术页单独取还要办的三个状态():
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = mgmt.index("async function renderSurgery(")
    body = mgmt[start:mgmt.index("\nasync function ", start + 1)]
    statuses = re.search(r'\[("requested", "approved", "scheduled")\]\.map\(\(s\) => api\(`/api/surgery/requests\?status=\$\{s\}`\)\)', body)
    assert statuses, "审批 / 排班 / 术中记录三个待办状态要单独取，不能只靠最新 100 条"
