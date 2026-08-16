"""通用附件服务：本地磁盘存储（MEDPLAT_UPLOAD_DIR 可配，默认 server/uploads/）。

- 上传：multipart，≤10MB，类型白名单（图片/PDF），按业务域角色守卫（越权 403）
- 下载：登录鉴权后按附件元数据回源磁盘文件
- 查询：按 owner_type + owner_id 列出业务对象的全部附件
- 接入场景：检查报告附件（影像截图/PDF）、不良事件佐证材料
"""
import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import AdverseEvent, Attachment, CourseMaterial, ExamReport, ExamRequest, User
from ..visibility import assert_org_visible

router = APIRouter(prefix="/api/attachments", tags=["附件"], dependencies=[Depends(get_current_user)])


def _authorize_owner_access(db: Session, user: User, owner_type: str, owner_id: int) -> None:
    """按附件所属业务对象校验读权限，用于列表/下载。

    原先下载/列表仅"登录即可"，与平台患者可见性模型冲突：任何登录用户可枚举 id
    下载他院患者检查报告 PDF/不良事件佐证。这里把附件的可见性对齐到其宿主对象所属机构：

    - exam_report → 关联检查申请的 from_org_id（申请机构可见其报告附件）；
    - adverse_event → 事件所属机构 org_id；
    - course_material → 培训课件，非患者数据，登录即可。
    """
    if owner_type == "exam_report":
        report = db.get(ExamReport, owner_id)
        if report is not None:
            request = db.get(ExamRequest, report.request_id)
            assert_org_visible(db, user, getattr(request, "from_org_id", None))
    elif owner_type == "adverse_event":
        event = db.get(AdverseEvent, owner_id)
        assert_org_visible(db, user, getattr(event, "org_id", None))
    # course_material 等非患者/非机构域：仅登录，无需机构校验

MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
# 业务域注册表：owner 模型 + 允许上传的角色（admin 始终放行；下载仅需登录）
_OWNERS: dict[str, tuple[type, tuple[str, ...]]] = {
    "exam_report": (ExamReport, ("doctor", "operator")),
    "adverse_event": (
        AdverseEvent,
        ("doctor", "pharmacist", "public_health", "operator", "director"),
    ),
    # 块4⑳：课件资源附件（课件/视频/文档）
    "course_material": (CourseMaterial, ("director", "public_health", "operator", "doctor")),
}


def register_owner(owner_type: str, model: type, roles: tuple[str, ...]) -> None:
    """子系统在装载时登记自己的附件业务域。

    做成注册制而不是在这里 import 子系统模型：附件服务是平台能力，
    平台反过来 import 子系统会把依赖方向做反（tests/test_spd_boundary.py 会拦）。
    重复注册以后到者为准——装卸开关反复开关时不该越积越多。
    """
    _OWNERS[owner_type] = (model, roles)


def store_upload(
    db: Session,
    *,
    data: bytes,
    filename: str,
    content_type: str,
    owner_type: str,
    owner_id: int,
    uploaded_by: int | None,
) -> Attachment:
    """校验（大小/类型/非空）并持久化一份附件，返回元数据行。**不 commit**。

    `uploaded_by=None` 表示居民端上传（居民不在 users 表内）。

    抽出来给居民端上传通道复用：居民令牌进不了本路由（router 级 get_current_user
    只认业务令牌），但存储与校验必须是同一份——两套白名单迟早一宽一严。
    """
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415, detail="附件类型不在白名单（仅支持图片 png/jpeg/gif/webp 与 PDF）"
        )
    if len(data) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="附件超过 10MB 大小限制")
    if not data:
        raise HTTPException(status_code=422, detail="附件内容为空")
    sha256 = hashlib.sha256(data).hexdigest()
    path = _stored_path(sha256)
    if not path.exists():
        path.write_bytes(data)
    attachment = Attachment(
        filename=filename or "unnamed",
        content_type=normalized,
        size=len(data),
        sha256=sha256,
        owner_type=owner_type,
        owner_id=owner_id,
        uploaded_by=uploaded_by,
    )
    db.add(attachment)
    return attachment


def _upload_root() -> Path:
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _stored_path(sha256: str) -> Path:
    """按 sha256 前2位分桶存储，同内容文件去重。"""
    bucket = _upload_root() / sha256[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    return bucket / sha256


def _out(a: Attachment) -> dict:
    return {
        "id": a.id,
        "filename": a.filename,
        "content_type": a.content_type,
        "size": a.size,
        "sha256": a.sha256,
        "owner_type": a.owner_type,
        "owner_id": a.owner_id,
        "uploaded_by": a.uploaded_by,
        "created_at": a.created_at.isoformat(),
    }


@router.post("", status_code=201)
async def upload_attachment(
    file: UploadFile = File(...),
    owner_type: str = Form(...),
    owner_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if owner_type not in _OWNERS:
        raise HTTPException(status_code=422, detail=f"未知附件业务域：{owner_type}")
    owner_model, allowed_roles = _OWNERS[owner_type]
    if user.role != "admin" and user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="当前角色不可为该业务对象上传附件")
    if db.get(owner_model, owner_id) is None:
        raise HTTPException(status_code=404, detail="挂接的业务对象不存在")
    data = await file.read(MAX_SIZE_BYTES + 1)
    attachment = store_upload(
        db,
        data=data,
        filename=file.filename or "unnamed",
        content_type=file.content_type or "",
        owner_type=owner_type,
        owner_id=owner_id,
        uploaded_by=user.id,
    )
    db.commit()
    return _out(attachment)


@router.get("")
def list_attachments(
    owner_type: str,
    owner_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if owner_type not in _OWNERS:
        raise HTTPException(status_code=422, detail=f"未知附件业务域：{owner_type}")
    _authorize_owner_access(db, user, owner_type, owner_id)
    return [
        _out(a)
        for a in db.query(Attachment)
        .filter(Attachment.owner_type == owner_type, Attachment.owner_id == owner_id)
        .order_by(Attachment.id)
        .all()
    ]


@router.get("/{attachment_id}")
def download_attachment(
    attachment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    _authorize_owner_access(db, user, attachment.owner_type, attachment.owner_id)
    path = _stored_path(attachment.sha256)
    if not path.exists():
        raise HTTPException(status_code=404, detail="附件文件缺失（存储目录可能被清理）")
    return FileResponse(
        path, media_type=attachment.content_type, filename=attachment.filename
    )
