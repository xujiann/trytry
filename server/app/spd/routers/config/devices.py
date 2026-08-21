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
from ....deps import get_current_user, paginate, require_admin, require_roles
from ...platform import User
from ...models import (
    SpdDataSource,
    SpdDevice,
    SpdSyncLog,
)
from ....visibility import assert_org_writable
from ._base import router


# ============================================================ 设备


class DeviceIn(BaseModel):
    sn: str = Field(min_length=1, max_length=64)
    device_type: str = Field(pattern="^(bp|glucose|band|scale|poct|ecg)$")
    model: str = Field(default="", max_length=64)
    org_id: int | None = None


def _device_out(d: SpdDevice) -> dict:
    return {
        "id": d.id, "sn": d.sn, "device_type": d.device_type, "model": d.model,
        "org_id": d.org_id, "bound_patient_id": d.bound_patient_id, "status": d.status,
        "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else "",
    }


@router.post("/devices", status_code=201,
             dependencies=[Depends(require_roles("director", "operator"))])
def create_device(
    body: DeviceIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    device = SpdDevice(**body.model_dump())
    db.add(device)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该设备序列号已登记") from None
    return _device_out(device)


@router.get("/devices")
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


@router.post("/devices/{device_id}/bind",
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
    device.bound_patient_id = body.patient_id
    device.status = "bound" if body.patient_id else "idle"
    db.commit()
    return _device_out(device)


# ============================================================ 数据源接入与监控


class DataSourceIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    source_type: str = Field(pattern="^(HIS|EMR|LIS|PACS|checkup|publichealth|device)$")
    org_id: int | None = None
    endpoint: str = Field(default="", max_length=256)
    freq_minutes: int = Field(default=60, ge=1, le=1440)
    scope: str = Field(default="", max_length=256)


def _source_out(s: SpdDataSource) -> dict:
    return {
        "id": s.id, "code": s.code, "name": s.name, "source_type": s.source_type,
        "org_id": s.org_id, "endpoint": s.endpoint, "freq_minutes": s.freq_minutes,
        "scope": s.scope, "active": s.active, "status": s.status,
        "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else "",
        "last_rows": s.last_rows, "last_latency_ms": s.last_latency_ms,
        "success_rate": round(s.success_rate, 2),
    }


@router.post("/data-sources", status_code=201, dependencies=[Depends(require_admin)])
def create_data_source(body: DataSourceIn, db: Session = Depends(get_db)):
    source = SpdDataSource(**body.model_dump())
    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该数据源编码已存在") from None
    return _source_out(source)


@router.get("/data-sources")
def list_data_sources(source_type: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdDataSource)
    if source_type:
        query = query.filter(SpdDataSource.source_type == source_type)
    return [_source_out(s) for s in query.order_by(SpdDataSource.id).limit(200).all()]


@router.patch("/data-sources/{source_id}", dependencies=[Depends(require_admin)])
def update_data_source(
    source_id: int, body: dict, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.get(SpdDataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    assert_org_writable(db, user, source.org_id)
    for key in ("name", "endpoint", "freq_minutes", "scope", "active", "status"):
        if key in body:
            setattr(source, key, body[key])
    db.commit()
    return _source_out(source)


class SyncLogIn(BaseModel):
    rows: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    success: bool = True
    message: str = Field(default="", max_length=256)


@router.post("/data-sources/{source_id}/sync-logs", status_code=201,
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


@router.get("/data-sources/{source_id}/sync-logs")
def list_sync_logs(source_id: int, response: Response, offset: int = 0, limit: int = 50,
                   db: Session = Depends(get_db)):
    query = db.query(SpdSyncLog).filter(SpdSyncLog.source_id == source_id)
    rows = paginate(query.order_by(SpdSyncLog.id.desc()), response, offset, limit)
    return [
        {"id": r.id, "started_at": r.started_at.isoformat(), "rows": r.rows,
         "latency_ms": r.latency_ms, "success": r.success, "message": r.message}
        for r in rows
    ]


@router.get("/data-sources-monitor")
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
