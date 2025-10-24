# tracker/views.py
import os
import json
import logging
import textwrap
from io import BytesIO
from datetime import datetime, timezone
from typing import Optional

import requests
from django.shortcuts import render
from django.http import HttpResponse
from dotenv import load_dotenv
from web3 import Web3

# Optional PDF dependency
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Load environment variables (adjust path/name as needed)
load_dotenv("variables.env")

# RPC endpoints (ensure variables.env has these)
RPC_ENDPOINTS = {
    "Ethereum Mainnet": os.getenv("WEB3_MAINNET"),
    "Sepolia Testnet": os.getenv("WEB3_SEPOLIA"),
    "Polygon Mainnet": os.getenv("WEB3_POLYGON"),
    "Binance Smart Chain": os.getenv("WEB3_BSC"),
}

# Explorer API config (env_key must be present in variables.env for fallback)
EXPLORER_APIS = {
    "Ethereum Mainnet": {
        "api_base": "https://api.etherscan.io/api",
        "env_key": "ETHERSCAN_API_KEY",
        "explorer_tx": "https://etherscan.io/tx/{}",
        "explorer_addr": "https://etherscan.io/address/{}",
    },
    "Sepolia Testnet": {
        "api_base": "https://api-sepolia.etherscan.io/api",
        "env_key": "ETHERSCAN_API_KEY",
        "explorer_tx": "https://sepolia.etherscan.io/tx/{}",
        "explorer_addr": "https://sepolia.infura.io/address/{}",
    },
    "Polygon Mainnet": {
        "api_base": "https://api.polygonscan.com/api",
        "env_key": "POLYGONSCAN_API_KEY",
        "explorer_tx": "https://polygonscan.com/tx/{}",
        "explorer_addr": "https://polygonscan.com/address/{}",
    },
    "Binance Smart Chain": {
        "api_base": "https://api.bscscan.com/api",
        "env_key": "BSCSCAN_API_KEY",
        "explorer_tx": "https://bscscan.com/tx/{}",
        "explorer_addr": "https://bscscan.com/address/{}",
    },
}

# Arkham (optional)
ARKHAM_KEY = os.getenv("ARKHAM_API_KEY")
ARKHAM_BASE = "https://api.arkhamintelligence.com"


# -------------------- Helpers --------------------
def get_w3_for_chain(chain_name: Optional[str]) -> Optional[Web3]:
    """Return connected Web3 instance for given chain name or None."""
    if not chain_name:
        chain_name = "Ethereum Mainnet"
    rpc = RPC_ENDPOINTS.get(chain_name)
    if not rpc:
        logger.debug("No RPC configured for chain: %s", chain_name)
        return None
    w3 = Web3(Web3.HTTPProvider(rpc))
    try:
        if not w3.is_connected():
            logger.warning("Web3 not connected for %s (RPC: %s)", chain_name, rpc)
            return None
    except Exception as e:
        logger.exception("Error checking Web3 connection for %s: %s", chain_name, e)
        return None
    return w3


def arkham_label_for(address: str) -> Optional[str]:
    """Return Arkham entity label for an address if available."""
    if not address or not ARKHAM_KEY:
        return None
    try:
        url = f"{ARKHAM_BASE}/intelligence/address/{address}/all"
        r = requests.get(url, headers={"API-Key": ARKHAM_KEY}, timeout=8)
        r.raise_for_status()
        data = r.json()
        for _, intel in data.items():
            if isinstance(intel, dict) and intel.get("arkhamEntity"):
                return intel["arkhamEntity"].get("id")
    except Exception as e:
        logger.debug("Arkham lookup failed for %s: %s", address, e)
    return None


def analyze_tx_source(tx_obj, w3: Web3) -> str:
    """
    Simple heuristic for transaction "source":
      - Prefer Arkham label if available
      - If input/data empty -> Transfer
      - If input present -> Contract Interaction / Token transfer heuristics
    """
    try:
        # prefer Arkham enrichment
        try:
            _from = tx_obj.get("from") if isinstance(tx_obj, dict) else getattr(tx_obj, "from", None)
            _to = tx_obj.get("to") if isinstance(tx_obj, dict) else getattr(tx_obj, "to", None)
        except Exception:
            _from = None
            _to = None

        if ARKHAM_KEY:
            if _from:
                lbl = arkham_label_for(_from)
                if lbl:
                    return f"Source: {lbl}"
            if _to:
                lbl = arkham_label_for(_to)
                if lbl:
                    return f"Dest: {lbl}"

        # input heuristics
        input_data = ""
        if isinstance(tx_obj, dict):
            input_data = tx_obj.get("input") or tx_obj.get("data") or ""
        else:
            input_data = getattr(tx_obj, "input", "") or getattr(tx_obj, "data", "") or ""

        if isinstance(input_data, bytes):
            input_data = input_data.hex()
        if not input_data or input_data == "0x":
            return "Transfer"

        # ERC20 transfer method id: a9059cbb
        if input_data.startswith("0xa9059cbb") or input_data.startswith("a9059cbb"):
            return "ERC-20 Transfer"

        # ERC721 transferFrom signature: 0x23b872dd
        if input_data.startswith("0x23b872dd") or input_data.startswith("23b872dd"):
            return "ERC-721 Transfer"

        # contract creation
        if not _to:
            return "Contract creation"

        # default
        return "Contract Interaction"
    except Exception as e:
        logger.debug("analyze_tx_source error: %s", e)
        return "Unknown"


def fetch_last_txs_from_explorer(chain_name: str, address: str, limit: int = 10):
    """
    Fallback using Etherscan/Polygonscan/BscScan APIs to fetch recent txs for an address.
    Returns:
      - list of tx dicts (newest first) on success
      - [] if explorer returned no txs
      - None if API key missing or request failed
    """
    cfg = EXPLORER_APIS.get(chain_name)
    if not cfg:
        return None
    api_key = os.getenv(cfg["env_key"])
    if not api_key:
        logger.debug("Explorer API key missing for chain %s (env var: %s)", chain_name, cfg["env_key"])
        return None

    params = {
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": api_key,
    }
    try:
        r = requests.get(cfg["api_base"], params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "1" and isinstance(data.get("result"), list):
            return data["result"]
        logger.debug("Explorer API returned status=%s message=%s", data.get("status"), data.get("message"))
        return []
    except Exception as exc:
        logger.exception("Explorer API request failed for %s: %s", chain_name, exc)
        return None


# -------------------- Views --------------------
def tx_search(request):
    """
    Search for a specific transaction hash and render tx_search.html.
    Context provides:
      - query, tx (dict or None), err, chain, chains
    """
    query = (request.GET.get("q") or "").strip()
    selected_chain = request.GET.get("chain")
    context = {
        "query": query,
        "tx": None,
        "err": None,
        "chain": selected_chain,
        "chains": list(RPC_ENDPOINTS.keys()),
    }

    if not query:
        return render(request, "tx_search.html", context)

    # basic validation
    if not query.startswith("0x") or len(query) != 66:
        context["err"] = "Invalid transaction hash format"
        return render(request, "tx_search.html", context)

    chains_to_search = [selected_chain] if selected_chain else list(RPC_ENDPOINTS.keys())

    found = False
    for chain_name in chains_to_search:
        rpc = RPC_ENDPOINTS.get(chain_name)
        if not rpc:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            if not w3.is_connected():
                logger.debug("RPC not connected for %s", chain_name)
                continue

            tx = w3.eth.get_transaction(query)
            receipt = w3.eth.get_transaction_receipt(query)
            block = w3.eth.get_block(receipt.blockNumber)
            timestamp = datetime.fromtimestamp(block.timestamp, tz=timezone.utc)

            from_label = arkham_label_for(tx.get("from")) if ARKHAM_KEY else None
            to_label = arkham_label_for(tx.get("to")) if ARKHAM_KEY and tx.get("to") else None

            context["tx"] = {
                "hash": query,
                "status": "Success" if (getattr(receipt, "status", receipt.get("status")) == 1) else "Failed",
                "from": (tx.get("from"), from_label),
                "to": (tx.get("to"), to_label),
                "value": float(Web3.from_wei(int(tx.get("value", 0)), "ether")),
                "gas": int(tx.get("gas", 0)),
                "block": int(receipt.blockNumber),
                "time": timestamp,
                "input": tx.get("input") or tx.get("data") or "",
            }
            context["chain"] = chain_name
            found = True
            break
        except Exception as e:
            logger.debug("Transaction not found on %s or error: %s", chain_name, e)
            continue

    if not found:
        context["err"] = f"Transaction {query} not found on selected chain(s)."

    return render(request, "tx_search.html", context)


def last10_from_tx(request):
    """
    Given ?q=<tx_hash>&chain=<optional chain>:
      - locate base tx
      - take base_tx['from'] as wallet
      - scan backwards collecting up to 10 txs where from OR to matches wallet
      - fallback to explorer if node scan fails
    Renders last10_graph.html with:
      txs, chart_json, total_value_eth, tx_count, wallet, query, chain, chains, err
    """
    tx_hash = (request.GET.get("q") or "").strip()
    selected_chain = request.GET.get("chain")
    context = {
        "query": tx_hash or None, 
        "chains": list(RPC_ENDPOINTS.keys()),
        "chain": selected_chain,
        "txs": None,
        "chart_json": None,
        "wallet": None,  # This gets set later after base tx is found
        "total_value_eth": 0.0,
        "tx_count": 0,
        "err": None,
    }

    # If no q provided, show form/page so user can paste a tx hash
    if not tx_hash:
        return render(request, "last10_from_tx.html", context)

    # basic validation for hash
    if not tx_hash.startswith("0x") or len(tx_hash) < 10:
        context["err"] = "Invalid transaction hash format"
        return render(request, "last10_from_tx.html", context)

    # Step 1: find base tx and a working w3 instance (try selected chain first)
    chains_to_search = [selected_chain] if selected_chain else list(RPC_ENDPOINTS.keys())
    base_tx = None
    w3 = None
    found_chain = None
    for chain_name in chains_to_search:
        rpc = RPC_ENDPOINTS.get(chain_name)
        if not rpc:
            continue
        try:
            candidate = Web3(Web3.HTTPProvider(rpc))
            if not candidate.is_connected():
                logger.debug("RPC not connected for %s", chain_name)
                continue
            base_tx = candidate.eth.get_transaction(tx_hash)
            w3 = candidate
            found_chain = chain_name
            break
        except Exception as e:
            logger.debug("tx not found on %s: %s", chain_name, e)
            continue

    if not base_tx or w3 is None:
        context["err"] = f"Transaction {tx_hash} not found on supported chains or node doesn't have it."
        return render(request, "last10_from_tx.html", context)

    # derive wallet from base tx
    from_addr = base_tx.get("from")
    if not from_addr:
        context["err"] = "Source address not found in base transaction."
        return render(request, "last10_from_tx.html", context)
    context["wallet"] = from_addr
    context["chain"] = found_chain

    # determine starting block
    try:
        start_block = int(base_tx.get("blockNumber") or w3.eth.block_number)
    except Exception:
        start_block = w3.eth.block_number

    # Step 2: scan backwards block-by-block (node-first)
    collected = []
    block_num = start_block
    safety_limit = 8000  # blocks to scan max (tune if needed)
    scanned = 0

    while block_num >= 0 and len(collected) < 10 and scanned < safety_limit:
        try:
            block = w3.eth.get_block(block_num, full_transactions=True)
        except Exception as e:
            logger.debug("Could not fetch block %s: %s", block_num, e)
            block_num -= 1
            scanned += 1
            continue

        block_ts = getattr(block, "timestamp", None) or (block.get("timestamp") if isinstance(block, dict) else None)
        for t in (block.transactions or []):
            # support AttributeDict-like and dict forms
            t_from = (t.get("from") if isinstance(t, dict) else getattr(t, "from", None))
            t_to = (t.get("to") if isinstance(t, dict) else getattr(t, "to", None))
            if not t_from:
                continue
            if (t_from and t_from.lower() == from_addr.lower()) or (t_to and t_to.lower() == from_addr.lower()):
                # normalize
                _hash = None
                if hasattr(t, "hash"):
                    h = getattr(t, "hash")
                    _hash = h.hex() if hasattr(h, "hex") else str(h)
                else:
                    _hash = t.get("hash")

                value_wei = int(t.get("value", 0) if isinstance(t, dict) else getattr(t, "value", 0) or 0)
                value_eth = float(Web3.from_wei(value_wei, "ether"))
                gas = int(t.get("gas", 0) if isinstance(t, dict) else getattr(t, "gas", 0) or 0)
                ts = datetime.fromtimestamp(int(block_ts), tz=timezone.utc) if block_ts else None

                tx_info = {
                    "hash": _hash,
                    "from": t_from,
                    "to": t_to,
                    "value_wei": value_wei,
                    "value_eth": value_eth,
                    "gas": gas,
                    "block": int(block_num),
                    "timestamp": ts,
                    "input": (t.get("input") if isinstance(t, dict) else getattr(t, "input", "")) or "",
                }
                try:
                    tx_info["source"] = analyze_tx_source(t if isinstance(t, dict) else t, w3)
                except Exception:
                    tx_info["source"] = "Unknown"

                collected.append(tx_info)
                if len(collected) >= 10:
                    break

        block_num -= 1
        scanned += 1

    # Step 3: if node scan returned nothing, attempt explorer fallback
    if not collected:
        logger.debug("Node scan returned 0 txs; attempting explorer fallback for %s on %s", from_addr, found_chain)
        explorer_txs = fetch_last_txs_from_explorer(found_chain, from_addr, limit=10)
        if explorer_txs is None:
            context["err"] = ("Explorer API error or missing API key. Check explorer keys in variables.env.")
            return render(request, "last10_from_tx.html", context)
        if not explorer_txs:
            context["err"] = f"Explorer returned no transactions for wallet {from_addr}."
            return render(request, "last10_from_tx.html", context)

        collected = []
        for et in explorer_txs:
            try:
                v_wei = int(et.get("value", 0) or 0)
            except Exception:
                v_wei = 0
            v_eth = float(Web3.from_wei(v_wei, "ether"))
            tx_info = {
                "hash": et.get("hash"),
                "from": et.get("from"),
                "to": et.get("to"),
                "value_wei": v_wei,
                "value_eth": v_eth,
                "gas": int(et.get("gas", 0) or 0),
                "block": int(et.get("blockNumber") or 0),
                "timestamp": datetime.fromtimestamp(int(et.get("timeStamp") or 0), tz=timezone.utc) if et.get("timeStamp") else None,
                "input": et.get("input") or "",
                "source": "Explorer (raw)",
            }
            # Add explorer URLs for template
            explorer_cfg = EXPLORER_APIS.get(found_chain)
            tx_info["explorer_url"] = explorer_cfg["explorer_tx"].format(tx_info["hash"])
            tx_info["to_explorer_url"] = explorer_cfg["explorer_addr"].format(tx_info["to"]) if tx_info["to"] else None
            collected.append(tx_info)

    if not collected:
        context["err"] = f"No recent transactions found for wallet {from_addr}."
        return render(request, "last10_from_tx.html", context)

    # sort newest -> oldest
    collected_sorted = sorted(collected, key=lambda x: x.get("block") or 0, reverse=True)[:10]

    # prepare chart payload and aggregates
    labels = []
    values = []
    gas_list = []
    hashes = []
    total_value = 0.0
    for tx in collected_sorted:
        ts = tx.get("timestamp") or datetime.now(timezone.utc)
        labels.append(f"{tx['block']} • {ts.strftime('%Y-%m-%d %H:%M')}")
        values.append(round(tx["value_eth"], 6))
        gas_list.append(tx["gas"])
        hashes.append(tx["hash"])
        total_value += tx["value_eth"]

    chart_payload = {"labels": labels, "values": values, "gas": gas_list, "hashes": hashes}

    context.update({
        "txs": collected_sorted,
        "chart_json": json.dumps(chart_payload),
        "total_value_eth": round(total_value, 6),
        "tx_count": len(collected_sorted),
        "err": None,
    })
    return render(request, "last10_from_tx.html", context)


def download_tx_pdf_plain(request):
    """
    Generate a simple plain-text PDF with core tx details.
    Query: ?q=<tx_hash>&chain=<optional chain>
    """
    tx_hash = (request.GET.get("q") or "").strip()
    selected_chain = request.GET.get("chain")
    if not tx_hash:
        return HttpResponse("Missing transaction hash (q parameter).", status=400)
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return HttpResponse("Invalid transaction hash format.", status=400)

    chains_to_search = [selected_chain] if selected_chain else list(RPC_ENDPOINTS.keys())
    found = False
    tx_data = None
    for chain_name in chains_to_search:
        rpc = RPC_ENDPOINTS.get(chain_name)
        if not rpc:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            if not w3.is_connected():
                continue
            tx = w3.eth.get_transaction(tx_hash)
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            block = w3.eth.get_block(receipt.blockNumber)
            timestamp = datetime.fromtimestamp(block.timestamp, tz=timezone.utc)

            from_addr = tx.get("from")
            to_addr = tx.get("to")
            tx_data = {
                "Hash": tx_hash,
                "Chain": chain_name,
                "Status": "Success" if (getattr(receipt, "status", receipt.get("status")) == 1) else "Failed",
                "From": from_addr or "",
                "To": to_addr or "",
                "Value (ETH)": str(Web3.from_wei(int(tx.get("value", 0) or 0), "ether")),
                "Gas (limit)": str(tx.get("gas", "")),
                "Gas Price (wei)": str(tx.get("gasPrice", "")),
                "Block": str(receipt.blockNumber),
                "Block Timestamp (UTC)": timestamp.isoformat(),
            }
            found = True
            break
        except Exception as e:
            logger.debug("Chain %s error when building pdf tx: %s", chain_name, e)
            continue

    if not found or not tx_data:
        return HttpResponse(f"Transaction {tx_hash} not found.", status=404)

    if not REPORTLAB_AVAILABLE:
        return HttpResponse("PDF generation dependency missing. Install reportlab (pip install reportlab).", status=500)

    # Create plain-text PDF
    buffer = BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin_x = 40
    margin_top = height - 40
    textobj = c.beginText(margin_x, margin_top)
    textobj.setFont("Courier", 10)
    textobj.setLeading(14)

    for key, val in tx_data.items():
        line = f"{key}: {val}"
        wrapped = textwrap.wrap(line, width=100) or [line]
        for w in wrapped:
            textobj.textLine(w)
        textobj.textLine("")
        if textobj.getY() < 80:
            c.drawText(textobj)
            c.showPage()
            textobj = c.beginText(margin_x, margin_top)
            textobj.setFont("Courier", 10)
            textobj.setLeading(14)

    c.drawText(textobj)
    c.showPage()
    c.save()

    pdf = buffer.getvalue()
    buffer.close()
    filename = f"tx_{tx_hash[:10]}.pdf"
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
