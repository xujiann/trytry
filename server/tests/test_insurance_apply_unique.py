"""医保两类待遇申报"业务上唯一、库上原本没约束"的不变式回归（P1-30）。

洞的形状比 P1-29 那三条更直白：`apply_special_disease` / `apply_dual_channel`
**连预检都没有**，是彻头彻尾的直插。同一个患者、同一个病种（同一种药）连点两次
提交，库里就静默躺下两条待批申报——不报错、两个回执看上去都成功，最后要管理层
去人工分辨"哪条才是真的、该驳回哪条"。并发只是让它更容易发生，顺序双击本来就
已经能写出两条。

迁移 `b9c8d7e6f5a4` 把两条不变式下沉成**部分唯一索引**（分别只锁
`status='applied'` 与 `status='pending'` 一态），接口层改走 `insert_or_conflict`。
本档钉三件事：

1. **行为面**：重复提交拿到 409，且文案与并发抢输者逐字节一致——对调用方来说
   "本来就重复"与"并发撞车"没有区别（走的是同一条代码路径，这也是不加预检的
   理由：两条路径迟早会给出两种文案）。
2. **部分性**：写成全量唯一会把"驳回后重新申报""待遇期满再认定"这种正常业务
   一并拒掉，那是另一种坏。所以逐条钉住"出了 applied/pending 这一态就放行"，
   以及换病种/换药/换患者互不牵连。
3. **防拆卸**：索引必须留在模型上、也必须真建在库上；再绕开接口层直插一次，
   看数据库自己是否抬手——SQLite 的库级写锁让线程探针对"索引被拆掉"不敏感，
   静态钉 + 直插才是确定性的网。真并发的取证在
   `tests/test_insurance_apply_unique_races.py`（真 PG，默认跳过）。
"""
import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, engine
from app.models import Base, DualChannelApp, SpecialDiseaseApp

# 两条文案的唯一副本：接口层、顺序重复、并发抢输三处必须读起来一模一样。
SPECIAL_409 = "该患者同病种已有待审核的特病申报，不可重复申报"
DUAL_409 = "该患者该药品已有待审核的双通道申报，请先由管理层审核后再申报"


def _patient(client, admin, name, id_card):
    resp = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1980-01-01"},
        headers=admin,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _apply_special(client, admin, patient_id, disease_name, reason="规律血透"):
    return client.post(
        "/api/insurance/special-diseases",
        json={"patient_id": patient_id, "disease_name": disease_name, "reason": reason},
        headers=admin,
    )


def _apply_dual(client, admin, patient_id, drug_name, reason="类风湿"):
    return client.post(
        "/api/insurance/dual-channel",
        json={"patient_id": patient_id, "drug_name": drug_name, "reason": reason},
        headers=admin,
    )


# ================================================================ 特病申报（applied 唯一）


def test_特病同患者同病种重复申报409且库里只留一条待批(client, admin):
    patient = _patient(client, admin, "特病唯一甲", "330281199001010011")

    first = _apply_special(client, admin, patient["id"], "尿毒症透析")
    assert first.status_code == 201, first.text
    # 回执形状不许因为改写入口而变（治理不得改响应字节）
    assert list(first.json()) == ["patient_id", "disease_name", "reason", "id", "status"]
    assert first.json()["status"] == "applied"

    second = _apply_special(client, admin, patient["id"], "尿毒症透析")
    assert second.status_code == 409, second.text
    assert second.json() == {"detail": SPECIAL_409}

    listed = client.get("/api/insurance/special-diseases?status=applied", headers=admin).json()
    mine = [r for r in listed if r["patient_id"] == patient["id"]]
    assert len(mine) == 1, f"同患者同病种应只剩一条待批，实际 {mine}"
    assert mine[0]["id"] == first.json()["id"]


def test_特病驳回后可以重新申报(client, admin):
    """部分索引只锁 applied 一态：驳回之后这个键就该重新可用。

    写成全量唯一这条会红——而"驳回后重新申报"是特病认定最正常不过的流程。
    """
    patient = _patient(client, admin, "特病唯一乙", "330281199001010012")
    first = _apply_special(client, admin, patient["id"], "恶性肿瘤门诊放化疗")
    assert first.status_code == 201, first.text

    reviewed = client.post(
        f"/api/insurance/special-diseases/{first.json()['id']}/review?approve=false",
        headers=admin,
    )
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "rejected"

    again = _apply_special(client, admin, patient["id"], "恶性肿瘤门诊放化疗")
    assert again.status_code == 201, again.text
    assert again.json()["id"] != first.json()["id"]


def test_特病批准后仍可再次申报(client, admin):
    """待遇期满后的再认定：批准的那条留作历史，新的一条照常受理。"""
    patient = _patient(client, admin, "特病唯一丙", "330281199001010013")
    first = _apply_special(client, admin, patient["id"], "重性精神病")
    assert first.status_code == 201, first.text
    assert client.post(
        f"/api/insurance/special-diseases/{first.json()['id']}/review?approve=true",
        headers=admin,
    ).json()["status"] == "approved"

    again = _apply_special(client, admin, patient["id"], "重性精神病")
    assert again.status_code == 201, again.text

    listed = client.get("/api/insurance/special-diseases", headers=admin).json()
    mine = [r for r in listed if r["patient_id"] == patient["id"]]
    assert sorted(r["status"] for r in mine) == ["applied", "approved"]


def test_特病换病种或换患者都不受牵连(client, admin):
    """键是 (patient_id, disease_name)：只锁这一对，别的都该照常放行。"""
    patient = _patient(client, admin, "特病唯一丁", "330281199001010014")
    other = _patient(client, admin, "特病唯一戊", "330281199001010015")
    assert _apply_special(client, admin, patient["id"], "尿毒症透析").status_code == 201
    assert _apply_special(client, admin, patient["id"], "血友病").status_code == 201
    assert _apply_special(client, admin, other["id"], "尿毒症透析").status_code == 201


# ================================================================ 双通道申报（pending 唯一）


def test_双通道同患者同药品重复申报409且库里只留一条待审(client, admin):
    patient = _patient(client, admin, "双通道唯一甲", "330281199001010021")

    first = _apply_dual(client, admin, patient["id"], "阿达木单抗")
    assert first.status_code == 201, first.text
    # 申报回执是 3 键的独立形状（见 DualChannelCreatedOut 的注释），不许走样
    assert list(first.json()) == ["id", "status", "drug_name"]
    assert first.json()["status"] == "pending"

    second = _apply_dual(client, admin, patient["id"], "阿达木单抗")
    assert second.status_code == 409, second.text
    assert second.json() == {"detail": DUAL_409}

    listed = client.get("/api/insurance/dual-channel?status=pending", headers=admin).json()
    mine = [r for r in listed if r["patient_id"] == patient["id"]]
    assert len(mine) == 1, f"同患者同药品应只剩一条待审核，实际 {mine}"
    assert mine[0]["id"] == first.json()["id"]


def test_双通道驳回后可以重新申报(client, admin):
    patient = _patient(client, admin, "双通道唯一乙", "330281199001010022")
    first = _apply_dual(client, admin, patient["id"], "诺西那生钠")
    assert first.status_code == 201, first.text

    reviewed = client.post(
        f"/api/insurance/dual-channel/{first.json()['id']}/review?approve=false&comment=资料不全",
        headers=admin,
    )
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "rejected"

    again = _apply_dual(client, admin, patient["id"], "诺西那生钠")
    assert again.status_code == 201, again.text
    assert again.json()["id"] != first.json()["id"]


def test_双通道通过后仍可再次申报(client, admin):
    patient = _patient(client, admin, "双通道唯一丙", "330281199001010023")
    first = _apply_dual(client, admin, patient["id"], "利妥昔单抗")
    assert first.status_code == 201, first.text
    assert client.post(
        f"/api/insurance/dual-channel/{first.json()['id']}/review?approve=true&comment=符合条件",
        headers=admin,
    ).json()["status"] == "approved"

    again = _apply_dual(client, admin, patient["id"], "利妥昔单抗")
    assert again.status_code == 201, again.text


def test_双通道换药品或换患者都不受牵连(client, admin):
    patient = _patient(client, admin, "双通道唯一丁", "330281199001010024")
    other = _patient(client, admin, "双通道唯一戊", "330281199001010025")
    assert _apply_dual(client, admin, patient["id"], "阿达木单抗").status_code == 201
    assert _apply_dual(client, admin, patient["id"], "托珠单抗").status_code == 201
    assert _apply_dual(client, admin, other["id"], "阿达木单抗").status_code == 201


# ================================================================ 防拆卸静态钉


@pytest.mark.parametrize(
    "table,index_name,columns,where_fragment",
    [
        ("special_disease_apps", "uq_special_disease_app_applied",
         ["patient_id", "disease_name"], "applied"),
        ("dual_channel_apps", "uq_dual_channel_pending",
         ["patient_id", "drug_name"], "pending"),
    ],
)
def test_两条申报不变式的部分唯一索引不许消失(table, index_name, columns, where_fragment):
    """模型侧的声明就是这两条不变式的落点，删掉就等于把静默双写的洞放回去。

    同时钉住"是部分索引"：写成全量唯一会拒掉合法的多条（驳回后重申、待遇期满
    再认定），那是另一种坏。
    """
    index = next(
        (i for i in Base.metadata.tables[table].indexes if i.name == index_name), None
    )
    assert index is not None, f"{table} 的 {index_name} 没了——静默双写的洞回来了"
    assert index.unique, f"{index_name} 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == columns, f"{index_name} 的键变了"
    where = str(index.dialect_options["sqlite"].get("where", ""))
    assert where_fragment in where, (
        f"{index_name} 的部分条件不再包含 {where_fragment!r}：全量唯一会拒掉合法的多条"
    )


def test_两条索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    inspector = sa_inspect(engine)
    for table, index_name in (
        ("special_disease_apps", "uq_special_disease_app_applied"),
        ("dual_channel_apps", "uq_dual_channel_pending"),
    ):
        names = {i["name"] for i in inspector.get_indexes(table)}
        assert index_name in names, f"{table} 上没有 {index_name}（库与模型对不上）"


def test_绕开接口层直插时库里真的拦得住(client, admin):
    """索引"在不在"与"拦不拦得住"是两回事。

    接口层现在只有一条路径（`insert_or_conflict`），行为用例看到的 409 完全依赖
    数据库抬手；而 SQLite 的库级写锁又让线程探针对"索引被拆掉"不敏感。这里绕开
    接口层直接写库——那正是并发抢输者实际到达的位置——看数据库自己是否报冲突。
    """
    patient = _patient(client, admin, "直插验证己", "330281199001010031")
    assert _apply_special(client, admin, patient["id"], "尿毒症透析").status_code == 201
    assert _apply_dual(client, admin, patient["id"], "阿达木单抗").status_code == 201

    db = SessionLocal()
    try:
        db.add(SpecialDiseaseApp(
            patient_id=patient["id"], disease_name="尿毒症透析", status="applied", reason="重复",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(DualChannelApp(
            patient_id=patient["id"], drug_name="阿达木单抗", status="pending",
            reason="重复", created_by=1,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()
