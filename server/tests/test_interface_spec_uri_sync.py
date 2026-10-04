"""《接口对接规范》§二与出入站代码同步：平台自定的 system / url 逐字写进规范，危急值写实际承载方式，批量导出的 manifest
字段与单轮上限照代码写（P2-1267，第三十七批「出站报文与上报的标准符合度」扫描 AA3-5）。

规范自称是「转换基准」，修前却没写出入站报文里平台自定的标识：电子健康卡号标识系统、检查项目编码系统、危急值扩展 url
三个 urn 只在 `integration.py` 的常量里；映射表还写着 DiagnosticReport「critical→flag」——R4 DiagnosticReport 没有 flag
元素，代码用命名扩展 `urn:medplat:critical` 承载、入站只认它：LIS / PACS 按规范送 `flag: true` 照 201，但 critical=False，
危急值闭环不启动。批量导出只有一句「批量 NDJSON」，文件命名、manifest 字段、amended 语义、单轮上限都没写。

这里盯三样：`integration.py` 里每个 `*_SYSTEM` / `*_URL` 字符串常量逐字出现在规范里（以后新加的常量没写进规范就红）；
规范里不再有「critical→flag」这类映射，危急值扩展照代码认的 JSON 写法给出；manifest 行的每个字段名、写进规范的单轮上限
与代码一致（规范里写下的数字要有东西盯着）。
"""
import ast
import inspect
import json
import re
from pathlib import Path

from app.routers import integration

SPEC = (Path(__file__).resolve().parents[2] / "docs" / "接口对接规范.md").read_text(encoding="utf-8")


def _uri_constants() -> dict[str, str]:
    return {name: value for name, value in vars(integration).items()
            if name.endswith(("_SYSTEM", "_URL")) and isinstance(value, str)}


def test_平台自定的标识系统与扩展url逐字写进规范():
    constants = _uri_constants()
    assert {"ID_CARD_SYSTEM", "EHC_SYSTEM", "EXAM_ITEM_SYSTEM", "CRITICAL_EXTENSION_URL"} <= set(constants), constants
    missing = {name: value for name, value in constants.items() if f"`{value}`" not in SPEC}
    assert not missing, f"对接规范没写这些出入站报文里用到的 system / url：{missing}"


def test_危急值写实际承载方式_不再写critical到flag():
    assert not re.search(r"critical\s*(→|->)\s*flag", SPEC), "R4 DiagnosticReport 没有 flag 元素，危急值由命名扩展承载"
    snippet = json.dumps({"url": integration.CRITICAL_EXTENSION_URL, "valueBoolean": True})
    assert snippet in SPEC, f"规范里要给出入站实际认的写法：{snippet}"


def _manifest_keys() -> set[str]:
    """`run_fhir_batch_export` 写 manifest 那一行的字典（带 generated_at 的那个）里出现的全部键，含按条件拼进去的 kind。"""
    tree = ast.parse(inspect.getsource(integration.run_fhir_batch_export))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "generated_at" for k in node.keys):
            return {key.value for sub in ast.walk(node) if isinstance(sub, ast.Dict) for key in sub.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    raise AssertionError("run_fhir_batch_export 里找不到写 manifest 行的字典")


def test_批量导出的manifest字段与单轮上限照代码写():
    keys = _manifest_keys()
    assert {"file", "resource_type", "rows", "from_id", "to_id", "generated_at", "kind"} <= keys, keys
    missing = sorted(key for key in keys if f"`{key}`" not in SPEC)
    assert not missing, f"对接规范没写 manifest 的这些字段：{missing}"
    limits = [int(n) for n in re.findall(r"至多(?:导出|取) (\d+) 条", SPEC)]
    assert limits and set(limits) == {integration.FHIR_EXPORT_BATCH_LIMIT}, (limits, integration.FHIR_EXPORT_BATCH_LIMIT)
