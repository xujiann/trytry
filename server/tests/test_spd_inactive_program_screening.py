"""停用的病种不再收新筛查、不再跑自动识别（P1-89）。

病种（`SpdProgram`）停用之后，同一个文件里的「签约建档」拒它（404「专病档案不存在或已停用」），
就诊登记触发的自动识别（`subscribers.py`）也只看启用的病种；唯独手工「筛查登记」
（`POST /api/spd/screenings`）与「按规则自动识别」（`POST /api/spd/screenings/auto-run`）
照收不误。而目录接口把停用的病种也列出来、三张表单的病种下拉照单全收，于是在正常界面上：

- 给停用病种登记筛查，201，疑似人进了目标池；
- 对停用病种点「按规则自动识别」，一次最多扫 5000 人，疑似的一并入池；

这些人从此**永远建不了档**（建档那头拒停用病种）——目标池被一批走不通的记录撑大，
待办与考核里的「待建档」数跟着虚高。

修法：两处与建档同一口径（`program is None or not program.active` → 404，同一句文案）；
前端三张业务表单（筛查登记、自动识别、签约建档）的病种下拉只列启用的，筛选栏仍列全部
（看历史要用）。启用病种的行为一字不变（特征化用例钉住）。
"""
import pathlib

import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdScreening

B = "/api/spd"
SPD_JS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js"
RULES = [{"field": "age", "op": ">=", "value": 60}]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "停用病种卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()
    patient = client.post("/api/patients", json={"name": "停用病种患者", "id_card": "330188195005050051",
                                                  "gender": "男", "birth_date": "1950-05-05"},
                          headers=admin).json()
    # 自动识别只扫本机构有就诊记录的人
    r = client.post("/api/encounters", json={"patient_id": patient["id"], "org_id": org["id"],
                                             "encounter_type": "outpatient"}, headers=admin)
    assert r.status_code == 201, r.text
    programs = {}
    for code in ("p189_on", "p189_off"):
        r = client.post(f"{B}/programs", json={"code": code, "name": f"停用校验病种{code}",
                                               "category": "chronic", "include_rules": RULES}, headers=admin)
        assert r.status_code == 201, r.text
        programs[code] = r.json()
    r = client.patch(f"{B}/programs/{programs['p189_off']['id']}", json={"active": False}, headers=admin)
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    return {"org": org["id"], "patient": patient["id"]}


def _written(program_code):
    with SessionLocal() as db:
        return (db.query(SpdScreening).filter(SpdScreening.program_code == program_code).count(),
                db.query(SpdCandidate).filter(SpdCandidate.program_code == program_code).count())


def test_特征化_启用病种照常登记筛查与自动识别(client, admin, world):
    r = client.post(f"{B}/screenings", json={"patient_id": world["patient"], "program_code": "p189_on",
                                              "org_id": world["org"]}, headers=admin)
    assert r.status_code == 201, r.text
    r = client.post(f"{B}/screenings/auto-run", json={"program_code": "p189_on", "org_id": world["org"]},
                    headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] >= 1


def test_停用病种登记筛查_404且一行不写(client, admin, world):
    r = client.post(f"{B}/screenings", json={"patient_id": world["patient"], "program_code": "p189_off",
                                              "org_id": world["org"]}, headers=admin)
    assert r.status_code == 404, (r.status_code, r.text[:200])
    assert "已停用" in r.json()["detail"]
    assert _written("p189_off") == (0, 0)


def test_停用病种自动识别_404且一行不写(client, admin, world):
    r = client.post(f"{B}/screenings/auto-run", json={"program_code": "p189_off", "org_id": world["org"]},
                    headers=admin)
    assert r.status_code == 404, (r.status_code, r.text[:200])
    assert "已停用" in r.json()["detail"]
    assert _written("p189_off") == (0, 0)


def test_与签约建档同一口径(client, admin, world):
    """建档那头本来就拒停用病种——这正是筛出来的人走不通的原因。"""
    r = client.post(f"{B}/enrollments", json={"patient_id": world["patient"], "program_code": "p189_off",
                                              "org_id": world["org"]}, headers=admin)
    assert r.status_code == 404 and "已停用" in r.json()["detail"], r.text


def test_前端三张开新业务的表单只列启用病种_筛选栏照旧列全部():
    src = SPD_JS.read_text(encoding="utf-8")
    assert "catalog.programs.filter((p) => !activeOnly || p.active)" in src
    for form_id in ("spd-screen-form", "spd-autoscreen-form", "spd-enroll-form"):
        start = src.index(f'id="{form_id}"')
        form = src[start: src.index("</form>", start)]
        assert "spdProgramOptions(catalog, false, true)" in form, f"{form_id} 的病种下拉还列着停用的病种"
    # 筛选栏（带「全部病种」空选项的）不收窄：看历史数据要能选到停用的病种
    assert "spdProgramOptions(catalog, true, true)" not in src
