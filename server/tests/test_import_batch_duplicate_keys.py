"""开办导入与批量开通的幂等只认库里已有的：同一个文件 / 同一批里撞键的后一条不再计「已存在」（P2-1271，第三十七批
「一次请求、一次导入里的重复元素」扫描 AA2-1）。

建号、收费目录、字典三件开办工具原先都是「键在 existing 里就幂等跳过」，再把本批刚导的键也 `existing.add` 进去：
两位同名「张伟」用同一个登录名，后一位没建号、口令清单里也没有他；收费项目同一编码一行 12.00、一行 25.00，库里只留
12.00；字典同一编码后一行的名称、规格丢了——三种都报「错误 0」、退出码 0。字典 HTTP 导入同形，页面写「跳过（编码
已存在）N 条」；村医批量开通同一批里同一账号两条，后一条记「已建档」。存量导入早按 P2-732 定了规矩（`_dup_in_batch`）：
同一文件里撞键的后一行记错误行、点名与第几行相同。修法：三件工具复用它（退出码照旧「有错误行即 1」）；字典 HTTP 导入
「先到为准」不改，回执只增不改，把「库里已有」与「本批重复」分开计数、点名重复的编码；村医批量开通同批重复的那条
点名与第几条相同，「已建档」只说库里原有的。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_charge_items  # noqa: E402
import import_dictionary  # noqa: E402
import import_users  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import ChargeItem, CodeEntry, CodeSystem, Organization, User  # noqa: E402

ORG_A, ORG_B = "P1271 甲卫生院", "P1271 乙卫生院"
PW, PW2 = "Passw0rd11", "Passw0rd22"


def _dup(first: int, what: str) -> str:
    return f"同批内重复：{what}与第 {first} 行相同（库里原本没有，不计「已存在」；请核对源文件）"


@pytest.fixture(scope="module", autouse=True)
def world(client):   # client：重建库表、种好 admin 与字典类型
    with SessionLocal() as db:
        db.add_all([Organization(name=ORG_A, org_type="township", level="township"),
                    Organization(name=ORG_B, org_type="township", level="township")])
        db.commit()


def _csv(tmp_path, name: str, header: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text(header + "\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _exit_code(monkeypatch, module, *argv: str) -> int:
    """按命令行跑一遍脚本的 `main()`，拿退出码。"""
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", *argv])
    return module.main()


def test_建号_同一文件同一个登录名_后一位记错误行点名第2行_退出码1(tmp_path, monkeypatch):
    header = "username,full_name,role,org_name,password"
    assert import_users.run_import(_csv(tmp_path, "old.csv", header, f"p1271_old,老王,operator,,{PW}")).imported == 1
    path = _csv(tmp_path, "users.csv", header,
                f"p1271_zw,张伟,doctor,{ORG_A},{PW}",
                f"p1271_zw,张伟,public_health,{ORG_B},{PW2}",
                f"p1271_old,老王,operator,,{PW}")
    dry = import_users.run_import(path, dry_run=True)
    # 修前 (1, 2, [])：第二位张伟与库里原有的老王一并计成「幂等跳过(用户名已存在)」
    assert (dry.imported, dry.skipped, dry.errors) == (1, 1, [(3, _dup(2, "用户名"))])
    assert _exit_code(monkeypatch, import_users, str(path)) == 1   # 修前 0
    with SessionLocal() as db:
        rows = db.query(User.role, Organization.name).join(Organization, User.org_id == Organization.id).filter(
            User.username == "p1271_zw").all()
        assert rows == [("doctor", ORG_A)]   # 只建了第一位；第二位等源文件改了登录名再导
        assert db.query(User).filter(User.username == "p1271_old").one().role == "operator"   # 库里原有的照旧幂等跳过


def test_收费目录_同一文件同一编码两个价_后一行记错误行点名第2行_退出码1(tmp_path, monkeypatch):
    header = "code,name,category,price,active"
    assert import_charge_items.run_import(_csv(tmp_path, "old.csv", header, "P1271-OLD,老项目,exam,8.00,true")).imported == 1
    path = _csv(tmp_path, "charge.csv", header,
                "P1271-310300001,心电图,exam,12.00,true",
                "P1271-310300001,心电图,exam,25.00,true",
                "P1271-OLD,老项目,exam,9.00,true")
    dry = import_charge_items.run_import(path, dry_run=True)
    assert (dry.imported, dry.skipped, dry.errors) == (1, 1, [(3, _dup(2, "编码"))])   # 修前 (1, 2, [])
    assert _exit_code(monkeypatch, import_charge_items, str(path)) == 1   # 修前 0
    with SessionLocal() as db:
        assert [p for (p,) in db.query(ChargeItem.price).filter(ChargeItem.code == "P1271-310300001")] == [12.0]
        assert db.query(ChargeItem).filter(ChargeItem.code == "P1271-OLD").one().price == 8.0   # 照旧不覆盖价格


def test_字典_同一文件同一编码两行_后一行记错误行点名第2行_退出码1(tmp_path, monkeypatch):
    header = "code,name,spec"
    quiet = {"progress_every": 0, "out": lambda *_: None}
    assert import_dictionary.run_import("drug", _csv(tmp_path, "old.csv", header, "P1271-OLD,老药,1g"), **quiet).imported == 1
    path = _csv(tmp_path, "dict.csv", header,
                "P1271-Y001,二甲双胍片,0.25g",
                "P1271-Y001,二甲双胍缓释片,0.5g",
                "P1271-OLD,老药改名,2g")
    dry = import_dictionary.run_import("drug", path, dry_run=True, **quiet)
    assert (dry.imported, dry.skipped, dry.errors) == (1, 1, [(3, _dup(2, "编码"))])   # 修前 (1, 2, [])
    assert _exit_code(monkeypatch, import_dictionary, "drug", str(path), "--progress-every", "0") == 1   # 修前 0
    with SessionLocal() as db:
        system_id = db.query(CodeSystem.id).filter(CodeSystem.code == "drug").scalar()
        entries = {code: (name, spec) for code, name, spec in db.query(CodeEntry.code, CodeEntry.name, CodeEntry.spec).filter(
            CodeEntry.system_id == system_id, CodeEntry.code.in_(["P1271-Y001", "P1271-OLD"]))}
        assert entries == {"P1271-Y001": ("二甲双胍片", "0.25g"), "P1271-OLD": ("老药", "1g")}


def test_字典HTTP导入_回执把库里已有与本批重复分开_点名重复编码(client, admin):
    old = client.post("/api/dictionaries/drug/import", headers=admin, json=[{"code": "P1271-H-OLD", "name": "老药"}])
    assert old.status_code == 200, old.text
    resp = client.post("/api/dictionaries/drug/import", headers=admin, json=[
        {"code": "P1271-X001", "name": "阿莫西林胶囊 0.25g"},
        {"code": "P1271-X001", "name": "阿莫西林胶囊 0.5g"},
        {"code": "P1271-H-OLD", "name": "老药改名"},
        {"code": "P1271-X001", "name": "阿莫西林胶囊 1g"},
    ])
    assert resp.status_code == 200, resp.text
    # 修前 {'imported': 1, 'skipped': 3}：重复的两条与库里已有的一条混在「跳过（编码已存在）」里。先到为准的口径不变
    assert resp.json() == {"imported": 1, "skipped": 3, "skipped_existing": 1, "skipped_duplicate": 2,
                           "duplicate_codes": ["P1271-X001"]}
    rows = client.get("/api/dictionaries/drug/entries?keyword=P1271-X001", headers=admin).json()
    assert [(e["code"], e["name"]) for e in rows] == [("P1271-X001", "阿莫西林胶囊 0.25g")]


def test_村医批量开通_同批同一账号两条_后一条点名第1条_不说已建档(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1271 东村卫生室", "org_type": "village", "level": "village"})
    assert org.status_code in (200, 201), org.text
    user = client.post("/api/users", headers=admin, json={
        "username": "p1271_vd", "password": "passw0rd1", "role": "doctor", "org_id": org.json()["id"],
        "full_name": "李村医"})
    assert user.status_code in (200, 201), user.text
    uid, oid = user.json()["id"], org.json()["id"]
    resp = client.post("/api/spd/village-doctors/batch", headers=admin, json={"items": [
        {"user_id": uid, "org_id": oid, "village": "东村"}, {"user_id": uid, "org_id": oid, "village": "西村"}]})
    assert resp.status_code == 200, resp.text
    # 修前 reason「已建档」：第一条刚建的档也算库里原有的
    assert resp.json() == {"created": 1, "skipped": [
        {"user_id": uid, "reason": "同批内重复：账号与第 1 条相同（库里原本没有，不计「已建档」；请核对）"}]}
    again = client.post("/api/spd/village-doctors/batch", headers=admin, json={"items": [
        {"user_id": uid, "org_id": oid, "village": "西村"}]})
    assert again.json() == {"created": 0, "skipped": [{"user_id": uid, "reason": "已建档"}]}   # 库里原有的照旧
    listed = client.get(f"/api/spd/village-doctors?org_id={oid}", headers=admin).json()
    assert [(v["user_id"], v["village"]) for v in listed] == [(uid, "东村")]
