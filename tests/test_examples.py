"""Every integration example must actually run — examples are documentation
that can rot; running them in CI keeps them honest."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent.parent / "examples"

RUNNABLE = [
    "demo.py",
    "openai_sdk.py",
    "anthropic_sdk.py",
]


@pytest.mark.parametrize("name", RUNNABLE)
def test_example_runs(name: str) -> None:
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / name)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["langgraph_langchain.py", "openai_agents_sdk.py"])
def test_framework_example_runs(name: str) -> None:
    pytest.importorskip("langgraph")
    pytest.importorskip("agents")
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / name)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
