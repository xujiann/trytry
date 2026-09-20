"""押金 / 收费项改档 / 设备与数据源 / 病种目录的界面入口守卫（P1-40，三模块清零）。

全平台棘轮只回答"这条路径有没有人调用过"。这三块各有它看不见、而写错了后果
不轻的事：

* **改档与调价是两条路**：价格要对外公示，调价依据与生效日期必须留痕，所以走
  `/reprice`；改名字/类别/启停不涉及公示，走 `PATCH`。合成一个入口的话，改个
  错别字也会在调价历史里留下一条无依据的记录。
* **退押金走押金专用通道**：`/deposits/refund` 与支付退款 `/payments/{id}/refund`
  不是同一本账——押金是预交的钱，支付是已完成的交易。
* **补记同步失败必须写原因**：成功率是算出来的，掉了却查不出为什么，等于这条
  日志白记。
* **定量控制目标至少要给一侧区间**：两侧都空的定量目标，"达标"没有判据。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
CLINICAL = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
SPD = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _billing() -> str:
    start = CLINICAL.index("async function renderBilling()")
    nxt = CLINICAL.index("async function ", start + 10)
    while "renderBilling" in CLINICAL[start:nxt] and nxt < len(CLINICAL) - 1:
        break
    return CLINICAL[start:start + 20000]


BILL = _billing()


@pytest.mark.parametrize(
    "path",
    [
        "/api/billing/deposits",
        "/api/billing/deposits/balance",
        "/api/billing/deposits/alerts",
        "/api/billing/deposits/refund",
        "/api/billing/charge-items/${ciEdit}",
    ],
)
def test_押金与收费项端点都调到(path):
    assert path in BILL, f"账单页没有调用 {path}"


def test_改档与调价是两条路():
    """价格要公示、调价要留痕走 reprice；改名字/类别/启停走 PATCH。"""
    assert "/reprice" in BILL and "charge-items/${ciEdit}" in BILL, "两条路没有都在"
    idx = BILL.index("charge-items/${ciEdit}")
    around = BILL[max(0, idx - 1200):idx]
    assert "不涉及公示" in around or "改个错别字" in around, (
        "没说清为什么分成两条路——合成一个入口的话，改个错别字也会在调价历史里"
        "留下一条无依据的记录"
    )
    assert "body.price" not in BILL[idx - 800:idx], "改档里混进了价格字段"


def test_退押金有二次确认且走专用通道():
    assert "/api/billing/deposits/refund" in BILL, "退押金没走押金专用通道"
    idx = BILL.index("/api/billing/deposits/refund")
    around = BILL[max(0, idx - 400):idx]
    assert "confirm(" in around, "退押金没有二次确认"


@pytest.mark.parametrize(
    "path",
    [
        "/api/spd/devices?",
        "/api/spd/devices/${",
        "/api/spd/data-sources-monitor",
        "/api/spd/data-sources/${",
        "/sync-logs",
        "/api/spd/programs/${",
        "/targets",
        "/versions",
        "/api/spd/targets/${",
    ],
)
def test_设备与病种目录端点都调到(path):
    assert path in SPD, f"spd 页面没有调用 {path}"


def test_补记同步失败必须写原因():
    assert "补记失败必须写明原因" in SPD, (
        "成功率是算出来的，掉了却查不出为什么，等于这条日志白记"
    )


def test_定量目标至少给一侧区间():
    assert "定量目标至少要填上限或下限" in SPD, (
        "两侧都空的定量目标，「达标」没有判据"
    )


def test_设备解绑走留空():
    """一台设备同时只绑一个患者；留空即解绑。"""
    idx = SPD.index("devBind.dataset.devBind")
    around = SPD[max(0, idx - 700):idx]
    assert "patient_id: null" in around, "没有解绑路径——换设备时旧绑定会一直挂着"


def test_版本历史说清了它的用途():
    assert "历史入组的判定依据按当时那一版看" in SPD, (
        "版本历史没说清用途：改纳入规则不该让历史入组的依据跟着变"
    )
