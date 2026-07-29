"""CLI entry point.

Usage:
  python agent/solve.py \
    --prompt  <path_to_task_prompt.txt> \
    --workdir <path_to_workdir> \
    --out     <path_to_result.json> \
    --model   claude-sonnet-4-6 \
    [--provider auto] \
    [--max-turns 100] [--max-output-tokens 16384]

Provider 自动从 --model 前缀检(claude-* / gpt- / gemini- / deepseek- /
grok- / mistral- / ollama/ / <owner>/<name>);如歧义传 --provider 显式覆盖。
"""
import argparse
import sys
from pathlib import Path

from agent.llm import LLMClient
from agent.loop import run_agent_loop


def main() -> int:
    parser = argparse.ArgumentParser(description="DFT-agent bench solver")
    parser.add_argument("--prompt", required=True, help="Path to task_prompt.txt")
    parser.add_argument("--workdir", required=True, help="Agent working directory")
    parser.add_argument("--out", required=True, help="Path to result.json to write")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument(
        "--provider", default=None,
        help="Override auto-detected provider; one of providers configured in providers.yaml "
             "(default: anthropic | openai | google | deepseek | moonshot | xai | mistral | "
             "openrouter | ollama). Pass when model id is ambiguous.",
    )
    parser.add_argument(
        "--config", default=None, type=Path,
        help="Path to providers.yaml override; default = bundled agent/providers.yaml "
             "(also resolvable via DFT_SOLVER_CONFIG env var).",
    )
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    args = parser.parse_args()

    system_prompt_path = Path(__file__).parent / "prompts" / "system.txt"
    system_prompt = system_prompt_path.read_text(encoding="utf-8")

    llm = LLMClient(model=args.model, provider=args.provider, config_path=args.config)

    return run_agent_loop(
        llm=llm,
        system_prompt=system_prompt,
        task_prompt_path=Path(args.prompt),
        workdir=Path(args.workdir),
        out_path=Path(args.out),
        max_turns=args.max_turns,
        max_output_tokens=args.max_output_tokens,
    )


if __name__ == "__main__":
    sys.exit(main())
