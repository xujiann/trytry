"""医生移动端提交术中记录，转归「死亡」时回执不再说「术后随访任务已自动派生」（P2-779，第二十批「界面文案 vs 行为」扫描 M2-5）。

后端按 P2-499 对转归「死亡」不排术后随访、不发通知，移动端回执原先一律写「术中记录已提交，术后随访任务已自动派生」。实测：
转归「死亡」提交 201，该手术的术后随访任务 0 条，回执照写「已自动派生」。这里按后端的同一个判据钉住回执分支。
"""
import inspect
from pathlib import Path

from app.routers import surgery

DOCTOR_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "m" / "doctor.js"


def test_回执按转归分支_与后端不派随访的判据同一个():
    assert 'if body.outcome != "死亡":' in inspect.getsource(surgery.create_record)   # 判据自证：后端死亡不派随访
    js = DOCTOR_JS.read_text(encoding="utf-8")
    start = js.index("/api/surgery/requests/${id}/record")
    # P2-1093 起经 cardForm 插表单：从 cardForm 的调用切到提交成功后重载列表为止
    body = js[js.rindex('"surg-record-form"', 0, start):js.index("await loadSurgery();", start)]
    assert 'const died = f.outcome.value === "死亡";' in body
    assert 'died ? "术中记录已提交（转归死亡，不派生术后随访）"' in body   # 修前一律「已自动派生」
