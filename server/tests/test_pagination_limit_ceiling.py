"""`limit` 默认值必须真的拿得到——`max_limit` 不能把它砍掉（运行期证据）。

静态判据在 `tests/test_pagination_governance.py::test_分页默认值不得被上限悄悄砍掉`，
这里补一条**真的把行数推过 500 再请求**的用例：静态判据认的是 AST 上的两个数字，
真正要保证的是「不传 `limit` 的调用方拿到的行数与改写前一致」。

挑 `GET /api/rbac/permissions` 而不是别的分页端点，是因为它最近临界：权限点由
启动时的路由表自动登记，本仓库现有 **487** 个写接口——再加十来个写接口就会越过
`deps.paginate` 的缺省上限 500。越过那天的表现是**授权页上少了一批权限点而没有
任何提示**，管理员会以为"平台没有这个权限可授"。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Permission


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_权限点超过500条时清单不被缺省上限截断(client, admin):
    db = SessionLocal()
    try:
        base = db.query(Permission).count()
        assert base > 0, "启动时没同步权限点，这条用例就失去了前提"
        db.add_all(
            Permission(code=f"POST:/api/_ceiling_probe/{i}", method="POST",
                       path=f"/api/_ceiling_probe/{i}", module="_ceiling_probe")
            for i in range(600 - base if base < 600 else 20)
        )
        db.commit()
        total = db.query(Permission).count()
    finally:
        db.close()
    assert total > 500, "构造的数据没过 500，这条用例证明不了任何事"

    resp = client.get("/api/rbac/permissions", headers=admin)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    # 不传 limit：该端点的默认值是 1000，改写成 paginate 之前的硬上限也是 1000
    assert len(rows) == total, (
        f"清单只给了 {len(rows)} 条、库里有 {total} 条——"
        "默认上限被 deps.paginate 的缺省 max_limit(500) 砍掉了"
    )
    assert resp.headers["X-Total-Count"] == str(total)
