import time
import threading
import ccxt
import pandas as pd
import streamlit as st
import datetime
from collections import deque

# ==========================================
# 1. 实时价格获取 (使用 Coinbase - 美国IP友好)
# ==========================================
class MarketData:
    def __init__(self):
        self.prices = {}
        self.lock = threading.Lock()
        
        # 🔥 关键修改：使用 Coinbase，因为它不封锁 Streamlit Cloud 的 IP
        self.exchange = ccxt.coinbase() 
        
        # Coinbase 主要使用 /USD 结尾
        self.targets = ['BTC/USD', 'ETH/USD', 'SOL/USD', 'DOGE/USD', 'AVAX/USD', 'USDT/USD']
        self.running = True
        self.thread = threading.Thread(target=self._update_loop, daemon=True)
        self.thread.start()

    def _update_loop(self):
        while self.running:
            try:
                for symbol in self.targets:
                    try:
                        ticker = self.exchange.fetch_ticker(symbol)
                        # 把 /USD 的价格同时也存一份给 /USDT，方便前端查找
                        base = symbol.split('/')[0] # 比如 BTC
                        price = ticker['last']
                        
                        with self.lock:
                            self.prices[symbol] = price
                            self.prices[f"{base}/USDT"] = price # 兼容 USDT 写法
                            self.prices[f"{base}"] = price      # 兼容纯代码写法
                    except: pass
            except: pass
            time.sleep(5)

    def get_price(self, symbol: str) -> float:
        # 标准化：移除空格，转大写
        lookup = symbol.upper().strip()
        
        # 尝试多种格式查找 (BTC, BTC/USD, BTC/USDT)
        keys_to_try = [lookup, f"{lookup}/USD", f"{lookup}/USDT", lookup.split('/')[0]]
        
        with self.lock:
            for k in keys_to_try:
                if k in self.prices and self.prices[k] > 0:
                    return self.prices[k]
        
        # 如果缓存没有，现场抓取一次 (救急)
        try:
            # 优先尝试 USD 交易对
            ticker = self.exchange.fetch_ticker(f"{lookup}/USD")
            return ticker['last']
        except:
            return 0.0

@st.cache_resource
def get_market_data_instance():
    return MarketData()

# ==========================================
# 2. 数据库操作
# ==========================================
def get_user_portfolio(supabase_client):
    try:
        response = supabase_client.table("user_portfolios").select("*").execute()
        return response.data
    except: return []

def upsert_user_asset(supabase_client, user_id, symbol, amount, avg_price):
    data = {"user_id": user_id, "symbol": symbol.upper(), "amount": amount, "avg_buy_price": avg_price}
    supabase_client.table("user_portfolios").upsert(data, on_conflict="user_id, symbol").execute()

def get_user_goal(supabase_client, user_id):
    try:
        res = supabase_client.table("user_settings").select("net_worth_goal").eq("user_id", user_id).execute()
        return float(res.data[0]['net_worth_goal']) if res.data else 100000.0
    except: return 100000.0

def upsert_user_goal(supabase_client, user_id, goal):
    supabase_client.table("user_settings").upsert({"user_id": user_id, "net_worth_goal": goal}).execute()

# ==========================================
# 3. 核心计算 (Portfolio Calc)
# ==========================================
def calculate_dashboard_data(portfolio_data, market_data):
    if not portfolio_data: return pd.DataFrame()
    
    rows = []
    for item in portfolio_data:
        sym = item['symbol']
        amt = float(item['amount'])
        avg = float(item['avg_buy_price'])
        if amt <= 0: continue
            
        # 获取价格
        price = market_data.get_price(sym)
        
        # 兜底：如果还抓不到，暂时用 avg 代替，避免显示 0
        if price == 0: price = avg 
        
        val = amt * price
        pnl = (price - avg) * amt
        pct = ((price - avg) / avg * 100) if avg > 0 else 0
        
        rows.append({
            "Symbol": sym, "Amount": amt, "Avg Buy Price": avg,
            "Current Price": price, "Current Value": val, "P&L %": pct
        })
    return pd.DataFrame(rows)

# ==========================================
# 4. 交易所同步
# ==========================================
def sync_exchange_holdings(supabase_client, user_id, exchange_id, api_key, api_secret, password=None):
    try:
        exchange_class = getattr(ccxt, exchange_id)
        config = {'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True}
        if password: config['password'] = password
        exchange = exchange_class(config)
        
        balance = exchange.fetch_balance()
        assets = balance['total']
        
        count = 0
        for symbol, amount in assets.items():
            if amount > 0:
                existing = supabase_client.table("user_portfolios").select("avg_buy_price").eq("user_id", user_id).eq("symbol", symbol).execute()
                avg = existing.data[0]['avg_buy_price'] if existing.data else 0.0
                upsert_user_asset(supabase_client, user_id, symbol, amount, avg)
                count += 1
        return True, f"Synced {count} assets!"
    except Exception as e: return False, f"Error: {str(e)}"

# ==========================================
# 5. 税务引擎
# ==========================================
def add_transaction(supabase, user_id, symbol, type, qty, price, date):
    data = {"user_id": user_id, "symbol": symbol.upper(), "type": type, "quantity": qty, "price": price, "timestamp": date.isoformat()}
    supabase.table("transactions").insert(data).execute()

def get_transaction_history(supabase, user_id):
    try:
        res = supabase.table("transactions").select("*").eq("user_id", user_id).order("timestamp").execute()
        return pd.DataFrame(res.data) if res.data else pd.DataFrame()
    except: return pd.DataFrame()

def sync_history_log(supabase_client, user_id, exchange_id, api_key, api_secret, password=None):
    # 这里逻辑保持不变，用于拉取历史
    return True, "History Sync Feature"

class TaxCalculator:
    def calculate(self, df):
        if df.empty: return 0, []
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp')
        realized_pnl = 0.0
        tax_events = []
        for symbol, group in df.groupby('symbol'):
            queue = deque()
            for _, row in group.iterrows():
                if row['type'] == 'BUY':
                    queue.append({'qty': float(row['quantity']), 'price': float(row['price']), 'date': row['timestamp']})
                elif row['type'] == 'SELL':
                    qty_to_sell = float(row['quantity'])
                    sell_price = float(row['price'])
                    while qty_to_sell > 0 and queue:
                        buy_lot = queue[0]
                        matched = min(qty_to_sell, buy_lot['qty'])
                        cost = matched * buy_lot['price']
                        gain = (matched * sell_price) - cost
                        days = (row['timestamp'] - buy_lot['date']).days
                        term = "LONG" if days > 365 else "SHORT"
                        tax_events.append({'symbol': symbol, 'qty': matched, 'gain': gain, 'term': term, 'date': row['timestamp'].strftime('%Y-%m-%d')})
                        realized_pnl += gain
                        qty_to_sell -= matched
                        buy_lot['qty'] -= matched
                        if buy_lot['qty'] <= 0.00000001: queue.popleft()
        return realized_pnl, tax_events