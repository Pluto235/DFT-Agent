from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent

_WORKSPACE_ROOT = REPO_ROOT.parent
_BENCH = _WORKSPACE_ROOT / "bench"
_AGENTS = _WORKSPACE_ROOT / "agent"

class ModelInfo(BaseModel):
    id: str
    provider: str = "anthropic"  # 'anthropic' / 'openai' / 'google' / 'deepseek' / 'moonshot' / 'xai' / 'mistral' / 'openrouter' / 'ollama'
    tier: str = "frontier"        # "frontier" / "standard" / "open"
    desc: str = ""
    # available=True ⇔ 此 deployment 已 smoke-verify + 该 provider 的 env key 假设已配。
    # available=False = solver 代码路径已支持(Phase 2 done via native SDK 双路径,2026-05-03),
    # 但当前 deployment 还没冒烟过这个 model（多半是缺 API key）。UI 仍 disable + 提示。
    available: bool = True
    # 原币定价（cache miss / standard）；前端按 price_currency + CNY_PER_USD 转 USD 显示。
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0
    # prompt cache 三段计价（None = 该 provider 无 cache 折扣 / 未公开 cache 价 → fall back 全 miss 算）。
    # deepseek 实测：cache_hit_rate ≈ miss_rate / 120（极便宜）；anthropic：read ~10x 折扣。
    cache_hit_price_per_mtok: float | None = None
    cache_miss_price_per_mtok: float | None = None
    price_currency: str = "USD"   # "USD" / "CNY"


class TaskTypeInfo(BaseModel):
    id: str
    label_en: str
    label_zh: str = ""
    description: str = ""
    supported_in_custom: bool = True


# Agent solver entry：每个 agent 是独立 CLI subprocess，通过 solver_path spawn。
# 多 agent 选择由 frontend nav dropdown 控制（task 139）。
# 当前只有 agent v1（dft-agent-bench-solver/agent/solve.py）；未来加 WarpLab / 别的时
# 在 config.toml 用 [[agents]] block 加新条目即可，不动代码。
class AgentEntry(BaseModel):
    id: str                           # 'v1-stable' / 'warplab-v2' 等
    name: str                         # frontend dropdown 显示用
    solver_path: str                  # absolute path to solver CLI
    description: str = ""
    available: bool = True             # False = UI disable + 提示
    # is_stable=True：normal + dev role 都可见；False：仅 dev role 可见（开发中 agent，未公开）
    # 与 task.metadata.is_stable 同语义；缺省 True 兼容老配置
    is_stable: bool = True
    # 每 agent 独立 venv 的 python 解释器绝对路径。空串 → fallback 到 PATH 上的 "python"。
    # 仅 custom 模式生效；harness 模式由 bench 自己控制 python（前置 conda bin）。
    python_path: str = ""
    # 每 agent 额外环境变量；solver_wrapper 在标准 env 装配之后 update 进去（覆盖优先）。
    # 典型用途：PSEUDO_DIR / agent 私有 secret env / 各 venv 自带 PATH 前缀。
    env: dict[str, str] = Field(default_factory=dict)


DEFAULT_MODELS: list[dict[str, Any]] = [
    # ── Anthropic（live-verified 0503，要求 ANTHROPIC_API_KEY）──
    # 价格自 platform.claude.com/docs/en/docs/about-claude/pricing fetch 0503；4.7 已降到 5/25。
    {"id": "claude-sonnet-4-6",  "provider": "anthropic", "tier": "frontier", "available": True,
     "desc": "Recommended · balanced cost / quality",
     "input_price_per_mtok": 3.0,  "output_price_per_mtok": 15.0, "price_currency": "USD"},
    {"id": "claude-opus-4-7",    "provider": "anthropic", "tier": "frontier", "available": True,
     "desc": "Highest reasoning · slower / pricier",
     "input_price_per_mtok": 5.0,  "output_price_per_mtok": 25.0, "price_currency": "USD"},
    {"id": "claude-haiku-4-5",   "provider": "anthropic", "tier": "standard", "available": True,
     "desc": "Fast · for sanity checks only",
     "input_price_per_mtok": 1.0,  "output_price_per_mtok": 5.0,  "price_currency": "USD"},

    # ── Moonshot Kimi（live-verified 0503，要求 MOONSHOT_API_KEY）──
    # 价格自 platform.kimi.com/docs/pricing/chat-k2 (cache miss)；K2.5 借用 K2.6 价格（官网无单独页）。
    {"id": "kimi-k2-turbo-preview", "provider": "moonshot", "tier": "standard", "available": True,
     "desc": "Moonshot · K2 turbo (fastest / cheapest)",
     "input_price_per_mtok": 8.0,  "output_price_per_mtok": 58.0, "price_currency": "CNY"},
    {"id": "kimi-k2-0905-preview",  "provider": "moonshot", "tier": "frontier", "available": True,
     "desc": "Moonshot · K2 frontier preview",
     "input_price_per_mtok": 4.0,  "output_price_per_mtok": 16.0, "price_currency": "CNY"},
    {"id": "kimi-k2.5",             "provider": "moonshot", "tier": "frontier", "available": True,
     "desc": "Moonshot · K2.5 reasoning (multimodal, supports_reasoning)",
     "input_price_per_mtok": 6.5,  "output_price_per_mtok": 27.0, "price_currency": "CNY"},

    # ── DeepSeek（v4 reasoning model；live-verified 0503，要求 DEEPSEEK_API_KEY）──
    # 价格自 api-docs.deepseek.com/zh-cn/quick_start/pricing。
    # v4-pro 当前在 75% 折扣期（截至 2026-05-31）：cache miss input 12→3 CNY/M，output 24→6 CNY/M。
    # cache_hit ¥0.025/M（折扣 ~120x，agent 复用 history 时 hit 率 95%+）。
    # solver llm.py 把 prompt_cache_hit/miss_tokens 拆开写进 transcript → cost.py 三段计价。
    # 折扣到期后改回 12/24（或 deepseek 出新价后更新）。
    {"id": "deepseek-v4-flash",  "provider": "deepseek",  "tier": "standard", "available": True,
     "desc": "DeepSeek · v4 flash (cheap reasoning)",
     "input_price_per_mtok": 1.0,  "output_price_per_mtok": 2.0,
     "cache_hit_price_per_mtok": 0.02, "cache_miss_price_per_mtok": 1.0,
     "price_currency": "CNY"},
    {"id": "deepseek-v4-pro",    "provider": "deepseek",  "tier": "frontier", "available": True,
     "desc": "DeepSeek · v4 pro (frontier reasoning)",
     "input_price_per_mtok": 3.0,  "output_price_per_mtok": 6.0,
     "cache_hit_price_per_mtok": 0.025, "cache_miss_price_per_mtok": 3.0,
     "price_currency": "CNY"},

    # ── 以下 provider 代码路径支持，但当前 deployment 未配 key/未冒烟，available=False ──
    # ── OpenAI ──
    {"id": "gpt-4o",             "provider": "openai",    "tier": "frontier", "available": False,
     "desc": "OpenAI · multi-modal flagship"},
    {"id": "o1",                 "provider": "openai",    "tier": "frontier", "available": False,
     "desc": "OpenAI · reasoning specialist"},
    {"id": "gpt-4o-mini",        "provider": "openai",    "tier": "standard", "available": False,
     "desc": "OpenAI · fast / cheap"},

    # ── Google ──
    {"id": "gemini-2.5-pro",     "provider": "google",    "tier": "frontier", "available": False,
     "desc": "Google · long-context reasoning"},
    {"id": "gemini-2.0-flash",   "provider": "google",    "tier": "standard", "available": False,
     "desc": "Google · fast multi-modal"},

    # ── xAI ──
    {"id": "grok-2-latest",      "provider": "xai",       "tier": "frontier", "available": False,
     "desc": "xAI · Grok 2"},

    # ── Mistral ──
    {"id": "mistral-large-latest","provider": "mistral",  "tier": "frontier", "available": False,
     "desc": "Mistral · multilingual flagship"},

    # ── OpenRouter（开源权重统一网关）──
    {"id": "meta-llama/llama-3.3-70b-instruct", "provider": "openrouter", "tier": "open", "available": False,
     "desc": "Meta Llama 3.3 70B · via OpenRouter"},
    {"id": "qwen/qwen-2.5-72b-instruct",        "provider": "openrouter", "tier": "open", "available": False,
     "desc": "Alibaba Qwen 2.5 72B · via OpenRouter"},

    # ── 本地 Ollama（自建 server）──
    {"id": "ollama/llama3.3:70b",               "provider": "ollama",     "tier": "open", "available": False,
     "desc": "Local Ollama · Llama 3.3 70B"},
    {"id": "ollama/qwen2.5:32b",                "provider": "ollama",     "tier": "open", "available": False,
     "desc": "Local Ollama · Qwen 2.5 32B"},
]

DEFAULT_TASK_TYPES: list[dict[str, Any]] = [
    {"id": "vc_relax",  "label_en": "Variable-cell relaxation", "label_zh": "变胞弛豫",
     "description": "Optimize lattice + atomic positions until forces and stress converge.",
     "supported_in_custom": True},
    {"id": "bands_dos", "label_en": "Bands + DOS",              "label_zh": "能带 + 态密度",
     "description": "Compute electronic band structure along high-symmetry path and density of states.",
     "supported_in_custom": True},
]


DEFAULT_AGENTS = [
    {"id": "v1-stable", "name": "ReAct v1", "solver_path": str(_AGENTS / "v1/agent/solve.py")},
    {"id": "v5.1", "name": "DFT-Agent v5.1", "solver_path": str(_AGENTS / "v5.1/solve.py")},
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_prefix="DABW_")

    # workspace 根（admin disk_usage check 用；默认 ~/workspace，可被 WORKSPACE_ROOT env 或 toml 覆盖）
    workspace_root: Path = _WORKSPACE_ROOT

    # 真实仓库路径（不允许指向 fixture / mock；缺省按 _REPOS_ROOT sibling 假设）
    bench_dir: Path = Path(os.environ.get("BENCH_DIR", str(_BENCH)))
    agent_solver_path: Path = Path(os.environ.get(
        "AGENT_SOLVER_PATH",
        str(_AGENTS / "v1/agent/solve.py")))

    # 派生（默认相对 bench_dir / repo root；可被 toml 覆盖）
    bench_questions_dir: Path | None = None
    bench_runs_dir: Path | None = None
    demo_runs_dir: Path = REPO_ROOT / "runs_local"
    custom_task_dir: Path | None = None

    # 调度
    max_concurrent_runs: int = 2
    max_concurrent_runs_limit: int = 8

    # GPU 排队模式：
    #   "backend_pin"   → spawn 时 gpu_allocator.acquire() + pin CUDA_VISIBLE_DEVICES。
    #                     整个 solver 生命周期持卡（即使在等 LLM 响应也占着）。
    #   "wrapper_flock" → backend 只透传 QE_GPU_POOL=0,1,2,...；每次 pw.x 调用时
    #                     wrapper 在 /tmp/qe-gpu-locks/gpu-N.lock 上 flock 抢一张卡，
    #                     podman 退出后 fd 关闭 → 锁自动释放。solver 等 LLM 时不占卡，
    #                     N 张 GPU 可服务 M >> N 个并发 solver。
    # 默认 backend_pin（向后兼容；现有 wrapper 无需改动）。切到 wrapper_flock 前必须
    # 部署支持 flock 的 wrapper —— 见 deploy/qe-gpu-wrap.sh.example。
    qe_gpu_queueing_mode: Literal["backend_pin", "wrapper_flock"] = "backend_pin"

    # 每种模式对应的 QE GPU wrapper 目录（spawn 时按 mode 选其一注入到
    # subprocess env QE_GPU_WRAPPER_DIR，harness/lib/solver_runner.py 已读该
    # env 前置 PATH）。默认值是工作区里两个 sibling wrapper dir：
    #   backend_pin   → 原 wrapper，PR #2 合入前就存在
    #   wrapper_flock → flock-aware sibling（NVHPC 24.7 + v7.5CN 不变，仅
    #                   加 flock 抢锁逻辑），由 stage A 部署到本路径
    # 想换其它路径在 config.toml 里覆盖即可；admin UI 不暴露（防误触）。
    qe_gpu_wrapper_dir_backend_pin: str = os.environ.get("QE_GPU_WRAPPER_DIR", "")
    qe_gpu_wrapper_dir_wrapper_flock: str = ""

    # 默认上限（0505 user 决策升级 50→100，与 solver argparse 对齐 + T2 类大题
    # 实测 50 turn 不够（杨博 0430 经验：CsPbI3 T2 turn budget 烧光 → 0 score）。
    # max_turns 是 cap 不是 quota——简单题 agent 早 submit 不会用满。详见
    # todo/web/run-defaults-ssot.md D1 决策。
    max_turns_default: int = 100
    custom_run_max_turns_default: int = 100
    max_turns_max: int = 200

    # solver subprocess wall-time 默认（秒）。0505 user 决策移除 batch 特殊 6h
    # 默认，batch 与 compose 统一从此字段读（todo run-defaults-ssot D2）。
    solver_timeout_default: int = 7200

    # 模型 / 任务类型 / schema
    available_models: list[ModelInfo] = Field(
        default_factory=lambda: [ModelInfo(**m) for m in DEFAULT_MODELS]
    )
    task_types: list[TaskTypeInfo] = Field(
        default_factory=lambda: [TaskTypeInfo(**t) for t in DEFAULT_TASK_TYPES]
    )
    agents: list[AgentEntry] = Field(
        default_factory=lambda: [AgentEntry(**a) for a in DEFAULT_AGENTS]
    )
    enhance_model: str = "claude-haiku-4-5"             # 与 prototype data.js MOCK_CONFIG 行 351 一致
    schema_version_supported: str = "1.3"

    # solver 默认 args（可空；harness 单跑模式仍需要 --model 走 solver_args）
    agent_solver_args: str = ""

    # API key（env 优先）
    anthropic_api_key: str = ""
    enhance_api_key: str = ""

    # 多 token auth：tokens.yaml 路径（默认 repo 根 tokens.yaml）。文件存在则按 yaml
    # 加载多 token registry；不存在则 fallback 到 .env 单 AUTH_TOKEN（backwards 兼容）。
    tokens_yaml: Path = REPO_ROOT / "tokens.yaml"

    # Agent registry（0508 加，外部 agent 自动注册落地点）：
    # admin endpoint write，startup time read 并合并到 Settings.agents 里（同 id 时
    # registry 覆盖 DEFAULT_AGENTS）。文件不存在视为空 registry。
    # 不进 git，与 tokens.yaml 同款 ops 文件。
    agent_registry_yaml: Path = REPO_ROOT / "agent_registry.yaml"
    # 外部 agent clone 缓存目录；admin endpoint clone 时写入子目录 <id>-<sha>/。
    agent_cache_dir: Path = REPO_ROOT / "runs_local" / "agent-cache"

    # CORS / 调试
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5180"])
    debug: bool = False

    def derive_paths(self) -> "Settings":
        if self.bench_questions_dir is None:
            self.bench_questions_dir = self.bench_dir / "questions"
        if self.bench_runs_dir is None:
            self.bench_runs_dir = self.bench_dir / "runs"
        if self.custom_task_dir is None:
            self.custom_task_dir = self.demo_runs_dir / "custom_tasks"
        return self


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def load_settings(config_path: Path | None = None) -> Settings:
    """加载顺序：config.toml < env < 显式 kwargs。"""
    cfg_path = config_path or (REPO_ROOT / "config.toml")
    toml_data = _load_toml(cfg_path)
    s = Settings(**toml_data)

    # 环境变量后置覆盖（pydantic-settings 已自动从 DABW_* 读，
    # 但 ANTHROPIC_API_KEY 是裸 env，单独处理）
    if not s.anthropic_api_key:
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not s.enhance_api_key:
        s.enhance_api_key = s.anthropic_api_key

    s = s.derive_paths()

    # 必要目录
    s.demo_runs_dir.mkdir(parents=True, exist_ok=True)
    if s.custom_task_dir:
        s.custom_task_dir.mkdir(parents=True, exist_ok=True)
    s.agent_cache_dir.mkdir(parents=True, exist_ok=True)

    # Merge agent registry（active entries 进 s.agents；同 id registry 覆盖 DEFAULT_AGENTS）
    _merge_registry_into_agents(s)

    return s


def _merge_registry_into_agents(s: "Settings") -> None:
    """读 agent_registry.yaml，把 active entries 加进 s.agents（同 id 替换）。

    分离成函数方便 reload（admin endpoint 注册新 agent 后 hot-reload 用）。
    """
    # 延迟 import 避免 circular（services.agent_registry 不能在 module top 导入 settings）
    from .services.agent_registry import load_registry, to_agent_entry_dicts

    if not s.agent_registry_yaml.exists():
        return
    try:
        reg = load_registry(s.agent_registry_yaml)
    except Exception as e:
        # 不让 startup 因 registry 损坏整个崩；打印 warning 跳过
        print(f"[settings] WARNING: failed to load agent registry from "
              f"{s.agent_registry_yaml}: {e}")
        return
    extra_dicts = to_agent_entry_dicts(reg)
    if not extra_dicts:
        return
    # 同 id 时 registry 覆盖（dict by id 后再 list 化）
    by_id: dict[str, AgentEntry] = {a.id: a for a in s.agents}
    for d in extra_dicts:
        by_id[d["id"]] = AgentEntry(**d)
    s.agents = list(by_id.values())


def reload_agent_registry(s: "Settings") -> None:
    """admin endpoint 注册新 agent 后调，让 in-memory s.agents 立即反映新 registry。"""
    _merge_registry_into_agents(s)


# 模块级单例（FastAPI lifespan 会重新 load 一次写到 app.state）
settings = load_settings()
