"""改变待办的动作办完立即刷新铃铛；医生移动端标已读后「未读消息」角标跟着变（P2-1312，第三十八批扫描 AB1-8）。

修前：管理端铃铛（`core.js::pollTodos`）只靠 30 秒一拍的轮询。站内消息页标已读后立即 `pollTodos()`，而改变 `todos.py`
各节状态的其余动作——开方（系统审出问题的转入「待药师审处方」）、审方通过 / 退回；开检查单、领取、出报告（「待诊断申请」，
出报告还可能新增一条危急值）、修订报告；危急值确认接收、处置反馈（「待确认危急值」「未闭环危急值」）；库存维护、新批次入库、
发药、冲销、调拨、召回、盘点、采购验收（管理层的「缺药预警」）——成功后只调 `route()` 重画本页：药师审完最后一张方，铃铛仍显示
「待药师审处方（1）」、下拉里还列着这张方，最长挂 30 秒。医生移动端（`m/doctor.js::bindNoticeRead`）标已读后只就地移除卡片，
「未读消息」角标照挂标记前的数。

修法：这些动作的成功分支照站内消息页的写法紧跟 `pollTodos()`；移动端标已读后照取数时的同一个接口重取未读总数、改写角标。

闸门是派生的：管理端页面文件里每一处改变铃铛各节的写请求（`TODO_WRITES`，按 todos.py 的节分组），其后到这一段处理结束之前
（下一处这类写请求、`catch (`、或 render 函数里的下一句）都要有 `pollTodos()`——新加一处这类动作忘了刷新铃铛，这里先红。
"""
import re
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 改变铃铛各节状态的写请求（管理端页面里的写法），按 `todos.py` 的节分组；铃铛角标还把站内消息未读数算在里面。
TODO_WRITES = {
    "待药师审处方（todos._pending_prescriptions）": [
        r'api\("/api/prescriptions",\s*\{\s*method:\s*"POST"',        # 开方：系统审出问题的转入药师审核
        r'api\(`/api/prescriptions/\$\{\w+\}/review`',                  # 审方通过 / 退回
    ],
    "待诊断申请（todos._pending_exams）": [
        r'api\("/api/exams",\s*\{\s*method:\s*"POST"',                  # 开单（含互认弹窗里那一路）
        r'api\(`/api/exams/\$\{\w+\}/(?:claim|report)`',                # 领取、出报告（出报告还可能新增一条危急值）
    ],
    "未闭环 / 待确认危急值（todos._critical_reports / _unacknowledged_critical）": [
        # 确认接收、处置反馈、修订（修订改判仍为危急值的复位成「已通知」，解除危急的出闭环）
        r'api\(`/api/exams/reports/\$\{\w+\}(?:/acknowledge|/resolve)?`,\s*\{\s*method:\s*"(?:POST|PATCH)"',
    ],
    "缺药预警（todos._stock_alerts → dispense.q_dispensable_shortage）": [
        r'api\("/api/pharmacy/(?:stocks|batches|transfers)",\s*\{\s*method:\s*"POST"',   # 库存维护、新批次入库、调拨
        r'api\(`/api/pharmacy/batches/\$\{\w+\}/recall`',               # 召回
        r'postAction\("/api/pharmacy/stock-takes"',                     # 盘点
        r'postAction\(`/api/pharmacy/purchase-orders/\$\{[\w.]+\}/receive`',   # 采购验收入库
        r'api\("/api/dispense",\s*\{\s*method:\s*"POST"',               # 发药
        r'api\(`/api/dispense/\$\{[^}]+\}/reverse`',                    # 冲销
    ],
    "站内消息未读（铃铛角标含未读数）": [
        r'postAction\("/api/notifications/read-all"',
        r'postAction\(`/api/notifications/\$\{\w+\}/read`',
    ],
}
PATTERNS = [p for group in TODO_WRITES.values() for p in group]
WRITE = re.compile("|".join(f"(?:{p})" for p in PATTERNS))
#: 一段处理到此为止：render 函数里的下一句（两格缩进起头），或 `catch (`
BOUNDARY = re.compile(r"\n  \S|\bcatch\s*\(")


def unrefreshed(code: str) -> list[str]:
    """写请求之后、这一段处理结束之前（也不越过下一处同类写请求）没有 `pollTodos()` 的，按「行号: 写请求开头」列出。"""
    hits = list(WRITE.finditer(code))
    missing = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(code)
        boundary = BOUNDARY.search(code, m.end(), end)
        if "pollTodos()" not in code[m.end():boundary.start() if boundary else end]:
            missing.append(f"{code.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")
    return missing


def _pages() -> dict[str, str]:
    """管理端的页面文件（`m/` 下是居民端与医生移动端，没有这只铃铛）。"""
    return {p.name: strip_comments(p.read_text(encoding="utf-8")) for p in sorted(STATIC.glob("*.js"))}


def test_改变待办的动作办完立即刷新铃铛():
    missing = {name: rows for name, code in _pages().items() if (rows := unrefreshed(code))}
    assert not missing, (
        "以下改变铃铛各节的写请求办完没有立即 pollTodos()（铃铛照挂旧数最长 30 秒、下拉里还列着已办的单）：\n  "
        + "\n  ".join(f"{name}:{row}" for name, rows in sorted(missing.items()) for row in rows))


def test_扫描本身没瞎():
    """每条判据都至少认出一处：接口改名、写法变了而判据没跟，这一类就从闸门底下溜走了。"""
    pages = _pages()
    for pattern in PATTERNS:
        assert any(re.search(pattern, code) for code in pages.values()), pattern
    # 修时 19 处：开方 / 审方、开单两路 / 修订 / 领取 / 出报告、确认接收 / 处置反馈、药房六处、盘点与验收、站内消息两处
    assert sum(len(WRITE.findall(code)) for code in pages.values()) >= 19


def test_判据自证_前一路刷新了_后一路没刷新照样红():
    crit = """
  $("#page-body").onclick = async (e) => {
    try {
      if (ack) { await api(`/api/exams/reports/${ack}/acknowledge`, { method: "POST" }); route(); pollTodos(); }
      if (resolve) {
        const done = await spdModal("处置反馈", [], { submit: (form) => api(`/api/exams/reports/${resolve}/resolve`,
          { method: "POST", body: "{}" }) });
        if (done) route();
      }
    } catch (err) { setMsg("#crit-msg", err.message, false); }
  };"""
    assert [row.split(": ", 1)[1] for row in unrefreshed(crit)] == [
        'api(`/api/exams/reports/${resolve}/resolve`,\n          { method: "POST"']
    assert unrefreshed(crit.replace("if (done) route();", "if (done) { route(); pollTodos(); }")) == []
    # 一行写完的处理函数：render 函数里的下一句起头即为界，不借下一句里的 pollTodos()
    one_liners = """
  $("#st-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pharmacy/stock-takes", {}, "#po-msg"); };
  $("#nt-readall").onclick = async () => { await postAction("/api/notifications/read-all", null, "#nt-msg"); pollTodos(); };"""
    assert [row.split(": ", 1)[1] for row in unrefreshed(one_liners)] == ['postAction("/api/pharmacy/stock-takes"']


def test_医生移动端标已读后重取未读总数改写角标():
    src = strip_comments((STATIC / "m" / "doctor.js").read_text(encoding="utf-8"))
    start = src.index("function bindNoticeRead(box) {")
    body = src[start:src.index("\n}\n", start)]
    after = body[body.index("await api(`/api/notifications/${btn.dataset.ntread}/read`"):]
    # 修前标完只移除卡片；重取用的是取数时同一个接口（loadTodos），角标的配色随未读是否为 0 换
    assert 'await api("/api/notifications/unread-count")' in after
    assert 'api("/api/notifications/unread-count")' in src[src.index("async function loadTodos()"):start]
    assert 'badge.textContent = unread;' in after
    assert 'badge.className = `badge ${unread ? "warn" : "zero"}`;' in after
