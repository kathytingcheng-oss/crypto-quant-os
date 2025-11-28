import time
import threading
import ccxt
import pandas as pd
import streamlit as st
import datetime
from collections import deque # <--- 必须有这个，用于 FIFO 计算

# ==========================================
# 1. 实时价格获取 (Market Data)
# ==========================================
class MarketData:
    def __init__(self):
        self.prices = {}
        self.lock = threading.Lock()
        self.exchange = ccxt.binance()
        # 监控主流币种
        self.targets = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'DOGE/USDT', 'XRP/USDT', 'ADA/USDT', 'AVAX/USDT']
        self.running = True
        self.thread = threading.Thread(target=self._update_loop, daemon=True)
        self.thread.start()

    def _update_loop(self):
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
        lookup = symbol.upper()
        if not lookup.endswith('/USDT'): lookup += '/USDT'
        with self.lock:
            return self.prices.get(lookup, 0.0)

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
            
        price = market_data.get_price(sym)
        if price == 0: price = avg # Fallback
        
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
                # 获取旧成本以免覆盖
                existing = supabase_client.table("user_portfolios").select("avg_buy_price")\
                    .eq("user_id", user_id).eq("symbol", symbol).execute()
                avg = existing.data[0]['avg_buy_price'] if existing.data else 0.0
                
                upsert_user_asset(supabase_client, user_id, symbol, amount, avg)
                count += 1
        return True, f"Synced {count} assets from {exchange_id.upper()}!"
    except Exception as e:
        return False, f"Sync Error: {str(e)}"

# ==========================================
# 5. 税务引擎逻辑 (The Missing Part!)
# ==========================================

def add_transaction(supabase, user_id, symbol, type, qty, price, date):
    """写入一笔交易记录"""
    data = {
        "user_id": user_id,
        "symbol": symbol.upper(),
        "type": type, # 'BUY' or 'SELL'
        "quantity": qty,
        "price": price,
        "timestamp": date.isoformat()
    }
    supabase.table("transactions").insert(data).execute()

def get_transaction_history(supabase, user_id):
    """读取所有交易历史"""
    try:
        res = supabase.table("transactions").select("*").eq("user_id", user_id).order("timestamp").execute()
        return pd.DataFrame(res.data) if res.data else pd.DataFrame()
    except: return pd.DataFrame()

class TaxCalculator:
    """FIFO 核心算法"""
    def calculate(self, df):
        if df.empty: return 0, []
        
        # 确保数据格式正确
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp')
        
        realized_pnl = 0.0
        tax_events = [] # 存储卖出事件
        
        # 按币种分组计算
        for symbol, group in df.groupby('symbol'):
            queue = deque() # 买入队列
            
            for _, row in group.iterrows():
                if row['type'] == 'BUY':
                    # 入队: [数量, 单价, 时间]
                    queue.append({'qty': float(row['quantity']), 'price': float(row['price']), 'date': row['timestamp']})
                
                elif row['type'] == 'SELL':
                    qty_to_sell = float(row['quantity'])
                    sell_price = float(row['price'])
                    
                    while qty_to_sell > 0 and queue:
                        buy_lot = queue[0] # 取出最早的买入
                        
                        # 这次能抵扣多少?
                        matched = min(qty_to_sell, buy_lot['qty'])
                        
                        # 算盈亏
                        cost = matched * buy_lot['price']
                        proceeds = matched * sell_price
                        gain = proceeds - cost
                        
                        # 算持有期 (长期/短期)
                        days = (row['timestamp'] - buy_lot['date']).days
                        term = "LONG" if days > 365 else "SHORT"
                        
                        # 记录事件
                        tax_events.append({
                            'symbol': symbol,
                            'qty': matched,
                            'gain': gain,
                            'term': term, # SHORT / LONG
                            'date': row['timestamp'].strftime('%Y-%m-%d')
                        })
                        
                        realized_pnl += gain
                        
                        # 更新剩余
                        qty_to_sell -= matched
                        buy_lot['qty'] -= matched
                        
                        # 如果这批买入用完了，移出队列
                        if buy_lot['qty'] <= 0.00000001:
                            queue.popleft()
                            
        return realized_pnl, tax_events
    # ==========================================
    # 6. 交易历史同步 (History Sync for Tax)
    # ==========================================
    def sync_history_log(supabase_client, user_id, exchange_id, api_key, api_secret, password=None):
        """
        深度同步：拉取交易历史用于税务计算
        注意：为了简化，这里默认尝试同步 '币种/USDT' 的交易对
        """
        try:
            # 1. 初始化交易所
            exchange_class = getattr(ccxt, exchange_id)
            config = {
                'apiKey': api_key, 'secret': api_secret,
                'enableRateLimit': True, 'options': {'defaultType': 'spot'}
            }
            if password: config['password'] = password
            exchange = exchange_class(config)
            
            # 2. 获取账户里有哪些币 (只同步有余额的币的历史，节省时间)
            balance = exchange.fetch_balance()
            assets = [coin for coin, amt in balance['total'].items() if amt > 0]
            
            synced_count = 0
            
            # 3. 遍历每个币，拉取历史
            for coin in assets:
                if coin == 'USDT': continue # 跳过 USDT 本身
                symbol = f"{coin}/USDT" # 假设主要对 USDT 交易
                
                try:
                    # 获取最近的成交记录
                    trades = exchange.fetch_my_trades(symbol, limit=50) # 限制最近50条，避免超时
                    
                    for t in trades:
                        # 解析数据
                        side = 'BUY' if t['side'] == 'buy' else 'SELL'
                        qty = float(t['amount'])
                        price = float(t['price'])
                        ts = datetime.datetime.fromtimestamp(t['timestamp']/1000.0)
                        trade_id = str(t['id'])
                        fee = t['fee']['cost'] if t.get('fee') else 0.0
                        
                        # 写入数据库 (利用 upsert 防止重复)
                        data = {
                            "user_id": user_id,
                            "exchange": exchange_id,
                            "symbol": coin,
                            "type": side,
                            "quantity": qty,
                            "price": price,
                            "fee": fee,
                            "timestamp": ts.isoformat(),
                            "trade_id": trade_id # 关键：防止重复的身份证
                        }
                        
                        # 使用 on_conflict 忽略重复项
                        supabase_client.table("transactions").upsert(
                            data, on_conflict="user_id, exchange, trade_id"
                        ).execute()
                        
                        synced_count += 1
                except Exception as e:
                    print(f"Skipping {symbol}: {e}")
                    continue
                    
            return True, f"Synced {synced_count} historical trades!"
            
        except Exception as e:
            return False, f"History Sync Error: {str(e)}"