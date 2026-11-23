"""v5 runtime layer — claude-agent-sdk orchestration.

driver.py          — ClaudeSDKClient loop driver
hooks.py           — PreToolUse / PostToolUse hooks (lint, ledger, transcript)
progress_ledger.py — Magentic-One-style turn-by-turn stuck detector (S1-S7)
memory.py          — in-session calc_id store
transcript.py      — v4-compatible transcript.json writer
"""
