"""全域慢专病 · 配置域：设备与数据源接入监控。

由原 `config.py`（1549 行）按业务分节拆出，见 ADR-0008。
路由对象与跨节工具在 `._base`，本模块只放本域的端点。
"""

from datetime import timedelta

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....clock import now_naive
from ....database import get_db
from ....patchtypes import UNSET
from ....deps import get_current_user, paginate, require_admin, require_roles
from ...platform import Organization, Patient, User
from ...models import (
    SpdDataSource,
    SpdDevice,
    SpdSyncLog,
)
from ....numtypes import INT4_MAX
from ....texttypes import NON_BLANK
from ....visibility import assert_org_writable
from ._base import router


# ============================================================ 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class DeviceOut(BaseModel):
    id: int
    sn: str
    device_type: str
    model: str
    # 未指定归属机构 / 未绑定患者时为 null
    org_id: int | None
    bound_patient_id: int | None
    status: str
    # 从未同步过时是空串（handler 已把 None 折成 ""），不是 null
    last_sync_at: str


class DataSourceOut(BaseModel):
    id: int
    code: str
    name: str
    source_type: str
    org_id: int | None
    endpoint: str
    freq_minutes: int
    scope: str
    active: bool
    status: str
    last_sync_at: str
    last_rows: int
    last_latency_ms: int
    # Float 列 + round(..., 2)：100 分也是 100.0，声明 float 即原样
    success_rate: float
    #: 状态文案（§13 取自后端，P2-174）：停用的一律「停用」，不看最近一次同步留下的状态
    status_name: str


class SyncRecordedOut(BaseModel):
    """登记同步结果后连带回最新的数据源快照——前端不必再拉一次列表。"""

    id: int
    source: DataSourceOut


class SyncLogOut(BaseModel):
    id: int
    started_at: str
    rows: int
    latency_ms: int
    success: bool
    message: str


class DataSourceMonitorOut(BaseModel):
    """接入总览。`by_status` 的键是数据源状态（running/delayed/failed…），
    只出现在**实际存在**的状态上，故是 dict 而非固定字段——没有 failed 的时候
    不该硬塞一个 `"failed": 0`。"""

    total: int
    by_status: dict[str, int]
    stale_over_24h: list[DataSourceOut]
    avg_success_rate: float


# ============================================================ 设备


class DeviceIn(BaseModel):
    sn: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    device_type: str = Field(pattern="^(bp|glucose|band|scale|poct|ecg)$")
    model: str = Field(default="", max_length=64)
    org_id: int | None = None


def _device_out(d: SpdDevice) -> dict:
    return {
        "id": d.id, "sn": d.sn, "device_type": d.device_type, "model": d.model,
        "org_id": d.org_id, "bound_patient_id": d.bound_patient_id, "status": d.status,
        "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else "",
    }


@router.post("/devices", response_model=DeviceOut, status_code=201,
             dependencies=[Depends(require_roles("director", "operator"))])
def create_device(
    body: DeviceIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    # 机构得在（P2-169）：写权限守卫对全域角色直接放行、不查机构在不在——填错的编号撞外键，被翻成「序列号已登记」
    if body.org_id is not None and db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    device = SpdDevice(**body.model_dump())
    db.add(device)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该设备序列号已登记") from None
    return _device_out(device)


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(
    response: Response,
    device_type: str | None = None,
    status: str | None = None,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdDevice)
    if device_type:
        query = query.filter(SpdDevice.device_type == device_type)
    if status:
        query = query.filter(SpdDevice.status == status)
    if org_id is not None:
        query = query.filter(SpdDevice.org_id == org_id)
    rows = paginate(query.order_by(SpdDevice.id), response, offset, limit)
    return [_device_out(d) for d in rows]


class DeviceBindIn(BaseModel):
    patient_id: int | None = None


@router.post("/devices/{device_id}/bind", response_model=DeviceOut,
             dependencies=[Depends(require_roles("director", "doctor", "operator"))])
def bind_device(
    device_id: int,
    body: DeviceBindIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """绑定/解绑设备。`patient_id` 为空即解绑——两个动作合一个接口，
    因为它们改的是同一列，分开会出现"解绑接口忘了改 status"这类不同步。"""
    device = db.get(SpdDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    assert_org_writable(db, user, device.org_id)
    # 患者编号原先一眼不看：填错了开发库存成悬空 id、生产库撞外键 500（P2-71 开外键约束后测出）
    if body.patient_id is not None and db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    device.bound_patient_id = body.patient_id
    device.status = "bound" if body.patient_id else "idle"
    db.commit()
    return _device_out(device)


# ============================================================ 数据源接入与监控


class DataSourceIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    source_type: str = Field(pattern="^(HIS|EMR|LIS|PACS|checkup|publichealth|device)$")
    org_id: int | None = None
    endpoint: str = Field(default="", max_length=256)
    freq_minutes: int = Field(default=60, ge=1, le=1440)
    scope: str = Field(default="", max_length=256)


#: `spd_data_sources.status` → 中文，措辞照抄列注释
DATA_SOURCE_STATUS_NAMES = {"running": "正常", "delayed": "延迟", "failed": "异常", "stopped": "停用"}


def _source_out(s: SpdDataSource) -> dict:
    return {
        "id": s.id, "code": s.code, "name": s.name, "source_type": s.source_type,
        "org_id": s.org_id, "endpoint": s.endpoint, "freq_minutes": s.freq_minutes,
        "scope": s.scope, "active": s.active, "status": s.status,
        "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else "",
        "last_rows": s.last_rows, "last_latency_ms": s.last_latency_ms,
        "success_rate": round(s.success_rate, 2),
        # 页面原先只看 status：停用（active=false）的数据源照旧显示最近一次同步留下的「正常」，
        # status=stopped 的又落进「其余一律异常」那一档（P2-174）
        "status_name": "停用" if not s.active else DATA_SOURCE_STATUS_NAMES.get(s.status, s.status),
    }


@router.post("/data-sources", response_model=DataSourceOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_data_source(body: DataSourceIn, db: Session = Depends(get_db)):
    # 同上（P1-90）：机构编号填错，生产库上会被报成「该数据源编码已存在」
    if body.org_id is not None and db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail=f"所属机构不存在（org_id={body.org_id}）")
    source = SpdDataSource(**body.model_dump())
    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该数据源编码已存在") from None
    return _source_out(source)


@router.get("/data-sources", response_model=list[DataSourceOut])
def list_data_sources(source_type: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdDataSource)
    if source_type:
        query = query.filter(SpdDataSource.source_type == source_type)
    return [_source_out(s) for s in query.order_by(SpdDataSource.id).limit(200).all()]


class DataSourcePatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    endpoint: str = Field(default=UNSET, max_length=256)
    freq_minutes: int = Field(default=UNSET, ge=1, le=1440)
    scope: str = Field(default=UNSET, max_length=256)
    active: bool = Field(default=UNSET)
    # running=正常, delayed=延迟, failed=异常, stopped=停用（见模型注释）
    status: str = Field(default=UNSET, pattern="^(running|delayed|failed|stopped)$")


@router.patch("/data-sources/{source_id}", response_model=DataSourceOut,
              dependencies=[Depends(require_admin)])
def update_data_source(
    source_id: int, body: DataSourcePatch, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.get(SpdDataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    assert_org_writable(db, user, source.org_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(source, key, value)
    db.commit()
    return _source_out(source)


class SyncLogIn(BaseModel):
    rows: int = Field(default=0, ge=0, le=INT4_MAX)
    latency_ms: int = Field(default=0, ge=0, le=INT4_MAX)
    success: bool = True
    message: str = Field(default="", max_length=256)


@router.post("/data-sources/{source_id}/sync-logs", response_model=SyncRecordedOut,
             status_code=201,
             dependencies=[Depends(require_roles("director", "operator"))])
def record_sync(
    source_id: int, body: SyncLogIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记一次同步结果，并刷新数据源的监控冗余列。

    成功率按最近 100 次算，而不是自建库以来的全量：一个月前的一次抖动
    不该永远压着今天的成功率，运维看的是"现在稳不稳"。
    """
    source = db.get(SpdDataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    assert_org_writable(db, user, source.org_id)
    log = SpdSyncLog(source_id=source_id, **body.model_dump())
    db.add(log)
    db.flush()
    recent = (
        db.query(SpdSyncLog.success)
        .filter(SpdSyncLog.source_id == source_id)
        .order_by(SpdSyncLog.id.desc())
        .limit(100)
        .all()
    )
    ok = sum(1 for (s,) in recent if s)
    source.success_rate = round(ok / len(recent) * 100, 2) if recent else 0.0
    source.last_sync_at = log.started_at
    source.last_rows = body.rows
    source.last_latency_ms = body.latency_ms
    if not body.success:
        source.status = "failed"
    elif body.latency_ms > source.freq_minutes * 60 * 1000:
        source.status = "delayed"
    else:
        source.status = "running"
    db.commit()
    return {"id": log.id, "source": _source_out(source)}


@router.get("/data-sources/{source_id}/sync-logs", response_model=list[SyncLogOut])
def list_sync_logs(source_id: int, response: Response, offset: int = 0, limit: int = 50,
                   db: Session = Depends(get_db)):
    query = db.query(SpdSyncLog).filter(SpdSyncLog.source_id == source_id)
    rows = paginate(query.order_by(SpdSyncLog.id.desc()), response, offset, limit)
    return [
        {"id": r.id, "started_at": r.started_at.isoformat(), "rows": r.rows,
         "latency_ms": r.latency_ms, "success": r.success, "message": r.message}
        for r in rows
    ]


@router.get("/data-sources-monitor", response_model=DataSourceMonitorOut)
def data_source_monitor(db: Session = Depends(get_db)):
    """接入总览：按状态汇总 + 24 小时内未同步的数据源清单。"""
    sources = db.query(SpdDataSource).filter(SpdDataSource.active.is_(True)).all()
    cutoff = now_naive() - timedelta(hours=24)
    stale = [
        _source_out(s) for s in sources if s.last_sync_at is None or s.last_sync_at < cutoff
    ]
    summary: dict[str, int] = {}
    for s in sources:
        summary[s.status] = summary.get(s.status, 0) + 1
    return {
        "total": len(sources),
        "by_status": summary,
        "stale_over_24h": stale,
        "avg_success_rate": round(
            sum(s.success_rate for s in sources) / len(sources), 2
        ) if sources else 0.0,
    }
