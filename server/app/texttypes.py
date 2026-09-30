"""文本入参的共用约束（P1-109）。

请求模型里 `Field(min_length=1)` 的本意是「这一项必须填」，可空格也是字符：一串半角空格、一个全角空格
照样过。2026-09-24 开发库实测：机构名、病种项目编码、用户名、员工姓名填一串空格，全部 201 落库——
清单里多出一行看不见名字的记录，下拉里多出一个空白选项，按名称 / 编码查重也拦不住第二串空格。

`NON_BLANK` 只多要求一件事：**至少有一个非空白字符**。pydantic 的 `pattern` 按 search 语义匹配（不是
整串匹配），`\\S` 命中任一非空白字符即过，所以：

- 合法值一个字节不变：前后带空格的照原样收下、照原样落库，不替人 strip（strip 会改掉存量查重与
  对接方比对的口径，那是另一件事）；
- 只挡全是空白的（半角空格、制表符、换行、全角空格 U+3000 都算空白）。

**只给请求模型用。** 出参模型若从请求模型继承到它，修之前已经存进去的纯空白行会让整个清单 500
（P1-63 / P2-40 同族）；继承到它的出参字段一律覆盖回不带它的声明。两件事都由
`tests/test_blank_required_text.py` 盯着：带 `min_length` 的请求文本字段必须挡得住纯空白，出参字段
不许带它。前端 `shared.js` 的 `errorText` 认得这条 pattern，把 pydantic 的英文原话换成「不能只填空格」。
"""
import re
import unicodedata


#: 必填文本至少要有一个非空白字符：`Field(min_length=1, max_length=N, pattern=NON_BLANK)`
NON_BLANK = r"\S"

#: 「逗号分隔」清单的分隔符：半角逗号、全角逗号、顿号（P1-137）。中文输入法打出来的是「，」「、」，只按半角逗号拆，
#: 整串会被当成**一个**词——DRG 分组的主诊断关键词、审方规则的禁忌诊断就此永远命中不了，不报错。
_LIST_SEPARATORS = re.compile(r"[,，、]")


def split_list(value: str | None) -> list[str]:
    """把界面 / 导入里「逗号分隔」的清单拆成去空白、去空项的列表；半角逗号、全角逗号、顿号都认（P1-137）。"""
    return [part.strip() for part in _LIST_SEPARATORS.split(value or "") if part.strip()]


def code_key(value: str | None) -> str:
    """编码的比对键：全角转半角（NFKC）、去首尾空白、大写（P1-218）。

    **只用于比对，不改落库的值**（与上面 NON_BLANK「不替人 strip」同一个取舍：落库口径怎么定另是一件事）。药品编码、
    疫苗编码这类「按编码找规则 / 找禁忌」的地方，原样比对时 `b01aa03`、`B01AA03 `、`Ｂ０１ＡＡ０３` 都等于「规则库里没有」
    ——审方直接系统审通过、禁忌拦不住。比对两侧都过它，写法不同的同一个编码才认得出是同一个。"""
    return unicodedata.normalize("NFKC", value or "").strip().upper()


def text_key(value: str | None) -> str:
    """文字的比对键：全角转半角（NFKC）、不分大小写（casefold）、去掉全部空白（P2-792）。

    给「关键词在不在这段文字里」的子串比对用：DRG 的主诊断 / 主手术关键词、审方规则的禁忌诊断。原先按原样比，手术写成
    `pci术` / `ＰＣＩ术`、诊断写成 `qt间期延长` / `ＱＴ间期延长` 都命中不了——经皮冠脉介入落进内科组，QT 延长的患者照开
    阿奇霉素、系统审通过。关键词与被查的文字两侧都过它；同 `code_key`，只用于比对，不改落库的值。"""
    return "".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def has_keyword(text: str | None, keywords) -> bool:
    """`keywords` 里有没有一个出现在 `text` 里：两侧都过 `text_key`，关键词归一后为空的不算命中（与 DRG 的 `_contains`
    同一句）。随访方案的诊断关键词（自动匹配、出院即派生）与病历质控的要点关键词共用（P2-917）：原先按原样
    `k in text`，诊断写成 `i10`、`Ｉ１０`、`copd急性加重` 就不派随访，也没有任何提示。"""
    key = text_key(text)
    return any(k and k in key for k in (text_key(kw) for kw in keywords or []))


#: 性别只有三个取值（P2-941）：区域结构、审方、慢专病规则、FHIR 出站都只认「男 / 女 / 未知」。常见的编码写法归一过来——
#: GB/T 2261.1（0 未知、1 男、2 女、9 未说明）、HL7 v2 PID-8（M / F / U / O / A / N）、FHIR（male / female / unknown /
#: other）、「男性 / 女性」。原先建档、更正、存量导入都原样收：导入的 1 / 2 / F 三行在区域结构里全算「未知」，存成「1」
#: 的男性有孕产档案时审方当孕产妇，更正成「女性」审批通过即落库
_GENDER_WORDS = {
    "男": "男", "女": "女", "未知": "未知", "男性": "男", "女性": "女", "": "未知",
    "0": "未知", "1": "男", "2": "女", "9": "未知",
    "m": "男", "f": "女", "u": "未知", "o": "未知", "a": "未知", "n": "未知",
    "male": "男", "female": "女", "unknown": "未知", "other": "未知",
}


def normalize_gender(value: str | None) -> str | None:
    """性别的常见写法归一成「男 / 女 / 未知」（拉丁字母不分大小写）；认不出返回 None，由调用方决定 422 还是记错误行。"""
    key = (value or "").strip()
    return _GENDER_WORDS.get(key.lower() if key.isascii() else key)
