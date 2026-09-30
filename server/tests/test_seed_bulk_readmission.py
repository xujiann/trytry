"""仿真灌数：住院数多于患者数时不再给同一患者灌两条「在院」（P2-1090，第三十一批「运维脚本与后台任务」扫描 C2-11）。

`seed_bulk.seed_admissions` 的患者按 `i % 患者数` 复用，每逢 `i % 5 == 0` 就灌一条在院：`--patients 1000 --admissions 3000`
时同一个人两条在院，撞 `uq_admission_patient_admitted`、整批回滚，「幂等可续跑」重跑停在原地、住院表 0 行。修后已经在院的
（库里的、本轮灌过的）这一次按出院灌。用一个独立的临时库跑，不往共用的测试库里灌仿真数据。
"""
import sys
from pathlib import Path

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Admission, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from seed_bulk import BulkSeeder  # noqa: E402


def test_住院数是患者数的五倍也灌得完_每人至多一条在院(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bulk.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        db.add(User(username="p21090-admin", password_hash="x", role="admin"))
        db.commit()
        seeder = BulkSeeder(db, batch_size=7)
        seeder.ensure_backbone(1)
        seeder.seed_patients(3)
        seeder.seed_admissions(15)   # 修前 IntegrityError：UNIQUE constraint failed: admissions.patient_id
        assert db.query(func.count(Admission.id)).scalar() == 15
        per_patient = (db.query(Admission.patient_id, func.count(Admission.id))
                       .filter(Admission.status == "admitted").group_by(Admission.patient_id).all())
        assert per_patient and all(count == 1 for _, count in per_patient), per_patient
        seeder.seed_admissions(20)   # 续跑（更大的总数）照样灌得完
        assert db.query(func.count(Admission.id)).scalar() == 20
    engine.dispose()
