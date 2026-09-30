"""批量识别把「没命中纳入、只命中排除」的人记成排除筛查并写进目标池（P2-1118，第三十二批「规则与配置的求值口径」
扫描 B4-2）。

`auto_screen` 扫本机构全部就诊患者，按 `rules.screen()` 判定：它先判排除、不管有没有命中纳入（医护登记筛查与
`exclusion_problem` 要的就是这个口径——「有禁忌」压过「符合适应证」）。于是本院看过感冒的孩子一命中种子排除规则
「age < 18」，就落一条 `result = excluded` 的筛查、外加一行目标池 excluded，理由写「未成年人不纳入成人高血压管理」；
重跑再写一遍，累计筛查、筛查转化率、报告「筛查情况」都把这些孩子算进去。修前实测（b4/r5）：成人 I10、两名儿童 J06、
成人 S52 批量识别得 `{scanned 4, suspect 1, excluded 2, normal 1}`，目标池出现两名感冒儿童 excluded。就诊触发的同一个
「规则自动识别」（`subscribers.on_encounter_created`）只写疑似。

修法：批量识别只对命中纳入的人判排除——纳入且排除 → excluded（照记筛查、进池）；没命中纳入 → 跳过、不写筛查不进池，
计入 normal。`rules.screen()` 不改。存量不改。
"""
from conftest import business_today

from app.database import SessionLocal

B = "/api/spd"


def _patient(client, admin, org, name, id_card, birth, code, diag):
    made = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card, "birth_date": birth})
    assert made.status_code in (200, 201), made.text
    pid = made.json()["id"]
    visit = client.post("/api/encounters", headers=admin, json={
        "patient_id": pid, "org_id": org, "diagnosis_code": code, "diagnosis_name": diag})
    assert visit.status_code in (200, 201), visit.text
    return pid


def _rows(org):
    from app.spd.models import SpdCandidate, SpdScreening

    with SessionLocal() as db:
        screenings = sorted((pid, result) for pid, result in db.query(SpdScreening.patient_id, SpdScreening.result)
                            .filter_by(org_id=org, program_code="hypertension"))
        pool = sorted((pid, status) for pid, status in db.query(SpdCandidate.patient_id, SpdCandidate.status)
                      .filter_by(org_id=org, program_code="hypertension"))
    return screenings, pool


def test_没命中纳入的不写筛查不进池_纳入且排除的照记(client, admin):
    year = business_today().year
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1118 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    adult = _patient(client, admin, org, "P1118 成人高血压", "330106197001011118", "1970-01-01", "I10", "原发性高血压")
    kid1 = _patient(client, admin, org, "P1118 感冒儿童甲", "330106201501011119", f"{year - 11}-01-01", "J06", "急性上呼吸道感染")
    kid2 = _patient(client, admin, org, "P1118 感冒儿童乙", "330106201601011110", f"{year - 10}-01-01", "J06", "急性上呼吸道感染")
    _patient(client, admin, org, "P1118 成人骨折", "330106198001011112", "1980-01-01", "S52", "桡骨骨折")
    teen = _patient(client, admin, org, "P1118 少年高血压", "330106201001011113", f"{year - 16}-01-01", "I10", "原发性高血压")
    version = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")["version"]

    resp = client.post(f"{B}/screenings/auto-run", headers=admin, json={"program_code": "hypertension", "org_id": org})
    assert resp.status_code == 200, resp.text
    # 修前 {scanned 5, suspect 1, excluded 3, normal 1}：两名感冒儿童算进了「排除」
    assert resp.json() == {"scanned": 5, "suspect": 1, "excluded": 1, "normal": 3, "rule_version": version}
    screenings, pool = _rows(org)
    assert screenings == sorted([(adult, "suspect"), (teen, "excluded")])   # 修前多两条儿童 excluded 筛查
    assert pool == sorted([(adult, "suspect"), (teen, "excluded")])         # 修前两名儿童进了高血压目标池

    again = client.post(f"{B}/screenings/auto-run", headers=admin, json={"program_code": "hypertension", "org_id": org})
    assert again.json() == resp.json()
    screenings, pool = _rows(org)
    assert not {kid1, kid2} & {pid for pid, _ in screenings + pool}   # 重跑也不写
