import time
import threading
import ccxt
import pandas as pd
import streamlit as st
import datetime
from collections import deque

# ==========================================
# 1. 实时价格获取 (Market Data - 增强版)
# ==========================================
class MarketData:
    def __init__(self):
        self.prices = {}
        self.lock = threading.Lock()
        self.exchange = ccxt.binance()
        # 监控主流币种 (后台循环抓取这些)
        self.targets = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'DOGE/USDT', 'XRP/USDT', 'ADA/USDT', 'AVAX/USDT']
        self.running = True
        
        # 启动后台线程
        self.thread = threading.Thread(target=self._update_loop, daemon=True)
        self.thread.start()

    def _update_loop(self):
        """后台静默更新，每5秒一次"""
        while self.running:
            try:
                for symbol in self.targets:
                    try:
                        ticker = self.exchange.fetch_ticker(symbol)
                        with self.lock:
                            self.prices[symbol] = ticker['last']
                    except: pass
            except: pass
            time.sleep(5)

    def get_price(self, symbol: str) -> float:
        """
        获取价格的核心方法：
        1. 先看内存缓存里有没有。
        2. 如果没有 (云端冷启动慢)，立刻发起一次网络请求现场抓取！
        """
        lookup = symbol.upper()
        # 处理一下常见格式，比如 BTC -> BTC/USDT
        if not '/' in lookup: lookup += '/USDT'
        
        # 1. 尝试从缓存读取
        with self.lock:
            if lookup in self.prices:
                return self.prices[lookup]
        
        # 2. 缓存没有？别慌，现场抓一次 (Fail-safe)
        try:
            # print(f"Cache miss for {lookup}, fetching live...")
            ticker = self.exchange.fetch_ticker(lookup)
            price = ticker['last']
            # 顺便存入缓存，方便下次用
            with self.lock:
                self.prices[lookup] = price
            return price
        except Exception as e:
            # print(f"Failed to fetch {lookup}: {e}")
            return 0.0

@st.cache_resource
def get_market_data_instance():
    return MarketData()

# ==========================================
# 2. 数据库操作 (Supabase Basic)
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
            
        # 这里会调用增强版的 get_price
        price = market_data.get_price(sym)
        
        # 如果还是抓不到(比如币种代码写错了)，才用成本价兜底
        if price == 0: price = avg 
        
        val = amt * price
        pnl = (price - avg) * amt
        pct = ((price - avg) / avg * 100) if avg > 0 else 0
        
        rows.append({
            "Symbol": sym, "Amount": amt, "Avg Buy Price": avg,
            "Current Price": price, "Current Value": val, "Unrealized P&L": pnl, "P&L %": pct
        })
    return pd.DataFrame(rows)

# ==========================================
# 4. 交易所同步 (API Sync)
# ==========================================
def sync_exchange_holdings(supabase_client, user_id, exchange_id, api_key, api_secret, password=None):
    try:
        exchange_class = getattr(ccxt, exchange_id)
        config = {
            'apiKey': api_key, 'secret': api_secret,
            'enableRateLimit': True, 'options': {'defaultType': 'spot'}
        }
        if password: config['password'] = password
        exchange = exchange_class(config)
        
        balance = exchange.fetch_balance()
        assets = balance['total']
        
        count = 0
        for symbol, amount in assets.items():
            if amount > 0:
                existing = supabase_client.table("user_portfolios").select("avg_buy_price")\
                    .eq("user_id", user_id).eq("symbol", symbol).execute()
                avg = existing.data[0]['avg_buy_price'] if existing.data else 0.0
                upsert_user_asset(supabase_client, user_id, symbol, amount, avg)
                count += 1
        return True, f"Synced {count} assets from {exchange_id.upper()}!"
    except Exception as e:
        return False, f"Sync Error: {str(e)}"

# ==========================================
# 5. 税务引擎逻辑
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
    try:
        exchange_class = getattr(ccxt, exchange_id)
        config = {'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True, 'options': {'defaultType': 'spot'}}
        if password: config['password'] = password
        exchange = exchange_class(config)
        
        balance = exchange.fetch_balance()
        assets = [coin for coin, amt in balance['total'].items() if amt > 0]
        synced_count = 0
        
        for coin in assets:
            if coin == 'USDT': continue
            symbol = f"{coin}/USDT"
            try:
                trades = exchange.fetch_my_trades(symbol, limit=50)
                for t in trades:
                    side = 'BUY' if t['side'] == 'buy' else 'SELL'
                    qty = float(t['amount'])
                    price = float(t['price'])
                    ts = datetime.datetime.fromtimestamp(t['timestamp']/1000.0)
                    trade_id = str(t['id'])
                    fee = t['fee']['cost'] if t.get('fee') else 0.0
                    
                    data = {"user_id": user_id, "exchange": exchange_id, "symbol": coin, "type": side, "quantity": qty, "price": price, "fee": fee, "timestamp": ts.isoformat(), "trade_id": trade_id}
                    supabase_client.table("transactions").upsert(data, on_conflict="user_id, exchange, trade_id").execute()
                    synced_count += 1
            except: continue
        return True, f"Synced {synced_count} historical trades!"
    except Exception as e: return False, f"Error: {str(e)}"

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