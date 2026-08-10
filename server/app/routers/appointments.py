"""预约诊疗：机构发布分时段号源，居民一站式预约（挂号/检查/检验）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Appointment, AppointmentSlot, Organization, Patient
from ..schemas import AppointmentCreate, AppointmentOut, SlotCreate, SlotOut

router = APIRouter(prefix="/api/appointments", tags=["预约诊疗"], dependencies=[Depends(get_current_user)])


@router.post("/slots", response_model=SlotOut, status_code=201, dependencies=[Depends(require_admin)])
def create_slot(body: SlotCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    slot = AppointmentSlot(**body.model_dump())
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


@router.get("/slots", response_model=list[SlotOut])
def list_slots(org_id: int | None = None, slot_date: str | None = None, db: Session = Depends(get_db)):
    query = db.query(AppointmentSlot)
    if org_id is not None:
        query = query.filter(AppointmentSlot.org_id == org_id)
    if slot_date:
        query = query.filter(AppointmentSlot.slot_date == slot_date)
    return query.order_by(AppointmentSlot.slot_date, AppointmentSlot.slot_time).limit(500).all()


@router.post("", response_model=AppointmentOut, status_code=201)
def book(body: AppointmentCreate, db: Session = Depends(get_db)):
    slot = db.get(AppointmentSlot, body.slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="号源不存在")
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if slot.booked >= slot.capacity:
        raise HTTPException(status_code=409, detail="号源已约满")
    existing = (
        db.query(Appointment)
        .filter(Appointment.slot_id == body.slot_id, Appointment.patient_id == body.patient_id)
        .first()
    )
    if existing and existing.status == "booked":
        raise HTTPException(status_code=409, detail="请勿重复预约")
    if existing:
        existing.status = "booked"
        slot.booked += 1
        db.commit()
        db.refresh(existing)
        return existing
    appointment = Appointment(**body.model_dump())
    slot.booked += 1
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    return appointment


@router.get("", response_model=list[AppointmentOut])
def list_appointments(patient_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(Appointment)
    if patient_id is not None:
        query = query.filter(Appointment.patient_id == patient_id)
    return query.order_by(Appointment.id.desc()).limit(500).all()


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel(appointment_id: int, db: Session = Depends(get_db)):
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="预约不存在")
    if appointment.status != "booked":
        raise HTTPException(status_code=409, detail=f"当前状态 {appointment.status} 不可取消")
    appointment.status = "cancelled"
    slot = db.get(AppointmentSlot, appointment.slot_id)
    if slot and slot.booked > 0:
        slot.booked -= 1
    db.commit()
    db.refresh(appointment)
    return appointment


@router.post("/{appointment_id}/fulfill", response_model=AppointmentOut)
def fulfill(appointment_id: int, db: Session = Depends(get_db)):
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="预约不存在")
    if appointment.status != "booked":
        raise HTTPException(status_code=409, detail=f"当前状态 {appointment.status} 不可核销")
    appointment.status = "fulfilled"
    db.commit()
    db.refresh(appointment)
    return appointment
