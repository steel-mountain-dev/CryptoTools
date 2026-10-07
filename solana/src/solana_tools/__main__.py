"""Allow ``python -m solana_tools`` alongside the installed ``solana-tools`` script."""

from solana_tools.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
