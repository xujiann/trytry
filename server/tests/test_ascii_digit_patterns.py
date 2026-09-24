"""入参里的数字只认半角：`pattern` 里不许用 `\\d`（P2-47）。

`\\d` 在 Python 与 pydantic 的正则里都认全角 / 阿拉伯-印度数字——「２０２６」「٢٠٢٦」照过形状。
这类值按字符串比较排在一切半角数字之后，落了库就是一条对不上任何筛选的记录：P2-46 的手术间
重复排班就是这么来的。仓库里月度期间与时刻早已改成 `[0-9]`（`datetypes` 里写着理由），
剩下的一处是预算年度：

- `POST /api/mgmt/budgets` 的 `year` 写着 `^\\d{4}$`，「２０２６」照收，这份预算永远对不上
  按「2026」查的执行对比；
- `GET /api/mgmt/budgets/execution` 的 `year` 干脆是裸 `str`，拼进 `period LIKE '{year}-%'`：
  `year=%` 把**所有年份**的收支加在一起报成「实际数」，而预算那边按等值查不到——执行率无从谈起、
  数字却一本正经。

修法：两处都只认 `[0-9]{4}`（查询参数用 `Query(pattern=...)`——带 pattern 的普通 `str` 参数
FastAPI 照常校验，丢校验器的只是 Annotated 别名那种写法，见 P1-88）。闸门：全仓 `pattern=`
关键字参数里出现 `\\d` 即红，零基线。顺带把完整日期的形状也改成 `[0-9]`：全角日期原先先过形状、
再在日历校验里被拒，报的是「日期不存在（请检查月份天数）」，文案对不上真正的毛病（月度那头早改了）。
"""
import ast
import pathlib

import pytest

from app.datetypes import check_date

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 零基线：一条都不许有（`scripts/dump_gate_status.py` 把它列进闸门现状）
BASELINE = 0


def _unicode_digit_patterns(sources: dict[str, str] | None = None) -> list[str]:
    files = {str(p.relative_to(APP_DIR)): p.read_text(encoding="utf-8")
             for p in sorted(APP_DIR.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    found = []
    for name, text in files.items():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.keyword) and node.arg == "pattern" and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and "\\d" in node.value.value:
                found.append(f"{name}:{node.value.lineno}")
    return sorted(found)


def test_pattern里不许用反斜杠d():
    bad = _unicode_digit_patterns()
    assert len(bad) <= BASELINE, (
        "以下 pattern 用了 \\d——它认全角 / 阿拉伯-印度数字，「２０２６」照过形状：\n  " + "\n  ".join(bad)
        + "\n\n数字一律写 [0-9]。"
    )


def test_判据自证_修复前的预算年度当场点名():
    snippet = 'class BudgetCreate(BaseModel):\n    year: str = Field(pattern=r"^\\d{4}$")\n'
    assert [v for v in _unicode_digit_patterns({"自证.py": snippet}) if v.startswith("自证.py")] == ["自证.py:2"]


def test_全角日期报的是格式不对而不是日期不存在():
    with pytest.raises(ValueError, match="格式"):
        check_date("２０２６-０９-２４")
    assert check_date("2026-09-24") == "2026-09-24"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", json={"name": "预算年度卫生院", "org_type": "township",
                                                   "level": "township"}, headers=admin).json()["id"]


@pytest.mark.parametrize("year", ["２０２６", "٢٠٢٦"])
def test_预算年度写全角_422(client, admin, org, year):
    r = client.post("/api/mgmt/budgets", json={"org_id": org, "year": year, "category": "income", "amount": 100},
                    headers=admin)
    assert r.status_code == 422, (year, r.status_code, r.text[:200])


def test_特征化_半角年度照常编制与对比(client, admin, org):
    r = client.post("/api/mgmt/budgets", json={"org_id": org, "year": "2031", "category": "income", "amount": 100},
                    headers=admin)
    assert r.status_code == 201, r.text
    body = client.get("/api/mgmt/budgets/execution", params={"org_id": org, "year": "2031"}, headers=admin).json()
    assert body["year"] == "2031" and body["income"]["budget"] == 100


@pytest.mark.parametrize("year", ["%", "20%", "２０３１", "31"])
def test_执行对比的年度不对_422而不是把别的年份加进来(client, admin, org, year):
    r = client.get("/api/mgmt/budgets/execution", params={"org_id": org, "year": year}, headers=admin)
    assert r.status_code == 422, (year, r.status_code, r.text[:200])
