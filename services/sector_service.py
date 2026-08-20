"""
services/sector_service.py
섹터 및 자산군 시계열/로테이션 수집 엔진
기존 ETF 수집 헬퍼 및 calculate_returns_matrix 복원 완료, AI Context용 모멘텀 변환 포함
"""
import logging
from datetime import datetime
import pandas as pd
import yfinance as yf
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# ==============================================================================
# 로테이션 ETF 자산군 매핑
# ==============================================================================
ROTATION_SECTORS = {
    "정보기술": "XLK",
    "금융": "XLF",
    "헬스케어": "XLV",
    "임의소비재": "XLY",
    "산업재": "XLI",
    "통신서비스": "XLC",
    "에너지": "XLE",
    "필수소비재": "XLP",
    "부동산": "XLRE",
    "유틸리티": "XLU",
    "소재": "XLB",
}

ROTATION_ASSET_CLASSES = {
    "미국 주식": "SPY",
    "글로벌 주식": "ACWI",
    "미국 장기국채": "TLT",
    "미국 중기국채": "IEF",
    "하이일드채권": "HYG",
    "금": "GLD",
    "원유": "USO",
    "달러": "UUP",
    "원자재 종합": "DBC",
}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_etf_history_map(tickers: tuple, period: str = "2y") -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticker = {
            executor.submit(yf.Ticker(t).history, period=period): t
            for t in tickers
        }
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                df = future.result()
                results[ticker] = df
            except Exception as e:
                logger.warning(f"ETF 수집 실패 ({ticker}): {e}")
                results[ticker] = pd.DataFrame()
    return results


# ==============================================================================
# [복원] 기존 sector_view.py 호환용 calculate_returns_matrix
# ==============================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def calculate_returns_matrix(etf_dict: dict, benchmark_ticker: str = "SPY") -> tuple:
    """섹터 및 자산군의 단기/장기 수익률 매트릭스 계산 (원본 복구)"""
    tickers = list(etf_dict.values())
    if benchmark_ticker not in tickers:
        tickers.append(benchmark_ticker)
        
    hist_map = fetch_etf_history_map(tuple(tickers), period="2y")
    curr_year = datetime.now().year
    
    records = []
    for name, ticker in etf_dict.items():
        df = hist_map.get(ticker)
        if df is None or df.empty or len(df) < 260:
            continue
            
        close = df['Close'].dropna()
        if len(close) < 20:
            continue
            
        curr_price = float(close.iloc[-1])
        
        # YTD calculation
        try:
            ytd_start_date = f"{curr_year-1}-12-31"
            if ytd_start_date in close.index:
                ytd_price = float(close.loc[ytd_start_date])
            else:
                ytd_price = float(close[close.index <= pd.to_datetime(ytd_start_date)].iloc[-1])
            ytd_ret = (curr_price / ytd_price - 1) * 100
        except Exception:
            ytd_ret = 0.0

        def get_ret(days):
            if len(close) > days:
                return (curr_price / float(close.iloc[-(days+1)]) - 1) * 100
            return 0.0
        
        records.append({
            "Sector": name,
            "Ticker": ticker,
            "Current Price": curr_price,
            "1W": get_ret(5),
            "1M": get_ret(21),
            "3M": get_ret(63),
            "6M": get_ret(126),
            "YTD": ytd_ret,
            "1Y": get_ret(252)
        })
        
    res_df = pd.DataFrame(records)
    return res_df, hist_map


# ==============================================================================
# [신규] 로테이션 모멘텀 계산 및 AI Context 포맷 변환
# ==============================================================================
@st.cache_data(ttl=300, show_spinner=False)
def get_rotation_momentum_for_ai() -> dict:
    """
    섹터 및 자산군의 1주/1개월/3개월 모멘텀을 계산.
    """
    ticker_map = {
        **ROTATION_SECTORS,
        **ROTATION_ASSET_CLASSES,
    }

    histories = fetch_etf_history_map(
        tuple(ticker_map.values()),
        period="6mo"
    )

    windows = {
        "1주": 5,
        "1개월": 21,
        "3개월": 63,
    }

    records = []

    for name, ticker in ticker_map.items():
        df = histories.get(ticker)

        if df is None or df.empty or len(df) < 64:
            continue

        close = df["Close"].dropna()

        if len(close) < 64:
            continue

        row = {
            "자산": name,
            "티커": ticker,
            "최신가": float(close.iloc[-1]),
        }

        for period_name, days in windows.items():
            old_price = float(close.iloc[-(days + 1)])
            current_price = float(close.iloc[-1])

            row[period_name] = (
                (current_price / old_price) - 1
            ) * 100

        records.append(row)

    df_result = pd.DataFrame(records)

    if not df_result.empty:
        for period_name in windows:
            df_result[f"{period_name}_순위"] = (
                df_result[period_name]
                .rank(ascending=False, method="min")
                .astype(int)
            )

    return {
        "sector": df_result[
            df_result["자산"].isin(ROTATION_SECTORS.keys())
        ].copy() if not df_result.empty else pd.DataFrame(),
        "asset_class": df_result[
            df_result["자산"].isin(ROTATION_ASSET_CLASSES.keys())
        ].copy() if not df_result.empty else pd.DataFrame(),
    }


def rotation_dataframe_to_context(df: pd.DataFrame, title: str) -> str:
    """모멘텀 순위 DF를 프롬프트 주입용 마크다운 표로 변환"""
    if df is None or df.empty:
        return f"\n#### {title}\n데이터 없음\n"

    result = [f"\n#### {title}"]
    result.append("| 자산 | 티커 | 1주 | 1개월 | 3개월 | 3개월 순위 |")
    result.append("|---|---|---:|---:|---:|---:|")

    for _, row in df.sort_values("3개월", ascending=False).iterrows():
        result.append(
            f"| {row['자산']} | {row['티커']} | "
            f"{row['1주']:+.2f}% | "
            f"{row['1개월']:+.2f}% | "
            f"{row['3개월']:+.2f}% | "
            f"{row['3개월_순위']} |"
        )

    return "\n".join(result)
