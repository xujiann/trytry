"""健康处方开具表单带「目标说明」（P2-1605，第四十七批「慢专病服务域」扫描 AK2-8 页面那一半）。

后端 `POST /api/spd/health-prescriptions` 早就收 `target_note`（最长 512），个案管理师页的处方清单有「目标说明」这一列、
居民端显示为「管理目标」，可开具表单（`#spd-rx-form`）只有用药 / 康复 / 生活三格——从页面开出的处方目标说明一律是空的，
清单那一列永远「—」。
修法：表单补一格 `target_note`（maxlength 与后端同一个上限）。作废 / 更正不在这一条（并入 P2-1493 待裁定）。

接口带 target_note 开具、清单读回已由 `test_spd_care_contract.py::test_健康处方开具与列表` 钉着，这里只钉页面。
"""
import os
import re

from app.spd.routers.care import PrescriptionIn

PAGE = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "pages-spd.js"), encoding="utf-8").read()


def _rx_form() -> str:
    start = PAGE.index('<form class="inline" id="spd-rx-form">')
    return PAGE[start:PAGE.index("</form>", start)]


def _max_length(field: str) -> int:
    (limit,) = [m.max_length for m in PrescriptionIn.model_fields[field].metadata if hasattr(m, "max_length")]
    return limit


def test_开具表单有目标说明一格_上限与后端一致():
    form = _rx_form()
    inputs = re.findall(r'<input name="target_note"[^>]*>', form)
    assert len(inputs) == 1, form   # 修前没有这一格
    assert f'maxlength="{_max_length("target_note")}"' in inputs[0], inputs[0]


def test_开具表单送的字段后端都收():
    names = re.findall(r'<(?:input|select|textarea) name="([a-z_]+)"', _rx_form())
    assert "target_note" in names
    assert set(names) <= set(PrescriptionIn.model_fields), names   # 写错字段名后端照单忽略，等于没送
