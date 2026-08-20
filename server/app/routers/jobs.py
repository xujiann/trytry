"""定时任务管理（T1.1）：任务清单、调度参数调整、手动触发、执行历史。

任务实现是代码资产，这里只能改调度参数（间隔/启停）与手动触发，
不能凭空造一个库里有、代码里没有的任务。

T6.7 整改：整个模块收敛到管理层。任务摘要里带着各类超期数量（慢病随访、
医废滞留、合同临期），这属于运营管理信息，没有理由对医师、药师开放。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import paginate, require_admin, require_roles
from ..models import JobRun, ScheduledJob
from ..scheduler import REGISTRY, job_lock, run_job

router = APIRouter(
    prefix="/api/jobs", tags=["定时任务"], dependencies=[Depends(require_roles("director"))]
)


@router.get("")
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
    interval_seconds: int | None = Field(default=None, ge=60)
    enabled: bool | None = None


@router.patch("/{name}", dependencies=[Depends(require_admin)])
def update_job(name: str, body: JobUpdate, db: Session = Depends(get_db)):
    """调整调度参数（限管理员）。间隔下限 60 秒，防止误配成高频空转。"""
    job = db.query(ScheduledJob).filter(ScheduledJob.name == name).first()
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if body.interval_seconds is not None:
        job.interval_seconds = body.interval_seconds
    if body.enabled is not None:
        job.enabled = body.enabled
    db.commit()
    return {"name": job.name, "interval_seconds": job.interval_seconds, "enabled": job.enabled}


@router.post("/{name}/run", status_code=201)
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


@router.get("/runs")
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
