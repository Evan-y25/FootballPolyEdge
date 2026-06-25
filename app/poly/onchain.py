"""
Read-only on-chain checks for the funder Safe: pUSD balance + V2-exchange
allowance. BUY orders spend pUSD, so the Safe must hold pUSD AND have approved
the CTF Exchange V2 (and Neg-Risk Exchange V2) to spend it — otherwise orders
fail at match time. We only READ here (approval must be granted via Polymarket
UI / a Safe tx; we cannot sign a Safe approval from the EOA).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"          # V2 collateral (6 decimals)
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
DEFAULT_RPC = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")

_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "o", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


def check_funder(funder: str, rpc: str = DEFAULT_RPC) -> dict:
    """Return pUSD balance + allowances of the funder Safe to the V2 exchanges."""
    out = {"ok": False, "pusd": None, "allow_exchange": None, "allow_negrisk": None, "error": None}
    if not funder:
        out["error"] = "no funder"
        return out
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        token = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=_ERC20_ABI)
        f = Web3.to_checksum_address(funder)
        bal = token.functions.balanceOf(f).call()
        ax = token.functions.allowance(f, Web3.to_checksum_address(EXCHANGE_V2)).call()
        an = token.functions.allowance(f, Web3.to_checksum_address(NEG_RISK_EXCHANGE_V2)).call()
        out.update(ok=True, pusd=round(bal / 1e6, 4),
                   allow_exchange=ax > 0, allow_negrisk=an > 0,
                   allow_exchange_raw=round(ax / 1e6, 2), allow_negrisk_raw=round(an / 1e6, 2))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
