"""按份额把一笔金额精确拆分到各方（最大余数法 / Hamilton）。

从 `routers/fund.py` 的结余分配里抽出的通用算法：逐户 `round(total*share, 2)`
会各丢/各多几分钱、合计对不上总额（100 元 3 户均分只分出 99.99）。分配的是
医共体真金白银，账必须分毫不差。

用法：给定总额（元）与各方权重，返回与权重同序的**分**级整数列表，合计恰为
`round(total*100)`。权重可为任意非负实数，内部归一化。全部为 0 或空列表返回全 0。
"""
from __future__ import annotations


def hamilton_cents(total: float, weights: list[float]) -> list[int]:
    """把 `total`（元）按 `weights` 归一化份额分配为**分**，合计恰等于 round(total*100)。

    - 先给每户向下取整到分，剩下的零头一分一分按小数余数从大到小补给各户；
    - 同余数按原顺序稳定（可复现）；
    - 负权重按 0 处理；权重和为 0 或列表为空时全部返回 0。
    """
    n = len(weights)
    if n == 0:
        return []
    clean = [max(float(w), 0.0) for w in weights]
    wsum = sum(clean)
    total_cents = round(total * 100)
    if wsum <= 0 or total_cents == 0:
        return [0] * n
    raw = [total_cents * (w / wsum) for w in clean]
    floors = [int(x) for x in raw]  # 金额非负，int() 即 floor
    remainder = total_cents - sum(floors)  # 待补零头，∈ [0, n)
    order = sorted(range(n), key=lambda i: raw[i] - floors[i], reverse=True)
    cents = floors[:]
    for i in order[: max(remainder, 0)]:
        cents[i] += 1
    return cents


def hamilton_amounts(total: float, weights: list[float]) -> list[float]:
    """同 `hamilton_cents`，但返回元（分/100）。合计恰等于 total（精确到分）。"""
    return [c / 100 for c in hamilton_cents(total, weights)]
