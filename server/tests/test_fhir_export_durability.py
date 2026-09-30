"""FHIR 批量导出：产出文件与 manifest 落盘（fsync）先于推进水位（P2-1114，第三十二批扫描 B1-5）。

修前 `run_fhir_batch_export` 里的 `_export` 用 `open("w")` 写 NDJSON、`open("a")` 追加 manifest，随即 `_wm_set`
提交水位（`upsert_unique` 当场 commit），全程没有一次 fsync（扫描计数：导出 1 条，`os.fsync` 被调用 0 次，水位已
提交）。水位推进与归档任务的删行是同一个角色：推进之后，这批行不会再被导出。文件和 manifest 却还停在页缓存里——
此时主机掉电或内核崩溃，重启后文件缺失或为空、manifest 少行，下一轮只导水位之后的新增，这批患者、就诊、报告永远
到不了省平台，也没有任何地方记得它们没送到。同目录下的归档任务（`jobs._archive_and_delete`）的规矩是「落盘先于
删行」「只 flush 不 fsync 等于没落盘」。

修后：NDJSON 写进同目录临时文件、`jobs._fsync`、`os.replace` 换名；manifest 追加后 `jobs._fsync`；然后才提交水位。

掉电在进程里复现不了，这里验它的前提：给 `os.fsync` 打桩，记下每次落盘的（inode, 字节数）；每次提交水位的那一刻，
产出目录里的每个文件（各 NDJSON 与 manifest）都必须已按当时的字节数落过盘。fsync 抛错时水位不得前进。

变异检查（均实测）：
- 换回修前的 `_export` → 两条全红（「提交水位 fhir_export_wm_patient 时这些文件还没落盘：[Patient_….ndjson,
  manifest.jsonl]」「落盘失败，水位却推进到了 2」）；
- 只去掉 manifest 的 fsync → 第一条红（只剩 manifest.jsonl 没落盘）；
- 只去掉 NDJSON 的 fsync → 两条全红；
- 只去掉失败时清临时文件那一句 → 第二条红（目录里留下 `.Patient_….tmp`）。
"""
import errno
import os

import pytest

from app.config import settings
from app.database import SessionLocal
from app.routers import integration
from app.routers.integration import FHIR_EXPORT_WM_KEYS, run_fhir_batch_export


@pytest.fixture()
def out_dir(tmp_path, monkeypatch):
    """导出落临时目录：settings 是进程单例，monkeypatch 属性即可（自动还原）。"""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    return tmp_path / "fhir_out"


@pytest.fixture(scope="module")
def world(client, admin):
    """患者、就诊、检查报告各一份：新增导出产三个 NDJSON，报告再修订一次产 amended 那一个。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21114 卫生院", "org_type": "township", "level": "township"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "褚导出", "id_card": "330281198802024012", "gender": "男", "birth_date": "1988-02-02"}).json()
    enc = client.post("/api/integration/fhir/Encounter", headers=admin, json={
        "resourceType": "Encounter",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"reference": f"Patient/{patient['ehc_no']}"},
        "serviceProvider": {"reference": f"Organization/{org['id']}"},
        "reasonCode": [{"coding": [{"system": "icd-10", "code": "J06.9"}], "text": "急性上呼吸道感染"}],
    })
    assert enc.status_code == 201, enc.text
    request = client.post("/api/exams", headers=admin, json={
        "patient_id": patient["id"], "from_org_id": org["id"], "center_type": "imaging",
        "item_code": "CT01", "item_name": "胸部CT"}).json()
    report = client.post("/api/integration/fhir/DiagnosticReport", headers=admin, json={
        "resourceType": "DiagnosticReport", "status": "final", "conclusion": "未见明显异常",
        "basedOn": [{"reference": f"ServiceRequest/{request['id']}"}]})
    assert report.status_code == 201, report.text
    return {"report_id": report.json()["report_id"]}


def _export() -> None:
    with SessionLocal() as db:
        run_fhir_batch_export(db)


def test_每次提交水位之前_产出目录里的每个文件都已落盘(client, admin, world, out_dir, monkeypatch):
    synced: set[tuple[int, int]] = set()          # 落过盘的（inode, 字节数）
    real_fsync = os.fsync

    def recording_fsync(fd):
        real_fsync(fd)
        st = os.fstat(fd)
        synced.add((st.st_ino, st.st_size))

    commits: list[tuple[str, dict[str, bool]]] = []   # 每次提交水位时：（水位 key，{文件名: 是否已按当时字节数落盘}）
    real_wm_set = integration._wm_set

    def checking_wm_set(db, key, value):
        states = {f.name: f.stat() for f in out_dir.iterdir()}
        commits.append((key, {name: (st.st_ino, st.st_size) in synced for name, st in states.items()}))
        real_wm_set(db, key, value)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(integration, "_wm_set", checking_wm_set)

    _export()                                       # 新增：Patient / Encounter / DiagnosticReport 各一个文件
    revised = client.patch(f"/api/exams/reports/{world['report_id']}", headers=admin, json={
        "conclusion": "右肺下叶小结节，建议随访", "reason": "复核修订"})
    assert revised.status_code == 200, revised.text
    _export()                                       # 修订再导：DiagnosticReport_amended 一个文件，外加修订史水位

    assert {key for key, _ in commits} == set(FHIR_EXPORT_WM_KEYS.values())   # 四个水位都推进过，判据没有空转
    last = commits[-1][1]
    assert "manifest.jsonl" in last and sum(name.endswith(".ndjson") for name in last) == 4, sorted(last)
    first_bad = next(((key, sorted(name for name, ok in files.items() if not ok))
                      for key, files in commits if not all(files.values())), None)
    assert first_bad is None, f"提交水位 {first_bad[0]} 时这些文件还没落盘：{first_bad[1]}"


def test_fsync_抛错时水位不前进_目录里也不留半成品(client, admin, world, out_dir, monkeypatch):
    created = client.post("/api/patients", headers=admin, json={
        "name": "卫断电", "id_card": "330281199203034016", "gender": "女", "birth_date": "1992-03-03"})
    assert created.status_code in (200, 201), created.text
    key = FHIR_EXPORT_WM_KEYS["Patient"]
    with SessionLocal() as db:
        before = integration._wm_get(db, key)
    assert before < created.json()["id"]            # 有增量可导，判据不是空转

    def failing_fsync(fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    raised = None
    try:
        _export()
    except OSError as exc:
        raised = exc
    with SessionLocal() as db:
        after = integration._wm_get(db, key)
    assert after == before, f"落盘失败，水位却推进到了 {after}（修前根本不 fsync，照样提交）"
    assert raised is not None                       # 照实失败，任务记失败、下一轮重导，而不是报成功
    assert sorted(p.name for p in out_dir.iterdir()) == []   # 没落成的 NDJSON 不留、manifest 不添行
