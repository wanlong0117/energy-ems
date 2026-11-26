"""打包入口契约：uvicorn 必须拿到 app 对象，不能拿到 "main:app" 字符串。

回归背景（v0.1.8）：main.py 曾写 `uvicorn.run("main:app", ...)`。源码下能跑，
一旦被 PyInstaller 冻结，入口脚本在 sys.modules 里只注册为 `__main__`，不存在名叫
main 的可导入模块，uvicorn 的 importlib.import_module("main") 直接抛
`Could not import module "main"`、退出码 3。CI 只编译不实跑，于是 v0.1.1 ~ v0.1.7
的全部 Windows 包都是「双击闪退」。

这里用 AST 静态守住这条契约（等价断言：第一个位置参数不能是字符串字面量），
真正的端到端证据由 CI 里的「跑起来打 /healthz」步骤提供。
"""
from __future__ import annotations

import ast
from pathlib import Path

import main

MAIN_PY: Path = Path(main.__file__).resolve()


def _uvicorn_run_calls() -> list[ast.Call]:
    """解析 main.py，返回所有 `uvicorn.run(...)` 调用节点。"""
    tree: ast.Module = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "uvicorn"
        ):
            calls.append(node)
    return calls


def test_uvicorn_run_receives_app_object_not_import_string():
    """Arrange/Act: 解析入口文件。Assert: uvicorn.run 的首个位置参数是对象而非字符串。"""
    calls = _uvicorn_run_calls()
    assert calls, "main.py 里应当有一处 uvicorn.run 启动调用"
    for call in calls:
        assert call.args, "uvicorn.run 必须传 app，不能只给关键字参数"
        first = call.args[0]
        assert not isinstance(first, ast.Constant), (
            "uvicorn.run 的首参不能是字符串字面量：PyInstaller 冻结后没有可导入的 "
            f"main 模块，会报 Could not import module \"main\"（当前写成 {ast.dump(first)}）"
        )
        # app 是本模块的全局对象；此处只做「是名字且叫 app」的弱校验，避免锁死实现
        assert isinstance(first, ast.Name) and first.id == "app", (
            f"uvicorn.run 应直接接收本模块的 app 对象，实际是 {ast.dump(first)}"
        )


def test_workers_not_set_above_one():
    """Arrange/Act: 解析同一处调用。Assert: 不得开多 worker——传 app 对象时 workers>1 会直接报错。"""
    for call in _uvicorn_run_calls():
        for keyword in call.keywords:
            if keyword.arg != "workers":
                continue
            value = keyword.value
            assert isinstance(value, ast.Constant) and value.value == 1, (
                "传 app 对象时 uvicorn 只支持 workers=1；要多进程必须改用导入字符串（冻结后不可用）"
            )
