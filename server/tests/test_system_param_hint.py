"""系统参数页给的示例键必须是后端真会读的键（P2-48）。

参数页的键输入框原先举例「portal.verify_lock_seconds」——后端没有任何地方读这个键：
居民端核验的锁定时长写死在 `portal.py`（`LoginFailureTracker(fail_limit=5, lock_seconds=600)`）。
管理员照着示例配上 300，保存成功、列表里也有，行为一丝不变。系统参数表是个通用键值存储，
今天真有消费方的只有公众号模板 id（`notify.WECHAT_TEMPLATE_PARAM_PREFIX + 类目`）与
FHIR 增量导出水位（程序自己写，不该手配）。示例改成前一种里真在发的类目。
"""
import pathlib
import re

from app.notify import WECHAT_TEMPLATE_PARAM_PREFIX

PAGE = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js"
APP_DIR = PAGE.parents[1]


def _example_key() -> str:
    m = re.search(r'<input name="key" placeholder="参数键（如 ([\w.]+)', PAGE.read_text(encoding="utf-8"))
    assert m, "系统参数页的键输入框找不到了——改了表单请同步本用例"
    return m.group(1)


def test_示例键是公众号模板键_且那个类目真的在发():
    key = _example_key()
    assert key.startswith(WECHAT_TEMPLATE_PARAM_PREFIX), f"示例键 {key} 没有消费方"
    category = key[len(WECHAT_TEMPLATE_PARAM_PREFIX):]
    senders = [p for p in APP_DIR.rglob("*.py") if f'category="{category}"' in p.read_text(encoding="utf-8")]
    assert senders, f"没有任何代码按类目 {category} 发通知——配了这个模板键也不会生效"
