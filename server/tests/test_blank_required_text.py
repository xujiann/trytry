"""必填文本字段收下纯空格（P1-109）。

请求模型里 `Field(min_length=1)` 的本意是「这一项必须填」，可空格也是字符。2026-09-24 开发库实测（修前代码）：
机构名填三个空格建机构 201、慢专病病种编码填一个空格 201、用户名填三个空格建账号 201、员工姓名填全角空格 201——
清单里多出一行看不见名字的记录，下拉里多出一个空白选项，按名称 / 编码查重也拦不住第二串空格。

修法：`app/texttypes.NON_BLANK`（`\\S`，至少一个非空白字符）挂到请求字段的 `pattern` 上。pydantic 的 `pattern`
按 search 语义匹配，合法值一个字节不变（前后带空格的照原样收下、照原样落库），只挡全是空白的；422 的 `detail`
仍是 FastAPI 的标准数组形状（`string_pattern_mismatch`），前端 `errorText` 认出这条 pattern 换成「不能只填空格」。

闸门（派生、零基线）：

1. **请求侧**：路由请求体（顺嵌套子模型）里带 `min_length` 的文本字段，声明的 pattern 必须挡得住同长度的纯空白
   （半角空格、全角空格、制表符各试一遍）。修前量出 282 个字段：修 268，认证与核验入参 14 个按设计（`BY_DESIGN`）。
2. **出参侧**：`response_model`（顺嵌套）的字段不许带 `NON_BLANK`——修之前存进去的纯空白行会让整个清单 500
   （P1-63 / P2-40 同族）。继承到它的 37 个出参字段（27 个出参模型）已在出参按原约束覆盖，不带它。
3. **第二层（P1-110，见文件后半）**：没写 `min_length`、却没有默认值（必须带上这一键）的文本字段，空串照收——
   传染病报告的病种、处方明细的药品、检查申请的项目、审方规则的药品编码，修前实测全部 201。
"""
from __future__ import annotations

import json
import pathlib
import re
import types
import typing

import pytest
import test_api_contract_governance as contract
from pydantic import BaseModel, Field

from app.texttypes import NON_BLANK

#: 基线：已清零，此后即零基线闸门（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 282 个字段收得下纯空白：同一批修 268，其余 14 个登记在下面的 BY_DESIGN。
BASELINE = 0
#: 出参字段带 NON_BLANK 的个数：零基线（继承到它的 37 个出参字段同一批在出参覆盖）。
OUTPUT_BASELINE = 0

#: 认证与核验入参：纯空白在处理函数里本就过不去（手机号走 `PHONE_RE`、验证码比对不上、姓名证件号核验查无此人、
#: 一码通解析不出人），改这些端点的校验层属 CLAUDE.md §8 复核范围（认证 / 越权），不在本条单方面改；
#: 与 P1-99（认证验签的数字判断）一并复核。只减不增。
_AUTH = "认证 / 核验入参（§8）：纯空白在处理函数里本就核验不过，改认证端点的校验层须人工复核，随 P1-99 一并"
BY_DESIGN = {
    "routers.portal:SendCodeIn.phone": _AUTH + "（发登录验证码，手机号另经 PHONE_RE）",
    "routers.portal:SmsLoginIn.phone": _AUTH + "（短信登录）",
    "routers.portal:SmsLoginIn.code": _AUTH + "（短信登录验证码）",
    "routers.portal:WeChatLoginIn.code": _AUTH + "（微信登录授权码）",
    "routers.portal:RealNameIn.name": _AUTH + "（实名核验，与证件号一起比对档案）",
    "routers.portal:RealNameIn.id_card": _AUTH + "（实名核验）",
    "routers.portal:BindPhoneIn.phone": _AUTH + "（补绑手机号，手机号另经 PHONE_RE）",
    "routers.portal:BindPhoneIn.code": _AUTH + "（补绑手机号验证码）",
    "routers.portal:BindWeChatIn.code": _AUTH + "（补绑微信授权码）",
    "routers.portal:FamilyMemberIn.name": _AUTH + "（家庭成员代管的身份核验）",
    "routers.portal:FamilyMemberIn.id_card": _AUTH + "（家庭成员代管的身份核验）",
    "routers.portal:ArchiveQuery.ehc_no": _AUTH + "（凭健康卡号与证件号查本人档案）",
    "routers.portal:ArchiveQuery.id_card": _AUTH + "（凭健康卡号与证件号查本人档案）",
    "routers.credentials:OneCodeResolve.code": _AUTH + "（一码通解析到人）",
}

#: 各试一遍的空白字符：半角空格、全角空格（U+3000，中文输入法最常见）、制表符
_BLANKS = (" ", "　", "\t")


# ================================================================ 判据
def _collect(tp, seen: set) -> None:
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp not in seen:
            seen.add(tp)
            for info in tp.model_fields.values():
                _collect(info.annotation, seen)
        return
    for arg in typing.get_args(tp):
        _collect(arg, seen)


def _metas(info) -> list:
    members = (info.annotation, *typing.get_args(info.annotation))
    return list(info.metadata) + [m for a in members for m in getattr(a, "__metadata__", ())]


def _patterns(info) -> list[str]:
    found = [getattr(m, "pattern", None) for m in _metas(info)]
    return [p if isinstance(p, str) else p.pattern for p in found if p is not None]


def _is_text(info) -> bool:
    ann = info.annotation
    if typing.get_origin(ann) is typing.Annotated:
        ann = typing.get_args(ann)[0]
    return ann is str or (typing.get_origin(ann) in (typing.Union, types.UnionType) and str in typing.get_args(ann))


def accepts_blank(info) -> bool:
    """带 `min_length` 的文本字段，同长度的纯空白能不能过它声明的全部 pattern（没有 pattern 即能过）。"""
    if not _is_text(info):
        return False
    min_len = max((m.min_length for m in _metas(info) if getattr(m, "min_length", None)), default=0)
    if min_len < 1:
        return False
    patterns = _patterns(info)
    return any(all(re.search(p, ch * min_len) for p in patterns) for ch in _BLANKS)


def _owner_key(cls: type, field: str) -> str:
    """字段按**声明它的类**记名：继承来的字段改在基类上，不按每个子类各报一遍。"""
    owner = next((base for base in cls.__mro__ if field in base.__dict__.get("__annotations__", {})), cls)
    return f"{owner.__module__.removeprefix('app.')}:{owner.__qualname__}.{field}"


def request_models() -> set[type]:
    seen: set = set()
    for _name, route in contract._iter_endpoints():
        stack = [route.dependant]
        while stack:
            dependant = stack.pop()
            for param in dependant.body_params:
                _collect(param.field_info.annotation, seen)
            stack.extend(dependant.dependencies)
    return seen


def response_models() -> set[type]:
    seen: set = set()
    for _name, route in contract._iter_endpoints():
        if route.response_model is not None:
            _collect(route.response_model, seen)
    return seen


def blank_accepting_fields(models: typing.Iterable[type] | None = None) -> set[str]:
    """请求模型里要求非空（`min_length>=1`）却收得下纯空白的文本字段（按声明它的类记名）。"""
    models = request_models() if models is None else models
    return {_owner_key(cls, f) for cls in models for f, info in cls.model_fields.items() if accepts_blank(info)}


def non_blank_outputs(models: typing.Iterable[type] | None = None) -> set[str]:
    """出参模型里带 `NON_BLANK` 的字段（按出参模型记名：修法是在这个出参模型上覆盖）。"""
    models = response_models() if models is None else models
    return {f"{cls.__module__.removeprefix('app.')}:{cls.__qualname__}.{f}"
            for cls in models for f, info in cls.model_fields.items() if NON_BLANK in _patterns(info)}


# ================================================================ 闸门
def test_要求非空的请求文本字段挡得住纯空白():
    new = sorted(blank_accepting_fields() - BY_DESIGN.keys())
    assert len(new) <= BASELINE, (
        "这些请求字段写了 min_length（要求必填），一串空格却照样过：\n  " + "\n  ".join(new)
        + "\n\n加上 `pattern=NON_BLANK`（`from app.texttypes import NON_BLANK`）；已有 pattern 的，让它挡得住纯空白。"
        "出参若继承了这个请求模型，出参上按原约束覆盖回不带它的声明（见 test_出参字段不带NON_BLANK）。"
    )


def test_按设计名单只减不增():
    stale = sorted(BY_DESIGN.keys() - blank_accepting_fields())
    assert stale == [], "这些已经挡得住纯空白（或已不存在），请从 BY_DESIGN 划掉：\n  " + "\n  ".join(stale)


def test_出参字段不带NON_BLANK():
    offenders = sorted(non_blank_outputs())
    assert len(offenders) <= OUTPUT_BASELINE, (
        "这些出参字段带着 NON_BLANK（多半是从请求模型继承来的）：\n  " + "\n  ".join(offenders)
        + "\n\n修之前存进去的纯空白行会让整个响应 500。在出参模型上按原约束覆盖这个字段、不带 pattern"
        "（照 `admin_mgmt.EmployeeOut.name` 的写法）。"
    )


def test_覆盖面自证_请求与出参两侧都扫得到():
    """防判据悄悄失明（路由遍历口径变了、一个字段都扫不到时，上面两条会空转成绿）。"""
    carrying = {_owner_key(cls, f) for cls in request_models() for f, info in cls.model_fields.items()
                if NON_BLANK in _patterns(info)}
    assert len(carrying) >= 268, len(carrying)
    for key in ("schemas:OrganizationCreate.name", "routers.users:UserCreate.username",
                "routers.admin_mgmt:EmployeeCreate.name", "spd.routers.config.catalog:ProgramIn.code"):
        assert key in carrying, key
    outputs = {cls.__qualname__ for cls in response_models()}
    assert {"EmployeeOut", "OrganizationOut", "SlotOut", "StockOut"} <= outputs


def test_判据自证_要求非空却收空白的点名_挡得住的不报():
    class Loose(BaseModel):
        a: str = Field(min_length=1, max_length=8)
        b: str | None = Field(default=None, min_length=1)
        c: str = Field(min_length=1, pattern=r"^.{1,8}$")          # 有 pattern，可照样收空白

    class Tight(BaseModel):
        a: str = Field(min_length=1, max_length=8, pattern=NON_BLANK)
        b: str | None = Field(default=None, min_length=1, pattern=NON_BLANK)
        c: str = Field(min_length=1, pattern="^(phone|sms)$")      # 枚举天然挡得住
        d: str = Field(default="", max_length=8)                   # 不要求非空，不在判据内
        e: int = Field(ge=1)

    here = f"{__name__.removeprefix('app.')}"
    assert blank_accepting_fields([Loose, Tight]) == {
        f"{here}:{Loose.__qualname__}.a", f"{here}:{Loose.__qualname__}.b", f"{here}:{Loose.__qualname__}.c"}

    class TightOut(Tight):
        id: int

    class CoveredOut(Tight):
        id: int
        a: str = Field(min_length=1, max_length=8)
        b: str | None = Field(default=None, min_length=1)

    assert non_blank_outputs([TightOut]) == {f"{here}:{TightOut.__qualname__}.a", f"{here}:{TightOut.__qualname__}.b"}
    assert non_blank_outputs([CoveredOut]) == set()
    # 覆盖不改字段顺序（出参字节不变的前提）
    assert list(CoveredOut.model_fields) == list(TightOut.model_fields)


def test_NON_BLANK_只挡纯空白_合法值一个字节不变():
    class M(BaseModel):
        name: str = Field(min_length=1, max_length=16, pattern=NON_BLANK)

    for ok in ("甲", " 甲乡卫生院 ", "a b", "　张三", "x\t"):
        assert M(name=ok).name == ok
    for bad in (" ", "   ", "　", "　　", "\t", " \n "):
        with pytest.raises(ValueError):
            M(name=bad)


def test_前端认得这条pattern():
    """`errorText` 按 `type` 与 `ctx` 认出「只填了空格」「必填留空」：常量改了，前端这句就对不上了。"""
    source = (pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "shared.js").read_text(encoding="utf-8")
    body = source[source.index("function errorText("):]
    body = body[:body.index("\n}\n")]
    assert "string_pattern_mismatch" in body and json.dumps(NON_BLANK) in body and "不能只填空格" in body
    # 第二层（P1-110）：必填留空是 `min_length=1` 的 string_too_short
    assert "string_too_short" in body and "min_length === 1" in body and "不能为空" in body


# ================================================================ 行为回归（修前实测见 docstring，四处全部 201）
_BLANK_ERROR = {"type": "string_pattern_mismatch", "ctx": {"pattern": NON_BLANK}}


def _blank_rejected(resp, field: str) -> None:
    assert resp.status_code == 422, resp.text
    errors = [e for e in resp.json()["detail"] if e["loc"][-1] == field]
    assert errors and all({k: e.get(k) for k in _BLANK_ERROR} == _BLANK_ERROR for e in errors), errors


def test_机构名只填空格_422(client, admin):
    for blank in ("   ", "　　"):
        resp = client.post("/api/organizations", headers=admin,
                           json={"name": blank, "org_type": "township", "level": "township"})
        _blank_rejected(resp, "name")


def test_前后带空格的合法值照原样落库(client, admin):
    resp = client.post("/api/organizations", headers=admin,
                       json={"name": " P1109 甲乡卫生院 ", "org_type": "township", "level": "township"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == " P1109 甲乡卫生院 "


def test_病种编码只填空格_422(client, admin):
    resp = client.post("/api/spd/programs", headers=admin, json={"code": " ", "name": "空白编码病种", "category": "chronic"})
    _blank_rejected(resp, "code")


def test_用户名只填空格_422(client, admin):
    resp = client.post("/api/users", headers=admin,
                       json={"username": "   ", "password": "pw123456", "role": "operator"})
    _blank_rejected(resp, "username")


def test_员工姓名只填空格_422_修前存进去的空白行照样列得出(client, admin):
    from app.database import SessionLocal
    from app.models import Employee

    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P1109 员工卫生院", "org_type": "township", "level": "township"}).json()["id"]
    _blank_rejected(client.post("/api/mgmt/employees", headers=admin, json={"org_id": org, "name": "　"}), "name")

    with SessionLocal() as db:   # 修之前经接口存进去的纯空白姓名（修前实测 201）
        db.add(Employee(org_id=org, name="　"))
        db.commit()
    listed = client.get("/api/mgmt/employees", headers=admin, params={"org_id": org})
    assert listed.status_code == 200, listed.text
    assert [e["name"] for e in listed.json()] == ["　"]


# ================================================================ 第二层（P1-110）：必填键收下空串
#
# 上面的判据只管写了 `min_length` 的字段。另一种写法连 `min_length` 都没有：字段**没有默认值**（请求里必须带上
# 这一键），值却可以是空串。2026-09-24 量出 24 个：7 个处理函数里也一眼不看——传染病报告的病种编码与病种名、
# 处方明细的药品编码与药名、检查申请的项目编码与项目名、审方规则的药品编码（修前实测全部 201，见下面的回归）；
# 其余 17 个逐条判过，登记在 EMPTY_BY_DESIGN。
#
# 判据：请求模型里的文本字段，没有默认值、没有校验器（`DateStr` 一类 Annotated 校验器、`@field_validator`），
# `min_length` 不到 1，声明的 pattern 也挡不住空串——就得补上 `min_length=1, pattern=NON_BLANK`，
# 或者写明理由登记进 EMPTY_BY_DESIGN。

#: 基线：已清零（2026-09-24 量出 7 个，同一批修完）。
EMPTY_BASELINE = 0

_HANDLER = "处理函数按取值集合校验，空串不在集合里即 422"
EMPTY_BY_DESIGN = {
    "routers.admin_mgmt:QcCreate.center_type": _HANDLER + "（`_CENTERS`）",
    "routers.admin_mgmt:RosterCreate.center_type": _HANDLER + "（`_CENTERS`）",
    "routers.exams:TemplateCreate.center_type": _HANDLER + "（`_CENTERS`）",
    "routers.publichealth:MonitorCreate.domain": _HANDLER + "（`_DOMAINS`，「未知监测领域」）",
    "routers.quality:AdverseEventCreate.event_type": _HANDLER + "（`_EVENT_TYPES`）",
    "routers.quality:InfectionReportCreate.infection_site": _HANDLER + "（`_INFECTION_SITES`）",
    "routers.rules:EvaluateIn.domain": _HANDLER + "（`DOMAIN_VARIABLES`）",
    "routers.surveys:SurveyCreate.target_type": _HANDLER + "（`_SURVEY_TARGETS`，与门户的 pattern 同一集合，见 P1-98）",
    "routers.workflows:StartIn.definition_key": "按键查启用中的流程定义，空串查不到即 404",
    "schemas:LoginRequest.username": _AUTH + "（登录，空用户名查无此人）",
    "schemas:LoginRequest.password": _AUTH + "（登录，空口令验不过）",
    "routers.auth:TotpCodeIn.code": _AUTH + "（双因素动态码，空码验不过）",
    "routers.users:ChangePassword.current_password": _AUTH + "（改密时核对原口令，空口令验不过）",
    "routers.portal:PortalSurveyCreate.ehc_no": _AUTH + "（已废弃的免登录评价，凭卡号与证件号核验身份）",
    "routers.portal:PortalSurveyCreate.id_card": _AUTH + "（已废弃的免登录评价，凭卡号与证件号核验身份）",
    "schemas:ExamReportCreate.conclusion":
        "出报告要求带上这一键、允许结论留空（只有所见的报告）——与 P1-98 `SAME_COLUMN_BY_DESIGN` 同一理由",
    "routers.admin_mgmt:ParamUpsert.value": "系统参数的值允许是空串（把一个参数清空），参数键已必填",
}


def _has_validator(cls: type, field: str, info) -> bool:
    if any(type(m).__name__ in ("BeforeValidator", "AfterValidator", "PlainValidator", "WrapValidator")
           for m in _metas(info)):
        return True
    return any(field in d.info.fields for d in cls.__pydantic_decorators__.field_validators.values())


def accepts_empty_required(cls: type, field: str, info) -> bool:
    """没有默认值（必须带上这一键）的文本字段，空串能不能过它声明的约束。"""
    if not _is_text(info) or not info.is_required() or _has_validator(cls, field, info):
        return False
    if max((m.min_length for m in _metas(info) if getattr(m, "min_length", None)), default=0) >= 1:
        return False
    return all(re.search(p, "") for p in _patterns(info))


def empty_accepting_required_fields(models: typing.Iterable[type] | None = None) -> set[str]:
    models = request_models() if models is None else models
    return {_owner_key(cls, f) for cls in models for f, info in cls.model_fields.items()
            if accepts_empty_required(cls, f, info)}


def test_第二层_必填键不收空串():
    new = sorted(empty_accepting_required_fields() - EMPTY_BY_DESIGN.keys())
    assert len(new) <= EMPTY_BASELINE, (
        "这些请求字段没有默认值（必须带上这一键），值却可以是空串，处理函数也没登记过会挡：\n  " + "\n  ".join(new)
        + "\n\n补上 `min_length=1, pattern=NON_BLANK`（出参继承的要覆盖回去）；处理函数里按集合 / 目录校验、"
        "或业务上允许空串的，写明理由登记进 EMPTY_BY_DESIGN。"
    )


def test_第二层_按设计名单只减不增():
    stale = sorted(EMPTY_BY_DESIGN.keys() - empty_accepting_required_fields())
    assert stale == [], "这些已经不收空串（或已不存在），请从 EMPTY_BY_DESIGN 划掉：\n  " + "\n  ".join(stale)
    assert not (EMPTY_BY_DESIGN.keys() & BY_DESIGN.keys()), "同一个字段只能登记在一张名单里"


def test_判据自证_第二层_必填键收空串的点名():
    from datetime import date

    from app.datetypes import DateStr
    from pydantic import field_validator

    class M(BaseModel):
        loose: str = Field(max_length=8)                            # 必须带、可为空串 → 点名
        enum: str = Field(pattern="^(a|b)$")                        # 枚举挡得住空串
        tight: str = Field(min_length=1, max_length=8, pattern=NON_BLANK)
        optional: str = Field(default="", max_length=8)             # 有默认值，空串是「没填」
        day: DateStr = date.today().isoformat()
        checked: str

        @field_validator("checked")
        @classmethod
        def _nonempty(cls, value: str) -> str:
            return value

    class N(BaseModel):
        day: DateStr                                                # Annotated 校验器自己挡空串

    here = f"{__name__}:{M.__qualname__}"
    assert empty_accepting_required_fields([M, N]) == {f"{here}.loose"}


# ---- 第二层回归（修前开发库实测：四个写接口全部 201，病种 / 药品 / 项目是空的）
def _empty_rejected(resp, field: str) -> None:
    assert resp.status_code == 422, resp.text
    assert any(e["loc"][-1] == field and e["type"] == "string_too_short" for e in resp.json()["detail"]), resp.text


@pytest.fixture(scope="module")
def p1110(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P1110 空串卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P1110 患者", "id_card": "330192198501010027"}).json()["id"]
    return {"org": org, "patient": patient}


def test_传染病报告病种为空_422_修前存进去的空行照样列得出(client, admin, p1110):
    from app.database import SessionLocal
    from app.models import InfectiousCase

    body = {"org_id": p1110["org"], "disease_code": "A15", "disease_name": "肺结核", "onset_date": "2026-09-01"}
    for field in ("disease_code", "disease_name"):
        _empty_rejected(client.post("/api/infectious/cases", headers=admin, json={**body, field: ""}), field)

    with SessionLocal() as db:   # 修之前经接口存进去的空病种报告（修前实测 201）
        db.add(InfectiousCase(org_id=p1110["org"], disease_code="", disease_name="", onset_date="2026-09-01"))
        db.commit()
    listed = client.get("/api/infectious/cases", headers=admin)
    assert listed.status_code == 200, listed.text
    assert any(c["disease_code"] == "" and c["org_id"] == p1110["org"] for c in listed.json())


def test_处方明细药品为空_422(client, admin, p1110):
    item = {"drug_code": "P1110-D", "drug_name": "空串测试药", "daily_dose": 1, "days": 1}
    for field in ("drug_code", "drug_name"):
        resp = client.post("/api/prescriptions", headers=admin, json={
            "patient_id": p1110["patient"], "org_id": p1110["org"], "items": [{**item, field: ""}]})
        _empty_rejected(resp, field)


def test_检查申请项目为空_422(client, admin, p1110):
    body = {"patient_id": p1110["patient"], "from_org_id": p1110["org"], "center_type": "imaging",
            "item_code": "CT", "item_name": "胸部CT"}
    for field in ("item_code", "item_name"):
        _empty_rejected(client.post("/api/exams", headers=admin, json={**body, field: ""}), field)


def test_审方规则药品编码为空_422_修前存进去的空编码规则照样列得出(client, admin):
    from app.database import SessionLocal
    from app.models import DrugRule

    _empty_rejected(client.post("/api/prescriptions/rules", headers=admin,
                                json={"drug_code": "", "max_daily_dose": 2}), "drug_code")
    resp = client.post("/api/prescriptions/rules/import", headers=admin, json=[{"drug_code": "", "max_daily_dose": 2}])
    assert resp.status_code == 422 and resp.json()["detail"][0]["type"] == "string_too_short", resp.text

    with SessionLocal() as db:   # 修之前经接口存进去的空编码规则（修前实测 201）
        db.add(DrugRule(drug_code="", max_daily_dose=2))
        db.commit()
    listed = client.get("/api/prescriptions/rules", headers=admin)
    assert listed.status_code == 200, listed.text
    assert "" in [r["drug_code"] for r in listed.json()]
