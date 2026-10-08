import runpy
import subprocess
import sys
import warnings

import pytest

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


@pytest.mark.parametrize("module", ["solana_tools", "solana_tools.cli"])
def test_main_module_entry_points(module: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process twin of the subprocess test above, so coverage sees the ``__main__`` guards."""
    monkeypatch.setattr(sys, "argv", ["solana-tools", "--help"])
    with warnings.catch_warnings():
        # re-running an already imported module as __main__ warns; that is expected here
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as exc:
            runpy.run_module(module, run_name="__main__")
    assert exc.value.code == 0
