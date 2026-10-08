"""Shared fakes so the CLI can be tested without touching the network."""

from types import SimpleNamespace
from typing import Any

import pytest
from solders.hash import Hash
from solders.pubkey import Pubkey

from solana_tools import cli

LAMPORTS_PER_TOKEN_ACCOUNT = 2_039_280
FAKE_TX_FEE = 5_000


def make_rpc_account(ui_amount: float, lamports: int = LAMPORTS_PER_TOKEN_ACCOUNT) -> Any:
    """Mimic one item of ``get_token_accounts_by_owner_json_parsed(...).value``."""
    return SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        account=SimpleNamespace(
            lamports=lamports,
            data=SimpleNamespace(parsed={"info": {"tokenAmount": {"uiAmount": ui_amount}}}),
        ),
    )


class FakeAsyncClient:
    """Stand-in for ``solana.rpc.async_api.AsyncClient``."""

    def __init__(self, accounts_by_program: dict[Pubkey, list[Any]], connected: bool = True):
        self.accounts_by_program = accounts_by_program
        self.connected = connected
        self.closed = False

    async def is_connected(self) -> bool:
        return self.connected

    async def get_token_accounts_by_owner_json_parsed(self, owner: Pubkey, opts: Any) -> Any:
        return SimpleNamespace(value=self.accounts_by_program.get(opts.program_id, []))

    async def get_latest_blockhash(self) -> Any:
        return SimpleNamespace(value=SimpleNamespace(blockhash=Hash.new_unique()))

    async def get_fee_for_message(self, message: Any) -> Any:
        return SimpleNamespace(value=FAKE_TX_FEE)

    async def close(self) -> None:
        self.closed = True


class FakeJitoClient:
    """Stand-in for ``jito_py_rpc.JitoJsonRpcSDK`` that records sent transactions and bundles."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.tip_account = Pubkey.new_unique()
        self.sent: list[str] = []
        self.bundles: list[list[str]] = []

    def get_random_tip_account(self) -> str:
        return str(self.tip_account)

    def send_txn(self, params: str, bundleOnly: bool = False) -> dict[str, Any]:
        self.sent.append(params)
        if self.fail:
            return {"success": False, "error": "rejected"}
        return {"success": True, "data": {"result": f"sig{len(self.sent)}"}}

    def send_bundle(self, bundle: list[str]) -> dict[str, Any]:
        self.bundles.append(bundle)
        if self.fail:
            return {"success": False, "error": "rejected"}
        return {"success": True, "data": {"result": f"bundle{len(self.bundles)}"}}


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the 1-second Jito rate-limit pause."""

    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", _sleep)
