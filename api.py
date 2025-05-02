# api.py  –  works with bookmap_<YYMMDD>.db produced by rtd_bridge
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from typing import List, Optional
import sqlite3, os, traceback, time, threading
from pydantic import BaseModel
import multiprocessing
from rtd_bridge import main as rtd_bridge_main

app = FastAPI(title="Market-Depth API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

class PriceLevel(BaseModel):
    price: float
    quantity: int
    side: str               # "BID" or "ASK"

class DepthResponse(BaseModel):
    symbol: str
    timestamp: int
    levels: List[PriceLevel]
    last_price: Optional[float] = None
    last_size:  Optional[int]  = None
    underlying_price: Optional[float] = None   # not populated yet

class SymbolRequest(BaseModel):
    symbol: str

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _connect_most_recent() -> sqlite3.Connection:
    today = datetime.now().strftime("%y%m%d")
    today_db = os.path.join(DATA_DIR, f"bookmap_{today}.db")
    if os.path.exists(today_db):
        return sqlite3.connect(today_db)

    # fallback → most recent file
    dbs = sorted(
        (f for f in os.listdir(DATA_DIR) if f.startswith("bookmap_") and f.endswith(".db")),
        reverse=True,
    )
    if not dbs:
        raise FileNotFoundError("no bookmap_*.db files in data/")

    return sqlite3.connect(os.path.join(DATA_DIR, dbs[0]))

def _depth_rows_to_pricelevels(rows):
    levels = []
    for row in rows:
        # rows now contain (symbol, timestamp, price, size, side)
        levels.append({
            "price": row[2],
            "quantity": row[3],
            "side": row[4]
        })
    return levels

# ------------------- active-symbol registry --------------------------
_active: set[str] = set()
_active_lock = threading.Lock()

@app.post("/symbols")
async def add_symbol(req: SymbolRequest):
    sym = req.symbol.strip()
    if not sym:
        raise HTTPException(400, "symbol required")
    print(f"Adding symbol to active set: {sym}")
    with _active_lock:
        _active.add(sym)
        print(f"Current active symbols: {_active}")

    return {"ok": True, "symbol": sym}

@app.get("/active_symbols")
async def active_symbols():
    with _active_lock:
        symbols = sorted(_active)
        #print(f"Returning active symbols: {symbols}")
        return {"symbols": symbols}
    
# ────────────────────────────────────────────────────────────────────
# routes
# ────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Market-Depth API is running"}

@app.get("/symbols")
async def get_symbols():
    try:
        # First check active symbols
        with _active_lock:
            active = list(_active)
        
        # Then check database for any additional symbols
        conn = _connect_most_recent()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT symbol FROM depth_levels")
        db_syms = [r[0] for r in cur.fetchall()]
        conn.close()
        
        # Combine both sources
        all_symbols = list(set(active + db_syms))
        print(f"Active symbols: {active}")
        print(f"DB symbols: {db_syms}")
        print(f"All symbols: {all_symbols}")
        return {"symbols": all_symbols}
    except Exception as e:
        print(f"Error in get_symbols: {str(e)}")
        raise HTTPException(500, detail=str(e))

@app.get("/depth/{symbol}", response_model=DepthResponse)
async def latest_depth(symbol: str):
    symbol = symbol.replace("%20", " ").strip()
    #print(f"[api] latest_depth {symbol}")

    try:
        conn = _connect_most_recent(); 
        conn.row_factory = sqlite3.Row; 
        cur = conn.cursor()        
        cur.execute("SELECT MAX(timestamp) FROM depth_levels WHERE symbol=?", (symbol,))
        ts = cur.fetchone()[0]        
        if ts is None:
            conn.close()
            print(f"[api] No data found for symbol {symbol}")
            return {
                "symbol": symbol, 
                "timestamp": int(time.time()*1000), 
                "levels": [],
                "last_price": None,
                "last_size": None
            }        # Get all levels including trades for this timestamp
        cur.execute("""
        SELECT price, SUM(size) AS quantity, side
        FROM depth_levels
        WHERE symbol=? AND timestamp=? AND side IN ('BID', 'ASK')
        GROUP BY price, side
        ORDER BY 
            CASE side 
                WHEN 'ASK' THEN price 
                WHEN 'BID' THEN -price 
            END ASC
        """, (symbol, ts))
        levels = [
            {"price": row[0], "quantity": row[1], "side": row[2]}
            for row in cur.fetchall()
        ]

        # Get the latest trade info from trades table
        cur.execute("""
        SELECT last_price, last_size 
        FROM trades 
        WHERE symbol=? 
        ORDER BY timestamp DESC 
        LIMIT 1
        """, (symbol,))
        trade = cur.fetchone()
        last_price = trade[0] if trade else None
        last_size = trade[1] if trade else None
        
        conn.close()

        #print(f"[api] {symbol} ts={ts}  levels={len(levels)}  last_price={last_price} last_size={last_size}")

        return {
            "symbol": symbol,
            "timestamp": ts,
            "levels": levels,
            "last_price": last_price,
            "last_size": last_size,
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e)+"\n"+traceback.format_exc())

@app.get("/historical_full/{symbol}")
async def historical_full(symbol: str, limit: Optional[int] = None):
    symbol = symbol.replace("%20", " ").strip()
    

    try:
        conn = _connect_most_recent();
        cur = conn.cursor()          # First get all unique timestamps for this symbol
        cur.execute("""
            SELECT DISTINCT timestamp 
            FROM depth_levels 
            WHERE symbol=? 
            ORDER BY timestamp""",
            (symbol,)
        )
        ts_all = [r[0] for r in cur.fetchall()]
        if not ts_all:
            return {"symbol": symbol, "snapshots": []}

        # Sample timestamps if limit is provided
        if limit and len(ts_all) > limit:
            step = max(1, len(ts_all)//limit)
            ts_sample = ts_all[::step]
            if ts_all[-1] not in ts_sample:
                ts_sample.append(ts_all[-1])
        else:
            ts_sample = ts_all

        snapshots = []
        for ts in ts_sample:
            # Get aggregated levels for this timestamp
            cur.execute("""
                SELECT price, SUM(size) as quantity, side 
                FROM depth_levels 
                WHERE symbol=? AND timestamp=? AND side IN ('BID', 'ASK')
                GROUP BY price, side
                ORDER BY 
                    CASE side 
                        WHEN 'ASK' THEN price 
                        WHEN 'BID' THEN -price 
                    END ASC""",
                (symbol, ts)
            )            # Convert rows to level objects with aggregated quantities
            levels = [
                {"price": row[0], "quantity": row[1], "side": row[2]}
                for row in cur.fetchall()
                if row[1] > 0  # Only include levels with positive quantity
            ]

            # Get the most recent trade before or at this timestamp
            cur.execute("""
                SELECT last_price, last_size 
                FROM trades 
                WHERE symbol=? AND timestamp<=? 
                ORDER BY timestamp DESC 
                LIMIT 1""",
                (symbol, ts)
            )
            t = cur.fetchone() or (None, None)

            # Only add snapshot if we have levels or trade data
            if levels or (t[0] is not None and t[1] is not None):
                snapshots.append({
                    "timestamp": ts,
                    "levels": levels,
                    "last_price": t[0],
                    "last_size": t[1],
                })

        conn.close()
        #print(f"[api] historical_full {symbol} snapshots={len(snapshots)}")
        return {"symbol": symbol, "snapshots": snapshots}
    except Exception as e:
        raise HTTPException(500, detail=str(e)+"\n"+traceback.format_exc())


if __name__ == "__main__":
    import uvicorn
    
    # Start the RTD bridge process
    rtd_bridge_process = multiprocessing.Process(target=rtd_bridge_main)
    rtd_bridge_process.start()
    print("RTD bridge process started")
    
    try:
        # Start the API server
        uvicorn.run(app, host="0.0.0.0", port=8080)
    finally:
        # Ensure we clean up the RTD bridge process
        rtd_bridge_process.terminate()
        rtd_bridge_process.join()
        print("RTD bridge process stopped")


