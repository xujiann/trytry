"""全域慢专病 · 配置域：服务团队与村医档案。

由原 `config.py`（1549 行）按业务分节拆出，见 ADR-0008。
路由对象与跨节工具在 `._base`，本模块只放本域的端点。
"""

from secrets import token_urlsafe

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....concurrency import insert_if_absent
from ....database import get_db
from ....datetypes import OptionalDateStr
from ....deps import get_current_user, paginate, require_roles
from ...platform import Organization, User
from ...models import (
    SpdTeam,
    SpdTeamMember,
    SpdVillageDoctor,
)
from ....visibility import assert_org_writable
from ._base import CONFIG_ROLES, _qr_svg, router


# ============================================================ 服务团队


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    org_id: int
    level: str = Field(default="township", pattern="^(county|township|village|center)$")
    program_codes: list[str] = Field(default_factory=list)
    leader_user_id: int | None = None
    dept: str = Field(default="", max_length=64)
    service_area: str = Field(default="", max_length=256)
    data_scope: str = Field(default="org", pattern="^(org|group|region)$")


class MemberIn(BaseModel):
    user_id: int
    member_role: str = Field(
        default="doctor",
        pattern="^(doctor|nurse|rehab|case_manager|village_doctor|expert)$",
    )
    program_codes: list[str] = Field(default_factory=list)
    stage_scope: str = Field(default="", max_length=64)
    patient_scope: str = Field(default="team", pattern="^(self|team|org|region)$")
    can_view: bool = True
    can_followup: bool = True
    can_referral: bool = False
    can_audit: bool = False
    can_assess: bool = False


def _team_out(t: SpdTeam, members: int | None = None) -> dict:
    out = {
        "id": t.id, "name": t.name, "org_id": t.org_id, "level": t.level,
        "program_codes": t.program_codes or [], "leader_user_id": t.leader_user_id,
        "dept": t.dept, "service_area": t.service_area, "data_scope": t.data_scope,
        "active": t.active,
    }
    if members is not None:
        out["member_count"] = members
    return out


@router.post("/teams", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_team(
    body: TeamIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    assert_org_writable(db, user, body.org_id)
    team = SpdTeam(**body.model_dump())
    db.add(team)
    db.commit()
    return _team_out(team, 0)


@router.get("/teams")
def list_teams(
    response: Response,
    org_id: int | None = None,
    level: str | None = None,
    program_code: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdTeam).filter(SpdTeam.active.is_(True))
    if org_id is not None:
        query = query.filter(SpdTeam.org_id == org_id)
    if level:
        query = query.filter(SpdTeam.level == level)
    rows = paginate(query.order_by(SpdTeam.id), response, offset, limit)
    if program_code:
        rows = [t for t in rows if program_code in (t.program_codes or [])]
    counts: dict[int, int] = {}
    if rows:
        ids = [t.id for t in rows]
        for m in db.query(SpdTeamMember).filter(SpdTeamMember.team_id.in_(ids)).all():
            counts[m.team_id] = counts.get(m.team_id, 0) + 1
    return [_team_out(t, counts.get(t.id, 0)) for t in rows]


@router.get("/teams/{team_id}")
def get_team(team_id: int, db: Session = Depends(get_db)):
    team = db.get(SpdTeam, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="团队不存在")
    members = db.query(SpdTeamMember).filter(SpdTeamMember.team_id == team_id).all()
    user_names = {
        u.id: u.full_name or u.username
        for u in db.query(User).filter(User.id.in_([m.user_id for m in members] or [0])).all()
    }
    out = _team_out(team, len(members))
    out["members"] = [
        {
            "id": m.id, "user_id": m.user_id, "user_name": user_names.get(m.user_id, ""),
            "member_role": m.member_role, "program_codes": m.program_codes or [],
            "stage_scope": m.stage_scope, "patient_scope": m.patient_scope,
            "can_view": m.can_view, "can_followup": m.can_followup,
            "can_referral": m.can_referral, "can_audit": m.can_audit,
            "can_assess": m.can_assess, "active": m.active,
        }
        for m in members
    ]
    return out


@router.patch("/teams/{team_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_team(
    team_id: int, body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    team = db.get(SpdTeam, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="团队不存在")
    assert_org_writable(db, user, team.org_id)
    for key in ("name", "level", "program_codes", "leader_user_id", "dept", "service_area",
                "data_scope", "active"):
        if key in body:
            setattr(team, key, body[key])
    db.commit()
    return _team_out(team)


@router.post("/teams/{team_id}/members", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def add_team_member(
    team_id: int,
    body: MemberIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team = db.get(SpdTeam, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="团队不存在")
    assert_org_writable(db, user, team.org_id)
    if db.get(User, body.user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    member = SpdTeamMember(team_id=team_id, **body.model_dump())
    db.add(member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该成员已在团队内") from None
    return {"id": member.id, "team_id": team_id, "user_id": member.user_id,
            "member_role": member.member_role}


@router.patch("/team-members/{member_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_team_member(member_id: int, body: dict, db: Session = Depends(get_db)):
    member = db.get(SpdTeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="团队成员不存在")
    for key in ("member_role", "program_codes", "stage_scope", "patient_scope", "can_view",
                "can_followup", "can_referral", "can_audit", "can_assess", "active"):
        if key in body:
            setattr(member, key, body[key])
    db.commit()
    return {"id": member.id, "member_role": member.member_role, "active": member.active}


@router.delete("/team-members/{member_id}", status_code=204,
               dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def remove_team_member(member_id: int, db: Session = Depends(get_db)):
    member = db.get(SpdTeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="团队成员不存在")
    db.delete(member)
    db.commit()
    return Response(status_code=204)


# ============================================================ 村医档案


class VillageDoctorIn(BaseModel):
    user_id: int
    org_id: int
    township: str = Field(default="", max_length=64)
    village: str = Field(default="", max_length=64)
    license_no: str = Field(default="", max_length=64)
    license_valid_to: OptionalDateStr = ""
    phone: str = Field(default="", max_length=20)


class VillageDoctorBatchIn(BaseModel):
    items: list[VillageDoctorIn] = Field(min_length=1, max_length=500)


def _vd_out(v: SpdVillageDoctor, name: str = "") -> dict:
    return {
        "id": v.id, "user_id": v.user_id, "user_name": name, "org_id": v.org_id,
        "township": v.township, "village": v.village, "license_no": v.license_no,
        "license_valid_to": v.license_valid_to, "phone": v.phone,
        "bind_token": v.bind_token, "active": v.active,
    }


@router.post("/village-doctors", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_village_doctor(
    body: VillageDoctorIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    if db.get(User, body.user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    record = SpdVillageDoctor(**body.model_dump(), bind_token=token_urlsafe(12))
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该用户已建村医档案") from None
    return _vd_out(record)


@router.post("/village-doctors/batch", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def batch_village_doctors(
    body: VillageDoctorBatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量开通村医账号档案。

    逐条 savepoint 而不是整批一次提交：一条重复就整批回滚，导入方只知道"失败了"，
    还得自己二分查是哪一行。这里返回逐行结果，重复的跳过、其余照常建。
    """
    created, skipped = [], []
    for item in body.items:
        assert_org_writable(db, user, item.org_id)
        if db.get(User, item.user_id) is None:
            skipped.append({"user_id": item.user_id, "reason": "用户不存在"})
            continue
        exists = (
            db.query(SpdVillageDoctor.id)
            .filter(SpdVillageDoctor.user_id == item.user_id)
            .first()
        )
        if exists is not None:
            skipped.append({"user_id": item.user_id, "reason": "已建档"})
            continue
        if insert_if_absent(
            db, SpdVillageDoctor(**item.model_dump(), bind_token=token_urlsafe(12))
        ):
            created.append(item.user_id)
        else:
            skipped.append({"user_id": item.user_id, "reason": "并发写入冲突，已跳过"})
    db.commit()
    return {"created": len(created), "skipped": skipped}


@router.get("/village-doctors")
def list_village_doctors(
    response: Response,
    org_id: int | None = None,
    township: str | None = None,
    village: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdVillageDoctor)
    if org_id is not None:
        query = query.filter(SpdVillageDoctor.org_id == org_id)
    if township:
        query = query.filter(SpdVillageDoctor.township == township)
    if village:
        query = query.filter(SpdVillageDoctor.village == village)
    rows = paginate(query.order_by(SpdVillageDoctor.id), response, offset, limit)
    names = {
        u.id: u.full_name or u.username
        for u in db.query(User).filter(User.id.in_([v.user_id for v in rows] or [0])).all()
    }
    return [_vd_out(v, names.get(v.user_id, "")) for v in rows]


@router.patch("/village-doctors/{vd_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_village_doctor(
    vd_id: int, body: dict, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = db.get(SpdVillageDoctor, vd_id)
    if record is None:
        raise HTTPException(status_code=404, detail="村医档案不存在")
    assert_org_writable(db, user, record.org_id)
    for key in ("township", "village", "license_no", "license_valid_to", "phone", "active"):
        if key in body:
            setattr(record, key, body[key])
    db.commit()
    return _vd_out(record)


@router.get("/village-doctors/{vd_id}/qr.svg")
def village_doctor_qr(vd_id: int, request: Request, db: Session = Depends(get_db)):
    """村医绑定二维码：扫码进入医生移动端并带上绑定令牌（村医赋能端 #1）。

    停用的村医不出码——码是入口，入口先于账号被回收。
    """
    record = db.get(SpdVillageDoctor, vd_id)
    if record is None:
        raise HTTPException(status_code=404, detail="村医档案不存在")
    if not record.active or not record.bind_token:
        raise HTTPException(status_code=409, detail="村医已停用或没有绑定令牌")
    url = f"{str(request.base_url).rstrip('/')}/m/doctor.html#bind={record.bind_token}"
    return Response(content=_qr_svg(url), media_type="image/svg+xml")
