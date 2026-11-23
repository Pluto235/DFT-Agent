"""GPU 分配池：spawn 时挑 active-run 数最少的 GPU 注入 CUDA_VISIBLE_DEVICES。

为什么需要：QE wrapper `qe-gpu-wrap.sh` 看 caller 的 `CUDA_VISIBLE_DEVICES`，没设
就 fallback 0；backend 之前没注入，所有 task 钉 GPU 0，多卡时其它 GPU 闲置。

策略：active-run-count 最少优先（不是 round-robin）—— 长短任务混合时 RR 容易把
多个长任务挤同一卡；count-based 保证任意时刻每卡 active 数差 ≤ 1。

不可用 GPU 列表：
  1. 启动时 nvidia-smi 探测；失败时 fallback config_kv['available_gpus']（"0,1" 字符串）；
     再失败 fallback ['0']（单卡）
  2. 没 nvidia-smi 也允许（CPU-only 部署）：`ids=[]`，acquire 永远返 None → 不注入 env

终态后必须 release，否则 count 永远涨。release 防 negative：max(0, ...)。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from typing import Callable, Optional


log = logging.getLogger("gpu_allocator")


def detect_gpus() -> list[int]:
    """nvidia-smi 失败 / 不存在时返 []，由 caller fallback。"""
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("nvidia-smi probe failed: %s", e)
        return []
    if out.returncode != 0:
        log.warning("nvidia-smi probe returncode=%s stderr=%s", out.returncode, out.stderr.strip())
        return []
    ids: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ids.append(int(line))
        except ValueError:
            log.warning("nvidia-smi bad index line: %r", line)
    return ids


class GpuAllocator:
    """thread-safe（asyncio）active-run-count 最少优先分配器。

    `enabled_resolver`：admin policy 读取入口；每次 acquire 调一次拿当前 enabled 集合。
    动态读取（不 cache）让 dev 在 admin 面板按 POST /api/admin/gpu_policy 后下一次
    promote 立即生效。None → 不过滤（兼容旧调用）。
    """

    def __init__(
        self,
        ids: list[int],
        enabled_resolver: Optional[Callable[[], set[int]]] = None,
    ):
        # ids 可空（CPU-only / 探测失败）— acquire 返 None
        self._counts: dict[int, int] = {i: 0 for i in ids}
        self._lock = asyncio.Lock()
        self._enabled_resolver = enabled_resolver

    @property
    def ids(self) -> list[int]:
        return list(self._counts.keys())

    def pool_ids(self) -> list[int]:
        """当前允许的 GPU id 集（detected ∩ enabled policy），sorted asc。
        当 settings.qe_gpu_queueing_mode == "wrapper_flock" 时 runners 用它拼
        QE_GPU_POOL env 透传给 solver subprocess —— wrapper 在 pool 上 flock 抢锁。

        与 acquire() 区别：不取 lock、不改 _counts，只读快照。"""
        detected = list(self._counts.keys())
        if self._enabled_resolver is None:
            return sorted(detected)
        try:
            enabled = self._enabled_resolver()
        except Exception:
            log.exception("gpu enabled_resolver raised in pool_ids; returning unfiltered")
            return sorted(detected)
        return sorted(g for g in detected if g in enabled)

    async def acquire(self, enabled: Optional[set[int]] = None) -> Optional[int]:
        """返回选中的 GPU id；空池 / `enabled` 过滤后空集 → None（caller 不注入 CUDA_VISIBLE_DEVICES）。

        `enabled`：caller 直传的过滤集合；优先级高于构造期 resolver。
        都没有 → 不过滤（和初版语义一致）。
        空 set 显式表"全 disable" → 永远返 None（管理员显式禁用所有 GPU 等价
        scheduler 暂停），已 running 不受影响。"""
        if not self._counts:
            return None
        async with self._lock:
            candidates = list(self._counts.keys())
            effective: Optional[set[int]] = enabled
            if effective is None and self._enabled_resolver is not None:
                try:
                    effective = self._enabled_resolver()
                except Exception:
                    log.exception("gpu enabled_resolver raised; treating as no filter")
                    effective = None
            if effective is not None:
                candidates = [i for i in candidates if i in effective]
            if not candidates:
                return None
            # min by count，相等取 id 较小的（稳定）
            gid = min(candidates, key=lambda i: (self._counts[i], i))
            self._counts[gid] += 1
            return gid

    async def release(self, gpu_id: Optional[int]) -> None:
        if gpu_id is None:
            return
        async with self._lock:
            cur = self._counts.get(gpu_id)
            if cur is None:
                # 重启后老 RunHandle 的 gpu_id 不在新 dict 里，跳过
                return
            self._counts[gpu_id] = max(0, cur - 1)

    def snapshot(self) -> dict[int, int]:
        # 不取 lock — 只读用于 /api/runs:scheduler_state 偶尔展示，eventually-consistent OK
        return dict(self._counts)
