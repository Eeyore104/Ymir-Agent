"""CodeBundle 语法预检（v1.2 T5.1 真跑后新增）。

背景：CodeBundle 原本只有字段定义、没有任何校验器，模型写出语法错误的 Python
也能通过校验，错误要等沙箱执行 ``pytest`` 才暴露 —— 那时一整轮模型调用已经
花掉、评测直接判 fail。

实测两次同一模式（2026-10-26）：
- ``v12-fastapi-scratch-4``：``tests/conftest.py:18`` ``def _unused()`` 前多一空格
- ``v12-fastapi-scratch-5``：``tests/test_tasks_api.py:14`` ``def app_client`` 前多一空格

本文件钉住预检行为的四条边界。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ymir.common.contracts import CodeBundle, CodeFile


class TestCodeBundleSyntaxCheck:
    def test_valid_python_passes(self) -> None:
        bundle = CodeBundle(
            files=[CodeFile(path="app/main.py", content="from fastapi import FastAPI\n")]
        )
        assert len(bundle.files) == 1

    def test_indentation_error_is_rejected(self) -> None:
        """T5.1 实测的原始形态：``@pytest.fixture()`` 之后的 ``def`` 多一个前导空格。"""
        bad = "import pytest\n\n\n@pytest.fixture()\n def _unused() -> None:\n    pass\n"
        with pytest.raises(ValidationError) as exc:
            CodeBundle(files=[CodeFile(path="tests/conftest.py", content=bad)])

        message = str(exc.value)
        assert "tests/conftest.py" in message
        assert "语法错误" in message

    def test_non_python_files_are_not_parsed(self) -> None:
        """非 ``.py`` 一律不解析：Vue/JS/TOML 的语法交给模板检查与沙箱。"""
        bundle = CodeBundle(
            files=[
                CodeFile(path="web/src/App.vue", content="<template> 故意不平衡"),
                CodeFile(path="pyproject.toml", content="[[[ 不是合法 TOML"),
                CodeFile(path="requirements.txt", content="fastapi>=0.115\n"),
            ]
        )
        assert len(bundle.files) == 3

    def test_multiple_errors_are_aggregated(self) -> None:
        """多处语法错误要在**一次**校验里全部报出，让模型一次改完。"""
        with pytest.raises(ValidationError) as exc:
            CodeBundle(
                files=[
                    CodeFile(path="a.py", content=" def broken(): pass\n"),
                    CodeFile(path="b.py", content="def f(:\n"),
                ]
            )

        message = str(exc.value)
        assert "a.py" in message
        assert "b.py" in message
