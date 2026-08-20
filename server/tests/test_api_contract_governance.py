"""接口标准治理——棘轮测试（ratchet）。

    混乱代码  →  标准接口   ，逐块治理，只进不退。

本仓库有 881 个端点，历史上仅 ~14% 声明了 `response_model`（响应契约）。一次性
全补不现实，也违反"改动限定在任务本身"。于是用棘轮：**记录当前欠账基线，断言
它永不变大**。效果——

* 新写端点漏 `response_model` → 欠账变大 → 本测试变红，逼着补上；
* 每治理一个模块 → 欠账变小 → 把 `BASELINE_WITHOUT_RESPONSE_MODEL` 下调到新值；
* 已治理完的模块（`FULLY_GOVERNED`）若被改回裸 dict → 单独变红，防止回退。

这与仓库既有的"欠账不许变长"用例（如 test_stage15 的机构归属欠账）是同一手法。

治理配方见 docs/接口标准与治理.md：先给端点写特征化网钉住当前 JSON，再加
`response_model`（字段与现输出一一对应，保持字节不变），跑绿后回来把基线下调。
"""
from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter
from fastapi.routing import APIRoute

import app.routers as platform_routers
import app.spd.routers as spd_routers

# —— 棘轮基线 ——
# 当前无 response_model 的 /api 端点数。**只允许调小，不允许调大。**
# 每治理一个模块就把这个数字改小（配合 FULLY_GOVERNED）。轨迹：881 中 757 → 756（checkups）→ 753（certs）→ 749（knowledge）→ 745（notifications）→ 743（infectious alerts/late-reports）→ 742（dictionaries import）→ 741（encounters /archive/360 全景视图，嵌套九段逐段建模）。
BASELINE_WITHOUT_RESPONSE_MODEL = 741

# 已完成治理（全部端点声明契约）的模块——这些不许回退。治理新模块后加进来。
FULLY_GOVERNED = {
    "contracts",
    "cssd",
    "organizations",
    "referrals",
    "telemedicine",
    "checkups",  # 样板迁移，见 test_checkups_characterization.py
    "certs",  # 见 test_certs_characterization.py
    "knowledge",  # 见 test_knowledge_characterization.py
    "notifications",  # 见 test_notifications_characterization.py
    "infectious",  # 见 test_infectious_alerts_characterization.py
    "dictionaries",  # 见 test_dictionaries_characterization.py
}


def _iter_endpoints():
    """遍历所有源路由模块里的 APIRoute（环境无关，不依赖 app.routes 的运行期封装）。"""
    for pkg in (platform_routers, spd_routers):
        for modinfo in pkgutil.iter_modules(pkg.__path__):
            if modinfo.name.startswith("_"):
                continue
            module = importlib.import_module(f"{pkg.__name__}.{modinfo.name}")
            for router in (v for v in vars(module).values() if isinstance(v, APIRouter)):
                for route in router.routes:
                    if not isinstance(route, APIRoute):
                        continue
                    if not (route.methods - {"HEAD", "OPTIONS"}):
                        continue
                    yield modinfo.name, route


def _coverage():
    total = 0
    without = 0
    per_module: dict[str, list[int]] = {}
    for mod, route in _iter_endpoints():
        total += 1
        per_module.setdefault(mod, [0, 0])
        per_module[mod][0] += 1
        if route.response_model is None:
            without += 1
            per_module[mod][1] += 1
    return total, without, per_module


def test_响应契约欠账不许变大():
    total, without, _ = _coverage()
    assert without <= BASELINE_WITHOUT_RESPONSE_MODEL, (
        f"无 response_model 的端点从基线 {BASELINE_WITHOUT_RESPONSE_MODEL} 涨到 {without}"
        f"（共 {total}）。新端点必须声明 response_model；见 docs/接口标准与治理.md。"
    )


def test_基线保持收紧不放水():
    # 若欠账已降到基线以下，说明有人治理了模块却没同步下调基线——提醒把基线改小，
    # 保持棘轮"咬得紧"。这条允许有 0 容差地提示，但不阻断（用 !=  的软提示）。
    _, without, _ = _coverage()
    assert without <= BASELINE_WITHOUT_RESPONSE_MODEL
    # 允许等于；小于时打印提示（不失败），引导下调基线。
    if without < BASELINE_WITHOUT_RESPONSE_MODEL:
        print(
            f"\n[提示] 欠账已降到 {without}，请把 BASELINE_WITHOUT_RESPONSE_MODEL "
            f"从 {BASELINE_WITHOUT_RESPONSE_MODEL} 下调到 {without}。"
        )


def test_已治理模块不许回退():
    _, _, per_module = _coverage()
    regressed = {
        m: per_module.get(m, [0, 0])[1]
        for m in FULLY_GOVERNED
        if per_module.get(m, [0, 0])[1] != 0
    }
    assert not regressed, (
        f"已治理模块出现无契约端点（回退）：{regressed}。这些模块应保持全部端点声明 response_model。"
    )
