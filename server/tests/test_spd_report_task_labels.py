"""慢专病报告的任务表印中文名、积分表印姓名，与页面同一份（P2-644，第十四批「导出 / 打印 vs 页面」扫描 R1-3）。

报告段落「待办任务」「超期预警」「服务工作量」直接印任务类型编码与优先级数字（followup / 1），「村医积分」只印
用户ID；同样的数据在页面上是「随访」「普通」、积分账户还带姓名。报告是打印 / 推送给机构看的，编码与数字编号在纸面上
认不出。积分表余额并列时原先不补尾键，新账户余额全是 0，同一份报告两次生成列的可以是不同的人。

修法：任务类型与优先级的中文名收进 `service.TASK_TYPE_NAMES` / `TASK_PRIORITY_NAMES`，规则元数据接口（界面拿去显示）
与报告段落都取它；积分表多一列姓名、按余额再按账户 id 排。认不出的类型原样印编码。
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPointAccount, SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2644 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2644 患者", "id_card": "330106197304042644"}).json()["id"]
    users = [client.post("/api/users", headers=admin, json={
        "username": f"p2644_vd{i}", "password": "Passw0rd!2644", "role": "doctor", "full_name": f"P2644 村医{i}",
        "org_id": org}).json()["id"] for i in range(3)]
    with SessionLocal() as db:
        for title, task_type, priority, status, due in (
                ("P2644 复诊", "revisit", 2, "pending", "2099-01-01"),
                ("P2644 复核", "screen", 3, "pending", "2099-01-02"),
                ("P2644 随访", "followup", 1, "pending", "2099-01-03"),
                ("P2644 超期转诊", "referral", 1, "overdue", "2020-01-01"),
                ("P2644 办结评估", "assess", 1, "done", "2099-01-04"),
                ("P2644 自定义", "custom_x", 1, "done", "2099-01-05")):
            db.add(SpdTask(patient_id=patient, org_id=org, program_code="P2644_PG", task_type=task_type,
                           title=title, status=status, due_date=due, priority=priority))
        # 后两位余额并列：按账户 id 收尾，先建的在前
        for user_id, balance in ((users[1], 30), (users[2], 30), (users[0], 80)):
            db.add(SpdPointAccount(user_id=user_id, org_id=org, balance=balance, earned=balance, used=0))
        db.commit()
    return {"org": org, "users": users}


def _section(key, org):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        return compose_section(db, {"key": key}, org, "daily")


def test_待办与超期表印类型与优先级的中文名(world):
    todo = _section("todo", world["org"])["rows"]
    assert [r[1:] for r in todo] == [["复诊", "2099-01-01", "紧急"], ["筛查复核", "2099-01-02", "特急"],
                                     ["随访", "2099-01-03", "普通"]]   # 修前 ["revisit", …, 2]
    alert = _section("alert", world["org"])["rows"]
    assert alert == [["P2644 超期转诊", "转诊", "2020-01-01"]]


def test_工作量表印类型中文名_认不出的原样印编码(world):
    rows = _section("workload", world["org"])["rows"]
    assert sorted(rows) == sorted([["评估", 1], ["custom_x", 1]])   # 修前 ["assess", 1]


def test_积分表带姓名_并列按账户先后(world):
    out = _section("points", world["org"])
    assert out["columns"] == ["用户ID", "姓名", "余额", "累计获得", "累计兑换"]
    u = world["users"]
    assert out["rows"] == [[u[0], "P2644 村医0", 80, 80, 0], [u[1], "P2644 村医1", 30, 30, 0],
                           [u[2], "P2644 村医2", 30, 30, 0]]


def test_元数据接口与两端界面的类型名是同一份(client, admin):
    from app.spd.service import TASK_PRIORITY_NAMES, TASK_TYPE_NAMES

    assert client.get("/api/spd/meta", headers=admin).json()["task_types"] == TASK_TYPE_NAMES

    def pairs(text):
        return dict(re.findall(r'(\w+): "([^"]+)"', text))

    spd = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    block = re.search(r"const SPD_TASK_TYPES = \{(.*?)\};", spd, re.S)
    assert block and pairs(block.group(1)) == TASK_TYPE_NAMES
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    inline = re.search(r'kv\("类型", esc\(\{(.*?)\}\[t\.task_type\]', doctor, re.S)
    assert inline and pairs(inline.group(1)) == TASK_TYPE_NAMES
    label = re.search(r"function spdPriorityLabel\(p\) \{\s*return (.*?);", spd, re.S)
    assert label and label.group(1) == 'p === 3 ? "特急" : p === 2 ? "紧急" : "普通"'
    assert TASK_PRIORITY_NAMES == {1: "普通", 2: "紧急", 3: "特急"}
