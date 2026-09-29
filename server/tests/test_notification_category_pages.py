"""站内消息「前往处理」落错页 / 没有去处：危急值落到共享诊断中心（那里没有「确认接收」），慢专病三类消息分类原样显示代号、没有按钮（P2-460）。

职工收到的站内消息只有四类（`notify_staff` / `notify_user` 发出）：危急值、慢专病任务催办、专病路径暂停、报告生成。
消息页原先按关联对象（`link_type`）找页面：危急值关联的是检查报告，落到「共享诊断中心」——那里的危急值面板只有打印 /
修订 / 修订史，「确认接收」「处置反馈」只在危急值操作台（`exams.py` 原话：危急值页是唯一的地方）；驾驶舱下钻走的是对的
`critical`。慢专病三类没有映射，分类列显示 spd_task / spd_path / spd_report，也没有「前往处理」。

修后先按分类找页面。闸门：后端发给职工的每个消息分类，页面上都要有中文名与去处——新增一类忘了配，这里红。
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


def staff_categories() -> set[str]:
    """`notify_staff(...)` / `notify_user(...)` 调用里写死的 `category="…"`。"""
    found = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in (
                    "notify_staff", "notify_user"):
                for kw in node.keywords:
                    if kw.arg == "category" and isinstance(kw.value, ast.Constant):
                        found.add(kw.value.value)
    return found


def _js_map(source: str, name: str) -> dict[str, str]:
    block = re.search(rf"const {name} = \{{(.*?)\n\}};", source, re.S)
    assert block, name
    return dict(re.findall(r"(\w+):\s*\"([^\"]*)\"", block.group(1)))


def _page_ids() -> set[str]:
    return set(re.findall(r'\{ id: "([\w-]+)"', (STATIC / "app.js").read_text(encoding="utf-8")))


def test_职工消息每一类都有中文名与去处():
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    names, by_category = _js_map(source, "NOTIFY_CATEGORIES"), _js_map(source, "NOTIFY_CATEGORY_PAGE")
    categories = staff_categories()
    assert categories >= {"critical_value", "spd_task", "spd_path", "spd_report"}, categories   # 判据自证：认得出
    missing = sorted(c for c in categories if c not in names or c not in by_category)
    assert not missing, f"这些职工消息分类在消息页上没有中文名或没有去处：{missing}"   # 修前 spd_* 三类
    assert set(by_category.values()) <= _page_ids(), by_category


def test_危急值去危急值操作台():
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    assert _js_map(source, "NOTIFY_CATEGORY_PAGE")["critical_value"] == "critical"   # 修前按关联对象落到 exams
    assert "NOTIFY_CATEGORY_PAGE[n.category] || NOTIFY_LINK_PAGE[n.link_type]" in source


def test_用户手册写的职工消息与代码一致():
    """P2-777（第二十批「界面文案 vs 行为」扫描 M2-6）：手册原先写职工站内消息「自动投递四类：危急值、检查报告出具、
    手术安排、出院随访」，后三类其实只发居民（`notify_patient`），医护一条也收不到。这里钉住：手册那一段点到的职工分类
    就是代码发给职工的那几类（按消息页的中文名找），居民类写明是发给居民的。"""
    manual = (ROOT.parent / "docs" / "用户手册.md").read_text(encoding="utf-8")
    item = manual[manual.index("**站内消息**（总览 → 站内消息）"):]
    item = item[:item.index("\n7. ")]
    names = _js_map((STATIC / "pages-mgmt.js").read_text(encoding="utf-8"), "NOTIFY_CATEGORIES")
    missing = sorted(c for c in staff_categories() if names[c] not in item)
    assert not missing, f"手册没写到这些发给职工的消息：{missing}"
    staff_part, _, resident_part = item.partition("发给居民")
    assert resident_part, item   # 修前没说居民类发给谁
    for resident_only in ("检查报告出具", "手术安排", "出院随访"):
        assert resident_only not in staff_part.split("投递给职工的")[-1].split("；")[0], resident_only   # 修前列成职工四类
