import time
import threading
import ccxt
import pandas as pd
import streamlit as st
import datetime
from collections import deque

# ==========================================
# 1. 实时价格获取 (修复数据覆盖 Bug 版)
# ==========================================
class MarketData:
    def __init__(self):
        self.prices = {}
        self.lock = threading.Lock()
        self.exchange = ccxt.kraken()
        self.running = True
        self.thread = threading.Thread(target=self._update_loop, daemon=True)
        self.thread.start()

    def _update_loop(self):
        while self.running:
            try:
                tickers = self.exchange.fetch_tickers()
                with self.lock:
                    for symbol, ticker in tickers.items():
                        if not ticker or ticker['last'] is None: continue
                        price = float(ticker['last'])
                        
                        # 1. 先存原始交易对 (如 ETH/BTC)
                        self.prices[symbol] = price
                        
                        # 2. 智能拆解 (🔥 核心修复在这里！)
                        if '/' in symbol:
                            parts = symbol.split('/')
                            base = parts[0]
                            quote = parts[1]
                            
                            # 只有当 Quote 是法币或稳定币时，才更新 Base 价格
                            # 这样 ETH/BTC (0.03) 就不会覆盖 ETH/USD (3000) 了
                            if quote in ['USD', 'USDT', 'USDC', 'DAI']:
                                self.prices[base] = price
                                self.prices[f"{base}/USD"] = price
                                self.prices[f"{base}/USDT"] = price
            except: pass
            time.sleep(5)

    def get_price(self, symbol: str) -> float:
        lookup = symbol.upper().strip()
        with self.lock:
            # 优先查缓存
            if lookup in self.prices: return self.prices[lookup]
            for k in [f"{lookup}/USD", f"{lookup}/USDT"]:
                if k in self.prices: return self.prices[k]
        
        # 稳定币兜底
        if lookup in ['USDC', 'USDT', 'DAI', 'BUSD', 'FDUSD']: return 1.0
        
        # 现场抓取
        try: return float(self.exchange.fetch_ticker(f"{lookup}/USD")['last'])
        except: return 0.0

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
    # 智能成本修正
    if avg_price == 0:
        if symbol in ['USDT', 'USDC', 'DAI', 'USD']:
            avg_price = 1.0
        else:
            try:
                existing = supabase_client.table("user_portfolios").select("avg_buy_price").eq("user_id", user_id).eq("symbol", symbol).execute()
                if existing.data: avg_price = existing.data[0]['avg_buy_price']
            except: pass
    
    data = {"user_id": user_id, "symbol": symbol.upper(), "amount": amount, "avg_buy_price": avg_price}
    supabase_client.table("user_portfolios").upsert(data, on_conflict="user_id, symbol").execute()

def delete_user_asset(supabase_client, user_id, symbol):
    try: supabase_client.table("user_portfolios").delete().eq("user_id", user_id).eq("symbol", symbol).execute()
    except: pass

def reset_user_portfolio(supabase_client, user_id):
    try: supabase_client.table("user_portfolios").delete().eq("user_id", user_id).execute()
    except: pass

def get_user_goal(supabase_client, user_id):
    try:
        res = supabase_client.table("user_settings").select("net_worth_goal").eq("user_id", user_id).execute()
        return float(res.data[0]['net_worth_goal']) if res.data else 100000.0
    except: return 100000.0

def upsert_user_goal(supabase_client, user_id, goal):
    supabase_client.table("user_settings").upsert({"user_id": user_id, "net_worth_goal": goal}).execute()

# ==========================================
# 3. 计算逻辑
# ==========================================
def calculate_dashboard_data(portfolio_data, market_data):
    if not portfolio_data: return pd.DataFrame()
    rows = []
    for item in portfolio_data:
        sym = item['symbol']
        amt = float(item['amount'])
        avg = float(item['avg_buy_price'])
        if amt <= 0: continue
        price = market_data.get_price(sym)
        if price == 0: price = avg 
        val = amt * price
        pnl = (price - avg) * amt
        pct = ((price - avg) / avg * 100) if avg > 0 else 0
        rows.append({"Symbol": sym, "Amount": amt, "Avg Buy Price": avg, "Current Price": price, "Current Value": val, "P&L %": pct})
    return pd.DataFrame(rows)

# ==========================================
# 4. 同步余额
# ==========================================
def sync_exchange_holdings(supabase_client, user_id, exchange_id, api_key, api_secret, password=None):
    try:
        exchange_class = getattr(ccxt, exchange_id)
        config = {'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True, 'options': {'defaultType': 'spot'}}
        if password: config['password'] = password
        exchange = exchange_class(config)
        balance = exchange.fetch_balance()
        assets = balance['total']
        count = 0
        for symbol, amount in assets.items():
            if amount > 0:
                upsert_user_asset(supabase_client, user_id, symbol, amount, 0)
                count += 1
        return True, f"Synced {count} assets!"
    except Exception as e: return False, f"Sync Error: {str(e)}"

# ==========================================
# 5. 核心：同步历史
# ==========================================
def add_transaction(supabase, user_id, symbol, type, qty, price, date):
    data = {"user_id": user_id, "symbol": symbol.upper(), "type": type, "quantity": qty, "price": price, "timestamp": date.isoformat()}
    supabase.table("transactions").insert(data).execute()
    recalculate_single_asset(supabase, user_id, symbol)

def get_transaction_history(supabase, user_id):
    try:
        res = supabase.table("transactions").select("*").eq("user_id", user_id).order("timestamp").execute()
        return pd.DataFrame(res.data) if res.data else pd.DataFrame()
    except: return pd.DataFrame()

def recalculate_single_asset(supabase, user_id, symbol):
    try:
        res = supabase.table("transactions").select("*").eq("user_id", user_id).eq("symbol", symbol).eq("type", "BUY").execute()
        buys = res.data
        if buys:
            total_cost = sum([float(b['price']) * float(b['quantity']) for b in buys])
            total_qty = sum([float(b['quantity']) for b in buys])
            if total_qty > 0:
                avg = total_cost / total_qty
                supabase.table("user_portfolios").update({"avg_buy_price": avg}).eq("user_id", user_id).eq("symbol", symbol).execute()
    except: pass

def fetch_special_converts(exchange, exchange_id):
    trades = []
    try:
        if exchange_id == 'binance' and hasattr(exchange, 'sapi_get_convert_tradeflow'):
            end = exchange.milliseconds()
            start = end - (90 * 24 * 60 * 60 * 1000)
            try:
                res = exchange.sapi_get_convert_tradeflow({'startTime': start, 'endTime': end, 'limit': 100})
                if 'list' in res:
                    for item in res['list']:
                        ts = datetime.datetime.fromtimestamp(item['createTime']/1000.0)
                        qty = float(item['toAmount'])
                        cost_total = float(item['fromAmount'])
                        price = cost_total / qty if qty > 0 else 0
                        trades.append({'symbol': item['toAsset'], 'side': 'BUY', 'amount': qty, 'price': price, 'timestamp': ts, 'id': f"bin_conv_{item['orderId']}"})
            except: pass
        elif exchange_id == 'okx':
            try:
                res = exchange.private_get_asset_convert_history()
                if 'data' in res:
                    for item in res['data']:
                        ts = datetime.datetime.fromtimestamp(int(item['cTime'])/1000.0)
                        qty = float(item['toAmt'])
                        price = float(item['price']) 
                        trades.append({'symbol': item['toCcy'], 'side': 'BUY', 'amount': qty, 'price': price, 'timestamp': ts, 'id': f"okx_conv_{item['orderId']}"})
            except: pass
        elif exchange_id in ['kraken', 'coinbase', 'kucoin']:
            if exchange.has['fetchLedger']:
                try:
                    ledger = exchange.fetch_ledger(limit=50) 
                    for item in ledger:
                        if item['type'] == 'trade' and float(item['amount']) > 0: 
                            trades.append({'symbol': item['currency'], 'side': 'BUY', 'amount': float(item['amount']), 'price': 0, 'timestamp': datetime.datetime.fromtimestamp(item['timestamp']/1000.0), 'id': str(item['id'])})
                except: pass
    except: pass
    return trades

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
            if coin in ['USD', 'USDT', 'USDC']: continue
            trades = []
            search_list = [f"{coin}/USDT", f"{coin}/USD", f"{coin}/USDC", f"{coin}/BTC", f"{coin}/ETH"]
            for symbol in search_list:
                try:
                    t = exchange.fetch_my_trades(symbol, limit=50)
                    if t: trades.extend(t)
                except: continue
            seen = set(); unique_trades = []
            for t in trades:
                if t['id'] not in seen: unique_trades.append(t); seen.add(t['id'])
            for t in unique_trades:
                ts = datetime.datetime.fromtimestamp(t['timestamp']/1000.0)
                data = {"user_id": user_id, "exchange": exchange_id, "symbol": coin, "type": 'BUY' if t['side']=='buy' else 'SELL', "quantity": float(t['amount']), "price": float(t['price']), "fee": 0, "timestamp": ts.isoformat(), "trade_id": str(t['id'])}
                supabase_client.table("transactions").upsert(data, on_conflict="user_id, exchange, trade_id").execute()
                synced_count += 1
            recalculate_single_asset(supabase_client, user_id, coin)
        
        special_trades = fetch_special_converts(exchange, exchange_id)
        for t in special_trades:
            if t['price'] > 0:
                data = {"user_id": user_id, "exchange": exchange_id, "symbol": t['symbol'], "type": t['side'], "quantity": t['amount'], "price": t['price'], "timestamp": t['timestamp'].isoformat(), "trade_id": t['id']}
                supabase_client.table("transactions").upsert(data, on_conflict="user_id, exchange, trade_id").execute()
                synced_count += 1
                recalculate_single_asset(supabase_client, user_id, t['symbol'])
        return True, f"Synced {synced_count} records!"
    except Exception as e: return False, f"Error: {str(e)}"

class TaxCalculator:
    def calculate(self, df):
        if df.empty: return 0, []
        df['timestamp'] = pd.to_datetime(df['timestamp'], format='mixed')
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