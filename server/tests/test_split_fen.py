"""按权重分到分的共用函数 `numtypes.split_fen`（最大余数法，原在基金结余分配里、抽出来给成本分摊共用）。

不变量：各份合计分毫等于总额；每份都是按比例精确值向下或向上取整到分（不出负数）；同余数按原顺序，可复现。
"""
import itertools
import math
import random

from app.numtypes import split_fen


def _cents(values):
    return [round(v * 100) for v in values]


def test_整除不了的零头按余数补齐_合计分毫等于总额():
    assert split_fen(100, [1, 1, 1]) == [33.34, 33.33, 33.33]   # 逐份四舍五入是 99.99
    assert split_fen(0.03, [50, 50]) == [0.02, 0.01]             # 同余数按原顺序
    assert split_fen(500.5, [60, 40]) == [300.3, 200.2]          # 整除得开的与逐份四舍五入一致


def test_零权重的份分不到钱():
    assert split_fen(10, [3, 0, 7]) == [3.0, 0.0, 7.0]
    assert split_fen(0.01, [99.9, 0.0, 0.1]) == [0.01, 0.0, 0.0]


def test_不变量抽样():
    """多份小额（最容易各自四舍五入凑不上）+ 随机金额与权重：合计恒等、每份夹在精确值的上下一分之间。"""
    rng = random.Random(20260927)
    cases = [(t / 100, w) for t in range(0, 30) for w in itertools.product((33, 33.3, 1, 0.2), repeat=3)]
    cases += [(rng.randint(0, 10**9) / 100, [rng.uniform(0, 100) for _ in range(rng.randint(1, 8))])
              for _ in range(2000)]
    for total, weights in cases:
        if sum(weights) <= 0:
            continue
        parts = split_fen(total, weights)
        assert sum(_cents(parts)) == round(total * 100), (total, weights, parts)
        for part, w in zip(_cents(parts), weights):
            exact = total * 100 * w / sum(weights)   # 容差只吸收浮点噪声，不放宽到「差一分也算」
            assert math.floor(exact - 1e-6) <= part <= math.ceil(exact + 1e-6), (total, weights, parts)
