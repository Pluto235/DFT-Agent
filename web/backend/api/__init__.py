from fastapi import APIRouter

from . import admin, compose, config, leaderboard, runs, subsets, tasks, tokens


def all_routers() -> list[APIRouter]:
    return [
        config.router, tasks.router, runs.router, compose.router,
        admin.router, leaderboard.router, subsets.router, tokens.router,
    ]
