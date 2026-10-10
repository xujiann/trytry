"""面板取数不比对请求序号：先发的旧响应晚到，就盖掉后选的那一份（P2-1012，第二十九批「页面状态残留」扫描 E2-5）。

住院管理「医嘱单」：先点甲、立刻改点乙，甲那次响应晚到（医嘱多、响应慢）把甲的医嘱画在面板里，标题只写「医嘱单」——
真浏览器实测「登记执行」记到了甲的医嘱上。同形：编码字典连着切、居民端号源连着改机构 / 日期、价格公示展开时的全表请求
常比按关键字的那次晚回来。`route()`（core.js）、`loadSpd()`（m.js）早已用序号修过同一个毛病。

修法：取数前记下序号，回来时序号过期就丢弃；医嘱单标题写上住院号。端到端用例在 `tests/e2e/test_flows.py`。

P2-1795（第五十三批扫描 AQ2-8）并进同一组：一批「按对象查、就地重画」的只读面板既没有序号也不写对象——成员端监测记录与
看趋势、健康日历、新生儿筛查史、诊间医防提醒、危急值留痕、住院执行记录、共享诊断修订史、流程流转记录。先查甲再查乙，甲的
回包晚到画在乙的号下（扫描实测：甲收缩压 182、乙 124，框里是乙、结果区成了 182）。逐处取序号（成功与出错两支都比对），
面板头写对象（姓名或编号，取得到什么写什么）。成员端监测记录的 node 用例在 `test_spd_member_measure_latest_wins.py`。
"""
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _src(name):
    return strip_comments((STATIC / name).read_text(encoding="utf-8"))


def _after(src, marker, length=900):
    start = src.index(marker)
    return src[start:start + length]


def _guarded(body, counter, call):
    bump = f"const seq = ++{counter};"
    check = f"seq !== {counter}"
    assert bump in body, f"没有取序号：{counter}"
    assert body.index(bump) < body.index(call) < body.index(check), f"回来时没比对序号：{counter}"


def test_医嘱单只画最后点的那一次住院_标题写住院号():
    src = _src("pages-clinical.js")
    assert "let ordersSeq = 0;" in src
    body = _after(src, "if (d.orders) {")
    # 医嘱单续页取全（P2-1693）：取数那一句换成 `fetchAllPages(api, …)`，序号照旧包住它
    _guarded(body, "ordersSeq", "await fetchAllPages(api, `/api/inpatient/orders?admission_id=")
    assert '$("#inp-orders-title").textContent = `医嘱单 · 住院 #${d.orders}`;' in body
    assert 'id="inp-orders-title"' in src


def test_接种页三块按患者号查的面板只画最后一次查的_出错也只认最后一次():
    """P2-1790（第五十三批扫描 AQ2-1）：接种前评估、禁忌清单、接种史；node 跑一遍见 test_vaccination_patient_panels_latest_wins.py。"""
    src = _src("pages-clinical.js")
    for marker, counter, call in (
        ('$("#vac-check").onsubmit = async (e) => {', "vacCheckSeq", "await api(`/api/vaccination/pre-check?"),
        ("const drawContras = async (pid) => {", "contraSeq", "await api(`/api/vaccination/contraindications?"),
        ('$("#vac-hist").onsubmit = async (e) => {', "vacHistSeq", "await api(`/api/vaccination/records?"),
    ):
        body = _after(src, marker)
        _guarded(body, counter, call)
        catch = body[body.index("catch (err) {"):body.index(f"seq !== {counter}")]
        assert f"if (seq === {counter})" in catch, f"出错那一支没比对序号：{counter}"
    lift = _after(src, '$("#contra-result").onclick = async (e) => {')
    assert "const seq = contraSeq;" in lift and "if (seq === contraSeq) drawContras(pid);" in lift   # 解除后的重画同样只认最后一次


def test_编码字典只画最后切的那一个():
    body = _after(_src("core.js"), "const draw = async (system) => {")
    _guarded(body, "dictSeq", "await api(`/api/dictionaries/")


def test_调阅授权清单只画最后一次查的_授权与撤销后的重画也只认最后一次():
    """P2-1791（第五十三批扫描 AQ2-3）；node 跑一遍见 test_archive_authorization_list_latest_patient.py。"""
    src = _src("core.js")
    src = src[src.index("async function renderPatients("):]
    body = _after(src, "const drawAuths = async (pid) => {")
    _guarded(body, "authSeq", "await api(`/api/patients/${pid}/authorizations`)")
    assert "if (seq === authSeq) throw err;" in body[body.index("catch (err) {"):]   # 过期的出错不往上抛、不写消息行
    for handler in ('$("#auth-grant-form").onsubmit', '$("#page-body").onclick'):   # 授权、撤销
        redraw = _after(src, handler, 1400)
        assert "const seq = authSeq;" in redraw and "if (seq === authSeq) await drawAuths(pid);" in redraw, handler


def test_居民端号源与价格公示只画最后一次查的():
    src = _src("m/m.js")
    _guarded(_after(src, "const drawSlots = async () => {"), "slotSeq", "await authApi(`/api/portal/me/slots")
    _guarded(_after(src, "async function loadPriceList("), "priceSeq", "await api(`/api/portal/price-list")


# ---------------------------------------------------------------- P2-1795（第五十三批扫描 AQ2-8）
def _block(src, marker, end="\n  };\n"):
    start = src.index(marker)
    return src[start:src.index(end, start)]


def _guarded_both(body, counter, call):
    """成功与出错两支都比对（P2-1795）：取序号在发请求前；出错那一支序号没过期才写原因，回来先比对、过期就丢。"""
    _guarded(body, counter, call)
    assert f"seq === {counter}" in body[body.index(call):], f"出错那一支没比对序号：{counter}"


def test_成员端监测记录与看趋势只画最后一次查的那一位_结果区写患者号():
    src = _src("pages-spd.js")
    assert "let measSeq = 0;" in src   # 两处共用一块结果区，共用一个序号
    query = _block(src, "const measQuery = async () => {")
    _guarded_both(query, "measSeq", "await api(`/api/spd/measurements?")
    assert "患者 ${esc(body.patient_id)} 的监测记录" in query
    trend = _block(src, '$("#spd-meas-trend-btn").onclick = async () => {')
    _guarded_both(trend, "measSeq", "await api(`/api/spd/measurements/trend?")
    assert "患者 ${esc(body.patient_id)} · ${esc(body.metric)} 的趋势" in trend


def test_健康日历只画最后一次查的那一位_日期前写患者号():
    src = _src("pages-spd.js")
    assert "let calSeq = 0;" in src
    body = _block(src, '$("#spd-cal-form").onsubmit = async (e) => {')
    _guarded_both(body, "calSeq", "await api(`/api/spd/health-calendar?")
    assert "患者 ${esc(q.patient_id)} · ${esc(cal.day)}：" in body


def test_新生儿筛查史只画最后一次点的那一位_标题写是谁():
    src = _src("pages-clinical.js")
    assert "let shistSeq = 0;" in src and '<h3 id="screen-title">新生儿筛查史</h3>' in src
    body = _block(src, "if (d.shist) {", "\n    }\n")
    _guarded_both(body, "shistSeq", "await api(`/api/maternal/children/")
    assert '$("#screen-title").textContent = `新生儿筛查史 · ' in body
    assert body.index('$("#screen-title").textContent') < body.index("await api(")   # 点下去就换标题、先清空


def test_诊间医防提醒只画最后一次查的那一位_结果头写患者号():
    src = _src("pages-clinical.js")
    assert "let remSeq = 0;" in src
    body = _block(src, '$("#rem-form").onsubmit = async (e) => {')
    _guarded_both(body, "remSeq", "await api(`/api/publichealth/reminders/")
    assert "患者 ${esc(r.patient_id)} 的诊间提醒" in body


def test_危急值留痕只画最后一次点的那一条_标题写报告号():
    src = _src("pages-clinical.js")
    assert "let trailSeq = 0;" in src and '<h3 id="crit-trail-title">处置留痕轨迹</h3>' in src
    body = _block(src, "if (trail) {", "\n      }\n")
    _guarded_both(body, "trailSeq", "await api(`/api/exams/reports/")
    assert '$("#crit-trail-title").textContent = `处置留痕轨迹 · 报告 ${trail}' in body


def test_住院执行记录只画最后一次点的那一条医嘱_换住院也作废在途的():
    src = _src("pages-clinical.js")
    assert "let execSeq = 0;" in src
    body = _block(src, "const drawExecutions = async (orderId) => {")
    _guarded_both(body, "execSeq", "await api(`/api/inpatient/orders/")
    assert "医嘱 ${esc(orderId)} 的执行记录" in body   # 标题本就写着医嘱号
    assert "execSeq += 1;" in _after(src, "if (d.orders) {")   # 换一次住院，在途的执行记录作废


def test_共享诊断修订史只画最后一次查的那份报告_表头写报告号():
    src = _src("core.js")
    assert "let revSeq = 0;" in src
    body = _block(src, "const drawRevisions = async (reportId) => {")
    _guarded_both(body, "revSeq", "await api(`/api/exams/reports/")
    assert "报告 ${esc(reportId)} 的修订史" in body


def test_流程流转记录只画最后一次点的那一个实例_标题写实例号():
    src = _src("pages-mgmt.js")
    assert "let historySeq = 0;" in src and '<h3 id="wf-history-title">流转记录</h3>' in src
    body = _block(src, "else if (d.history) {", "\n      } else return;")
    _guarded_both(body, "historySeq", "await api(`/api/workflows/instances/")
    assert '$("#wf-history-title").textContent = `流转记录 · 实例 ${d.history}' in body
