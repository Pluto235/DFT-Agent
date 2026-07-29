"""Provider 路由配置加载。

文件格式: agent/providers.yaml（见该文件注释）。
Discovery 顺序:
  1. LLMClient(config_path=...) / solve.py --config 显式传
  2. env var DFT_SOLVER_CONFIG
  3. 默认 agent/providers.yaml（随包发）

模块级 cache 默认 registry 一次，避免每次 LLMClient init 都 read yaml。
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class ProviderConfig:
    """单个 provider 的路由 + 认证配置。"""
    id: str
    type: str                          # anthropic_native | openai_compat
    key_env: Optional[str]             # env var 名；None = 无需 key（如 Ollama）
    api_key: Optional[str]             # 字面 key（非生产用）；优先级高于 key_env
    base_url: Optional[str]            # openai_compat endpoint；None = SDK 自家 default
    model_patterns: tuple[str, ...]    # 匹配此 provider 的 model id glob 列表


@dataclass(frozen=True)
class ProviderRegistry:
    """所有 provider 的有序集合。order 决定路由优先级（先匹配先选）。"""
    providers: tuple[ProviderConfig, ...]

    def detect(self, model: str) -> str:
        """model id → provider id；首个匹配 pattern 的 provider。"""
        for p in self.providers:
            for pat in p.model_patterns:
                if fnmatch.fnmatchcase(model, pat):
                    return p.id
        valid_patterns = ", ".join(
            f"{p.id}: {list(p.model_patterns)}" for p in self.providers
        )
        raise ValueError(
            f"cannot detect provider for model='{model}'; "
            f"no pattern matched. Configured: {valid_patterns}"
        )

    def get(self, provider_id: str) -> ProviderConfig:
        for p in self.providers:
            if p.id == provider_id:
                return p
        valid = ", ".join(p.id for p in self.providers)
        raise ValueError(
            f"unknown provider '{provider_id}'; valid: {valid}"
        )

    def ids(self) -> list[str]:
        return [p.id for p in self.providers]


@dataclass(frozen=True)
class ModelRegistry:
    """v4.3: 每个角色（planner/executor/verifier/...）的默认 model id。

    优先级（高→低）:
      1. cli_overrides[role]             — solve.py CLI flag
      2. env var DFT_MODEL_<ROLE>        — 本地 dev 方便
      3. models[role]                    — providers.yaml `models:` 块
      4. models["default"]               — 兜底
    """
    models: dict[str, str]   # role → model id; 至少要有 "default"

    def resolve(
        self,
        role: str,
        cli_overrides: Optional[dict[str, str]] = None,
    ) -> str:
        if cli_overrides and cli_overrides.get(role):
            return cli_overrides[role]
        env_key = f"DFT_MODEL_{role.upper()}"
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val
        if role in self.models:
            return self.models[role]
        if "default" in self.models:
            return self.models["default"]
        raise KeyError(
            f"no model configured for role '{role}'; "
            f"set models.{role} or models.default in providers.yaml, "
            f"or pass --{role.replace('_', '-')}-model on the CLI, "
            f"or set env {env_key}"
        )


_BUNDLED_CONFIG_PATH = Path(__file__).parent / "providers.yaml"


def _resolve_config_path(explicit: Optional[Path] = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("DFT_SOLVER_CONFIG")
    if env:
        return Path(env)
    return _BUNDLED_CONFIG_PATH


def _parse_yaml(config_path: Optional[Path] = None) -> tuple[Path, dict]:
    path = _resolve_config_path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"provider config not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: invalid YAML — {e}") from e
    if not isinstance(data, dict) or "providers" not in data:
        raise ValueError(f"{path}: missing top-level 'providers' key")
    return path, data


def load_registry(config_path: Optional[Path] = None) -> ProviderRegistry:
    """读 yaml → ProviderRegistry。报错信息含路径，便于排查 typo。"""
    path, data = _parse_yaml(config_path)
    raw_providers = data["providers"]
    if not isinstance(raw_providers, dict):
        raise ValueError(f"{path}: 'providers' must be a mapping (got {type(raw_providers).__name__})")
    items: list[ProviderConfig] = []
    for pid, raw in raw_providers.items():
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: provider '{pid}' must be a mapping")
        ptype = raw.get("type")
        if ptype not in ("anthropic_native", "openai_compat"):
            raise ValueError(
                f"{path}: provider '{pid}' has invalid type {ptype!r}; "
                f"must be 'anthropic_native' or 'openai_compat'"
            )
        patterns = raw.get("model_patterns") or []
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"{path}: provider '{pid}' model_patterns must be list of strings")
        items.append(ProviderConfig(
            id=pid,
            type=ptype,
            key_env=raw.get("key_env"),
            api_key=raw.get("api_key"),
            base_url=raw.get("base_url"),
            model_patterns=tuple(patterns),
        ))
    return ProviderRegistry(providers=tuple(items))


def load_model_registry(config_path: Optional[Path] = None) -> ModelRegistry:
    """读 yaml 顶层 `models:` 块 → ModelRegistry。

    缺失 `models:` 块时返回空 registry — 此时只能依赖 cli_overrides 或 env。
    若同时缺 `models.default`，调用 `resolve()` 未命中角色会抛 KeyError（明确报错）。
    """
    path, data = _parse_yaml(config_path)
    raw_models = data.get("models")
    if raw_models is None:
        return ModelRegistry(models={})
    if not isinstance(raw_models, dict):
        raise ValueError(
            f"{path}: 'models' must be a mapping (got {type(raw_models).__name__})"
        )
    out: dict[str, str] = {}
    for role, mid in raw_models.items():
        if not isinstance(role, str) or not isinstance(mid, str):
            raise ValueError(
                f"{path}: models.{role!r} must map string→string (got value {mid!r})"
            )
        out[role] = mid
    return ModelRegistry(models=out)


_default_registry: Optional[ProviderRegistry] = None
_default_model_registry: Optional[ModelRegistry] = None


def get_default_registry() -> ProviderRegistry:
    """读 bundled yaml（or DFT_SOLVER_CONFIG）一次后缓存。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = load_registry()
    return _default_registry


def get_default_model_registry() -> ModelRegistry:
    global _default_model_registry
    if _default_model_registry is None:
        _default_model_registry = load_model_registry()
    return _default_model_registry


def reset_default_registry() -> None:
    """主要给测试用 — 清缓存。"""
    global _default_registry, _default_model_registry
    _default_registry = None
    _default_model_registry = None
