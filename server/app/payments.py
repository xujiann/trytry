"""HTTP 网关式支付通道：PaymentGateway 协议的真通道实现（工程包 I2）。

微信/支付宝/银联的直连 SDK 不进本仓库（与 sms.py 同一取舍）：部署方自建或
采购一个"聚合支付网关"，按下面的薄协议对接一次即可。金额一律**分**为单位的
整数，避免浮点金额在通道两侧各舍各的。

网关侧须实现的三个接口（出网请求均带 X-Timestamp / X-Signature 签名头，
算法见 app/egress.py，密钥为 MEDPLAT_PAYMENT_GATEWAY_KEY）：

- 下单  POST {base}/pay
    请求 {"order_id": int, "amount_fen": int, "channel": str, "notify_url": str}
    应答 {"accepted": true, "trade_no": str, "pay_url": str, "qr_code": str}
    accepted 只代表**受理**：订单停在 pending，支付结果由网关回调
    ``POST /api/billing/payments/callback``（notify_url，部署方拼上站点域名；
    同一签名口径）异步确认后才转 paid——与内置 MockGateway"下单即 paid"的
    同步语义不同，routers/billing.py 的 create_payment 按 pending 标志分流。
- 退款  POST {base}/refund
    请求 {"trade_no": str, "amount_fen": int}
    应答 {"success": true, "refund_no": str, "message": str}
    网关式简化为同步应答；漏单/掉单由日终对账兜底。
- 流水  GET  {base}/transactions?date=YYYY-MM-DD
    应答 {"transactions": [{"trade_no": str, "amount_fen": int}]}
    供日终对账拉取通道侧净额流水（替代 Mock 的本地镜像）。

通道故障不抛给调用方（pay/refund 返回失败结果），唯 query_transactions
例外：对账拉不到流水时**必须**失败——返回空列表会把当日全部本地单误判成
"通道缺失"，一份错误的对账单比没有对账单更糟。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from .egress import signed_headers

logger = logging.getLogger("medplat.payments")

#: 网关回调的站内路径；notify_url 传相对路径，由部署方在网关侧配置站点域名。
CALLBACK_PATH = "/api/billing/payments/callback"

_TIMEOUT_SECONDS = 5.0


def to_fen(amount: float) -> int:
    """元 → 分（金额过网关一律整数分，见模块文档）。"""
    return int(round(amount * 100))


class HttpGatewayPaymentGateway:
    """通用 HTTP 支付网关通道（异步确认语义，注册为 channel="gateway"）。"""

    name = "gateway"

    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.key = key

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """签名并 POST；网络/协议异常一律返回 None（调用方转失败结果）。"""
        import httpx

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **signed_headers(self.key, body)}
        try:
            resp = httpx.post(f"{self.url}{path}", content=body, headers=headers, timeout=_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("[PAY-HTTP] 支付网关调用异常 path=%s", path)
            return None
        if resp.status_code >= 400:
            logger.error("[PAY-HTTP] 网关拒绝 path=%s status=%s body=%s", path, resp.status_code, resp.text[:200])
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.error("[PAY-HTTP] 网关应答非 JSON path=%s body=%s", path, resp.text[:200])
            return None
        return data if isinstance(data, dict) else None

    def pay(self, order_id: int, amount: float, channel: str) -> dict:
        """网关下单。成功=受理（pending=True），到账由回调确认。"""
        data = self._post(
            "/pay",
            {
                "order_id": order_id,
                "amount_fen": to_fen(amount),
                "channel": channel,
                "notify_url": CALLBACK_PATH,
            },
        )
        if data is None:
            return {"success": False, "trade_no": "", "message": "支付网关不可达或应答异常"}
        if not data.get("accepted"):
            return {
                "success": False,
                "trade_no": "",
                "message": str(data.get("message", "网关未受理下单"))[:256],
            }
        return {
            "success": True,
            "pending": True,  # 异步确认：受理≠到账，订单停在 pending 等回调
            "trade_no": str(data.get("trade_no", "")),
            "pay_url": str(data.get("pay_url", "")),
            "qr_code": str(data.get("qr_code", "")),
            "message": "",
        }

    def refund(self, trade_no: str, amount: float) -> dict:
        """网关退款（同步应答，差错由日终对账兜底）。"""
        data = self._post("/refund", {"trade_no": trade_no, "amount_fen": to_fen(amount)})
        if data is None:
            return {"success": False, "refund_no": "", "message": "支付网关不可达或应答异常"}
        return {
            "success": bool(data.get("success")),
            "refund_no": str(data.get("refund_no", "")),
            "message": str(data.get("message", ""))[:256],
        }

    def query_transactions(self, db: Session, date: str) -> list[dict]:
        """拉取某日通道流水（对账用）。拉取失败抛 RuntimeError——见模块文档。"""
        import httpx

        headers = signed_headers(self.key, b"")  # GET 无体，对空串签名
        try:
            resp = httpx.get(
                f"{self.url}/transactions", params={"date": date}, headers=headers, timeout=_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.exception("[PAY-HTTP] 拉取通道流水异常 date=%s", date)
            raise RuntimeError("支付网关流水拉取失败，对账中止（本地数据未变）") from exc
        if resp.status_code >= 400:
            logger.error("[PAY-HTTP] 流水接口拒绝 status=%s body=%s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"支付网关流水接口返回 {resp.status_code}，对账中止")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError("支付网关流水应答非 JSON，对账中止") from exc
        rows = data.get("transactions", []) if isinstance(data, dict) else data
        out: list[dict] = []
        for row in rows:
            out.append(
                {
                    "trade_no": str(row.get("trade_no", "")),
                    "amount": round(int(row["amount_fen"]) / 100.0, 2)
                    if "amount_fen" in row
                    else round(float(row.get("amount", 0.0)), 2),
                }
            )
        return out
