"""存量导入 dry-run 不再把每行的 ORM 对象留在会话里直到最后回滚（P2-1155，第三十三批扫描 A4-6）。

修前：`ImportContext.checkpoint` 在 dry-run 下既不提交也不 flush、不逐出，`SessionLocal` 又是 autoflush=False——每导入
一行建的 ORM 对象都留在会话里，全部跑完才回滚。就诊 2 万 → 6 万行 dry-run 峰值 36 → 106 MB（约 1.76 KB/行），同样 6 万
行实导只要 24 MB；几百万行的迁移预检会在迁移机上 OOM。修后 dry-run 满一批写进事务（flush）再清出会话（expunge_all），
最后整个回滚；写进事务还顺带让 dry-run 与实导一样撞得到库侧约束。

判重的两个集合（同一文件里见过的键、库里原有的键）照旧留着：前者要报「与第几行相同」，实导也一样占着。所以量的是
「dry-run 与实导同一个量级、按行数的增量也与实导相当」，而不是「完全不涨」。报告数字（将导入 / 跳过 / 错误明细）与
实导逐条一致，dry-run 不落库。
"""
import csv
import sys
import tracemalloc
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal, engine
from app.models import Admission, Bed, Encounter, Organization, Patient, User, Ward

ORG = "P21155 卫生院"
CARDS = [f"33012719900101{i:04d}" for i in range(40)]
ENCOUNTER_HEADER = "id_card,org_name,visit_date,encounter_type,doctor_name,diagnosis_code,diagnosis_name"


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()
    with SessionLocal() as db:
        db.add(User(username="admin", password_hash="x", role="admin"))   # 经办账号（created_by），不经 app 启动
        db.add(Organization(name=ORG, org_type="township", level="township"))
        for i, card in enumerate(CARDS):
            db.add(Patient(name=f"P21155 {i}", id_card=card, ehc_no=f"EHC-P21155-{i}"))
        db.commit()


def _csv(tmp_path, name: str, header: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text(header + "\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _encounter_lines(n: int, start: int = 0) -> list[str]:
    """n 行互不相同的就诊（患者 × 就诊日期各不相同），全都该导入。"""
    return [
        f"{CARDS[i % len(CARDS)]},{ORG},{(date(2020, 1, 1) + timedelta(days=i // len(CARDS))).isoformat()},"
        "outpatient,医生,I10,高血压"
        for i in range(start, start + n)
    ]


def _count(model) -> int:
    with SessionLocal() as db:
        return db.query(model).count()


def _numbers(rep) -> tuple:
    return rep.imported, rep.skipped, rep.filled, rep.errors


@contextmanager
def _write_fails_on(table: str, column: str, value: str):
    """这一行写进库时失败（SQLite 触发器 RAISE(ABORT)）：模拟库侧约束，写法同 `test_import_legacy_text_width`。"""
    name = f"p21155_boom_{table}"
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON {table} WHEN NEW.{column} = '{value}' "
            "BEGIN SELECT RAISE(ABORT, 'P21155 模拟写库失败'); END"
        ))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))


def test_就诊_dry_run报告与实导逐条一致_且不落库(tmp_path):
    """每 3 行一批，跨好几个批界：导入、库里已有的幂等跳过、同一文件里的重复、患者不存在、日期非法都在里头。"""
    valid = _encounter_lines(10)
    run_import("encounters", _csv(tmp_path, "old.csv", ENCOUNTER_HEADER, valid[0], valid[5]))   # 库里原有两条
    before = _count(Encounter)
    path = _csv(
        tmp_path, "mix.csv", ENCOUNTER_HEADER,
        *valid[:4], valid[1],                                     # 第 6 行与第 3 行相同
        f"330127199001019999,{ORG},2026-01-01,outpatient,医生,I10,高血压",   # 患者不存在
        f"{CARDS[0]},{ORG},2026-02-30,outpatient,医生,I10,高血压",           # 日历上没有的日子
        *valid[4:],
    )
    dry = run_import("encounters", path, dry_run=True, batch_size=3)
    assert _count(Encounter) == before   # dry-run 不落库
    real = run_import("encounters", path, batch_size=3)
    assert _numbers(dry) == _numbers(real)
    assert (dry.imported, dry.skipped, [line for line, _ in dry.errors]) == (8, 2, [6, 7, 8])


def test_机构上级跨批_dry_run报告与实导一致(tmp_path):
    """每行一批：下级在后几批才出现，上级的 id 是前几批 flush 出来的——清出会话后照样认得。"""
    path = _csv(
        tmp_path, "orgs.csv", "name,org_type,level,parent_name,address",
        "P21155 县医院,lead_hospital,county,,", "P21155 甲镇,township,township,P21155 县医院,",
        "P21155 甲村,village,village,P21155 甲镇,", "P21155 乙村,village,village,P21155 甲镇,",
    )
    dry = run_import("organizations", path, dry_run=True, batch_size=1)
    assert _count(Organization) == 1
    real = run_import("organizations", path, batch_size=1)
    assert _numbers(dry) == _numbers(real) == (4, 0, 0, [])


def test_住院就地建的病区床位跨批复用_dry_run报告与实导一致(tmp_path):
    """每 2 行一批：病区、床位在第一批就地建，后几批按 id 复用；同床两条在院照样报错。"""
    path = _csv(
        tmp_path, "adm.csv", "id_card,org_name,ward_name,bed_no,admitted_at,discharged_at,diagnosis_name",
        f"{CARDS[0]},{ORG},P21155 内科,1,2026-09-01,,肺炎", f"{CARDS[1]},{ORG},P21155 内科,2,2026-09-01,,肺炎",
        f"{CARDS[2]},{ORG},P21155 内科,3,2026-09-02,,肺炎", f"{CARDS[3]},{ORG},P21155 内科,1,2026-09-02,,肺炎",
        f"{CARDS[4]},{ORG},P21155 内科,1,2026-08-01,2026-08-05,肺炎",
    )
    dry = run_import("admissions", path, dry_run=True, batch_size=2)
    assert (_count(Admission), _count(Ward), _count(Bed)) == (0, 0, 0)
    real = run_import("admissions", path, batch_size=2)
    assert _numbers(dry) == _numbers(real)
    assert (dry.imported, [line for line, _ in dry.errors]) == (4, [5])   # 第 5 行：1 床已有人在院


@pytest.mark.parametrize("where", ["满一批时", "最后不满一批"])
def test_dry_run撞得到库侧约束_说明照P2_1094(tmp_path, where):
    """修前 dry-run 从不 flush 就诊：库侧约束（这里用触发器模拟）要到实导批末提交时才撞上，预检报「错误: 0 行」。"""
    lines = _encounter_lines(5, start=400)   # 就诊日期都是 2020-01-11，与别的用例错开
    boom = 1 if where == "满一批时" else 4   # 每 2 行一批：第 3 行在第一批里，第 6 行在最后不满的那一批里
    lines[boom] = lines[boom].replace(",I10,", ",P21155BOOM,")
    path = _csv(tmp_path, "boom.csv", ENCOUNTER_HEADER, *lines)
    out: list[str] = []
    with _write_fails_on("encounters", "diagnosis_code", "P21155BOOM"), pytest.raises(IntegrityError) as caught:
        run_import("encounters", path, dry_run=True, batch_size=2, out=out.append)
    end = 3 if where == "满一批时" else 6
    assert f"第 2～{end} 行校验到一半失败（dry-run 不落库），第 {end} 行之后未校验" in caught.value.__notes__[0]
    assert "【校验模式 dry-run，未落库】" in out[0] and "IntegrityError: P21155 模拟写库失败" in out[0]
    with SessionLocal() as db:
        assert db.query(Encounter).filter(Encounter.created_at == datetime(2020, 1, 11)).count() == 0


def _peak_bytes(**kwargs) -> tuple[int, object]:
    tracemalloc.start()
    try:
        rep = run_import("encounters", progress_every=0, batch_size=200, out=lambda *_: None, **kwargs)
        return tracemalloc.get_traced_memory()[1], rep
    finally:
        tracemalloc.stop()


def _delete_encounters_from(first_day: date) -> None:
    with SessionLocal() as db:
        db.query(Encounter).filter(Encounter.created_at >= datetime.combine(first_day, datetime.min.time())).delete(
            synchronize_session=False)
        db.commit()


def test_dry_run峰值内存与实导同一个量级_按行数的增量也相当(tmp_path):
    """就诊 1,000 行与 3,000 行各跑一遍 dry-run 与实导（实导完删掉，下一轮「库里原有的键」不变）。

    修前 dry-run 每行多留一个约 1.4 KB 的 ORM 对象：3,000 行时峰值是实导的数倍，按行数的增量也是实导的数倍。"""
    start = 4000   # 就诊日期从 2020-04-10 起，与上面几条用例的数据错开
    first_day = date(2020, 1, 1) + timedelta(days=start // len(CARDS))
    peaks: dict[tuple[int, bool], int] = {}
    for n in (1000, 3000):
        path = _csv(tmp_path, f"enc_{n}.csv", ENCOUNTER_HEADER, *_encounter_lines(n, start=start))
        for dry_run in (True, False):
            peak, rep = _peak_bytes(csv_path=path, dry_run=dry_run)
            assert (rep.imported, rep.errors) == (n, [])
            peaks[(n, dry_run)] = peak
            if not dry_run:
                _delete_encounters_from(first_day)
    dry_growth = peaks[(3000, True)] - peaks[(1000, True)]
    real_growth = peaks[(3000, False)] - peaks[(1000, False)]
    assert peaks[(3000, True)] < peaks[(3000, False)] * 1.25, (
        f"3,000 行：dry-run 峰值 {peaks[(3000, True)] / 1e6:.2f} MB，实导 {peaks[(3000, False)] / 1e6:.2f} MB"
        "——dry-run 又把每行的 ORM 对象留在会话里了"
    )
    assert dry_growth < real_growth * 1.3 + 200_000, (
        f"1,000 → 3,000 行：dry-run 峰值涨 {dry_growth / 1e6:.2f} MB，实导涨 {real_growth / 1e6:.2f} MB"
    )
