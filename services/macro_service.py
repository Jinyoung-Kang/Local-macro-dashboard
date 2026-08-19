"""
services/macro_service.py
거시경제 지표, 금리, 환율, 원자재 데이터 수집 엔진
ThreadPoolExecutor 기반 비동기 병렬 I/O 처리 적용
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from config import MACRO_CATEGORIES, FRED_BASE_URL, get_fred_key

logger = logging.getLogger(__name__)


def fetch_ticker_data(symbol: str, period: str = "1mo", interval: str = "1d") -> pd.DataFrame:
    """단일 티커 yfinance 데이터 수집 순수 함수"""
    try:
        tk = yf.Ticker(symbol)
        df = tk.history(period=period, interval=interval)
        if df.empty or len(df) < 2:
            return pd.DataFrame()
        df = df.reset_index()
        df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
        return df.sort_values("Date").reset_index(drop=True)
    except Exception as e:
        logger.warning(f"티커({symbol}) 조회 실패: {e}")
        return pd.DataFrame()


def fetch_fred_series(series_id: str, start_date: str = None, fred_api_key: str = "") -> pd.DataFrame:
    """단일 FRED 시계열 데이터 수집 순수 함수"""
    if not fred_api_key:
        return pd.DataFrame()

    if not start_date:
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    url = f"{FRED_BASE_URL}/series/observations"
    params = {
        "series_id": series_id,
        "api_key": fred_api_key,
        "file_type": "json",
        "observation_start": start_date,
        "sort_order": "asc"
    }

    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            obs = res.json().get("observations", [])
            if obs:
                records = []
                for o in obs:
                    v = o.get("value", ".")
                    if v != ".":
                        records.append({"Date": pd.to_datetime(o["date"]), "Close": float(v)})
                df = pd.DataFrame(records)
                return df.sort_values("Date").reset_index(drop=True) if not df.empty else pd.DataFrame()
    except Exception as e:
        logger.warning(f"FRED({series_id}) 조회 실패: {e}")
    return pd.DataFrame()


@st.cache_data(ttl=30, show_spinner=False)
def get_collected_macro_data():
    """
    모든 거시경제 티커 및 FRED 데이터를 ThreadPoolExecutor로 병렬 수집
    반환값: (collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev)
    """
    # 1. 메인 스레드에서 시크릿 사전 로드
    fred_key = get_fred_key()

    all_tickers = {}
    for cat in MACRO_CATEGORIES.values():
        all_tickers.update(cat)

    def _fetch_one_yf(name, symbol):
        try:
            df = fetch_ticker_data(symbol, period="1mo")
            return name, df, None
        except Exception as e:
            return name, None, str(e)

    collected = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_fetch_one_yf, name, symbol): name
            for name, symbol in all_tickers.items()
        }
        for future in as_completed(futures):
            name, df, err = future.result()
            collected[name] = df if err is None else None

    # 2. FRED 주요 금리 시리즈 병렬 수집
    fred_series_map = {
        "US_10Y": "DGS10",
        "US_2Y": "DGS2",
        "FEDFUNDS": "FEDFUNDS",
        "M2": "M2SL"
    }

    def _fetch_one_fred(name, sid):
        try:
            df = fetch_fred_series(sid, fred_api_key=fred_key)
            return name, df, None
        except Exception as e:
            return name, None, str(e)

    with ThreadPoolExecutor(max_workers=4) as executor:
        fred_futures = {
            executor.submit(_fetch_one_fred, name, sid): name
            for name, sid in fred_series_map.items()
        }
        for future in as_completed(fred_futures):
            name, df, err = future.result()
            collected[name] = df if err is None else None

    # 3. 10Y/2Y 금리 추출 및 스프레드 기초값 연산
    rate_10y_curr, rate_10y_prev = 0.0, 0.0
    rate_2y_curr, rate_2y_prev = 0.0, 0.0

    df_10y = collected.get("미국채 10년물 금리", collected.get("US_10Y"))
    df_2y = collected.get("미국채 2년물 금리", collected.get("US_2Y"))

    if df_10y is not None and isinstance(df_10y, pd.DataFrame) and not df_10y.empty and len(df_10y) >= 2:
        rate_10y_curr = float(df_10y["Close"].iloc[-1])
        rate_10y_prev = float(df_10y["Close"].iloc[-2])
    elif df_10y is not None and isinstance(df_10y, pd.DataFrame) and not df_10y.empty and len(df_10y) == 1:
        rate_10y_curr = float(df_10y["Close"].iloc[-1])
        rate_10y_prev = rate_10y_curr

    if df_2y is not None and isinstance(df_2y, pd.DataFrame) and not df_2y.empty and len(df_2y) >= 2:
        rate_2y_curr = float(df_2y["Close"].iloc[-1])
        rate_2y_prev = float(df_2y["Close"].iloc[-2])
    elif df_2y is not None and isinstance(df_2y, pd.DataFrame) and not df_2y.empty and len(df_2y) == 1:
        rate_2y_curr = float(df_2y["Close"].iloc[-1])
        rate_2y_prev = rate_2y_curr

    return collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev
