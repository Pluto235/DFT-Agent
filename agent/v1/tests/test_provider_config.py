"""provider_config.py 测试 — yaml 加载 / pattern 匹配 / config_path 覆盖。"""
from pathlib import Path

import pytest

from agent.provider_config import (
    ProviderConfig,
    ProviderRegistry,
    get_default_registry,
    load_registry,
    reset_default_registry,
)


def test_bundled_yaml_loads_and_parses():
    """随包发的 providers.yaml 能被解析。"""
    reset_default_registry()
    reg = get_default_registry()
    ids = reg.ids()
    # 至少应有 anthropic + openai + moonshot(kimi 走这);openrouter 是 catch-all
    for required in ("anthropic", "openai", "moonshot", "openrouter", "ollama"):
        assert required in ids, f"missing {required} in bundled providers.yaml"


@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-4-6", "anthropic"),
    ("kimi-latest", "moonshot"),
    ("kimi-k1.5-preview", "moonshot"),
    ("moonshot-v1-128k", "moonshot"),
    ("deepseek-chat", "deepseek"),
    ("ollama/llama3:8b", "ollama"),
    ("meta-llama/llama-3.3-70b-instruct", "openrouter"),  # owner/name catch-all
])
def test_default_registry_routes(model, expected):
    assert get_default_registry().detect(model) == expected


def test_more_specific_pattern_wins_over_catchall(tmp_path):
    """ollama/* 必须排在 */* 之前。"""
    reg = get_default_registry()
    assert reg.detect("ollama/llama3:8b") == "ollama"
    assert reg.detect("anyone/any-model") == "openrouter"


def test_load_custom_yaml(tmp_path: Path):
    """自定义 yaml 路径能加载,新 provider 生效。"""
    custom = tmp_path / "custom.yaml"
    custom.write_text("""
providers:
  myco:
    type: openai_compat
    key_env: MYCO_API_KEY
    base_url: "https://api.myco.example.com/v1"
    model_patterns:
      - "myco-*"
""", encoding="utf-8")
    reg = load_registry(custom)
    assert reg.ids() == ["myco"]
    assert reg.detect("myco-7b") == "myco"
    cfg = reg.get("myco")
    assert cfg.type == "openai_compat"
    assert cfg.base_url == "https://api.myco.example.com/v1"
    assert cfg.key_env == "MYCO_API_KEY"
    assert cfg.api_key is None


def test_load_yaml_inline_api_key(tmp_path):
    """api_key 字段直接放 key string(本地测试)。"""
    custom = tmp_path / "c.yaml"
    custom.write_text("""
providers:
  local:
    type: openai_compat
    api_key: "test-12345"
    base_url: "http://localhost:9999/v1"
    model_patterns: ["local-*"]
""", encoding="utf-8")
    reg = load_registry(custom)
    cfg = reg.get("local")
    assert cfg.api_key == "test-12345"
    assert cfg.key_env is None


def test_load_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_registry(tmp_path / "nope.yaml")


def test_load_missing_providers_key(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: bar\n", encoding="utf-8")
    with pytest.raises(ValueError, match="providers"):
        load_registry(bad)


def test_load_invalid_type(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("""
providers:
  bad:
    type: weird_thing
    model_patterns: []
""", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid type"):
        load_registry(bad)


def test_load_invalid_yaml_syntax(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("providers:\n  - this is not a mapping at top\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_registry(bad)


def test_unknown_provider_get_raises():
    reg = get_default_registry()
    with pytest.raises(ValueError, match="unknown provider"):
        reg.get("not-a-provider")


def test_dft_solver_config_env_var(tmp_path, monkeypatch):
    """DFT_SOLVER_CONFIG env var 改路径。"""
    custom = tmp_path / "env.yaml"
    custom.write_text("""
providers:
  envonly:
    type: openai_compat
    key_env: ENVONLY_KEY
    base_url: null
    model_patterns: ["envmodel-*"]
""", encoding="utf-8")
    monkeypatch.setenv("DFT_SOLVER_CONFIG", str(custom))
    reset_default_registry()
    try:
        reg = get_default_registry()
        assert reg.ids() == ["envonly"]
    finally:
        reset_default_registry()  # 不污染后续测试


def test_pattern_order_matters(tmp_path):
    """同一 model 可能匹配多 provider 的 pattern;先 declared 的胜出。"""
    custom = tmp_path / "order.yaml"
    custom.write_text("""
providers:
  first:
    type: openai_compat
    base_url: "http://a/v1"
    api_key: "k1"
    model_patterns: ["foo-*"]
  second:
    type: openai_compat
    base_url: "http://b/v1"
    api_key: "k2"
    model_patterns: ["foo-bar-*"]
""", encoding="utf-8")
    reg = load_registry(custom)
    # first 先 declared,即使 second 的 pattern 更具体
    assert reg.detect("foo-bar-1") == "first"
