"""转诊超时预警回显的阈值与实际筛选用的不一致：带 hours=10000 筛的是 720 小时，回执却写 10000（P2-333）。

`referral_alerts` 把阈值收在 1～720 小时再筛，回执的 `threshold_hours` 却是原参数；页面标题「超过 N 小时未推进」照它显示。
修法：回显实际采用的阈值。
"""
import pytest

URL = "/api/spd/referrals-alerts"


@pytest.mark.parametrize(("asked", "applied"), [(10000, 720), (0, 1), (-5, 1), (48, 48)])
def test_回执写实际采用的阈值(client, admin, asked, applied):
    got = client.get(URL, headers=admin, params={"hours": asked})
    assert got.status_code == 200, got.text
    assert got.json()["threshold_hours"] == applied   # 修前回显原参数
