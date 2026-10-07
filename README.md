# CryptoTools

A collection of command-line tools for crypto traders, with one Python package per blockchain.
Each package installs and runs on its own.

## Packages

| Chain  | Package             | Description                                                        |
|--------|---------------------|--------------------------------------------------------------------|
| Solana | [`solana/`](solana/README.md) | Finds empty SPL token accounts in a wallet and closes them to recover their rent SOL |

Installation and usage steps are in each package's README.

## Repository layout

```text
CryptoTools/
├── README.md        # this file
└── solana/          # Solana tools (solana-tools CLI)
    ├── README.md
    ├── pyproject.toml
    ├── src/solana_tools/
    └── tests/
```
