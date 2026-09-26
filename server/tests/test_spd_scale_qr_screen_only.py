"""非筛查量表的二维码扫进居民端，悄悄预选了另一张问卷（P2-366）。

量表二维码「扫码直达居民端自查页并预选该量表」（`scale_qr`），可居民端自查页只列筛查类量表、答卷只记筛查：
风险评估 / 分期 / 康复量表照样出码，扫进去找不到那张问卷，页面又静默回落到列表首项——居民以为答的是扫的
那张（如「慢专病综合风险评估量表」），答卷记成了另一个病种的筛查，还可能提示申请专病服务。

修法：只给筛查类量表出码（后端 422、管理端按钮只挂在筛查量表上）；居民端认不出扫进来的量表（非筛查类 / 码已
失效）就明说，不再不声不响。居民扫码自评要不要做另行待裁定。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _published_scale(client, admin, code, category):
    scale = client.post("/api/spd/scales", headers=admin, json={
        "code": code, "name": f"{code} 量表", "category": category, "program_code": "hypertension",
        "items": [{"key": "q1", "title": "是否头晕", "type": "single",
                   "options": [{"label": "否", "score": 0}, {"label": "是", "score": 2}]}]})
    assert scale.status_code == 201, scale.text
    published = client.post(f"/api/spd/scales/{scale.json()['id']}/publish", headers=admin)
    assert published.status_code == 200, published.text
    return scale.json()["id"]


def test_非筛查量表不出居民自查码(client, admin):
    risk = _published_scale(client, admin, "p2366_risk", "risk")
    resp = client.get(f"/api/spd/scales/{risk}/qr.svg", headers=admin)
    assert resp.status_code == 422, resp.text   # 修前 200：出了一张扫进去找不到问卷的码
    assert "筛查" in resp.json()["detail"]
    screen = _published_scale(client, admin, "p2366_screen", "screen")
    ok = client.get(f"/api/spd/scales/{screen}/qr.svg", headers=admin)
    assert ok.status_code == 200 and b"<svg" in ok.content, ok.text


def test_管理端只在筛查量表上挂二维码按钮():
    src = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    at = src.index('data-scale-qr="${sc.id}"')
    guard = src[src.rindex("${", 0, at):at]
    assert 'sc.category === "screen"' in guard, guard   # 修前：已发布的一律挂「二维码」


def test_居民端认不出扫进来的量表时明说_不静默回落():
    src = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    start = src.index("if (scaleTokenFromQr) {")
    block = src[start:src.index('scaleTokenFromQr = "";', start)]
    # 找到了就预选；找不到（非筛查类）与码失效（catch）两条路都要把话写到页面上
    assert re.search(r"\belse\s+\$\(\"#spd-scale-notice\"\)\.textContent\s*=", block), block
    catch = block[block.index("catch (err)"):]
    assert '$("#spd-scale-notice").textContent' in catch, catch   # 修前 catch 是空的
    assert 'id="spd-scale-notice"' in src
