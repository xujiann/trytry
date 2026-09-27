"""定时任务管理（T1.1）：任务清单、调度参数调整、手动触发、执行历史。

任务实现是代码资产，这里只能改调度参数（间隔/启停）与手动触发，
不能凭空造一个库里有、代码里没有的任务。

T6.7 整改：整个模块收敛到管理层。任务摘要里带着各类超期数量（慢病随访、
医废滞留、合同临期），这属于运营管理信息，没有理由对医师、药师开放。
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import serialized_on
from ..database import get_db
from ..numtypes import INT4_MAX
from ..deps import paginate, require_admin, require_roles
from ..models import JobRun, ScheduledJob
from ..scheduler import REGISTRY, job_lock, run_job

router = APIRouter(
    prefix="/api/jobs", tags=["定时任务"], dependencies=[Depends(require_roles("director"))]
)


class JobOut(BaseModel):
    """任务清单行。`last_run_at`/`next_run_at` 是 isoformat **或空串**
    （从未跑过是 ""，不是 null）→ str。"""

    id: int
    name: str
    title: str
    interval_seconds: int
    enabled: bool
    last_run_at: str
    next_run_at: str
    last_status: str
    implemented: bool


@router.get("", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db)):
    """任务清单：库中调度参数 + 代码中是否有对应实现。"""
    rows = db.query(ScheduledJob).order_by(ScheduledJob.name).all()
    return [
        {
            "id": j.id,
            "name": j.name,
            "title": j.title,
            "interval_seconds": j.interval_seconds,
            "enabled": j.enabled,
            "last_run_at": j.last_run_at.isoformat() if j.last_run_at else "",
            "next_run_at": j.next_run_at.isoformat() if j.next_run_at else "",
            "last_status": j.last_status,
            # False = 库里有记录但代码里没有实现（如回滚了某个版本），调度器会跳过
            "implemented": j.name in REGISTRY,
        }
        for j in rows
    ]


class JobUpdate(BaseModel):
    # 列容量（P1-93 第四层：按任务名查出来再改，原先判据看不见；PG 上超 integer 即 500）
    interval_seconds: int | None = Field(default=None, ge=60, le=INT4_MAX)
    enabled: bool | None = None


class JobUpdateOut(BaseModel):
    name: str
    interval_seconds: int
    enabled: bool


@router.patch("/{name}", response_model=JobUpdateOut, dependencies=[Depends(require_admin)])
def update_job(name: str, body: JobUpdate, db: Session = Depends(get_db)):
    """调整调度参数（限管理员）。间隔下限 60 秒，防止误配成高频空转。

    改间隔同时重排下次到期（P2-467）：`next_run_at` 是上次执行时按旧间隔算好落库的，原先只改间隔不动它——日跑的任务
    改成每小时，照旧要等到明天这个点才跑下一次，而运维手册「超过间隔 3 倍未执行即告警」按新间隔算，3 小时后就误报。
    取「原定到期」与「上次执行 + 新间隔」中早的那个：改短了按新间隔提前（已过点的下一轮调度就跑），改长了不把
    已经排好的这一次往后推。

    重排是读-改-写，圈进这一行的临界区、锁到手后重读再算：调度器恰好把这个任务跑完时，按锁外读到的旧值重排，
    会把它刚推到明天的下次到期拽回过去，下一轮调度就再跑一遍。PG 上是行锁——调度器收尾的那条 UPDATE 要么已经
    提交（重读读得到）、要么排在后面等（它按自己读到的间隔落下次到期，与 P2-467 之前一样，新间隔晚一轮生效）；
    SQLite 只在开发库用，进程内锁不拦调度线程，重读到提交之间的窗口仍在。
    """
    job = db.query(ScheduledJob).filter(ScheduledJob.name == name).first()
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    with serialized_on(db, ScheduledJob, job.id):
        db.refresh(job)
        if body.interval_seconds is not None:
            job.interval_seconds = body.interval_seconds
            if job.next_run_at is not None:   # 从未排过的本就立即到期，不动
                rescheduled = (job.last_run_at or now_naive()) + timedelta(seconds=body.interval_seconds)
                job.next_run_at = min(job.next_run_at, rescheduled)
        if body.enabled is not None:
            job.enabled = body.enabled
        db.commit()
    return {"name": job.name, "interval_seconds": job.interval_seconds, "enabled": job.enabled}


class JobRunReceiptOut(BaseModel):
    """触发回执（6 键）：与 8 键历史行不同形（少 trigger/created_at），两个模型。"""

    id: int
    job_name: str
    status: str
    message: str
    affected: int
    duration_ms: int


@router.post("/{name}/run", response_model=JobRunReceiptOut, status_code=201)
def trigger_job(name: str, db: Session = Depends(get_db)):
    """手动触发一次（限管理层）：排障与补跑用，执行结果同样落 JobRun。

    **走与调度循环同一把执行锁**：此前这里直接调 `run_job`，手工补跑会和正在跑的
    同一个调度任务并发——多数任务是"扫一批然后改状态"，并发跑轻则重复发通知、
    重则把同一批单子处理两次。锁被占用时返回 409 让调用方稍后重试，
    而不是排队等待：这是个同步 HTTP 接口，长任务会把连接一直挂住。
    """
    if name not in REGISTRY:
        raise HTTPException(status_code=404, detail="任务不存在或无对应实现")
    with job_lock(name) as token:
        if token is None:
            raise HTTPException(status_code=409, detail="该任务正在执行中，请稍后重试")
        run = run_job(db, name, trigger="manual")
    return {
        "id": run.id,
        "job_name": run.job_name,
        "status": run.status,
        "message": run.message,
        "affected": run.affected,
        "duration_ms": run.duration_ms,
    }


class JobRunOut(BaseModel):
    id: int
    job_name: str
    trigger: str
    status: str
    message: str
    affected: int
    duration_ms: int
    created_at: str


@router.get("/runs", response_model=list[JobRunOut])
def list_runs(
    response: Response,
    job_name: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """执行历史（分页，总数见 X-Total-Count）。"""
    query = db.query(JobRun)
    if job_name:
        query = query.filter(JobRun.job_name == job_name)
    if status:
        query = query.filter(JobRun.status == status)
    rows = paginate(query.order_by(JobRun.id.desc()), response, offset, limit)
    return [
        {
            "id": r.id,
            "job_name": r.job_name,
            "trigger": r.trigger,
            "status": r.status,
            "message": r.message,
            "affected": r.affected,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
