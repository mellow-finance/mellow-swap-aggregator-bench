"""JSON-RPC and Etherscan v2 access with retries."""
import time

import requests

from aggbench.config import ENV, RPCS

_session = requests.Session()


def rpc(method, params, url=None, retries=5):
    """JSON-RPC call with simple retry/backoff (drpc occasionally drops requests). Defaults to the Ethereum RPC."""
    url = url or RPCS["ethereum"]
    for attempt in range(retries):
        try:
            r = _session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60).json()
            if "error" in r:
                raise RuntimeError(r["error"])
            return r["result"]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)


def latest_block():
    return int(rpc("eth_blockNumber", []), 16)


def etherscan(chain_id, **params):
    """Etherscan v2 multichain API; 'No records found' is a valid empty answer."""
    q = {"chainid": chain_id, "apikey": ENV["ETHERSCAN_API_KEY"], **params}
    for _ in range(5):
        r = requests.get("https://api.etherscan.io/v2/api", params=q, timeout=60).json()
        if r.get("status") == "1" or r.get("message") == "No records found":
            return r["result"]
        time.sleep(1)
    raise RuntimeError(r)
