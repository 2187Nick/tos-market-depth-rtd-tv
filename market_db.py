# market_db
import os, sqlite3, time
from contextlib import contextmanager
from typing import List, Dict, Optional

class MarketDB:
    def __init__(self, path: Optional[str] = None):
        if path is None:
            today = time.strftime('%y%m%d')
            path = os.path.join('data', f'bookmap_{today}.db')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path  # Store the path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS depth_levels(
            symbol TEXT,
            timestamp REAL,
            price REAL,
            size INTEGER,
            side TEXT,
            PRIMARY KEY(symbol,timestamp,price,side)
        );
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            timestamp REAL,
            last_price REAL,
            last_size INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_depth_symbol_ts ON depth_levels(symbol,timestamp);
        CREATE INDEX IF NOT EXISTS idx_depth_symbol_price ON depth_levels(symbol,price);
        CREATE INDEX IF NOT EXISTS idx_trade_symbol_ts ON trades(symbol,timestamp);
        """)    
    
    def insert_depth_levels(self, rows: list[tuple]):
        """
        rows → [(symbol, ts, price, size, side), ...]
        where side is either 'BID' or 'ASK'
        """
        try:
            with self.conn:
                # Insert new rows without replacing existing ones
                # OR REPLACE prevents a unique-key clash if the same price shows up again for that millisecond.)
                self.conn.executemany(
                    "INSERT OR REPLACE INTO depth_levels(symbol,timestamp,price,size,side) "
                    "VALUES (?,?,?,?,?)", rows
                )

                """ print(f"[db] wrote {len(rows)} depth rows @ ts={rows[0][1]}  "
                    f"prices={[p for _,_,p,_,_ in rows][:10]}") """
                
                # Verify insertion
                symbol = rows[0][0] if rows else None
                if symbol:
                    cur = self.conn.cursor()
                    #print(f"[db] INSERT attempted {len(rows)} rows")
                    cur.execute(
                        "SELECT COUNT(*) FROM depth_levels WHERE symbol=? AND timestamp=?", 
                        (symbol, rows[0][1])
                    )
                    row = cur.fetchone()
                    #print(f"[db] now holds {row[0]} rows for ts={rows[0][1]}")
                    #print(f"Inserted {row[0]} depth levels for {symbol} at timestamp {rows[0][1]}")

        except Exception as e:
            print(f"Error inserting depth levels: {str(e)}")
            import traceback
            traceback.print_exc() 

    def insert_trades(self, rows: list[tuple]):
        """
        rows → [(symbol, ts, last_price, last_size), ...]
        """
        try:
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO trades(symbol,timestamp,last_price,last_size) "
                    "VALUES (?,?,?,?)", rows)
                #print(f"[db] Successfully inserted {len(rows)} trades")
                #for row in rows:
                    #print(f"[db] Trade: {row[0]} @ {row[2]}×{row[3]} ts={row[1]}")
        except Exception as e:
            print(f"[db] Error inserting trades: {str(e)}")
            import traceback
            traceback.print_exc()

    def store_trade(self, symbol: str, last_price: float, last_size: int,
                     ts: float | None = None):
        if ts is None:
            ts = time.time()
        with self.conn:
            self.conn.execute(
                """INSERT INTO trades(symbol,timestamp,last_price,last_size)
                   VALUES (?,?,?,?)""",
                (symbol, ts, last_price, last_size))

    # ---------- readers ----------
    def latest_depth(self, symbol: str, limit_levels: int = 100):
        cur = self.conn.execute(
            """SELECT * FROM depth_levels
               WHERE symbol=? AND timestamp=(
                 SELECT MAX(timestamp) FROM depth_levels WHERE symbol=?)
               ORDER BY CASE side WHEN 'ASK' THEN price END ASC,
                        CASE side WHEN 'BID' THEN price END DESC
               LIMIT ?""", (symbol, symbol, limit_levels))
        return cur.fetchall()

    def depth_history(self, symbol: str, start: float, end: float):
        cur = self.conn.execute(
            """SELECT * FROM depth_levels
               WHERE symbol=? AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp,
                        CASE side WHEN 'ASK' THEN price END ASC,
                        CASE side WHEN 'BID' THEN price END DESC""",
            (symbol, start, end))
        return cur.fetchall()

    def trade_history(self, symbol: str, start: float, end: float):
        cur = self.conn.execute(
            """SELECT timestamp,last_price, last_size
               FROM trades
               WHERE symbol=? AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp""", (symbol, start, end))
        return cur.fetchall()
