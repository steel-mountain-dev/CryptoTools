import subprocess
import sys

import solana_tools


def test_version_is_exposed() -> None:
    assert solana_tools.__version__ == "0.1.0"


def test_python_dash_m_runs_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "solana_tools", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "show" in result.stdout
    assert "close_accounts" in result.stdout
