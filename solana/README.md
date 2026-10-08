# CryptoTools – Solana

Command-line tools for Solana traders. The `solana-tools` CLI finds the empty (zero-balance)
SPL token accounts in a wallet and can close them to recover the SOL locked up as rent.

Both token programs are supported: SPL Token (Tokenkeg) and Token-2022.

> **Note:** The tools connect to **Solana mainnet** (`https://api.mainnet.solana.com`), and
> `close_accounts` sends real transactions through the Jito block engine. Each transaction
> closes up to 59 accounts and costs the network fee plus a 1,000-lamport Jito tip.

## Requirements

- Python **3.14** or newer. The CLI uses features that first appeared in 3.14, such as
  masked password input.
- `git`, to clone the repository

## 1. Install Python 3.14

Choose one of these options.

**macOS (Homebrew)**

```bash
brew install python@3.14
```

**pyenv (macOS / Linux)**

```bash
pyenv install 3.14
pyenv local 3.14
```

**uv**

```bash
uv python install 3.14
```

**Windows / other platforms:** download the installer from
[python.org/downloads](https://www.python.org/downloads/).

Check the installed version:

```bash
python3.14 --version
```

## 2. Set up the environment

The project lives in the `solana/` folder of the `CryptoTools` repository.

```bash
git clone <repository-url> CryptoTools
cd CryptoTools/solana
```

Create a virtual environment and activate it:

```bash
python3.14 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

Install the package in editable mode:

```bash
pip install --upgrade pip
pip install -e .
```

For development, install the extra tools as well (pytest, ruff, mypy):

```bash
pip install -e ".[dev]"
```

Runtime dependencies (installed automatically):

| Package       | Purpose                                     |
|---------------|---------------------------------------------|
| `solana`      | Async Solana RPC client and SPL Token helpers |
| `solders`     | Solana types: keys, messages, transactions  |
| `jito-py-rpc` | Sends transactions through the Jito block engine |

## 3. Usage

Activate the virtual environment first (`source .venv/bin/activate`), then run:

```bash
solana-tools --help
```

### `show`: report closable accounts

Read-only. The command asks for a **public wallet address**, lists its zero-balance token
accounts and estimates the cost of closing them.

```bash
solana-tools show
```

```text
Wallet address: <your-public-address>
Balance information
    closable accounts:          12 =>  10 token-keg + 2 token-2020
    recoverable SOL:            0.02448
    lamports:                   24480000
    approximate close costs:    0.000006 SOL
```

### `close_accounts`: close zero-balance accounts

The command asks for the wallet's **base58 private key**, which is hidden as you type. It then
closes every zero-balance token account and sends the recovered SOL back to the same wallet.

```bash
solana-tools close_accounts
```

```text
Wallet private key: ********
address: <your-public-address>
Accounts Processed: 12/12
Transaction IDs: ['5x...', ...]
```

How it works:

- Up to 59 close instructions go into each transaction.
- Each transaction includes a Jito tip and is sent through the Jito block engine.
- Transactions go out at most once per second, to respect Jito's rate limit.

#### `--bundles`: send Jito bundles instead

```bash
solana-tools close_accounts --bundles
```

```text
Wallet private key: ********
address: <your-public-address>
Accounts Processed: 12/12
Bundle IDs: ['3f...', ...]
```

- Transactions are grouped into Jito bundles of up to 5, so up to 295 accounts per bundle.
- Only the first transaction of each bundle pays a tip. The tip is Jito's live tip floor
  (25th percentile of landed tips plus 5%), never less than 1,000 lamports. If the tip floor
  can't be fetched, it falls back to 1,000 lamports.
- A bundle lands all-or-nothing: if one transaction in it fails, none of them are applied.

> **Security:** Your private key never leaves your machine and is not saved. It only signs
> transactions locally. Run this command only on a machine you trust, and run `show` first
> to check what will be closed.

## Development

```bash
pytest                       # run the tests
ruff check src tests         # lint
ruff format src tests        # format
mypy src                     # type-check
```

## License

MIT
