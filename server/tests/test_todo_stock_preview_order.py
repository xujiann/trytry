"""待办「缺药预警」预览按机构排、同一机构内没有尾键（P2-971，第二十七批「排名、Top-N、并列与空值的排序语义」扫描 G4-7
的尾键那一半）。

`_stock_alerts` 原先 `order_by(DrugStock.org_id).limit(100)`：同一机构内谁先谁后由库决定，PG 上每次入库、发药（原地 UPDATE）
都可能换一批——铃铛每节只取前 5 条、医生移动端前 20 条，同一家的缺药预警每刷新一次换一批。同文件其余四节都按唯一的
id 排。修法：补 `DrugStock.id` 尾键。按缺口严重程度排（库存 / 阈值升序）另行待裁定。
"""
import ast
import inspect
import textwrap

from app.routers import todos


def test_缺药预警预览按机构再按库存行编号排():
    tree = ast.parse(textwrap.dedent(inspect.getsource(todos._stock_alerts)))
    orders = [ast.unparse(node) for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "order_by"]
    assert orders and all(o.endswith("order_by(DrugStock.org_id, DrugStock.id)") for o in orders), orders   # 修前只按机构
