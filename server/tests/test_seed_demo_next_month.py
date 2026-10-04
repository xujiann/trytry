"""演示种子跨月重启不再崩在会计那段（P2-1440，第四十二批扫描 AF3-8）。

`scripts/seed_demo.py` 由 `start.sh` 每次启动都跑一遍（失败被 `|| true` 吞掉）。会计那段原先按「本月有没有凭证」判要不要灌，
凭证号又写死成 JZ-2026-001 / 002，而同一机构的凭证号永久唯一（`models/finance.py`）：进了下个月本月一张凭证都没有，再交同号
的 JZ-2026-001 撞 409，接着拿 409 的响应体取 `id`——扫描实测第二遍抛 `KeyError: 'id'`（种子脚本会计段），最后一个请求是
`POST /api/accounting/vouchers 409`，其后成本、物资、慢专病各段与末端自检从此都不跑，会计、成本两页的本月都是空的，也没人知道。
P2-1095 修过「拿 409 的响应体取 id」，但只修了号源那一处，回归用例也只测同一天跑两遍。

修法照 P2-1095 号源那一处：按凭证号（县医院名下）判是否已有，已有就跳过（只增不改），新建的不是 201 就不去过账。

这里照 `test_seed_demo_rerun.py` 的进程内跑法跑两遍：头一遍按真实日期；第二遍把种子看到的「今天」拨到下个月——种子
`from datetime import date` 在 `run_path` 时才取，换掉 `datetime.date` 即可，应用里早已 import 好的 `date` 不受影响。

末端自检里「抗菌药物强度已可算」按种子看到的「本月」统计，而种子的抗菌药处方只在头一遍那个月里开过（按患者、机构、药品
编码判重，不随月份再开）——跨月的第二遍它必不过。这一条修前被会计段的崩溃挡着、从来跑不到，不在本条修（另行回报）；
下面只容这一条不过，其余自检项照旧必须过，修好了从 `MONTH_BOUND_CHECKS` 里划掉。
"""
import ast
import datetime as dt
import re
import runpy
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import DepartmentCost, Organization, Voucher
from app.routers import portal

SEED_DEMO = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo.py"
#: 按种子看到的本月统计、种子又不随月份再灌的自检项：跨月第二遍必不过（另行回报，只减不增）
MONTH_BOUND_CHECKS = {"抗菌药物强度已可算"}


_REAL_DATE = dt.date


def _next_month_day() -> tuple[int, int]:
    today = _REAL_DATE.today()
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


class _NextMonth(dt.date):
    """`today()` 回下个月 2 日；别的用法都照真实的 date。"""

    @classmethod
    def today(cls):
        return cls(*_next_month_day(), 2)


def _county_vouchers() -> list[tuple[str, str, str]]:
    with SessionLocal() as db:
        county = db.query(Organization.id).filter(Organization.name == "县人民医院").scalar()
        return sorted((v.voucher_no, v.period, v.status)
                      for v in db.query(Voucher).filter(Voucher.org_id == county).all())


def test_演示种子跨月重跑_会计段不再崩_其后各段与末端自检照跑(monkeypatch, capsys):
    reset_database()
    with TestClient(app):   # 只借 lifespan 种一遍启动数据（admin 账号、各类目录）就退出
        pass
    responses: list[tuple[str, str, int]] = []

    def _client(*args, **kwargs):
        # 与 start.sh 真起服务时一样，脚本只看得到状态码——服务端异常回 500，不抛进脚本；每个响应都记下来
        client = TestClient(app, raise_server_exceptions=False)
        client.event_hooks["response"].append(
            lambda r: responses.append((r.request.method, r.request.url.path, r.status_code)))
        return client

    monkeypatch.setattr(httpx, "Client", _client)
    monkeypatch.setattr(sys, "argv", ["seed_demo.py", "http://testserver"])
    monkeypatch.setattr(portal, "SEND_COOLDOWN_SECONDS", 0)

    runpy.run_path(str(SEED_DEMO), run_name="__main__")
    assert [r for r in responses if r[2] >= 400] == []
    assert "末端自检通过" in capsys.readouterr().out
    vouchers = _county_vouchers()
    this_month = _REAL_DATE.today().strftime("%Y-%m")
    assert vouchers == [("JZ-2026-001", this_month, "posted"), ("JZ-2026-002", this_month, "draft")]

    responses.clear()
    failed: list[str] = []
    with monkeypatch.context() as patch:
        patch.setattr(dt, "date", _NextMonth)
        try:
            runpy.run_path(str(SEED_DEMO), run_name="__main__")   # 修前：会计段 KeyError: 'id'，其后各段与末端自检都不跑
        except SystemExit as exc:   # 末端自检没过：说清是哪几项（别的异常照旧抛出来，用例即红）
            found = re.match(r"演示数据自检未通过：(\[.*?\])（", str(exc))
            assert found, exc
            failed = ast.literal_eval(found.group(1))
    assert set(failed) <= MONTH_BOUND_CHECKS, failed   # 只容按本月统计的那一项不过
    out = capsys.readouterr().out
    assert failed or "末端自检通过" in out              # 末端自检跑到了：要么全过，要么只差上面那一项

    # 会计段：两张凭证都已在，一张不再交、原样不动（只增不改）
    assert not [r for r in responses if r[:2] == ("POST", "/api/accounting/vouchers")]
    assert _county_vouchers() == vouchers
    # 其后各段照跑：下个月的科室直接成本照头一遍那样归集上了，慢专病段与末端自检的最后一项都发出去了
    next_month = "%d-%02d" % _next_month_day()
    with SessionLocal() as db:
        def per_month(period: str) -> int:
            return db.query(DepartmentCost).filter(DepartmentCost.period == period).count()

        assert per_month(next_month) == per_month(this_month) > 0
    assert ("GET", "/api/spd/referrals-stats/closure", 200) in responses      # 慢专病段末尾那行输出
    assert ("GET", "/api/spd/report-instances", 200) in responses            # 末端自检的最后一项（只在自检里取）
