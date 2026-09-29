"""疫苗按批号反查的弹窗写「共 N 人次」：N 是这一批的接种人次（同一人打两剂记两次），原先写成「共 N 名受种者」（P2-780，
第二十批「界面文案 vs 行为」扫描 M2-10）。

实测：同一婴儿用同一批次打两剂，接口 `total=2`，弹窗显示「共 2 名受种者」。后端 docstring 早写明 total 是「实际接种了多少
人次」；召回时要通知的人数会被剂次数虚高。要不要另出去重后的人数（这个端点的收口还等 P1-49）不在此列。
"""
import inspect
from pathlib import Path

from app.routers import vaccine_supply

CLINICAL_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "pages-clinical.js"


def test_弹窗按人次说_与后端的口径同一个():
    assert "人次" in inspect.getsource(vaccine_supply.batch_recipients)   # 判据自证：后端 total 数的是接种记录
    js = CLINICAL_JS.read_text(encoding="utf-8")
    popup = js[js.index("/api/vaccine-supply/batches/${d.recipients}/recipients"):]
    popup = popup[popup.index("alert("):popup.index("\n    }\n")]   # 只看弹给人看的那句，不看注释
    assert "共接种 ${r.total} 人次" in popup and "名受种者" not in popup   # 修前「共 N 名受种者」
