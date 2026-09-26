"""慢专病的扫描广播无人在线即丢：平台同形状的预警会转发运维告警 webhook，慢专病这一份没有（P2-273）。

`spd.platform.broadcast` 的 docstring 写着「与平台 `jobs.py::_alert` 同一形状」；平台那份在广播确定没送达时（无 Redis
总线且本进程没有在线连接）转发 webhook 摘要（工程包 P2），慢专病这份只广播——夜间 / 节假日无人在线时，任务超期与数据源
同步的结果就此静默。修法：同一句兜底（未配置 webhook 时仍是空操作）。
"""
from app.spd import platform


def test_广播没送达_转发webhook兜底(monkeypatch):
    sent = []
    monkeypatch.setattr(platform._ws_manager, "broadcast", lambda message, target_org_id=None: False)
    monkeypatch.setattr(platform, "_send_alert", lambda kind, message: sent.append((kind, message)) or True)
    platform.broadcast("spd_task_overdue", "慢专病任务超期", 3)
    assert sent == [("unattended:spd_task_overdue", "慢专病任务超期：3 条（无在线管理端，广播未送达）")]   # 修前 []


def test_送达了或者没有数就不外发(monkeypatch):
    sent = []
    monkeypatch.setattr(platform, "_send_alert", lambda kind, message: sent.append(kind) or True)
    monkeypatch.setattr(platform._ws_manager, "broadcast", lambda message, target_org_id=None: True)
    platform.broadcast("spd_sync", "慢专病数据源同步", 2)
    monkeypatch.setattr(platform._ws_manager, "broadcast", lambda message, target_org_id=None: False)
    platform.broadcast("spd_sync", "慢专病数据源同步", 0)
    assert sent == []
