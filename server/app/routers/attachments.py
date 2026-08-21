"""通用附件服务：字节存取走 `app.storage` 抽象（默认本地磁盘，MEDPLAT_UPLOAD_DIR 可配）。

- 上传：multipart，≤10MB，类型白名单（图片/PDF），角色守卫 + 挂接对象归属校验
- 下载：登录鉴权 + **按业务域可见性校验**后回源磁盘文件，患者类附件留痕
- 查询：按 owner_type + owner_id 列出业务对象的全部附件（同一套可见性校验）
- 接入场景：检查报告附件（影像截图/PDF）、不良事件佐证材料、课件资源、慢专病任务佐证
- 存储（A5 整改）：本路由不再直接摸磁盘，统一经 `app.storage.get_storage()`——
  多实例部署本地后端须挂共享卷、对象存储后端如何注册，见 `app/storage.py` docstring

**可见性（P0 整改）**：此前下载/列举只要"登录"就放行——任何账号按 id 顺序遍历，
就能把全县的检查报告附件拉下来，且不留任何痕迹，正是 CLAUDE.md §8 明令禁止的
"按 id 直取、不校验归属、无留痕"。现在每个业务域必须在注册时**显式声明**
可见性口径（见 `OwnerSpec.scope`），声明不了就注册不进来。
"""
import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import AdverseEvent, Attachment, CourseMaterial, ExamReport, ExamRequest, User
from ..storage import get_storage
from ..visibility import assert_obj_org_writable, assert_org_visible, assert_patient_visible

router = APIRouter(prefix="/api/attachments", tags=["附件"], dependencies=[Depends(get_current_user)])

MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}

#: 可见性口径。取值刻意只有三档，多了就没人说得清某个附件到底谁能看：
#:   patient —— 附件挂在患者身上，走 `assert_patient_visible`（校验 + 写 AccessLog）
#:   org     —— 附件挂在机构身上，走机构可见性；读用 visible、写用 writable
#:   all     —— 与患者/机构都无关的公共资料（如课件），登录即可
SCOPES = ("patient", "org", "all")


@dataclass(frozen=True)
class OwnerSpec:
    """一个附件业务域的注册信息。

    `scope` **没有默认值**，是刻意的：漏声明就在装载期 TypeError 炸掉，
    而不是安静地退化成"谁都能下载"。安全边界上，吵闹的失败远好过沉默的放行。
    """

    owner_type: str
    model: type
    roles: tuple[str, ...]                      # 允许上传的角色（admin 始终放行）
    scope: str
    #: scope="patient" 时把 owner 对象解析成 patient_id；有的模型不直接挂患者
    #: （如 ExamReport 要经 ExamRequest 才找得到患者），所以给的是解析函数而非列名。
    patient_of: Callable[[Session, Any], int | None] | None = None
    org_attr: str = "org_id"                    # scope="org" 时的机构列名

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"未知可见性口径 {self.scope!r}，只能是 {SCOPES}")
        if self.scope == "patient" and self.patient_of is None:
            raise ValueError("scope='patient' 必须给出 patient_of 解析函数")
        if self.scope == "org" and not hasattr(self.model, self.org_attr):
            # 列名写错时 getattr 取到 None，而 assert_org_visible 对 None 直接放行——
            # 机构档就此**静默失效**。这与本模块"吵闹地失败"的取向正相反，注册时拦下。
            raise ValueError(
                f"{self.model.__name__} 没有列 {self.org_attr!r}，"
                f"scope='org' 的机构校验会静默失效"
            )
        if self.scope == "patient":
            too_long = [a for a in ACTIONS if len(_resource(self.owner_type, a)) > RESOURCE_MAX]
            if too_long:
                raise ValueError(
                    f"owner_type {self.owner_type!r} 过长：resource 串超过 "
                    f"AccessLog.resource 的 {RESOURCE_MAX} 字符，留痕会被静默丢弃"
                )


#: `AccessLog.resource` 的列宽。超长在 PostgreSQL 上会写不进去，而留痕失败是被
#: **吞掉**的（见 `visibility._write_access_log`：记不下来不能挡住看病）——
#: 于是超长的后果不是报错，而是"看了但没留痕"，比看不了更糟。
#: 所以 resource 串统一由 `_resource()` 构造，并在注册业务域时就把长度校验掉。
RESOURCE_MAX = 32
ACTIONS = ("download", "upload", "list")


def _resource(owner_type: str, action: str) -> str:
    """留痕里的 resource 串。前缀用 `att` 而非 `attachment`，为长 owner_type 留出余量。"""
    return f"att:{owner_type}:{action}"


def _exam_report_patient(db: Session, report: ExamReport) -> int | None:
    """检查报告本身不挂患者，患者在申请单上。"""
    request = db.get(ExamRequest, report.request_id)
    return request.patient_id if request else None


_OWNERS: dict[str, OwnerSpec] = {
    "exam_report": OwnerSpec(
        "exam_report", ExamReport, ("doctor", "operator"), "patient",
        patient_of=_exam_report_patient,
    ),
    "adverse_event": OwnerSpec(
        "adverse_event",
        AdverseEvent,
        ("doctor", "pharmacist", "public_health", "operator", "director"),
        "org",
    ),
    # 块4⑳：课件资源附件（课件/视频/文档）。课件是面向全员的培训资料，
    # 既不含患者数据也不属于某一家机构，故 scope="all"：登录即可下载。
    "course_material": OwnerSpec(
        "course_material", CourseMaterial,
        ("director", "public_health", "operator", "doctor"), "all",
    ),
}


def register_owner(
    owner_type: str,
    model: type,
    roles: tuple[str, ...],
    scope: str,
    *,
    patient_of: Callable[[Session, Any], int | None] | None = None,
    org_attr: str = "org_id",
) -> None:
    """子系统在装载时登记自己的附件业务域。

    做成注册制而不是在这里 import 子系统模型：附件服务是平台能力，
    平台反过来 import 子系统会把依赖方向做反（tests/test_spd_boundary.py 会拦）。
    重复注册以后到者为准——装卸开关反复开关时不该越积越多。

    `scope` 是**必填**的（见 `OwnerSpec` 的说明）：新接一个业务域时必须先回答
    "这类附件谁能看"，答不上来就接不进来。
    """
    _OWNERS[owner_type] = OwnerSpec(
        owner_type, model, roles, scope, patient_of=patient_of, org_attr=org_attr
    )


def _spec(owner_type: str) -> OwnerSpec:
    spec = _OWNERS.get(owner_type)
    if spec is None:
        raise HTTPException(status_code=422, detail=f"未知附件业务域：{owner_type}")
    return spec


def assert_owner_visible(
    db: Session,
    user: User,
    spec: OwnerSpec,
    owner_id: int,
    *,
    resource: str,
    write: bool = False,
    missing_ok: bool = False,
) -> None:
    """按业务域口径校验"这个人能不能碰这个 owner 对象"；患者类顺带留痕。

    `owner is None` 默认按 404 处理（调用方本来就要判存在性，合成一处少一次漏判）。
    `missing_ok=True` 时改为静默放行：列举接口对不存在的 owner 一直返回空列表，
    这是既有契约，不该借安全整改顺手改掉——反正没有对象也就没有数据可泄。

    `write` **只影响机构档**（读 visible / 写 writable，见 `visibility` 里那两个
    函数各自的说明）。患者档没有读写之分：`patient_basis` 判的是"本机构与这位患者
    有没有关系"，有关系的机构既能看也能写，所以患者档下 `write` 不起作用——
    这是口径本身如此，不是漏了一个分支。
    """
    owner = db.get(spec.model, owner_id)
    if owner is None:
        if missing_ok:
            return
        raise HTTPException(status_code=404, detail="挂接的业务对象不存在")
    if spec.scope == "all":
        return
    if spec.scope == "org":
        if write:
            assert_obj_org_writable(db, user, owner, spec.org_attr)
        else:
            assert_org_visible(db, user, getattr(owner, spec.org_attr, None))
        return
    # scope == "patient"：assert_patient_visible 自带 AccessLog 留痕
    patient_id = spec.patient_of(db, owner) if spec.patient_of else None
    if patient_id is None:
        # 挂接对象找不到患者（如申请单被删）——宁可拒，也不要退化成无校验
        raise HTTPException(status_code=404, detail="挂接的业务对象已失去患者关联")
    assert_patient_visible(db, user, patient_id, resource=resource)


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
    get_storage().save(sha256, data)  # 内容寻址、幂等：同 sha256 已存在则跳过
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
    spec = _spec(owner_type)
    if user.role != "admin" and user.role not in spec.roles:
        raise HTTPException(status_code=403, detail="当前角色不可为该业务对象上传附件")
    # 角色对了不等于这份对象归你：没有这一步，乙院的医师能往甲院患者的报告上挂材料
    assert_owner_visible(
        db, user, spec, owner_id, resource=_resource(owner_type, "upload"), write=True
    )
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
    """列举某业务对象的附件。与下载同一套可见性校验——文件名里常带患者姓名，
    只挡下载不挡列举等于把目录留在门外。"""
    spec = _spec(owner_type)
    assert_owner_visible(
        db, user, spec, owner_id, resource=_resource(owner_type, "list"), missing_ok=True
    )
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
    """下载附件。**先按挂接对象的业务域口径校验可见性**（患者类同时留痕）。

    修的洞：此前只要登录就能按 id 顺序把全县的检查报告附件拉下来，且不留痕迹。
    """
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    spec = _spec(attachment.owner_type)
    assert_owner_visible(
        db, user, spec, attachment.owner_id,
        resource=_resource(attachment.owner_type, "download"),
    )
    storage = get_storage()
    if not storage.exists(attachment.sha256):
        raise HTTPException(status_code=404, detail="附件文件缺失（存储目录可能被清理）")
    path = storage.local_path(attachment.sha256)
    if path is not None:
        # 本地后端：FileResponse 零拷贝回源，与抽象前的响应逐字节一致
        return FileResponse(
            path, media_type=attachment.content_type, filename=attachment.filename
        )
    # 非本地后端（对象存储等）：流式回源。header 口径与 FileResponse 一致。
    return StreamingResponse(
        storage.open(attachment.sha256),
        media_type=attachment.content_type,
        headers={
            "content-disposition": f'attachment; filename="{attachment.filename}"',
        },
    )
