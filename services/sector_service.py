# services/sector_service.py
import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime

@st.cache_data(ttl=300, show_spinner=False)
def fetch_etf_history_map(tickers: tuple, period: str = "2y"):
    """
    지정된 ETF 티커들의 수정종가(Close) 히스토리를 딕셔너리로 반환합니다.
    """
    data_map = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            df = t.history(period=period)
            if df is not None and not df.empty:
                df = df.dropna(subset=['Close'])
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                data_map[ticker] = df['Close']
        except Exception:
            continue
    return data_map

def calculate_returns_matrix(etf_info_dict: dict, benchmark_ticker: str = "SPY"):
    """
    모든 대상 ETF의 기간별(1W, 1M, 3M, 6M, 1Y, YTD) 수익률 및 벤치마크 대비 알파를 연산합니다.
    """
    all_tickers = tuple(list(etf_info_dict.keys()) + [benchmark_ticker])
    history_map = fetch_etf_history_map(all_tickers, period="2y")
    
    if benchmark_ticker not in history_map or history_map[benchmark_ticker].empty:
        return None, None

    bench_series = history_map[benchmark_ticker]
    curr_year = datetime.now().year
    
    records = []
    
    for ticker, info in etf_info_dict.items():
        if ticker not in history_map or len(history_map[ticker]) < 10:
            continue
            
        s = history_map[ticker]
        curr_price = s.iloc[-1]
        
        # 기간별 시작 종가 산출 (영업일 기준 근사치)
        p_1w = s.iloc[-6] if len(s) >= 6 else s.iloc[0]
        p_1m = s.iloc[-22] if len(s) >= 22 else s.iloc[0]
        p_3m = s.iloc[-64] if len(s) >= 64 else s.iloc[0]
        p_6m = s.iloc[-127] if len(s) >= 127 else s.iloc[0]
        p_1y = s.iloc[-253] if len(s) >= 253 else s.iloc[0]
        
        # YTD 기준일 (당해 첫 거래일 종가)
        ytd_sub = s[s.index.year == curr_year]
        p_ytd = ytd_sub.iloc[0] if not ytd_sub.empty else curr_price
        
        # 수익률 (%)
        r_1w = ((curr_price - p_1w) / p_1w) * 100
        r_1m = ((curr_price - p_1m) / p_1m) * 100
        r_3m = ((curr_price - p_3m) / p_3m) * 100
        r_6m = ((curr_price - p_6m) / p_6m) * 100
        r_1y = ((curr_price - p_1y) / p_1y) * 100
        r_ytd = ((curr_price - p_ytd) / p_ytd) * 100
        
        records.append({
            "ticker": ticker,
            "name": info["name"],
            "type": info.get("type", info.get("category", "-")),
            "price": curr_price,
            "1W": r_1w,
            "1M": r_1m,
            "3M": r_3m,
            "6M": r_6m,
            "1Y": r_1y,
            "YTD": r_ytd
        })

    if not records:
        return None, None

    df_matrix = pd.DataFrame(records)

    # 벤치마크(SPY) 수익률 산출
    b_curr = bench_series.iloc[-1]
    b_1w = bench_series.iloc[-6] if len(bench_series) >= 6 else bench_series.iloc[0]
    b_1m = bench_series.iloc[-22] if len(bench_series) >= 22 else bench_series.iloc[0]
    b_3m = bench_series.iloc[-64] if len(bench_series) >= 64 else bench_series.iloc[0]
    b_6m = bench_series.iloc[-127] if len(bench_series) >= 127 else bench_series.iloc[0]
    b_1y = bench_series.iloc[-253] if len(bench_series) >= 253 else bench_series.iloc[0]
    b_ytd_sub = bench_series[bench_series.index.year == curr_year]
    b_ytd = b_ytd_sub.iloc[0] if not b_ytd_sub.empty else b_curr

    bench_returns = {
        "1W": ((b_curr - b_1w) / b_1w) * 100,
        "1M": ((b_curr - b_1m) / b_1m) * 100,
        "3M": ((b_curr - b_3m) / b_3m) * 100,
        "6M": ((b_curr - b_6m) / b_6m) * 100,
        "1Y": ((b_curr - b_1y) / b_1y) * 100,
        "YTD": ((b_curr - b_ytd) / b_ytd) * 100
    }

    # 벤치마크 대비 초과성과 (Alpha = Sector Return - SPY Return)
    for p in ["1W", "1M", "3M", "6M", "1Y", "YTD"]:
        df_matrix[f"{p}_alpha"] = df_matrix[p] - bench_returns[p]

    return df_matrix, history_map
