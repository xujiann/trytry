"""阶段十一 权限模型：自定义角色与权限点授权。

**内置六角色的判定不动**。一次性把 248 处 `require_roles` 改成查权限点，
收益是统一，代价是 248 个改动点里只要错一个就是越权——而越权错误的表现
往往是"某个角色多了点权限"，不会有人报障。所以这里走的是叠加而非替换：
内置角色照旧走内存比较，自定义角色查权限点（判定入口见 `deps.require_roles`）。

权限点由平台启动时**从路由表自动登记**，不手工维护。手工维护的清单与真实
接口的偏差是这类系统最常见也最难查的问题：清单里有的接口不存在、存在的接口
清单里没有，两种偏差都表现为"权限配了不生效"。
"""
import inspect

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..concurrency import insert_if_absent
from ..database import get_db
from ..deps import ROLE_NAMES, get_current_user, require_admin, row_dict
from ..models import Permission, Role, RolePermission, User

router = APIRouter(
    prefix="/api/rbac", tags=["角色与权限"], dependencies=[Depends(get_current_user)]
)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------- 启动时登记


def _iter_api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _iter_api_routes(route.routes)


def _declared_roles(route) -> set[str]:
    """从依赖树里挖出该路由在代码里声明的角色（仅供展示与"复制内置角色"）。"""
    roles: set[str] = set()

    def walk(dependencies):
        for sub in dependencies:
            name = getattr(sub.call, "__name__", "")
            if name == "checker":
                try:
                    declared = inspect.getclosurevars(sub.call).nonlocals.get("roles")
                except TypeError:  # pragma: no cover
                    declared = None
                if declared:
                    roles.update(declared)
            elif name == "require_admin":
                roles.add("admin")
            walk(sub.dependencies)

    walk(route.dependant.dependencies)
    return roles


def sync_permissions(app, db: Session) -> dict:
    """把路由表里的写接口同步成权限点（幂等）。

    **只增不删**：接口下线后其权限点留着，因为已经授出去的授权记录还引用它，
    删掉会让"这个角色当年被授过什么"变成一段空白。停用与否交由展示层判断。
    """
    existing = {p.code for p in db.query(Permission.code).all()}
    added = 0
    for route in _iter_api_routes(app.routes):
        for method in route.methods & WRITE_METHODS:
            code = f"{method}:{route.path}"
            if code in existing:
                continue
            parts = route.path.strip("/").split("/")
            # 这个函数在启动时跑。多进程部署（gunicorn -w 4）下 4 个 worker
            # 同时启动、同时读到同一份 existing、同时插同一批权限点，
            # 后插的抛 IntegrityError——**worker 起不来**。用 SAVEPOINT
            # 把冲突圈在单行内：别人抢先建了就当已存在，同步照常完成。
            landed = insert_if_absent(
                db,
                Permission(
                    code=code,
                    method=method,
                    path=route.path,
                    module=parts[1] if len(parts) > 1 else "",
                    builtin_roles=",".join(sorted(_declared_roles(route))),
                ),
            )
            existing.add(code)
            added += 1 if landed else 0
    db.commit()
    return {"added": added, "total": len(existing)}


def seed_builtin_roles(db: Session) -> int:
    """内置六角色预置。已存在的不动——名称可能已被本地改过。"""
    created = 0
    for key, name in ROLE_NAMES.items():
        if db.query(Role).filter(Role.key == key).first() is None:
            # 同 sync_permissions：多 worker 同时启动会同时预置同一批角色
            if insert_if_absent(db, Role(key=key, name=name, builtin=True)):
                created += 1
    db.commit()
    return created


# ---------------------------------------------------------------- 角色


class RoleIn(BaseModel):
    key: str = Field(min_length=2, max_length=32, pattern="^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=256)


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    active: bool | None = None


def _role_out(role: Role, permission_count: int = 0) -> dict:
    return {
        "id": role.id,
        "key": role.key,
        "name": role.name,
        "description": role.description,
        "builtin": role.builtin,
        "active": role.active,
        "permission_count": permission_count,
        # 内置角色的权限来自代码声明，不来自授权表，故这里明说而不是显示 0
        "permission_source": "代码内 require_roles 声明" if role.builtin else "权限点授权",
    }


# ---------------------------------------------------------------- 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class RoleOut(BaseModel):
    id: int
    key: str
    name: str
    description: str
    builtin: bool
    active: bool
    permission_count: int
    # 内置角色的权限来自代码声明、不来自授权表——这里明说，而不是显示一个 0
    # 让人以为"这个角色没权限"
    permission_source: str


class RoleDeletedOut(BaseModel):
    deleted: bool


class PermissionRevokedOut(BaseModel):
    revoked: bool


class PermissionOut(BaseModel):
    id: int
    code: str
    method: str
    path: str
    module: str
    builtin_roles: str


class PermissionBriefOut(BaseModel):
    """角色权限清单里的权限只带五个键（没有 builtin_roles）——
    与 `/permissions` 全量清单不同形，故是两个模型。"""

    id: int
    code: str
    module: str
    path: str
    method: str


class ModuleOut(BaseModel):
    module: str
    permission_count: int


class RolePermissionsOut(BaseModel):
    """角色的权限点。`note` 是**条件键**：只有内置角色才有（它的权限不走授权表，
    要当面说清楚，否则看到空列表会以为是没配）。声明成带默认值的可选字段会给
    自定义角色也注入 `"note": null`，故端点带 `response_model_exclude_unset=True`。
    """

    role: RoleOut
    permissions: list[PermissionBriefOut]
    note: str | None = None


class GrantResultOut(BaseModel):
    role_id: int
    granted: int
    already_had: int
    # 不存在的权限点 id 单列报出，不静默忽略——静默忽略会让人以为授成功了
    unknown_permission_ids: list[int]
    total: int


@router.get("/roles", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db)):
    roles = db.query(Role).order_by(Role.builtin.desc(), Role.id).all()
    counts = row_dict(
        db.query(RolePermission.role_id, sa.func.count(RolePermission.id))
        .group_by(RolePermission.role_id)
        .all()
    )
    return [_role_out(r, counts.get(r.id, 0)) for r in roles]


@router.post("/roles", response_model=RoleOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_role(body: RoleIn, db: Session = Depends(get_db)):
    if body.key in ROLE_NAMES:
        raise HTTPException(status_code=409, detail="该 key 与内置角色冲突")
    role = Role(builtin=False, **body.model_dump())
    db.add(role)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="角色 key 已存在") from None
    db.refresh(role)
    return _role_out(role)


@router.patch("/roles/{role_id}", response_model=RoleOut,
              dependencies=[Depends(require_admin)])
def update_role(role_id: int, body: RoleUpdate, db: Session = Depends(get_db)):
    role = _role(db, role_id)
    data = body.model_dump(exclude_unset=True)
    if role.builtin and data.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail="内置角色不可停用——全平台 200 多处守卫引用它，停用等于拆平台",
        )
    for field, value in data.items():
        if value is not None:
            setattr(role, field, value)
    db.commit()
    db.refresh(role)
    return _role_out(role)


@router.delete("/roles/{role_id}", response_model=RoleDeletedOut,
               dependencies=[Depends(require_admin)])
def delete_role(role_id: int, db: Session = Depends(get_db)):
    """删除自定义角色。内置角色不可删——删掉 doctor 这一行，全平台
    `require_roles("doctor")` 会同时失效，那不是配置，是拆平台。"""
    role = _role(db, role_id)
    if role.builtin:
        raise HTTPException(status_code=422, detail="内置角色不可删除")
    in_use = db.query(User.id).filter(User.role == role.key).first()
    if in_use is not None:
        raise HTTPException(status_code=409, detail="仍有用户使用该角色，请先调整用户角色")
    db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
    db.delete(role)
    db.commit()
    return {"deleted": True}


def _role(db: Session, role_id: int) -> Role:
    role = db.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return role


# ---------------------------------------------------------------- 权限点


@router.get("/permissions", response_model=list[PermissionOut])
def list_permissions(
    module: str | None = None, keyword: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(Permission)
    if module:
        query = query.filter(Permission.module == module)
    if keyword:
        query = query.filter(Permission.path.like(f"%{keyword}%"))
    rows = query.order_by(Permission.module, Permission.path).limit(1000).all()
    return [
        {"id": p.id, "code": p.code, "method": p.method, "path": p.path,
         "module": p.module, "builtin_roles": p.builtin_roles}
        for p in rows
    ]


@router.get("/modules", response_model=list[ModuleOut])
def list_modules(db: Session = Depends(get_db)):
    rows = (
        db.query(Permission.module, sa.func.count(Permission.id))
        .group_by(Permission.module)
        .order_by(Permission.module)
        .all()
    )
    return [{"module": m, "permission_count": c} for m, c in rows]


class GrantIn(BaseModel):
    permission_ids: list[int] = Field(default_factory=list)
    # 按模块整体授权：比逐个勾 600 多个权限点现实得多
    modules: list[str] = Field(default_factory=list)
    # 以某个内置角色的默认授权为起点
    copy_from_builtin: str | None = None


@router.get("/roles/{role_id}/permissions", response_model=RolePermissionsOut,
            response_model_exclude_unset=True)
def role_permissions(role_id: int, db: Session = Depends(get_db)):
    role = _role(db, role_id)
    if role.builtin:
        return {
            "role": _role_out(role),
            "permissions": [],
            "note": "内置角色的权限来自代码内 require_roles 声明，不走授权表；"
                    "如需按权限点管理，请新建自定义角色并从内置角色复制起点",
        }
    rows = (
        db.query(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .filter(RolePermission.role_id == role_id)
        .order_by(Permission.module, Permission.path)
        .all()
    )
    return {
        "role": _role_out(role, len(rows)),
        "permissions": [
            {"id": p.id, "code": p.code, "module": p.module, "path": p.path,
             "method": p.method}
            for p in rows
        ],
    }


@router.post("/roles/{role_id}/permissions", response_model=GrantResultOut,
             dependencies=[Depends(require_admin)])
def grant_permissions(role_id: int, body: GrantIn, db: Session = Depends(get_db)):
    """授权（增量，不覆盖）。三种给法可叠加：逐个、按模块、复制内置角色。"""
    role = _role(db, role_id)
    if role.builtin:
        raise HTTPException(
            status_code=422, detail="内置角色的权限由代码声明，不接受授权表配置"
        )
    targets: set[int] = set(body.permission_ids)
    if body.modules:
        targets |= {
            pid for (pid,) in db.query(Permission.id)
            .filter(Permission.module.in_(body.modules)).all()
        }
    if body.copy_from_builtin:
        if body.copy_from_builtin not in ROLE_NAMES:
            raise HTTPException(status_code=422, detail="copy_from_builtin 须为内置角色 key")
        like = f"%{body.copy_from_builtin}%"
        targets |= {
            pid for (pid,) in db.query(Permission.id)
            .filter(Permission.builtin_roles.like(like)).all()
        }
    if not targets:
        raise HTTPException(status_code=422, detail="未指定任何权限点")

    existing = {
        pid for (pid,) in db.query(RolePermission.permission_id)
        .filter(RolePermission.role_id == role_id).all()
    }
    valid = {
        pid for (pid,) in db.query(Permission.id).filter(Permission.id.in_(targets)).all()
    }
    unknown = targets - valid
    added = 0
    for pid in sorted(valid - existing):
        # 两个管理员同时给同一个角色授权，existing 都读不到同一条，
        # 就都去插——一条撞车，整次授权回滚成 500。授没授上以落库为准。
        if insert_if_absent(db, RolePermission(role_id=role_id, permission_id=pid)):
            added += 1
    db.commit()
    return {
        "role_id": role_id,
        "granted": added,
        "already_had": len(valid & existing),
        # 不存在的权限点 id 单列报出，不静默忽略——静默忽略会让人以为授成功了
        "unknown_permission_ids": sorted(unknown),
        "total": len(existing) + added,
    }


@router.delete(
    "/roles/{role_id}/permissions/{permission_id}", response_model=PermissionRevokedOut,
    dependencies=[Depends(require_admin)]
)
def revoke_permission(role_id: int, permission_id: int, db: Session = Depends(get_db)):
    row = (
        db.query(RolePermission)
        .filter(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == permission_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="该角色未被授予此权限点")
    db.delete(row)
    db.commit()
    return {"revoked": True}
