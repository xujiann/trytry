"""统一支付的金额占位按渠道写明留空时收哪一份（P2-605，第十二批「前端提示 vs 后端校验」扫描 Z3-8）。

`billing.create_payment` 金额留空时按渠道取默认额：医保渠道记本单的统筹支付额（`insurance_pay`），其余渠道记押金冲抵后
应补缴的自付额（P1-142）。页面的占位原先一律写「空=押金冲抵后应补缴的自付额」——选了医保留空，收进的是统筹那一份。
"""
import inspect
from pathlib import Path

from app.routers import billing

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def test_后端的默认额确实按渠道分两种():
    src = inspect.getsource(billing.create_payment)
    assert 'settlement.insurance_pay if body.channel == "insurance" else round(settlement.self_pay - offset, 2)' in src


def test_页面占位跟着渠道换():
    start = PAGE.index('$("#pay-form").onsubmit = async (e) => {')
    body = PAGE[start:PAGE.index('$("#recon-form").onsubmit', start)]
    assert 'payChannel.value === "insurance"' in body
    assert '"金额(元，空=本单医保统筹支付额)"' in body and '"金额(元，空=押金冲抵后应补缴的自付额)"' in body
    assert "payChannel.onchange = payHint;" in body and "payHint();" in body
