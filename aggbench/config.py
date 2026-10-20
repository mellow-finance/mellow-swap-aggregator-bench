"""Paths, secrets and the addresses the study is about.

Secrets are read by name (ENV_KEYS) from ./.env or the file in BENCH_ENV_FILE (see .env.example); process
environment variables take precedence.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"  # all inputs/outputs of the scripts
CONTRACTS = ROOT / "contracts"
DATA.mkdir(exist_ok=True)

ENV_KEYS = ("ETH_RPC", "PLASMA_RPC", "MANTLE_RPC", "ROBIN_RPC", "ETHERSCAN_API_KEY", "FYND_API_KEY", "ZERO_X_API_KEY")


def _load_env():
    env = {}
    path = Path(os.environ.get("BENCH_ENV_FILE", ROOT / ".env"))
    if path.exists():
        for line in open(path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    env.update({k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)})
    return env


ENV = _load_env()
RPCS = {"ethereum": ENV.get("ETH_RPC"), "plasma": ENV.get("PLASMA_RPC"), "mantle": ENV.get("MANTLE_RPC"), "robinhood": ENV.get("ROBIN_RPC")}
ETHERSCAN_CHAIN_ID = {"ethereum": 1, "plasma": 9745, "mantle": 5000}

# SwapModule factories: Ethereum uses 0xE357..., the L2 deployments use 0xC5a5... (flexible-vaults scripts/*/Constants.sol)
FACTORIES = {"ethereum": "0xE3575055a24d8642DFA3a51ec766Ef2db2671659", "plasma": "0xC5a52E4bB718Dfe86938e5cB967362EdA1E62698",
             "mantle": "0xC5a52E4bB718Dfe86938e5cB967362EdA1E62698", "robinhood": "0xC5a52E4bB718Dfe86938e5cB967362EdA1E62698"}
VAULTS = [  # vaults whose swap modules define the benchmarked pairs
    "0x277C6A642564A91ff78b008022D65683cEE5CCC5", "0x6a37725ca7f4CE81c004c955f7280d5C704a249e", "0x014e6DA8F283C4aF65B2AA0f201438680A004452",
    "0xDF0fb76Df2c21F79798949A4E886cd22D1C085d7", "0x0cC65147BF7F615A8dD9E78e2c53158F8E01754d",
    "0x49DAb986A4288bE616f44733d56397d3410fD331",  # plasma
    "0xF9DD401ff0f806b71dE62a936c34B930d0876022",  # mantle
    "0xEe94bb5a8487954312c470F2cb457fd7329B421F",  # robinhood
    "0x841e213864046111E43d237703d71FaBe91Ef9e0",  # plasma
]
CURATOR_EOAS = ["0xe49384e6Aca6A26c82Eaa161A9211fA60fEee3Ef", "0xE98Be1E5538FCbD716C506052eB1Fd5d6fC495A3"]  # tx.origin of past swaps

# Where each aggregator parks the surplus above its quote (found by tracing tokenOut transfers in simulations):
#  - Kyber: executor 0x8f10b4... forwards it to this EIP-7702 EOA, the user gets quote + 1 wei
#  - Fynd: TychoRouterV3 credits it to its own ERC-6909 vault (FeeCalculator positive-slippage capture)
#  - 0x: the volume-fee recipient (also receives the 15 bps fee itself)
SURPLUS_SINK = {"kyber": "0x1111110f0f73c0b2ef09ec012eae758b3e03a902",
                "fynd": "0x1644d2477f809cc2c71bccfd6dc9497e3f83210d",
                "0x": "0xad01c20d5886137e056775af56915de824c8fce5"}
KYBER_ROUTER = "0x6131b5fae19ea4f9d964eac0408e4408b66337b5"
STABLES = ("USDC", "USDT", "USDe", "PYUSD")  # valued at $1; also Matcha's 0%-fee stable<->stable set
