"""专病 360 卡片把已解绑的服务包印成「余 N（已用 x%）」，与同页档案明细的「已解绑」对不上（P2-1576，第四十六批扫描 AJ3-6）。

`GET /api/spd/patients/{id}/profile` 回出这份档案的全部绑定（含已解绑的，带 `status`），页面 `spdProfileHtml` 却不看状态：扣 1 次再
解绑，卡片照写「高血压上门包 余 3（已用 25.0%）」，扣次却 409「该服务包已解绑，不能扣减」；解绑再绑（P2-754）之后出现两张同名卡、
都写「余 N」。同页档案明细（`spdEnrollmentDetailHtml`）一直写「绑定中 / 已解绑」，居民端也按 P2-557 印状态。

修法：接口不动。卡片印绑定状态——接口没下发状态中文（`service.PACKAGE_BINDING_STATUS_NAMES` 只给居民端），照同页明细的取法，两处
共用 `spdBindingTag`；已解绑的灰显、不写「余 N」，绑定中的照旧。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jssrc import strip_comments

B = "/api/spd"
SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start:SRC.index("\n}\n", start) + 2]


def _const(name: str) -> str:
    start = SRC.index(f"const {name} = {{")
    return SRC[start:SRC.index("};", start) + 2]


@pytest.fixture(scope="module")
def profile(client, admin):
    """扣 1 次、解绑、再绑同一个包（P2-754）：画像里同名的两条绑定，一条已解绑、一条绑定中。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21576 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21576 居民", "id_card": "330106197301011576", "gender": "男", "birth_date": "1973-01-01"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    package = client.post(f"{B}/service-packages", headers=admin, json={
        "code": "p21576_pkg", "name": "高血压上门包", "program_code": "hypertension", "price": 120, "period_days": 365,
        "items": [{"code": "visit", "name": "上门随访", "times": 4, "price": 30}]})
    assert package.status_code == 201, package.text
    path = f"{B}/enrollments/{enrollment.json()['id']}/packages"
    first = client.post(path, headers=admin, json={"package_id": package.json()["id"]})
    assert first.status_code == 201, first.text
    used = client.post(f"{B}/package-bindings/{first.json()['id']}/usages", headers=admin, json={"item_code": "visit"})
    assert used.status_code == 201, used.text
    assert client.post(f"{B}/package-bindings/{first.json()['id']}/unbind", headers=admin).status_code == 200
    assert client.post(path, headers=admin, json={"package_id": package.json()["id"]}).status_code == 201
    got = client.get(f"{B}/patients/{patient}/profile", headers=admin)
    assert got.status_code == 200, got.text
    return got.json()


def test_接口照旧回出全部绑定与状态(profile):
    rows = [(b["package_name"], b["status"], b["remaining"], b["usage_rate"]) for b in profile["programs"][0]["packages"]]
    assert sorted(rows) == [("高血压上门包", "bound", 4, 0.0), ("高血压上门包", "unbound", 3, 25.0)]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_跑一遍_已解绑的卡片不写余量_绑定中的照旧(profile):
    stubs = ("const esc = (s) => String(s ?? \"\");\nconst spdTag = () => \"\";\nconst table = () => \"\";\n"
             "const panel = (title, body) => body;\nconst SPD_RISK = {}, SPD_TASK_STATUS = {}, SPD_MEAS_LEVEL = {},"
             " SPD_REF_STATUS = {};\n")
    helper = _function("spdBindingTag") if "function spdBindingTag(" in SRC else ""   # 修前没有这个帮手，照跑原卡片
    script = (stubs + _const("SPD_ENROLL_STATUS") + "\n" + helper + "\n"
              + _function("spdProfileHtml") + "\nconsole.log(spdProfileHtml(JSON.parse(process.argv[1])));")
    html = subprocess.run(["node", "-e", script, json.dumps(profile, ensure_ascii=False)],
                          capture_output=True, text=True, check=True, timeout=60).stdout
    line = next(row for row in html.splitlines() if "服务包：" in row)
    unbound, bound = sorted(line.split("服务包：", 1)[1].split("；"), key=lambda part: "已解绑" not in part)
    assert "已解绑" in unbound and "余" not in unbound and "已用" not in unbound, line   # 修前「高血压上门包 余 3（已用 25.0%）」
    assert 'class="muted"' in unbound, line   # 灰显
    assert "绑定中" in bound and "余 4（已用 0%）" in bound, line


def test_卡片与明细共用一处绑定状态写法():
    card = strip_comments(_function("spdProfileHtml"))
    detail = strip_comments(_function("spdEnrollmentDetailHtml"))
    assert "spdBindingTag(b)" in card and "spdBindingTag(b)" in detail
    assert "绑定中" not in card and "绑定中" not in detail   # 状态文案只在 spdBindingTag 里写一处
    tag = strip_comments(_function("spdBindingTag"))
    assert '"bound" ? \'<span class="tag green">绑定中</span>\' : \'<span class="tag">已解绑</span>\'' in tag
