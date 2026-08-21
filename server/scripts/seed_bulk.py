#!/usr/bin/env python3
"""仿真规模数据生成器（生产整改 A11）：给容量验证/压测灌接近生产量级的数据。

`scripts/seed_demo.py` 造的是演示用的小数据；这里解决的是另一个问题——
"统计接口在三年数据上还快不快、连接池在真实并发下够不够"只能在**仿真规模**
的库上回答。生成的数据覆盖压测重点接口的数据面：患者检索（patients）、
运行效率/用药分析（admissions + encounters + prescriptions + bill_details）。

用法（先建好库表：`python -m alembic upgrade heads`，注意是复数 heads）：

    export MEDPLAT_DATABASE_URL=postgresql+psycopg2://user:pass@host/medplat_bench
    python scripts/seed_bulk.py --patients 100000 --encounters 1000000 \
        --admissions 100000 --prescriptions 200000 --bill-details 500000

实现要点：
- **分批 bulk_insert_mappings + 分批提交**（--batch-size，默认 5000）：
  单事务插百万行会把 WAL/内存顶爆，逐行 ORM add 则慢两个数量级；
- **幂等可续跑**：每类数据带可识别标记（ehc_no/编码前缀 `SIM-`、
  doctor_name/diagnosis_name 为"仿真…"），启动时先数已有量、从断点继续，
  Ctrl-C 或崩溃后重跑同一命令即可接着灌，不会造出重复主数据；
- **进度输出**：每批打印累计行数与速率，长任务不做哑巴；
- 时间戳按索引均匀铺开在**过去 36 个月**里，让"按月统计"类接口
  （efficiency / drug-use）在任意月份都有数据可聚合。

只往库里**增**数据（种子约定：幂等只增不改），不碰既有业务行。
禁止对生产库执行——这是造数工具，不是演示数据。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Admission,
    Bed,
    BillDetail,
    ChargeItem,
    DrugRule,
    Encounter,
    Organization,
    Patient,
    Prescription,
    PrescriptionItem,
    User,
    Ward,
)

DOCTOR_MARK = "仿真医师"
DIAG_MARK = "仿真诊断"
MONTHS_SPAN_DAYS = 36 * 30  # 时间戳铺开的跨度：约 36 个月

# 仿真收费目录/药品目录（幂等：按 code 查了再加）
SIM_CHARGE_ITEMS = [
    ("SIM-DRUG-1", "仿真药品甲", "drug", 12.5),
    ("SIM-DRUG-2", "仿真药品乙", "drug", 30.0),
    ("SIM-TRT-1", "仿真治疗项目", "treatment", 55.0),
    ("SIM-EXAM-1", "仿真检查项目", "exam", 120.0),
]
SIM_DRUG_RULES = [
    # (code, 抗菌药, DDD)——留一条 DDD=0 的抗菌药，让"未覆盖数"路径也有量
    ("SIM-ABX-1", True, 1.5),
    ("SIM-ABX-0", True, 0.0),
    ("SIM-DRUG-1", False, 1.0),
]


def _ts(base: datetime, i: int) -> datetime:
    """按索引把时间戳均匀铺开在过去 MONTHS_SPAN_DAYS 天里（确定性、可续跑）。"""
    return base - timedelta(minutes=(i * 137) % (MONTHS_SPAN_DAYS * 24 * 60))


def _progress(kind: str, done: int, total: int, started: float) -> None:
    rate = done / max(time.monotonic() - started, 1e-9)
    print(f"  {kind}: {done}/{total}（{rate:,.0f} 行/秒）", flush=True)


class BulkSeeder:
    def __init__(self, db, batch_size: int):
        self.db = db
        self.batch = batch_size
        self.base_ts = datetime.utcnow()

    # ---------------- 基础骨架（机构/病区/床位/目录，量小，直接幂等补齐） ----------------

    def ensure_backbone(self, orgs: int) -> None:
        for n in range(1, orgs + 1):
            name = f"仿真机构{n:03d}"
            org = self.db.query(Organization).filter(Organization.name == name).first()
            if org is None:
                org = Organization(name=name, org_type="township" if n > 1 else "lead_hospital",
                                   level="township" if n > 1 else "county")
                self.db.add(org)
                self.db.flush()
            ward = self.db.query(Ward).filter(Ward.org_id == org.id, Ward.name == "仿真病区").first()
            if ward is None:
                ward = Ward(org_id=org.id, name="仿真病区")
                self.db.add(ward)
                self.db.flush()
            have_beds = self.db.query(Bed).filter(Bed.ward_id == ward.id).count()
            for b in range(have_beds, 20):
                self.db.add(Bed(ward_id=ward.id, bed_no=f"SIM{b:03d}", status="free"))
        for code, name, category, price in SIM_CHARGE_ITEMS:
            if self.db.query(ChargeItem.id).filter(ChargeItem.code == code).first() is None:
                self.db.add(ChargeItem(code=code, name=name, category=category, price=price))
        for code, antibiotic, ddd in SIM_DRUG_RULES:
            if self.db.query(DrugRule.id).filter(DrugRule.drug_code == code).first() is None:
                self.db.add(DrugRule(drug_code=code, max_daily_dose=10, antibiotic=antibiotic,
                                     active=True, ddd=ddd))
        self.db.commit()
        self.orgs = [
            {"org_id": o.id, "ward_id": w.id, "bed_id": b.id}
            for o, w, b in self.db.query(Organization, Ward, Bed)
            .filter(Ward.org_id == Organization.id, Ward.name == "仿真病区",
                    Bed.ward_id == Ward.id, Bed.bed_no == "SIM000",
                    Organization.name.like("仿真机构%"))
            .all()
        ]
        creator = self.db.query(User.id).order_by(User.id).first()
        if creator is None:
            raise SystemExit("库里没有任何用户（created_by 无从取值）：请先启动过一次应用让种子跑完")
        self.creator_id = creator[0]

    # ---------------- 患者 ----------------

    def seed_patients(self, total: int) -> None:
        done = self.db.query(Patient).filter(Patient.ehc_no.like("SIM-%")).count()
        started = time.monotonic()
        while done < total:
            n = min(self.batch, total - done)
            rows = [
                {
                    "ehc_no": f"SIM-{i:012d}",
                    "name": f"仿真患者{i}",
                    "id_card": f"SIM{i:015d}",
                    "gender": "男" if i % 2 else "女",
                    "birth_date": f"{1940 + i % 80}-01-01",
                    "created_at": _ts(self.base_ts, i),
                }
                for i in range(done, done + n)
            ]
            self.db.bulk_insert_mappings(Patient, rows)
            self.db.commit()
            done += n
            _progress("patients", done, total, started)
        # 供下游外键引用的样本池（上限 20 万，控内存；引用按模循环分布）
        self._patient_ids = [
            pid for (pid,) in self.db.query(Patient.id)
            .filter(Patient.ehc_no.like("SIM-%")).limit(200_000).all()
        ]

    def _require_patients(self, kind: str) -> None:
        if not self._patient_ids:
            raise SystemExit(f"{kind} 需要仿真患者可引用：请带上 --patients N（或先跑一次患者灌注）")

    # ---------------- 就诊 ----------------

    def seed_encounters(self, total: int) -> None:
        if total:
            self._require_patients("encounters")
        done = self.db.query(Encounter).filter(Encounter.doctor_name == DOCTOR_MARK).count()
        started = time.monotonic()
        pts, orgs = self._patient_ids, self.orgs
        while done < total:
            n = min(self.batch, total - done)
            rows = [
                {
                    "patient_id": pts[i % len(pts)],
                    "org_id": orgs[i % len(orgs)]["org_id"],
                    "doctor_name": DOCTOR_MARK,
                    "encounter_type": "outpatient",
                    "diagnosis_name": DIAG_MARK,
                    "created_at": _ts(self.base_ts, i),
                }
                for i in range(done, done + n)
            ]
            self.db.bulk_insert_mappings(Encounter, rows)
            self.db.commit()
            done += n
            _progress("encounters", done, total, started)
        self._encounters = [
            {"id": eid, "org_id": oid, "patient_id": pid}
            for eid, oid, pid in self.db.query(
                Encounter.id, Encounter.org_id, Encounter.patient_id
            ).filter(Encounter.doctor_name == DOCTOR_MARK).limit(200_000).all()
        ]

    # ---------------- 住院 ----------------

    def seed_admissions(self, total: int) -> None:
        self._require_patients("admissions")
        done = self.db.query(Admission).filter(Admission.doctor_name == DOCTOR_MARK).count()
        started = time.monotonic()
        pts, orgs = self._patient_ids, self.orgs
        while done < total:
            n = min(self.batch, total - done)
            rows = []
            for i in range(done, done + n):
                site = orgs[i % len(orgs)]
                admitted = _ts(self.base_ts, i)
                discharged = admitted + timedelta(days=1 + i % 15) if i % 5 else None  # 20% 在院
                rows.append({
                    "patient_id": pts[i % len(pts)],
                    "org_id": site["org_id"],
                    "ward_id": site["ward_id"],
                    "bed_id": site["bed_id"],
                    "doctor_name": DOCTOR_MARK,
                    "diagnosis_name": DIAG_MARK,
                    "status": "admitted" if discharged is None else "discharged",
                    "admitted_at": admitted,
                    "discharged_at": discharged,
                    "created_by": self.creator_id,
                })
            self.db.bulk_insert_mappings(Admission, rows)
            self.db.commit()
            done += n
            _progress("admissions", done, total, started)

    # ---------------- 处方（含明细，供 drug-use 聚合） ----------------

    def seed_prescriptions(self, total: int) -> None:
        self._require_patients("prescriptions")
        done = self.db.query(Prescription).filter(Prescription.diagnosis_name == DIAG_MARK).count()
        started = time.monotonic()
        pts, orgs = self._patient_ids, self.orgs
        while done < total:
            n = min(self.batch, total - done)
            rx_rows = [
                {
                    "patient_id": pts[i % len(pts)],
                    "org_id": orgs[i % len(orgs)]["org_id"],
                    "diagnosis_name": DIAG_MARK,
                    "status": "auto_passed",
                    "created_by": self.creator_id,
                    "created_at": _ts(self.base_ts, i),
                }
                for i in range(done, done + n)
            ]
            self.db.bulk_insert_mappings(Prescription, rx_rows)
            self.db.flush()
            # 续跑安全：明细按刚插入区间的处方 id 反查生成（每方 2 条）
            new_ids = [
                rid for (rid,) in self.db.query(Prescription.id)
                .filter(Prescription.diagnosis_name == DIAG_MARK)
                .order_by(Prescription.id.desc()).limit(n).all()
            ]
            item_rows = []
            for j, rid in enumerate(new_ids):
                item_rows.append({"prescription_id": rid, "drug_code": "SIM-ABX-1",
                                  "drug_name": "仿真抗菌药", "daily_dose": 1.0 + j % 3,
                                  "days": 1 + j % 7})
                item_rows.append({"prescription_id": rid, "drug_code": "SIM-DRUG-1",
                                  "drug_name": "仿真药品甲", "daily_dose": 2.0, "days": 3})
            self.db.bulk_insert_mappings(PrescriptionItem, item_rows)
            self.db.commit()
            done += n
            _progress("prescriptions", done, total, started)

    # ---------------- 费用明细（供门诊药占比聚合） ----------------

    def seed_bill_details(self, total: int) -> None:
        done = self.db.query(BillDetail).filter(BillDetail.item_code.like("SIM-%")).count()
        started = time.monotonic()
        encs = self._encounters
        if not encs:
            print("  bill_details: 跳过（没有仿真就诊可挂）", flush=True)
            return
        items = SIM_CHARGE_ITEMS
        while done < total:
            n = min(self.batch, total - done)
            rows = []
            for i in range(done, done + n):
                enc = encs[i % len(encs)]
                code, name, _category, price = items[i % len(items)]
                rows.append({
                    "patient_id": enc["patient_id"],
                    "encounter_id": enc["id"],
                    "item_code": code,
                    "item_name": name,
                    "unit_price": price,
                    "quantity": 1 + i % 3,
                    "amount": price * (1 + i % 3),
                    "created_by": self.creator_id,
                    "created_at": _ts(self.base_ts, i),
                })
            self.db.bulk_insert_mappings(BillDetail, rows)
            self.db.commit()
            done += n
            _progress("bill_details", done, total, started)


def main() -> int:
    parser = argparse.ArgumentParser(description="仿真规模数据生成（幂等可续跑，禁止对生产库执行）")
    parser.add_argument("--orgs", type=int, default=25, help="仿真机构数（默认25，医共体典型规模）")
    parser.add_argument("--patients", type=int, default=0)
    parser.add_argument("--encounters", type=int, default=0)
    parser.add_argument("--admissions", type=int, default=0)
    parser.add_argument("--prescriptions", type=int, default=0)
    parser.add_argument("--bill-details", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        seeder = BulkSeeder(db, args.batch_size)
        print("准备骨架（机构/病区/床位/目录）…", flush=True)
        seeder.ensure_backbone(args.orgs)
        seeder.seed_patients(args.patients)
        if args.encounters or args.bill_details:
            seeder.seed_encounters(args.encounters)
        if args.admissions:
            seeder.seed_admissions(args.admissions)
        if args.prescriptions:
            seeder.seed_prescriptions(args.prescriptions)
        if args.bill_details:
            seeder.seed_bill_details(args.bill_details)
        print("完成。", flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
