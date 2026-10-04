"""应用启动时调度循环的首拍什么也不跑（P2-1282）。

`main.py` 的 lifespan 注释原先写「首个 tick 在 30 秒后，单测早已结束，不会产生干扰」——不对：`scheduler_loop` 先 tick
再睡 `TICK_SECONDS`，首拍随应用启动**立即**执行，每个 TestClient 起来都会跑一拍。不产生干扰靠的是另一件事：
`sync_registry` 给新登记的任务把 `next_run_at` 排在一个周期之后，空库起来那一拍没有到期任务。注释改成照实写，这里把
两头钉住——以后有人把新任务的 `next_run_at` 留空（或排到过去），每次启动、每个用例模块起 TestClient 都会当场跑它。
"""
import inspect

from app import scheduler
from app.database import SessionLocal
from app.models import ScheduledJob


def test_调度循环先跑一拍再睡():
    source = inspect.getsource(scheduler.scheduler_loop)
    assert source.index("to_thread(tick)") < source.index("sleep(TICK_SECONDS)"), source


def test_刚起来的库里没有到期任务_首拍什么也不跑(client):   # client：空库按 lifespan 起一次（含种子化与首拍）
    with SessionLocal() as db:
        jobs = db.query(ScheduledJob).all()
        assert jobs, "种子化应当登记了定时任务"
        assert [j.name for j in jobs if j.next_run_at is None] == []
        assert scheduler.due_jobs(db) == []
        assert [j.name for j in jobs if j.last_run_at is not None] == []   # 首拍确实什么也没跑
