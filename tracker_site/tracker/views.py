import os
import requests
import logging
from datetime import datetime, timezone
from django.shortcuts import render
from web3 import Web3
from dotenv import load_dotenv


logger = logging.getLogger(__name__)
 
# Load environment variables
load_dotenv("variables.env")

# RPC Endpoints
RPC_ENDPOINTS = {
    "Ethereum Mainnet": os.getenv("WEB3_MAINNET"),
    "Sepolia Testnet": os.getenv("WEB3_SEPOLIA"),
    "Polygon Mainnet": os.getenv("WEB3_POLYGON"),
    "Binance Smart Chain": os.getenv("WEB3_BSC"),
}

# Arkham setup (optional)
ARKHAM_KEY = os.getenv("ARKHAM_API_KEY")
ARKHAM_BASE = "https://api.arkhamintelligence.com"


def arkham_label_for(address):
    """Fetch Arkham entity label for an address (if available)."""
    if not address or not ARKHAM_KEY:
        return None
    url = f"{ARKHAM_BASE}/intelligence/address/{address}/all"
    try:
        r = requests.get(url, headers={"API-Key": ARKHAM_KEY}, timeout=10)
        r.raise_for_status()
        data = r.json()
        for _, intel in data.items():
            if intel.get("arkhamEntity"):
                return intel["arkhamEntity"]["id"]
    except Exception as e:
        logger.warning(f"ARKHAM ERROR for {address}: {e}")
    return None


def tx_search(request):
    """Transaction search view with chain selection."""
    query = (request.GET.get("q") or "").strip()
    selected_chain = request.GET.get("chain")  # from dropdown
    context = {
        "query": query,
        "tx": None,
        "err": None,
        "chain": selected_chain,
        "chains": list(RPC_ENDPOINTS.keys()),
    }

    if not query:
        return render(request, "tx_search.html", context)

    # Validate transaction hash
    if not query.startswith("0x") or len(query) != 66:
        context["err"] = "Invalid transaction hash format"
        return render(request, "tx_search.html", context)

    chains_to_search = [selected_chain] if selected_chain else RPC_ENDPOINTS.keys()

    tx_found = False
    for chain_name in chains_to_search:
        rpc = RPC_ENDPOINTS.get(chain_name)
        if not rpc:
            continue

        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            if not w3.is_connected():
                logger.warning(f"❌ {chain_name}: Not connected")
                continue

            tx = w3.eth.get_transaction(query)
            receipt = w3.eth.get_transaction_receipt(query)
            block = w3.eth.get_block(receipt.blockNumber)

            timestamp = datetime.fromtimestamp(block.timestamp, tz=timezone.utc)
            from_label = arkham_label_for(tx["from"])
            to_label = arkham_label_for(tx["to"]) if tx["to"] else None

            context["tx"] = {
                "hash": query,
                "status": "Success" if receipt.status == 1 else "Failed",
                "from": (tx["from"], from_label),
                "to": (tx["to"], to_label),
                "value": Web3.from_wei(tx["value"], "ether"),
                "gas": tx["gas"],
                "block": receipt.blockNumber,
                "time": timestamp,
            }
            context["chain"] = chain_name
            tx_found = True
            break  # stop at first successful chain

        except Exception as e:
            logger.warning(f"⚠️ {chain_name}: {e}")
            continue

    if not tx_found:
        context["err"] = f"Transaction {query} not found on selected chain(s)."

    return render(request, "tx_search.html", context)

