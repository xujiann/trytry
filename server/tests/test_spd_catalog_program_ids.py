"""目录接口的病种不带病种号，新建路径模板的病种下拉拿「第几个 + 1」冒充：号一不连续，模板就挂到别的病种上（P2-96）。

各端下拉共用的目录（`GET /api/spd/catalog`）给团队、量表、专病中心、路径模板都带 `id`，唯独病种只有编码与名称；可建路径
模板的接口收的是病种号（`program_id`）。管理端「路径模板」表单于是写成 `catalog.programs.map((p, i) => value = i + 1)`——
病种号从 1 起连续时碰巧对，号一不连续就错位：生产库（PostgreSQL）的序列被一次失败的插入吃掉一个号（新建病种编码重复
409，就是先取号再撞唯一约束），此后新建的病种号都比「第几个 + 1」大；选中空档后的第二个病种，模板挂到的是空档后的第一个
病种。页面上的模板表不显示病种，建错了看不出来；按病种查模板、启动路径（P2-95 起要求同病种）才会对不上。

修法：目录的病种带病种号，表单按号取；启动路径的模板下拉带上病种名。
"""
from sqlalchemy import func


def test_目录的病种带病种号_号不连续也对得上(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdProgram

    with SessionLocal() as db:
        top = db.query(func.max(SpdProgram.id)).scalar()
        # 生产库的序列被失败的插入吃掉一个号，就是这样一个空档
        db.add(SpdProgram(id=top + 2, code="p296_gap", name="P296 号不连续的病种", category="specialty"))
        db.commit()
    programs = client.get("/api/spd/catalog", headers=admin).json()["programs"]
    by_code = {p["code"]: p for p in programs}
    assert by_code["p296_gap"]["id"] == top + 2   # 修前没有 id 这一栏
    # 页面原先的冒充法：第几个 + 1——对不上
    assert [p["code"] for p in programs].index("p296_gap") + 1 != top + 2
    with SessionLocal() as db:
        assert {p["code"]: p["id"] for p in programs} == {
            code: pid for pid, code in db.query(SpdProgram.id, SpdProgram.code).order_by(SpdProgram.id).limit(100)}
