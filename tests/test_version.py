"""Версия объявлена в двух местах: её видит и пакет, и потребитель — они обязаны совпадать.

Проверка стоит здесь, а не «на глаз при выпуске»: расхождение означает, что колесо установится под одной
версией, а назовётся другой, и разбираться в этом будет тот, у кого сломался образ.
"""
from __future__ import annotations

import pathlib
import tomllib

import agent_core

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_package_version_matches_pyproject():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert agent_core.__version__ == declared, (
        f"версия пакета ({agent_core.__version__}) не совпадает с pyproject.toml ({declared})"
    )
