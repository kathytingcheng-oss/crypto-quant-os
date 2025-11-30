import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import time
import datetime
import extra_streamlit_components as stx
from supabase import create_client, Client
import price_engine
import ccxt

# ==========================================
# 1. 页面配置与 CSS
# ==========================================
st.set_page_config(page_title="CRYPTO QUANT OS", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Roboto+Mono:wght@400;700&display=swap');

    .stApp { background-color: #05070a; color: #e0f7ff; font-family: 'Roboto Mono', monospace; }
    [data-testid="stSidebar"] { background-color: #0b0f15; border-right: 1px solid #30363d; }

    .stButton > button {
        background-color: #00ff41 !important; color: #000000 !important;
        font-family: 'Orbitron', sans-serif !important; font-weight: 900 !important;
        border: none !important; box-shadow: 0 0 10px rgba(0, 255, 65, 0.3) !important;
    }
    .stButton > button:hover { background-color: #00cc33 !important; transform: scale(1.02); }

    .stTextInput input, .stSelectbox div[data-baseweb="select"], .stNumberInput input {
        background-color: #0d1117 !important; color: #00f3ff !important; border: 1px solid #30363d !important; font-weight: bold !important;
    }
    label { color: #ffffff !important; font-family: 'Orbitron', sans-serif !important; letter-spacing: 1px !important; }

    .login-container { background: rgba(13, 17, 23, 0.95); padding: 40px; border-radius: 12px; border: 1px solid #30363d; }
    
    .hud-card { background: #161b22; border-radius: 8px; padding: 20px; border: 1px solid #30363d; }
    .card-value { font-family: 'Orbitron'; font-size: 2rem; font-weight: 700; color: #fff; }
    .glow-blue { border-bottom: 3px solid #00f3ff; }
    .glow-green { border-bottom: 3px solid #00ff41; }
    .glow-red { border-bottom: 3px solid #ff003c; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. 初始化
# ==========================================
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except: st.stop()

if 'user' not in st.session_state: st.session_state.user = None
if 'session' in st.session_state:
    try: supabase.auth.set_session(st.session_state.session.access_token, st.session_state.session.refresh_token)
    except: del st.session_state.session

# ==========================================
# 3. 辅助函数
# ==========================================
def render_hud(title, val, sub, theme="blue"):
    return f"""
    <div class="hud-card glow-{theme}">
        <div style="color:#8b949e; font-size:0.8rem; letter-spacing:1px; margin-bottom:5px;">{title}</div>
        <div class="card-value" style="color: var(--neon-{theme})">{val}</div>
        <div style="font-size:0.9rem; color: #fff; margin-top:5px;">{sub}</div>
    </div>
    """

# ==========================================
# 4. Login UI
# ==========================================
def login_ui():
    st.write(""); st.write("")
    c1, c2, c3 = st.columns([1, 1.2, 1])
    with c2:
        st.markdown("<h1 style='text-align:center; color:#00ff41; font-family:Orbitron; font-size: 3.5rem; text-shadow: 0 0 20px rgba(0,255,65,0.4);'>QUANT OS</h1>", unsafe_allow_html=True)
        with st.container():
            st.markdown('<div class="login-container">', unsafe_allow_html=True)
            tab1, tab2 = st.tabs(["🔒 LOGIN", "📝 REGISTER"])
            with tab1:
                email = st.text_input("EMAIL", key="l_e")
                pwd = st.text_input("PASSWORD", type="password", key="l_p")
                if st.button("ENTER SYSTEM ➜", use_container_width=True):
                    try:
                        res = supabase.auth.sign_in_with_password({"email": email, "password": pwd})
                        st.session_state.user = res.user; st.session_state.session = res.session; st.rerun()
                    except Exception as e: st.error(str(e))
            with tab2:
                email = st.text_input("NEW EMAIL", key="r_e")
                pwd = st.text_input("NEW PASSWORD", type="password", key="r_p")
                if st.button("CREATE ID ➜", use_container_width=True):
                    try: supabase.auth.sign_up({"email": email, "password": pwd}); st.success("Check Email")
                    except Exception as e: st.error(str(e))
            st.markdown('</div>', unsafe_allow_html=True)

# ==========================================
# 5. Main App
# ==========================================
def main_app():
    user = st.session_state.user
    cookie_manager = stx.CookieManager()
    cookies = cookie_manager.get_all()
    
    # 🔥 1. 先计算数据 (解决报错的关键！)
    market = price_engine.get_market_data_instance()
    raw = price_engine.get_user_portfolio(supabase)
    df = price_engine.calculate_dashboard_data(raw, market)
    
    # 指标计算
    val = df['Current Value'].sum() if not df.empty else 0
    cost = (df['Amount'] * df['Avg Buy Price']).sum() if not df.empty else 0
    pnl = val - cost
    pct = (pnl/cost*100) if cost>0 else 0
    goal = price_engine.get_user_goal(supabase, user.id)
    goal_pct = min((val/goal*100), 100) if goal>0 else 0
    est_tax = max(pnl * (st.session_state.get('tax_rate', 30.0)/100), 0)

    # --- Sidebar ---
    with st.sidebar:
        st.markdown("### ⚙ CONTROL PANEL")
        mode = st.radio("MODE", ["MANUAL ENTRY", "AUTO SYNC (API)"])
        st.divider()
        
        if mode == "MANUAL ENTRY":
            with st.form("manual"):
                sym = st.text_input("SYMBOL", value="BTC").upper()
                amt = st.number_input("QUANTITY", min_value=0.0, format="%.4f")
                avg = st.number_input("AVG PRICE", 0.0, format="%.2f")
                if st.form_submit_button("SAVE"):
                    price_engine.upsert_user_asset(supabase, user.id, sym, amt, avg); st.rerun()
        else:
            exchange = st.selectbox("EXCHANGE", ["binance", "okx", "bybit", "kraken", "kucoin", "bitget", "gate"])
            c_key = cookies.get(f"{exchange}_key", ""); c_sec = cookies.get(f"{exchange}_sec", ""); c_pass = cookies.get(f"{exchange}_pass", "")
            
            api_key = st.text_input("API Key", value=str(c_key), type="password")
            api_sec = st.text_input("Secret Key", value=str(c_sec), type="password")
            
            needs_pass = exchange in ['okx', 'kucoin', 'bitget', 'gate']
            pass_val = str(c_pass) if c_pass else ""
            password = None
            if st.checkbox("Requires Passphrase?", value=bool(c_pass) or needs_pass):
                password = st.text_input("Passphrase", value=pass_val, type="password")
            
            remember = st.checkbox("Remember Keys")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("SYNC BAL"):
                    if remember:
                        exp = datetime.datetime.now() + datetime.timedelta(days=30)
                        cookie_manager.set(f"{exchange}_key", api_key, expires_at=exp)
                        cookie_manager.set(f"{exchange}_sec", api_sec, expires_at=exp)
                        if password: cookie_manager.set(f"{exchange}_pass", password, expires_at=exp)
                    with st.spinner("Updating..."):
                        ok, msg = price_engine.sync_exchange_holdings(supabase, user.id, exchange, api_key, api_sec, password)
                        if ok: st.success("Updated"); time.sleep(1); st.rerun()
                        else: st.error(msg)
            with col2:
                if st.button("SYNC LOG"):
                    with st.spinner("Fetching..."):
                        ok, msg = price_engine.sync_history_log(supabase, user.id, exchange, api_key, api_sec, password)
                        if ok: st.success("Done"); time.sleep(1); st.rerun()
                        else: st.error(msg)
            if st.button("Clear Keys"):
                cookie_manager.delete(f"{exchange}_key"); cookie_manager.delete(f"{exchange}_sec"); 
                if password: cookie_manager.delete(f"{exchange}_pass")
                st.rerun()

        st.divider()
        with st.expander("🎯 TAX & GOAL"):
            cur = price_engine.get_user_goal(supabase, user.id)
            new = st.number_input("Target $", value=float(cur), step=5000.0)
            if 'tax_rate' not in st.session_state: st.session_state.tax_rate = 30.0
            st.session_state.tax_rate = st.slider("Tax Rate %", 0.0, 50.0, st.session_state.tax_rate)
            if st.button("SAVE"): price_engine.upsert_user_goal(supabase, user.id, new); st.rerun()

        # 🔥 2. 资产管理 (删除功能) - 现在 df 已经有值了，不会报错！
        with st.expander("🗑️ MANAGE ASSETS"):
            asset_list = [row['Symbol'] for row in df.to_dict('records')] if not df.empty else []
            if asset_list:
                to_del = st.selectbox("Select Asset", asset_list)
                if st.button(f"DELETE {to_del}"):
                    price_engine.delete_user_asset(supabase, user.id, to_del)
                    st.toast("Deleted"); time.sleep(0.5); st.rerun()
                st.write("")
                if st.button("⚠️ RESET ALL", type="primary"):
                    price_engine.reset_user_portfolio(supabase, user.id)
                    st.success("Cleared"); time.sleep(0.5); st.rerun()
            else: st.caption("Empty Portfolio")

        if st.button("LOGOUT"): supabase.auth.sign_out(); st.session_state.user = None; st.rerun()

    # --- Dashboard UI ---
    st.markdown("### 📡 SYSTEM STATUS: ONLINE")
    c1, c2, c3 = st.columns(3)
    with c1: st.markdown(render_hud("NET WORTH", f"${val:,.2f}", "TOTAL ASSETS", "blue"), unsafe_allow_html=True)
    with c2: st.markdown(render_hud("24H P&L", f"${pnl:,.2f}", f"{pct:+.2f}%", "green" if pnl>=0 else "red"), unsafe_allow_html=True)
    with c3: 
        t_c = "red" if est_tax > 0 else "green"
        st.markdown(render_hud("EST. TAX BILL", f"${est_tax:,.2f}", "LIABILITY ALERT", t_c), unsafe_allow_html=True)
    
    st.markdown(f"""
    <div style="margin-top:15px; margin-bottom:10px; display:flex; justify-content:space-between; font-size:0.8rem; color:#8b949e;">
        <span>YTD COMPLETION</span><span style="color:#00ff41">{goal_pct:.1f}%</span>
    </div>
    <div style="margin-bottom:30px; background:#21262d; height:10px; border-radius:5px; overflow:hidden;">
        <div style="width:{goal_pct}%; background:linear-gradient(90deg, #00f3ff, #00ff41); height:100%; box-shadow: 0 0 10px rgba(0, 255, 65, 0.5);"></div>
    </div>
    """, unsafe_allow_html=True)

    # --- Table & Charts ---
    c_left, c_right = st.columns([2, 1])
    
    with c_left:
        st.markdown("#### 📊 LIVE POSITIONS")
        if not df.empty:
            # 🔥 3. 换回原生 dataframe (配合 config.toml 就是黑色的，不会有乱码)
            st.dataframe(
                df,
                column_order=['Symbol', 'Amount', 'Avg Buy Price', 'Current Price', 'Current Value', 'P&L %'],
                hide_index=True, use_container_width=True, height=400,
                column_config={
                    "Symbol": st.column_config.TextColumn("Asset"),
                    "Amount": st.column_config.NumberColumn("Holdings", format="%.4f"),
                    "Avg Buy Price": st.column_config.NumberColumn("Avg Buy", format="$%.2f"),
                    "Current Price": st.column_config.NumberColumn("Price", format="$%.2f"),
                    "Current Value": st.column_config.NumberColumn("Value", format="$%.2f"),
                    "P&L %": st.column_config.ProgressColumn("Performance", format="%.2f%%", min_value=-100, max_value=100)
                }
            )
        else: st.info("Waiting for data...")

    with c_right:
        st.markdown("#### 🍩 ALLOCATION")
        if not df.empty:
            fig = go.Figure(data=[go.Pie(labels=df['Symbol'], values=df['Current Value'], hole=.6)])
            fig.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', showlegend=False, margin=dict(t=0,b=0,l=0,r=0), height=300)
            fig.update_traces(marker=dict(colors=['#00f3ff', '#00ff41', '#ff003c', '#e0f7ff']))
            st.plotly_chart(fig, use_container_width=True)
    
    # --- Tax Engine ---
    st.markdown("---"); st.markdown("### 🏛 THE TAX ENGINE")
    
    # 1. 录入区 (修复报错显示)
    with st.expander("➕ Manual Tax Record"):
        with st.form("t_rec"):
            cc1, cc2, cc3, cc4 = st.columns(4)
            tt = cc1.selectbox("TYPE", ["BUY", "SELL"])
            ts = cc2.text_input("SYM", "BTC").upper()
            tq_str = cc3.text_input("QTY", value="0.0000")
            tp_str = cc4.text_input("PRICE ($)", value="0.00")
            td = st.date_input("DATE", datetime.date.today())
            
            if st.form_submit_button("ADD"):
                # 第一步：先检查数字格式
                try:
                    tq = float(tq_str)
                    tp = float(tp_str)
                except ValueError:
                    st.error("❌ 格式错误：请输入纯数字 (例如 0.002)")
                    st.stop() # 停止执行，不进入下一步
                
                # 第二步：尝试写入数据库 (捕获真实错误)
                try:
                    price_engine.add_transaction(supabase, user.id, ts, tt, tq, tp, td)
                    st.success("Recorded")
                    time.sleep(0.5); st.rerun()
                except Exception as e:
                    st.error(f"❌ Database Error: {e}")

    # 2. 获取数据
    tx_df = price_engine.get_transaction_history(supabase, user.id)
    
    if not tx_df.empty:
        # 计算税务
        calc = price_engine.TaxCalculator()
        realized, events = calc.calculate(tx_df)
        
        # 顶部功能栏：清空按钮
        col_info, col_clear = st.columns([3, 1])
        with col_info:
            st.markdown(f"**REALIZED P&L:** <span style='color:{'#00ff41' if realized>0 else '#ff003c'}'>${realized:,.2f}</span>", unsafe_allow_html=True)
        with col_clear:
            if st.button("🗑️ CLEAR HISTORY"):
                price_engine.clear_all_transactions(supabase, user.id)
                st.success("Cleared"); time.sleep(0.5); st.rerun()

        # 选项卡展示
        tab_tax, tab_ledger = st.tabs(["💰 REALIZED P&L (Tax Events)", "📜 FULL LEDGER (Manage)"])
        
        # Tab 1: 税务事件 (只显示卖出结果)
        with tab_tax:
            if events:
                for e in reversed(events[-5:]):
                    c = "#00ff41" if e['term']=="LONG" else "#ff003c"
                    tag = "LONG TERM" if e['term']=="LONG" else "SHORT TERM"
                    st.markdown(f"""
                    <div style="background:#161b22; border-left:4px solid {c}; padding:15px; margin-bottom:10px; border-radius:4px; display:flex; justify-content:space-between; align-items:center;">
                        <div><span style="color:{c}; font-weight:bold">SOLD</span> <span style="color:#fff; font-weight:bold; margin-left:10px;">{e['symbol']} ({e['qty']:.4f})</span></div>
                        <div><span style="background:{c}; color:#000; padding:2px 8px; border-radius:2px; font-weight:bold; font-size:0.8rem; margin-right:15px;">{tag}</span><span style="color:#8b949e">Gain:</span> <span style="color:{c}; font-weight:bold">${e['gain']:,.2f}</span></div>
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.info("No taxable events yet (You haven't sold anything).")

        # Tab 2: 完整账本 (带删除功能！)
        with tab_ledger:
            st.caption("Click 'X' to delete a specific record.")
            
            # 遍历显示每一条记录，并加一个删除按钮
            # 为了美观，我们用 st.columns 来模拟表格
            
            # 表头
            h1, h2, h3, h4, h5, h6 = st.columns([2, 1, 1, 2, 2, 1])
            h1.markdown("**Date**")
            h2.markdown("**Type**")
            h3.markdown("**Asset**")
            h4.markdown("**Qty**")
            h5.markdown("**Price**")
            h6.markdown("**Action**")
            st.divider()

            # 列表内容 (倒序显示，最新的在上面)
            # 注意：iterrows 性能较差，但对于几十条记录没问题
            for index, row in tx_df.sort_values('timestamp', ascending=False).iterrows():
                c1, c2, c3, c4, c5, c6 = st.columns([2, 1, 1, 2, 2, 1])
                
                # 格式化日期
                ts_str = pd.to_datetime(row['timestamp']).strftime('%Y-%m-%d')
                
                c1.write(ts_str)
                c2.write(row['type'])
                c3.write(row['symbol'])
                c4.write(f"{float(row['quantity']):.4f}")
                c5.write(f"${float(row['price']):,.2f}")
                
                # 删除按钮 (使用唯一的 key 防止冲突)
                if c6.button("❌", key=f"del_{row['id']}"):
                    price_engine.delete_transaction(supabase, row['id'])
                    st.toast("Deleted"); time.sleep(0.5); st.rerun()
            
    else:
        st.info("No transaction history. Add a record above.")

    time.sleep(2)
    st.rerun()


if __name__ == "__main__":
    if st.session_state.user: main_app()
    else: login_ui()