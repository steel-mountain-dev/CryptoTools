import asyncio

import math
import requests
from jito_py_rpc import JitoJsonRpcSDK


async def main():
    items = [1,2]
    print(items)

    batch_l1 =  chunk_it(  chunk_it(items, 20), 5)

    print(f"batch_l1: {batch_l1}")

    jito_client = JitoJsonRpcSDK(url="https://mainnet.block-engine.jito.wtf/api/v1")
    response = jito_client.get_bundle_statuses(['26fa469392fa4ca220936f830bf21565fe34ede761b190ed87f72738cfd7dd2e'])
    print(response)

    tip_needed = get_minimum_jito_tip_in_lamports()
    print(f"Dynamic Jito tip to include: {tip_needed} lamports")


def get_minimum_jito_tip_in_lamports() -> int:
    """
    Fetches the live Jito tip floor and returns the 25th percentile value
    converted to Lamports, with a minor safety buffer applied.
    """
    url = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"

    try:
        # Request the real-time tip data from Jito's API
        response = requests.get(url, timeout=5)
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
            return max(buffered_lamports, 1000)

    except Exception as e:
        print(f"Failed to fetch Jito tip floor due to: {e}. Falling back to static safety value.")

    # Fallback to your original static safety value in lamports if the API is down
    return 1000

def chunk_it(lst: list, batch_size):
    def internal(list, batch_size):
        for i in range(0, len(list), batch_size):
            yield list[i:i + batch_size]
    return list(internal(lst, batch_size))

if __name__ == '__main__':
    asyncio.run(main())