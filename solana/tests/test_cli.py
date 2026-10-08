import asyncio
import base64
import struct

import pytest
import spl.token.instructions as spl_token
from solana.constants import LAMPORTS_PER_SOL
from solders.account import Account
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from solders.transaction_metadata import TransactionMetadata
from spl.token.models import CloseAccountParams

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
PER_TX = cli.MAX_CLOSE_INSTRUCTIONS_PER_TRANSACTION
PER_BUNDLE = cli.JITO_TRANSACTIONS_PER_BUNDLE
MAX_V1_ACCOUNT_KEYS = 64


def empty_accounts(count: int) -> list:
    return [make_rpc_account(0.0) for _ in range(count)]


# --- argument parsing -------------------------------------------------------


@pytest.mark.parametrize("command", ["show", "close_accounts"])
def test_parser_accepts_commands(command: str) -> None:
    assert cli.build_parser().parse_args([command]).command == command


@pytest.mark.parametrize(
    ("argv", "use_bundles"),
    [(["close_accounts"], False), (["close_accounts", "--bundles"], True)],
)
def test_parser_bundles_flag(argv: list[str], use_bundles: bool) -> None:
    assert cli.build_parser().parse_args(argv).bundles is use_bundles


@pytest.mark.parametrize("argv", [[], ["unknown"], ["show", "--bundles"]])
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


@pytest.mark.parametrize(
    ("argv", "use_bundles"),
    [(["close_accounts"], False), (["close_accounts", "--bundles"], True)],
)
def test_main_dispatches_close_accounts(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], use_bundles: bool
) -> None:
    called = []

    async def fake_close(use_bundles: bool = False) -> None:
        called.append(("close", use_bundles))

    monkeypatch.setattr(cli, "_close_all_accounts", fake_close)
    assert cli.main(argv) == 0
    assert called == [("close", use_bundles)]


def test_main_reports_errors_and_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def boom() -> None:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(cli, "_show_wallet_status", boom)
    assert cli.main(["show"]) == 1
    assert "rpc down" in capsys.readouterr().out


# --- console colors ---------------------------------------------------------


def test_console_colors_plain_when_terminal_cannot_colorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("_colorize.can_colorize", lambda **_k: False)
    assert cli.ConsoleColors().title("hello") == "hello"


def test_console_colors_ansi_when_terminal_can_colorize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("_colorize.can_colorize", lambda **_k: True)
    colors = cli.ConsoleColors()
    for styled in (colors.title("x"), colors.section("x"), colors.data("x"),
                   colors.summary("x"), colors.error("x")):
        assert "x" in styled
        assert "\x1b[" in styled


# --- jito tip floor ---------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: object, error: Exception | None = None):
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> object:
        return self.payload


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([{"landed_tips_25th_percentile": 0.00001}], 10_500),  # 10_000 lamports + 5% buffer
        ([{"landed_tips_25th_percentile": 0.0000001}], cli.MIN_JITO_TIP),  # below the floor
        ([{}], cli.MIN_JITO_TIP),  # field missing
        ([], cli.MIN_JITO_TIP),  # no data
    ],
)
def test_jito_tip_floor(monkeypatch: pytest.MonkeyPatch, payload: object, expected: int) -> None:
    calls = []

    def fake_get(url: str, timeout: float) -> FakeResponse:
        calls.append((url, timeout))
        return FakeResponse(payload)

    monkeypatch.setattr(cli.requests, "get", fake_get)
    assert cli._get_minimum_jito_tip_in_lamports() == expected
    assert calls == [(cli.JITO_BUNDLES_URL, 5)]


@pytest.mark.parametrize(
    "fake_get",
    [
        lambda *_a, **_k: (_ for _ in ()).throw(ConnectionError("offline")),
        lambda *_a, **_k: FakeResponse(None, error=RuntimeError("503 offline")),
    ],
    ids=["request-fails", "http-error"],
)
def test_jito_tip_floor_falls_back_when_api_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_get: object
) -> None:
    monkeypatch.setattr(cli.requests, "get", fake_get)
    assert cli._get_minimum_jito_tip_in_lamports() == cli.MIN_JITO_TIP
    out = capsys.readouterr().out
    assert "Failed to fetch Jito tip floor" in out
    assert "offline" in out


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
    accounts = {KEG: empty_accounts(PER_TX), T2022: empty_accounts(1)}
    monkeypatch.setattr(cli, "AsyncClient", lambda _url: FakeAsyncClient(accounts))
    monkeypatch.setattr("builtins.input", lambda _prompt: str(Pubkey.new_unique()))

    asyncio.run(cli._show_wallet_status())
    out = capsys.readouterr().out

    total = PER_TX + 1
    lamports = total * LAMPORTS_PER_TOKEN_ACCOUNT
    # one more account than fits in a transaction -> 2 transactions, each paying tip + fee
    cost = 2 * (cli.MIN_JITO_TIP + FAKE_TX_FEE)
    assert f"{total} =>  {PER_TX} token-keg + 1 token-2020" in out
    assert f"{lamports / LAMPORTS_PER_SOL:.2f}" in out
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


def _decode_v1(encoded: str, keypair: Keypair) -> VersionedTransaction:
    """Decode a sent transaction and check what every close transaction must satisfy."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(encoded))
    assert int(tx.version()) == 1
    assert all(tx.verify_with_results())  # signed by the wallet
    tx.sanitize()  # raises if the message is malformed, e.g. more than 64 account keys
    assert len(tx.message.account_keys) <= MAX_V1_ACCOUNT_KEYS
    assert tx.message.account_keys[0] == keypair.pubkey()  # wallet pays the fees
    # v1 has no network defaults: an unset limit is 0 and the transaction fails on mainnet
    config = tx.message.config
    assert config.compute_unit_limit == (
        cli.COMPUTE_UNITS_PER_INSTRUCTION * len(tx.message.instructions)
    )
    assert config.loaded_accounts_data_size_limit == cli.MAX_LOADED_ACCOUNTS_DATA_SIZE
    return tx


def _tip_data(keypair: Keypair, tip_account: Pubkey, lamports: int) -> bytes:
    return bytes(transfer(TransferParams(
        from_pubkey=keypair.pubkey(), to_pubkey=tip_account, lamports=lamports
    )).data)


@pytest.mark.usefixtures("no_sleep")
def test_close_accounts_batches_and_signs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keypair = Keypair()
    # The second batch mixes both token programs: the worst case for the 64-account v1 limit.
    rpc = FakeAsyncClient({KEG: empty_accounts(PER_TX + 3), T2022: empty_accounts(PER_TX + 2)})
    jito = FakeJitoClient()
    _patch_close(monkeypatch, keypair, rpc, jito)

    asyncio.run(cli._close_all_accounts())
    out = capsys.readouterr().out

    total = 2 * PER_TX + 5
    assert len(jito.sent) == 3
    sizes = []
    for encoded in jito.sent:
        tx = _decode_v1(encoded, keypair)
        # every transaction starts with the Jito tip
        tip = tx.message.instructions[0]
        assert tx.message.account_keys[tip.accounts[1]] == jito.tip_account
        assert bytes(tip.data) == _tip_data(keypair, jito.tip_account, cli.MIN_JITO_TIP)
        sizes.append(len(tx.message.instructions) - 1)  # minus the Jito tip transfer
    assert sizes == [PER_TX, PER_TX, 5]

    # progress counter is cumulative
    assert f"Accounts Processed: {PER_TX}/{total}" in out
    assert f"Accounts Processed: {2 * PER_TX}/{total}" in out
    assert f"Accounts Processed: {total}/{total}" in out
    assert "['sig1', 'sig2', 'sig3']" in out
    assert rpc.closed


@pytest.mark.usefixtures("no_sleep")
def test_close_accounts_in_bundles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_tip = 12_345
    keypair = Keypair()
    # One full bundle plus a 2-transaction bundle. The second bundle's first transaction carries
    # the tip and mixes both token programs: the worst case for the 64-account v1 limit.
    total = PER_TX * PER_BUNDLE + PER_TX + 5
    keg = PER_TX * PER_BUNDLE + 1
    rpc = FakeAsyncClient({KEG: empty_accounts(keg), T2022: empty_accounts(total - keg)})
    jito = FakeJitoClient()
    _patch_close(monkeypatch, keypair, rpc, jito)
    monkeypatch.setattr(cli, "_get_minimum_jito_tip_in_lamports", lambda: bundle_tip)

    asyncio.run(cli._close_all_accounts(use_bundles=True))
    out = capsys.readouterr().out

    assert jito.sent == []
    assert [len(bundle) for bundle in jito.bundles] == [PER_BUNDLE, 2]
    closes = []
    for bundle in jito.bundles:
        for index, encoded in enumerate(bundle):
            tx = _decode_v1(encoded, keypair)
            instructions = tx.message.instructions
            if index == 0:  # only the first transaction of each bundle tips Jito
                tip = instructions[0]
                assert tx.message.account_keys[tip.accounts[1]] == jito.tip_account
                assert bytes(tip.data) == _tip_data(keypair, jito.tip_account, bundle_tip)
                instructions = instructions[1:]
            else:
                assert jito.tip_account not in tx.message.account_keys
            closes.append(len(instructions))
    assert closes == [PER_TX] * PER_BUNDLE + [PER_TX, 5]

    assert f"Accounts Processed: {PER_TX * PER_BUNDLE}/{total}" in out
    assert f"Accounts Processed: {total}/{total}" in out
    assert "Bundle IDs: ['bundle1', 'bundle2']" in out
    assert rpc.closed


def test_close_accounts_reports_failed_bundles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rpc = FakeAsyncClient({KEG: empty_accounts(1)})
    _patch_close(monkeypatch, Keypair(), rpc, FakeJitoClient(fail=True))
    monkeypatch.setattr(cli, "_get_minimum_jito_tip_in_lamports", lambda: cli.MIN_JITO_TIP)

    asyncio.run(cli._close_all_accounts(use_bundles=True))
    out = capsys.readouterr().out

    assert "Failed to send bundle: rejected" in out
    assert "Bundle IDs: []" in out


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


# --- transaction limits (executed in a local Solana VM) ---------------------

# Loading the Token-2022 program on mainnet is ~1.38 MB, a full mixed batch ~1.5 MB.
MAINNET_WORST_CASE_LOADED_BYTES = 1_504_674


def _token_account_data(owner: Pubkey, program: Pubkey) -> bytes:
    """An initialized, empty SPL token account, as the token programs lay it out."""
    data = (
        bytes(Pubkey.new_unique())  # mint
        + bytes(owner)
        + struct.pack("<Q", 0)  # amount
        + bytes(36)  # delegate: none
        + b"\x01"  # state: initialized
        + bytes(12)  # is_native: none
        + struct.pack("<Q", 0)  # delegated amount
        + bytes(36)  # close authority: none
    )
    if program == T2022:  # account type + ImmutableOwner extension, like real Token-2022 accounts
        data += b"\x02" + struct.pack("<HH", 7, 0)
    return data


@pytest.mark.parametrize(
    "counts",
    [{T2022: PER_TX}, {KEG: PER_TX}, {KEG: PER_TX // 2, T2022: PER_TX - PER_TX // 2}],
    ids=["all-token-2022", "all-token-keg", "mixed"],
)
def test_full_batch_executes_within_its_compute_budget(counts: dict[Pubkey, int]) -> None:
    svm = LiteSVM()
    owner = Keypair()
    tip_account = Pubkey.new_unique()
    svm.airdrop(owner.pubkey(), 10**10)
    svm.airdrop(tip_account, 10**9)

    instructions = [transfer(TransferParams(
        from_pubkey=owner.pubkey(), to_pubkey=tip_account, lamports=cli.MIN_JITO_TIP
    ))]
    closed = []
    for program, count in counts.items():
        for _ in range(count):
            address = Pubkey.new_unique()
            data = _token_account_data(owner.pubkey(), program)
            rent = svm.minimum_balance_for_rent_exemption(len(data))
            svm.set_account(address, Account(rent, data, program, False, 0))
            instructions.append(spl_token.close_account(CloseAccountParams(
                program_id=program, account=address, dest=owner.pubkey(), owner=owner.pubkey()
            )))
            closed.append(address)

    tx = cli._build_v1_transaction(owner, instructions, svm.latest_blockhash())
    result = svm.send_transaction(tx)

    # LiteSVM does not enforce v1 limits, so compare what was used against what was requested
    assert isinstance(result, TransactionMetadata), result  # FailedTransactionMetadata otherwise
    assert result.compute_units_consumed() <= tx.message.config.compute_unit_limit
    assert MAINNET_WORST_CASE_LOADED_BYTES <= tx.message.config.loaded_accounts_data_size_limit
    assert all(svm.get_account(address) is None for address in closed)

