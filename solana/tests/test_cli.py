import asyncio
import base64

import pytest
from solana.constants import LAMPORTS_PER_SOL
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from solana_tools import cli
from tests.conftest import (
    FAKE_TX_FEE,
    LAMPORTS_PER_TOKEN_ACCOUNT,
    FakeAsyncClient,
    FakeJitoClient,
    make_rpc_account,
)

KEG = cli.TokenType.TOKEN_KEG.address
T2022 = cli.TokenType.TOKEN_2022.address


# --- argument parsing -------------------------------------------------------


@pytest.mark.parametrize("command", ["show", "close_accounts"])
def test_parser_accepts_commands(command: str) -> None:
    assert cli.build_parser().parse_args([command]).command == command


@pytest.mark.parametrize("argv", [[], ["unknown"]])
def test_parser_rejects_missing_or_unknown_command(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


# --- main dispatch ----------------------------------------------------------


def test_main_dispatches_show(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    async def fake_show() -> None:
        called.append("show")

    monkeypatch.setattr(cli, "_show_wallet_status", fake_show)
    assert cli.main(["show"]) == 0
    assert called == ["show"]


def test_main_dispatches_close_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    async def fake_close() -> None:
        called.append("close")

    monkeypatch.setattr(cli, "_close_all_accounts", fake_close)
    assert cli.main(["close_accounts"]) == 0
    assert called == ["close"]


def test_main_reports_errors_and_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def boom() -> None:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(cli, "_show_wallet_status", boom)
    assert cli.main(["show"]) == 1
    assert "rpc down" in capsys.readouterr().out


# --- token type -------------------------------------------------------------


def test_token_type_addresses() -> None:
    assert KEG == Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    assert T2022 == Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


# --- get_zero_balance_token_accounts ---------------------------------------


def test_zero_balance_accounts_are_filtered_across_both_programs() -> None:
    empty_keg = make_rpc_account(0.0)
    funded_keg = make_rpc_account(12.5)
    empty_2022 = make_rpc_account(0.0, lamports=3_000_000)
    client = FakeAsyncClient({KEG: [empty_keg, funded_keg], T2022: [empty_2022]})

    result = asyncio.run(cli._get_zero_balance_token_accounts(Pubkey.new_unique(), client))

    assert result == [
        cli.TokenAccount(empty_keg.pubkey, LAMPORTS_PER_TOKEN_ACCOUNT, KEG),
        cli.TokenAccount(empty_2022.pubkey, 3_000_000, T2022),
    ]


def test_zero_balance_accounts_requires_connection() -> None:
    client = FakeAsyncClient({}, connected=False)
    with pytest.raises(Exception, match="not connected"):
        asyncio.run(cli._get_zero_balance_token_accounts(Pubkey.new_unique(), client))


# --- get_min_transaction_fee -----------------------------------------------


def test_min_transaction_fee() -> None:
    assert asyncio.run(cli._get_min_transaction_fee(FakeAsyncClient({}))) == FAKE_TX_FEE


# --- show -------------------------------------------------------------------


def test_show_reports_balances_and_costs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts = {KEG: [make_rpc_account(0.0) for _ in range(21)], T2022: [make_rpc_account(0.0)]}
    monkeypatch.setattr(cli, "AsyncClient", lambda _url: FakeAsyncClient(accounts))
    monkeypatch.setattr("builtins.input", lambda _prompt: str(Pubkey.new_unique()))

    asyncio.run(cli._show_wallet_status())
    out = capsys.readouterr().out

    lamports = 22 * LAMPORTS_PER_TOKEN_ACCOUNT
    # 22 accounts -> 2 transactions of up to 20 closes, each paying tip + fee
    cost = 2 * (cli.MIN_JITO_TIP + FAKE_TX_FEE)
    assert "22 =>  21 token-keg + 1 token-2020" in out
    assert str(lamports / LAMPORTS_PER_SOL) in out
    assert str(lamports) in out
    assert f"{cost / LAMPORTS_PER_SOL:.6f} SOL" in out


def test_show_without_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "  ")
    asyncio.run(cli._show_wallet_status())
    assert "not provided" in capsys.readouterr().out


# --- close_accounts ---------------------------------------------------------


def _patch_close(
    monkeypatch: pytest.MonkeyPatch, keypair: Keypair, rpc: FakeAsyncClient, jito: FakeJitoClient
) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_a, **_k: str(keypair))
    monkeypatch.setattr(cli, "AsyncClient", lambda _url: rpc)
    monkeypatch.setattr(cli, "JitoJsonRpcSDK", lambda url: jito)


@pytest.mark.usefixtures("no_sleep")
def test_close_accounts_batches_and_signs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keypair = Keypair()
    rpc = FakeAsyncClient(
        {KEG: [make_rpc_account(0.0) for _ in range(40)], T2022: [make_rpc_account(0.0)] * 5}
    )
    jito = FakeJitoClient()
    _patch_close(monkeypatch, keypair, rpc, jito)

    asyncio.run(cli._close_all_accounts())
    out = capsys.readouterr().out

    # 45 accounts -> batches of 20, 20, 5
    assert len(jito.sent) == 3
    sizes = []
    for encoded in jito.sent:
        tx = Transaction.from_bytes(base64.b64decode(encoded))
        tx.verify()  # signed by the wallet
        assert tx.message.account_keys[0] == keypair.pubkey()  # wallet pays the fees
        sizes.append(len(tx.message.instructions) - 1)  # minus the Jito tip transfer
    assert sizes == [20, 20, 5]

    # progress counter is cumulative
    assert "Accounts Processed: 20/45" in out
    assert "Accounts Processed: 40/45" in out
    assert "Accounts Processed: 45/45" in out
    assert "['sig1', 'sig2', 'sig3']" in out
    assert rpc.closed


@pytest.mark.usefixtures("no_sleep")
def test_close_accounts_reports_failed_sends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rpc = FakeAsyncClient({KEG: [make_rpc_account(0.0)]})
    _patch_close(monkeypatch, Keypair(), rpc, FakeJitoClient(fail=True))

    asyncio.run(cli._close_all_accounts())
    out = capsys.readouterr().out

    assert "Failed to send transaction: rejected" in out
    assert "Transaction IDs: []" in out


def test_close_accounts_with_nothing_to_close(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    jito = FakeJitoClient()
    _patch_close(monkeypatch, Keypair(), FakeAsyncClient({}), jito)

    asyncio.run(cli._close_all_accounts())

    assert "No accounts found" in capsys.readouterr().out
    assert jito.sent == []
