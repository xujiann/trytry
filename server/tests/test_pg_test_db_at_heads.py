"""集成档开跑前把共用的 PG 测试库推到 heads（P2-581，conftest 块6）。

第九十二轮本机集成档红三条、CI 绿：P2-577 给处方明细加了列，本机常驻的开发库还停在上一轮的结构，各条并发用例的
夹具只在「缺表 / 缺索引」时才升级，表都在就谁也不升级，字母序靠前又碰到新列的用例全红在「列不存在」上。
这里不连库，只钉住三件事：什么时候升级、升级命令对准的是哪个库、失败时怎么报。
"""
import subprocess
import sys

import pytest

from conftest import _pg_url_to_upgrade, _upgrade_pg_to_heads

URL = "postgresql+psycopg2://postgres:pw-in-url@127.0.0.1:55432/medplat_test"


class _Item:
    def __init__(self, *markers: str):
        self._markers = set(markers)

    def get_closest_marker(self, name):
        return object() if name in self._markers else None


def test_这一档有真PG用例且给了库地址才升级(monkeypatch):
    monkeypatch.setenv("MEDPLAT_PG_TEST_URL", URL)
    assert _pg_url_to_upgrade([_Item(), _Item("integration")]) == URL
    assert _pg_url_to_upgrade([_Item(), _Item("e2e")]) == ""   # 单元档、e2e 档不碰任何库
    monkeypatch.delenv("MEDPLAT_PG_TEST_URL")
    assert _pg_url_to_upgrade([_Item("integration")]) == ""   # 没给库地址：集成档整档 skip，不升级


def test_升级命令推到两条链的heads_对准的是PG测试库而不是单元档的SQLite():
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    _upgrade_pg_to_heads(URL, run=fake_run, wait=0)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [sys.executable, "-m", "alembic", "upgrade", "heads"]   # 复数：平台链 + spd 链
    assert kwargs["env"]["MEDPLAT_DATABASE_URL"] == URL   # conftest 顶部把它设成了单元档的 SQLite
    assert kwargs["cwd"].endswith("server")   # alembic.ini 在这里


def test_失败重试之后整档报错_报错不带库地址():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "sqlalchemy.exc.OperationalError: lock timeout")

    with pytest.raises(RuntimeError) as exc:
        _upgrade_pg_to_heads(URL, run=fake_run, attempts=3, wait=0)
    assert len(calls) == 3
    assert "lock timeout" in str(exc.value)
    assert "pw-in-url" not in str(exc.value) and URL not in str(exc.value)   # 地址里可能有口令


def test_第二次成功就不再重试():
    results = iter([1, 0])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, next(results), "", "busy")

    _upgrade_pg_to_heads(URL, run=fake_run, attempts=3, wait=0)
    assert len(calls) == 2
