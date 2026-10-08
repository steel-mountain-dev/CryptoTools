import argparse
import asyncio
import base64
import getpass
import math
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import batched
from typing import Final

import requests
from jito_py_rpc import JitoJsonRpcSDK
from solana.constants import LAMPORTS_PER_SOL
from solana.rpc.async_api import AsyncClient
from solana.rpc.models import TokenAccountOpts

from solders.solders import Pubkey, Keypair, MessageV1, TransactionConfig, Instruction
from solders.hash import Hash
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

import spl.token.instructions as spl_token
from spl.token.constants import TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID
from spl.token.models import CloseAccountParams

# Cluster	Public RPC endpoint	Description
# Mainnet	https://api.mainnet.solana.com	Production network using real SOL.
# Devnet	https://api.devnet.solana.com	Developer testing network. Use the Solana Faucet to get Devnet SOL.
# Testnet	https://api.testnet.solana.com	Validator testing network.

MAX_CLOSE_INSTRUCTIONS_PER_TRANSACTION: Final[int] = 59
# v1 transactions get no network defaults: an unset compute-unit or loaded-data limit is 0 and
# the transaction fails (MaxLoadedAccountsDataSizeExceeded). Measured on mainnet: a Token-2022
# close uses ~1,464 CU, a Token-Keg close ~120 CU and the tip transfer 150 CU, so 3,000 CU per
# instruction leaves 2x headroom. Loading the Token-2022 program alone is ~1.38 MB; 64 MiB is the
# runtime maximum and what legacy transactions get by default.
COMPUTE_UNITS_PER_INSTRUCTION: Final[int] = 3_000
MAX_LOADED_ACCOUNTS_DATA_SIZE: Final[int] = 64 * 1024 * 1024
MIN_JITO_TIP: Final[int] = 1000
JITO_RPC_SDK:  Final[str] = 'https://mainnet.block-engine.jito.wtf/api/v1'
JITO_BUNDLES_URL:  Final[str] = 'https://bundles.jito.wtf/api/v1/bundles/tip_floor'
JITO_TRANSACTIONS_PER_BUNDLE: Final[int] = 5
SOLANA_RPC:  Final[str] = 'https://api.mainnet.solana.com'




def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="solana-tools",
        description=(
            "SOLANA convenience tools for traders"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sub.add_parser("show", help="Retrieve all zero balance accounts and report")
    close = sub.add_parser("close_accounts", help="Close all zero balance token accounts")
    close.add_argument(
        "--bundles",
        action="store_true",
        help=f"send Jito bundles of up to {JITO_TRANSACTIONS_PER_BUNDLE} transactions, "
             "with one tip per bundle at the live tip floor",
    )

    return parser

@dataclass(frozen=True)
class TokenAccount:
    address: Pubkey
    lamports: int
    program_id: Pubkey


class TokenType(Enum):
    TOKEN_KEG  = TOKEN_PROGRAM_ID,
    TOKEN_2022 = TOKEN_2022_PROGRAM_ID

    def __init__(self, address):
        self.address = address

class ConsoleColors:

    def __init__(self):
        from _colorize import can_colorize, decolor, get_theme

        if can_colorize():
            self._theme = get_theme(force_color=True)
            self._decolor = lambda x: x
        else:
            self._theme = get_theme(force_no_color=True)
            self._decolor = decolor

    def title(self, text: str) -> str:
        return self._decolor(f"{self._theme.argparse.prog_extra}{text}{self._theme.argparse.reset}")


    def section(self, text: str) -> str:
        return self._decolor(f"{self._theme.argparse.summary_short_option}{text}{self._theme.argparse.reset}")

    def data(self, text: str) -> str:
        return self._decolor(f"{self._theme.argparse.summary_long_option}{text}{self._theme.argparse.reset}")

    def summary(self, text: str) -> str:
        return self._decolor(f"{self._theme.argparse.summary_label}{text}{self._theme.argparse.reset}")

    def error(self, text: str) -> str:
        return self._decolor(f"{self._theme.traceback.error_highlight}{text}{self._theme.argparse.reset}")

def _get_minimum_jito_tip_in_lamports() -> int:
    """
    Fetches the live Jito tip floor and returns the 25th percentile value
    converted to Lamports, with a minor safety buffer applied.
    """

    try:
        # Request the real-time tip data from Jito's API
        response = requests.get(JITO_BUNDLES_URL, timeout=5)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list) and len(data) > 0:
            # Extract the 25th percentile tip in SOL
            current_floor_sol = data[0].get("landed_tips_25th_percentile", 0.0)

            # Convert SOL to Lamports (1 SOL = 1,000,000,000 Lamports)
            lamports = math.floor(current_floor_sol * 1_000_000_000)

            # Add a 5% buffer to absorb minor traffic variations between slots
            buffered_lamports = math.floor(lamports * 1.05)

            # Ensure it never drops below an absolute minimum baseline (e.g., 1,000 lamports)
            return max(buffered_lamports, MIN_JITO_TIP)

    except Exception as e:
        print(f"Failed to fetch Jito tip floor due to: {e}. Falling back to static safety value.")

    # Fallback to your original static safety value in lamports if the API is down
    return MIN_JITO_TIP

async def _get_min_transaction_fee(client: AsyncClient) -> int:
    # 1. Fetch the latest blockhash
    blockhash_resp = await client.get_latest_blockhash()
    recent_blockhash = blockhash_resp.value.blockhash

    # 2. Build a dummy transfer instruction to compile into a message
    from_wallet = Pubkey.new_unique()
    ix = transfer(TransferParams(
        from_pubkey=from_wallet,
        to_pubkey=Pubkey.new_unique(),
        lamports=1000
    ))

    # 3. Create the message layout
    message = MessageV1.try_compile(from_wallet, [ix], recent_blockhash)

    # 4. Fetch fee for this specific layout
    fee_resp = await client.get_fee_for_message(message)
    return fee_resp.value or 0


async def _close_all_accounts(use_bundles: bool = False) -> None:

    colors = ConsoleColors()

    if use_bundles:
        print(colors.error("Bundles almost never land, must be low tip issues. use with caution."))

    private_key = Keypair.from_base58_string(getpass.getpass("Wallet private key: ", echo_char='*').strip())
    jito_client = JitoJsonRpcSDK(url=JITO_RPC_SDK)
    client = AsyncClient(SOLANA_RPC)

    accounts = await _get_zero_balance_token_accounts(private_key.pubkey(), client)
    print(f"address: {private_key.pubkey()}")

    if len(accounts) == 0:
        print(colors.title("No accounts found"))
        return

    jito_tip_account = Pubkey.from_string(jito_client.get_random_tip_account())

    close_instructions: list[Instruction] = []

    for token_account in accounts:
        close_instructions.append( spl_token.close_account(
            CloseAccountParams(
                program_id= token_account.program_id,
                account= token_account.address,
                dest= private_key.pubkey(),
                owner= private_key.pubkey()
            ))
        )

    if use_bundles:
        await _close_all_accounts_bundle(close_instructions, private_key, jito_client, client, jito_tip_account)
    else:
        await _close_all_accounts_single_tx(close_instructions, private_key, jito_client, client, jito_tip_account)

    await client.close()

def _build_v1_transaction(
    private_key: Keypair, instructions: list[Instruction], blockhash: Hash
) -> VersionedTransaction:
    """Compile and sign a v1 transaction with explicit limits (see COMPUTE_UNITS_PER_INSTRUCTION)."""
    config = TransactionConfig(
        compute_unit_limit=COMPUTE_UNITS_PER_INSTRUCTION * len(instructions),
        loaded_accounts_data_size_limit=MAX_LOADED_ACCOUNTS_DATA_SIZE,
    )
    message = MessageV1.try_compile(private_key.pubkey(), instructions, blockhash, config)
    return VersionedTransaction(message, [private_key])

async def _close_all_accounts_single_tx(close_instructions: list[Instruction], private_key: Keypair, jito_client: JitoJsonRpcSDK, client: AsyncClient, jito_tip_account: Pubkey) -> None:
    trx_ids = []
    processed_accounts = 0
    for instruction_set in batched(close_instructions, MAX_CLOSE_INSTRUCTIONS_PER_TRANSACTION):

        processed_accounts += len(instruction_set)
        base_instructions = [
            transfer(TransferParams(
                from_pubkey=private_key.pubkey(),
                to_pubkey=jito_tip_account,
                lamports=MIN_JITO_TIP
            ))
        ]

        recent_blockhash = await client.get_latest_blockhash()
        transaction = _build_v1_transaction(
            private_key, base_instructions + list(instruction_set), recent_blockhash.value.blockhash
        )

        result = jito_client.send_txn(params=base64.b64encode(bytes(transaction)).decode('ascii'), bundleOnly=False)

        if result['success']:
            trx_ids.append(result['data']['result'])
            print(f'\rAccounts Processed: {processed_accounts}/{len(close_instructions)}', end='')
        else:
            print(f"\nFailed to send transaction: {result.get('error', 'Unknown error')}")
        # JITO's limit, max transactions/second = 1
        await asyncio.sleep(1)

    print(f"\nTransaction IDs: {trx_ids}")

async def _close_all_accounts_bundle(close_instructions: list[Instruction], private_key: Keypair, jito_client: JitoJsonRpcSDK, client: AsyncClient, jito_tip_account: Pubkey) -> None:
    bundle_ids = []
    processed_accounts = 0
    for chunk in batched(batched(close_instructions, MAX_CLOSE_INSTRUCTIONS_PER_TRANSACTION),
                         JITO_TRANSACTIONS_PER_BUNDLE):
        bundle = []

        for index, instruction_set in enumerate(chunk):
            processed_accounts += len(instruction_set)
            base_instructions = []
            if index == 0:
                bundle_tip = _get_minimum_jito_tip_in_lamports()
                base_instructions.append(
                    transfer(TransferParams(
                        from_pubkey=private_key.pubkey(),
                        to_pubkey=jito_tip_account,
                        lamports= bundle_tip
                    ))
                )

            recent_blockhash = await client.get_latest_blockhash()
            transaction = _build_v1_transaction(
                private_key, base_instructions + list(instruction_set), recent_blockhash.value.blockhash
            )
            bundle.append(base64.b64encode(bytes(transaction)).decode('ascii'))

        result = jito_client.send_bundle(bundle)
        if result['success']:
            bundle_ids.append(result['data']['result'])
            print(f'\rAccounts Processed: {processed_accounts}/{len(close_instructions)}', end='')
        else:
            print(f"Failed to send bundle: {result.get('error', 'Unknown error')}")

    print(f"\nBundle IDs: {bundle_ids}")

async def _show_wallet_status() -> None:
    wallet_int =  input("Wallet address: ").strip()
    if not wallet_int:
        print("Wallet address is not provided.")
        return

    client = AsyncClient(SOLANA_RPC)
    accounts = await _get_zero_balance_token_accounts(Pubkey.from_string(wallet_int), client)

    lamports = 0

    min_processing_fee = await _get_min_transaction_fee(client)

    keg_accounts = 0
    twenty_twenty_accounts = 0

    for account in accounts:
        lamports += account.lamports
        keg_accounts +=  1 if account.program_id == TokenType.TOKEN_KEG.address else 0
        twenty_twenty_accounts +=  1 if account.program_id == TokenType.TOKEN_2022.address else 0


    cost  = math.ceil(math.ceil(len(accounts) / MAX_CLOSE_INSTRUCTIONS_PER_TRANSACTION)) * ( MIN_JITO_TIP + min_processing_fee )


    colors = ConsoleColors()
    print(colors.title("Balance information"))
    print(colors.section("    closable accounts:\t\t") + colors.data(f"{len(accounts)} =>  " + colors.summary(f"{keg_accounts} token-keg + {twenty_twenty_accounts} token-2020")))
    print(colors.section("    recoverable SOL:\t\t") + colors.data(f"{lamports / LAMPORTS_PER_SOL}"))
    print(colors.section("    lamports:\t\t\t") + colors.data(f"{lamports}"))
    print(colors.section("    approximate close costs:\t") + colors.data(f"{cost / LAMPORTS_PER_SOL :.6f} SOL"))


async def _get_zero_balance_token_accounts(wallet: Pubkey, client: AsyncClient) -> Sequence[TokenAccount]:

    if not await client.is_connected():
        raise Exception("Client is not connected")

    zero_balance_accounts = []

    for token_program in TokenType:
        token_accounts = await client.get_token_accounts_by_owner_json_parsed(
            owner = wallet,
            opts = TokenAccountOpts( program_id= token_program.address, encoding="jsonParsed" )
        )

        for account in token_accounts.value:
            if float(account.account.data.parsed['info']['tokenAmount']['uiAmount']) == 0:
                zero_balance_accounts.append(
                    TokenAccount(
                        address = account.pubkey,
                        lamports = account.account.lamports,
                        program_id= token_program.address))


    return zero_balance_accounts


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    args = build_parser().parse_args(argv)

    try:
        match args.command:
            case "show":
                asyncio.run(_show_wallet_status())
            case "close_accounts":
                asyncio.run(_close_all_accounts(use_bundles=args.bundles))
    except KeyboardInterrupt as e:
        print(ConsoleColors().error("\nCancelled."))
        return 130
    except Exception as e:
        print(ConsoleColors().error(e.__str__()))
        return 1
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
