"""pytest 配置：给 --run-slow 加开关。

默认 slow 测试（真调 Claude API + 真跑 GPU QE）会被 skip。
加 --run-slow 才会真跑，用于 MVP 验收：

  pytest harness/tests/test_si_t1_smoke.py -v --run-slow
"""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run slow smoke tests (real LLM + QE)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="need --run-slow to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
