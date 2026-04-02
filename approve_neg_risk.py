#!/usr/bin/env python3
"""
One-time setup: approve Polymarket's exchange contracts to spend your USDC.e.

This sends 3 on-chain transactions on Polygon to grant max allowance to:
  - Standard Exchange
  - Neg Risk Exchange
  - Neg Risk Adapter

Only needs to be run once per wallet. Safe to re-run — approving an already-
approved contract just overwrites with the same max value.

Usage:
  source venv/bin/activate
  python approve_neg_risk.py
"""
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

import os
from web3 import Web3

PRIVATE_KEY = os.getenv("POLYMARKET_FUNDER_PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY", "")
if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_FUNDER_PRIVATE_KEY (or POLYMARKET_PRIVATE_KEY) not set in .env")
    sys.exit(1)

RPC_URL    = "https://polygon-bor-rpc.publicnode.com"
USDC_E     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
MAX_UINT256 = 2**256 - 1

SPENDERS = {
    "Standard Exchange":  "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk Exchange":  "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter":   "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

ABI = [{
    "constant": False,
    "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount",  "type": "uint256"},
    ],
    "name": "approve",
    "outputs": [{"name": "", "type": "bool"}],
    "type": "function",
}]

w3      = Web3(Web3.HTTPProvider(RPC_URL))
account = w3.eth.account.from_key(PRIVATE_KEY)
token   = w3.eth.contract(address=USDC_E, abi=ABI)

print(f"Wallet : {account.address}")
print(f"Network: Polygon (chain {w3.eth.chain_id})")
print()

nonce = w3.eth.get_transaction_count(account.address)

for name, spender in SPENDERS.items():
    print(f"Approving {name} ({spender})...")
    tx = token.functions.approve(spender, MAX_UINT256).build_transaction({
        "from":     account.address,
        "nonce":    nonce,
        "gas":      100_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx sent : {tx_hash.hex()}")
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"  Confirmed.")
    nonce += 1

print()
print("All approvals set. neg_risk orders should now execute.")
