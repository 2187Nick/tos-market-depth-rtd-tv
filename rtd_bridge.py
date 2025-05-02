# rtd_bridge 
import pythoncom, time, threading, sys, signal
from collections import defaultdict, deque
from typing import Optional, List, Dict
from dataclasses import dataclass

from src.rtd.client import RTDClient           # COM wrapper
from market_db import MarketDB                 # SQLite helper
from src.utils.quote import Quote              # RTD quote model
import requests

API_URL   = "http://localhost:8080"
EXCHANGES = ["A", "B", "C", "I", "N", "Q", "W", "X", "Z"]  # depth feeds

# ─── global cache ────────────────────────────────────────────────────────────
# symbol  -> { ts -> {"price": Optional[float], "size": Optional[int]} }
_trade_cache: Dict[str, Dict[int, Dict[str, float | int | None]]] = \
    defaultdict(lambda: defaultdict(lambda: {"price": None, "size": None}))

_trade_lock = threading.Lock()

# --- Polling Logic ---
_polling_stop_evt = threading.Event()

def polling_worker(client: RTDClient, poll_interval: float):
    """Worker function to poll for active symbols periodically."""
    global active_symbols # Need to modify the global set
    #print("[PollingThread] Starting...")
    last_poll_time = 0
    while not _polling_stop_evt.is_set():
        now = time.time()
        if now - last_poll_time >= poll_interval:
            try:
                #print("[PollingThread] Fetching active symbols...")
                # Fetch symbols (keep timeout reasonable)
                resp = requests.get(f"{API_URL}/active_symbols", timeout=2)
                resp.raise_for_status() # Raise exception for bad status codes
                data = resp.json()
                current_active = data.get("symbols", [])

                # Find newly added symbols
                new_syms_to_add = []
                with active_symbols_lock:
                    # Create sets for efficient comparison
                    existing_set = set(active_symbols)
                    current_set = set(current_active)

                    newly_added = current_set - existing_set

                    if newly_added:
                        #print(f"[PollingThread] Found new symbols: {list(newly_added)}")
                        new_syms_to_add = list(newly_added)
                        active_symbols.update(newly_added) # Update the global set

                # Subscribe outside the lock
                if new_syms_to_add:
                    #print(f"[PollingThread] Subscribing to {len(new_syms_to_add)} new symbols...")
                    for sym in new_syms_to_add:
                         try:
                             #print(f"[PollingThread] Calling add_option_contract for {sym}")
                             client.add_option_contract(sym, EXCHANGES)
                             #print(f"[PollingThread] Subscribed to {sym}")
                         except Exception as e_sub:
                             print(f"[PollingThread] Error subscribing to {sym}: {e_sub}")
                             # Should potentially remove from active_symbols if subscribe fails critically

                last_poll_time = now
                #print("[PollingThread] Poll cycle complete.")

            except requests.exceptions.RequestException as e_req:
                print(f"[PollingThread] Error fetching active symbols: {e_req}")
                # Don't update last_poll_time on error, retry sooner
            except Exception as e:
                print(f"[PollingThread] Unexpected error in polling thread: {e}")
                import traceback
                traceback.print_exc()
                # Don't update last_poll_time on error, retry sooner

        # Sleep briefly to prevent busy-waiting
        _polling_stop_evt.wait(0.1) # Check stop flag every 100ms

    print("[PollingThread] Stopping.")

@dataclass
class MarketMakerQuote:
    price: float | None = None
    size: int | None = None
    last_update: float = 0

class MarketMakerState:
    def __init__(self):
        self.bid = MarketMakerQuote()
        self.ask = MarketMakerQuote()

# Initialize empty market maker state for each exchange
def create_exchange_dict():
    return {ex: MarketMakerState() for ex in EXCHANGES}

# Per-symbol dictionary tracking each market maker's latest quotes
# Format: symbol -> exchange -> MarketMakerState
_mm_state: Dict[str, Dict[str, MarketMakerState]] = defaultdict(create_exchange_dict)
_mm_state_lock = threading.Lock()

# Track when we last published a snapshot for each symbol
_last_snapshot: Dict[str, float] = defaultdict(float)
SNAPSHOT_INTERVAL = 0.25 #1.0  # Publish full book every second

def debug_print_mm_state(symbol: str):
    """Print the current state of all market makers for a symbol"""
    print(f"\n[DEBUG] Market Maker State for {symbol}:")
    for exchange in EXCHANGES:
        state = _mm_state[symbol][exchange]
        bid = state.bid
        ask = state.ask
        print(f"{exchange:>2}: Bid: {bid.price:>7.2f}×{bid.size:<4} "
              f"Ask: {ask.price:>7.2f}×{ask.size:<4} "
              f"Updated: {time.strftime('%H:%M:%S', time.localtime(bid.last_update))}")
    print("----------------------------------------")

# Helper function to aggregate market maker quotes
def aggregate_levels(symbol: str) -> tuple[list, list]:
    """Aggregate all market maker quotes for a symbol into price levels.
    Returns (bids, asks) where each is a list of (price, total_size) tuples."""
    # Aggregate by price level
    bid_levels: Dict[float, int] = defaultdict(int)
    ask_levels: Dict[float, int] = defaultdict(int)
    
    with _mm_state_lock:
        for exchange, state in _mm_state[symbol].items():
            if state.bid.price is not None and state.bid.size is not None:
                bid_levels[state.bid.price] += state.bid.size
            if state.ask.price is not None and state.ask.size is not None:
                ask_levels[state.ask.price] += state.ask.size
    
    # Convert to sorted lists
    bids = [(p, sz) for p, sz in bid_levels.items() if p > 0 and sz > 0]
    asks = [(p, sz) for p, sz in ask_levels.items() if p > 0 and sz > 0]
    bids.sort(reverse=True)  # Sort bids high to low
    asks.sort()              # Sort asks low to high
    return bids, asks

def _ingest_quotes(quotes: List[Quote], db: MarketDB) -> None:
    # Track trades per symbol for this batch of quotes
    pending_trades: Dict[str, Dict[int, tuple[float | None, int | None]]] = defaultdict(dict)
    snapshot_symbols = set()
    now = time.time() 

    for q in quotes:
        # Parse symbol and exchange from RTD format (e.g. "SPY&N" -> "SPY", "N")
        parts = q.symbol.split("&")
        sym = parts[0]
        exchange = parts[1] if len(parts) > 1 and parts[1] in EXCHANGES else None
            
        ts = int(q.timestamp * 1000)
        typ = q.quote_type.value if hasattr(q.quote_type, "value") else str(q.quote_type)          # Handle trades (LAST/LAST_SIZE) separately from market maker quotes
        if typ == "LAST":
            with _trade_lock:
                entry = _trade_cache[sym][ts]
                entry["price"] = q.value
                if entry["size"] is not None:                    # we already had size
                    db.insert_trades([(sym, ts, entry["price"], entry["size"])])
                    del _trade_cache[sym][ts]                    # done – forget it
            continue

        elif typ == "LAST_SIZE":
            with _trade_lock:
                entry = _trade_cache[sym][ts]
                entry["size"] = int(q.value or 0)
                if entry["price"] is not None:                   # we already had price
                    db.insert_trades([(sym, ts, entry["price"], entry["size"])])
                    del _trade_cache[sym][ts]
            continue

        # Skip market maker quotes without valid exchange
        if not exchange:
            continue

        with _mm_state_lock:
            mm_state = _mm_state[sym][exchange]
            
            if typ == "BID":
                if q.value != mm_state.bid.price:  # Only update if changed
                    mm_state.bid.price = q.value
                    mm_state.bid.last_update = now
            elif typ == "BID_SIZE":
                if q.value != mm_state.bid.size:  # Only update if changed
                    mm_state.bid.size = int(q.value or 0)
                    mm_state.bid.last_update = now
            elif typ == "ASK":
                if q.value != mm_state.ask.price:  # Only update if changed
                    mm_state.ask.price = q.value
                    mm_state.ask.last_update = now
            elif typ == "ASK_SIZE":
                if q.value != mm_state.ask.size:  # Only update if changed
                    mm_state.ask.size = int(q.value or 0)
                    mm_state.ask.last_update = now
            # Print debug info after each quote update
            #debug_print_mm_state(sym)

        # Check if we need a new snapshot for this symbol
        if now - _last_snapshot[sym] >= SNAPSHOT_INTERVAL:
            snapshot_symbols.add(sym)    # Process and store complete trade pairs first
    
    
    trade_rows = []

    for sym, sym_trades in pending_trades.items():
        #print(f"[bridge] Processing trades for {sym}: {len(sym_trades)} pending trades")
        for ts, (price, size) in sym_trades.items():
            if price is not None and size is not None:
                try:
                    # Store with correct column order (symbol, ts, last_price, last_size)
                    trade_rows.append((sym, ts, price, size))
                    #print(f"[bridge✔] Complete trade ready: {sym} @ price={price}×size={size} ts={ts}")
                except Exception as e:
                    print(f"[bridge] Error preparing trade row: {e}")
                    continue

    # Store complete trade pairs
    if trade_rows:
        #print(f"[bridge✔] Attempting to store {len(trade_rows)} complete trades")
        try:
            #print(f"[bridge] Trade rows to insert: {trade_rows}")
            db.insert_trades(trade_rows)
            #print(f"[bridge✔] Successfully stored {len(trade_rows)} trades")
        except Exception as e:
            print(f"[bridge] Error storing trades: {str(e)}")
            import traceback
            traceback.print_exc()

    symbols_processed_count = 0
    if snapshot_symbols:
        #print(f"--- Snapshot check passed for: {list(snapshot_symbols)} at {start_snapshot_processing:.4f} (delta: {start_snapshot_processing - func_start_time:.4f}s) ---")

        # Generate and store snapshots for symbols that need them
        for sym in list(snapshot_symbols): # Iterate over a copy
            agg_start_time = time.time()
            bids, asks = aggregate_levels(sym) # This acquires _mm_state_lock
            agg_duration = time.time() - agg_start_time

            depth_rows = []

            ts = int(now * 1000)

            # Add all bid levels
            for price, total_size in bids:
                depth_rows.append((sym, ts, price, total_size, 'BID'))

            # Add all ask levels
            for price, total_size in asks:
                depth_rows.append((sym, ts, price, total_size, 'ASK'))

            # Add the latest trade if we have one for this symbol
            if sym in pending_trades:
                latest_ts = max(pending_trades[sym].keys())
                price, size = pending_trades[sym][latest_ts]
                if price is not None and size is not None:
                    depth_rows.append((sym, latest_ts, price, size, 'TRADE'))

            if depth_rows:
                try:
                    db.insert_depth_levels(depth_rows) # This might block
                except Exception as e:
                     print(f"[ERROR] DB Insert failed for {sym}: {e}")

                _last_snapshot[sym] = now # Update timestamp *after* successful processing
            else:
                 # It's important to update even if no rows, otherwise the condition
                 # 'now - _last_snapshot[sym] >= SNAPSHOT_INTERVAL' might *always* be true
                 # if a symbol temporarily has no depth, preventing future snapshots.
                 _last_snapshot[sym] = now
                 print(f"[WARN] {sym} @ {now:.4f}: Snapshot triggered but no depth rows generated. Updated last_snapshot time.")


active_symbols: set[str] = set()
active_symbols_lock = threading.Lock()

def fetch_active_symbols() -> set[str]:
    try:
        resp = requests.get(f"{API_URL}/active_symbols", timeout=2)
        if resp.status_code != 200:
            return set()
        data = resp.json()
        new_syms: list[str] = []
        with active_symbols_lock:
            for sym in data.get("symbols", []):
                if sym not in active_symbols:
                    new_syms.append(sym)
                    active_symbols.add(sym)
        return set(new_syms)
    except Exception:
        return set()

def main() -> int:
    db = MarketDB()
    client = RTDClient(heartbeat_ms=250)
    client.initialize() # Initialize COM and server connection in main thread

    # Queue for quotes - This part seems fine
    client.add_quote_listener(lambda q: _ingest_quotes(q if isinstance(q, list) else [q], db))

    stop_evt = threading.Event()
    signal.signal(signal.SIGINT,  lambda *_: stop_evt.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_evt.set())

    # Start the polling thread
    POLL_SECS = 5.0 # Maybe increase polling interval slightly if 1s is too aggressive
    polling_thread = threading.Thread(target=polling_worker, args=(client, POLL_SECS), daemon=True)
    polling_thread.start()

    while not stop_evt.is_set():
        try:
            pythoncom.PumpWaitingMessages() 
            time.sleep(0.1) # Sleep briefly to prevent busy-waiting

        except Exception as e_pump:
            print(f"[MainThread] Error during PumpWaitingMessages: {e_pump}")
            time.sleep(0.01)

    _polling_stop_evt.set() # Signal the polling thread to stop
    polling_thread.join(timeout=5.0) # Wait for polling thread to finish
    if polling_thread.is_alive():
        print("[MainThread] Warning: Polling thread did not exit cleanly.")

    print("[MainThread] Disconnecting RTD client...")
    try:
        client.Disconnect() # Disconnect client cleanly
    except Exception as e_disc:
        print(f"[MainThread] Error during client disconnect: {e_disc}")

    print("[MainThread] Exiting.")
    return 0

if __name__ == "__main__":
    # COM initialization for the main thread happens inside client.initialize()
    sys.exit(main())