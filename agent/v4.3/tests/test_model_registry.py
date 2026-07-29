"""ModelRegistry + LLMClient.from_role() — v4.3 多模型路由测试。

不打 LLM；只测 yaml 加载、优先级链、错误信息。
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from agent.provider_config import (
    ModelRegistry,
    load_model_registry,
    reset_default_registry,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_default_registry()
    yield
    reset_default_registry()


@pytest.fixture
def yaml_with_models(tmp_path):
    """提供 minimal providers.yaml 含 models 块。"""
    p = tmp_path / "providers.yaml"
    p.write_text(textwrap.dedent("""\
        models:
          default: deepseek-chat
          planner: deepseek-chat
          executor: deepseek-chat
          verifier: deepseek-chat
        providers:
          anthropic:
            type: anthropic_native
            key_env: ANTHROPIC_API_KEY
            model_patterns: ["claude-*"]
          deepseek:
            type: openai_compat
            key_env: DEEPSEEK_API_KEY
            base_url: "https://api.deepseek.com/v1"
            model_patterns: ["deepseek-*"]
    """))
    return p


def test_load_models_block(yaml_with_models):
    reg = load_model_registry(yaml_with_models)
    assert reg.models["default"] == "deepseek-chat"
    assert reg.models["planner"] == "deepseek-chat"


def test_resolve_role_from_yaml(yaml_with_models):
    reg = load_model_registry(yaml_with_models)
    assert reg.resolve("planner") == "deepseek-chat"
    assert reg.resolve("executor") == "deepseek-chat"


def test_resolve_falls_back_to_default(yaml_with_models):
    reg = load_model_registry(yaml_with_models)
    # 没在 yaml 显式声明 → 用 default
    assert reg.resolve("unknown_role") == "deepseek-chat"


def test_resolve_cli_override_wins(yaml_with_models):
    reg = load_model_registry(yaml_with_models)
    assert reg.resolve("planner",
                       cli_overrides={"planner": "claude-sonnet-4-6"}) == "claude-sonnet-4-6"


def test_resolve_env_override(yaml_with_models, monkeypatch):
    reg = load_model_registry(yaml_with_models)
    monkeypatch.setenv("DFT_MODEL_PLANNER", "gpt-4o")
    assert reg.resolve("planner") == "gpt-4o"


def test_resolve_priority_cli_over_env(yaml_with_models, monkeypatch):
    reg = load_model_registry(yaml_with_models)
    monkeypatch.setenv("DFT_MODEL_PLANNER", "env-model")
    assert reg.resolve("planner",
                       cli_overrides={"planner": "cli-model"}) == "cli-model"


def test_missing_models_block_returns_empty(tmp_path):
    """yaml 没 models 块 → 空 registry；resolve 缺 default 应明确报错。"""
    p = tmp_path / "providers.yaml"
    p.write_text(textwrap.dedent("""\
        providers:
          deepseek:
            type: openai_compat
            key_env: DEEPSEEK_API_KEY
            model_patterns: ["deepseek-*"]
    """))
    reg = load_model_registry(p)
    assert reg.models == {}
    with pytest.raises(KeyError, match="no model configured for role"):
        reg.resolve("planner")


def test_empty_registry_with_cli_override():
    """空 registry + cli override 仍可工作。"""
    reg = ModelRegistry(models={})
    assert reg.resolve("planner",
                       cli_overrides={"planner": "claude-sonnet-4-6"}) == "claude-sonnet-4-6"


def test_invalid_models_value_type(tmp_path):
    """models[role] 必须是 string，否则报错。"""
    p = tmp_path / "providers.yaml"
    p.write_text(textwrap.dedent("""\
        models:
          planner: 42
        providers:
          deepseek:
            type: openai_compat
            key_env: DEEPSEEK_API_KEY
            model_patterns: ["deepseek-*"]
    """))
    with pytest.raises(ValueError, match="must map string"):
        load_model_registry(p)
