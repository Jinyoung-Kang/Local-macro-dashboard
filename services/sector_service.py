"""
services/sector_service.py
섹터 및 자산군 시계열/로테이션 수집 엔진
기존 ETF 수집 헬퍼 및 calculate_returns_matrix 복원 완료, AI Context용 모멘텀 변환 포함
(sector_view.py 호환을 위한 딕셔너리 구조 및 Close Series 반환 버그 수정 반영)
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
# [수정 완료] 기존 sector_view.py 호환용 calculate_returns_matrix
# ==============================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def calculate_returns_matrix(
    etf_dict: dict,
    benchmark_ticker: str = "SPY",
) -> tuple[pd.DataFrame, dict]:
    """
    config.py ETF 설정 구조를 받아 기간별 수익률 매트릭스를 계산합니다.

    입력 구조:
    {
        "XLK": {"name": "정보기술 (Technology)", "type": "공격 / 성장"},
        "XLF": {"name": "금융 (Financials)", "type": "경기민감"},
    }

    반환:
    - DataFrame: 기존 sector_view.py 호환 컬럼
    - dict: 티커별 yfinance 시계열 (Close Series만 포함)
    """
    tickers = list(etf_dict.keys())

    if benchmark_ticker not in tickers:
        tickers.append(benchmark_ticker)

    raw_hist_map = fetch_etf_history_map(
        tuple(tickers),
        period="2y",
    )

    history_map = {}

    # 이전 요청사항 적용: Close Series만 추출하여 history_map 구성
    for ticker, df in raw_hist_map.items():
        if df is None or df.empty or "Close" not in df.columns:
            continue

        close = df["Close"].dropna()

        if not close.empty:
            history_map[ticker] = close

    current_year = datetime.now().year
    records = []

    for ticker, info in etf_dict.items():
        close = history_map.get(ticker)

        if close is None or len(close) < 20:
            continue

        current_price = float(close.iloc[-1])

        def calc_return(days: int) -> float:
            if len(close) <= days:
                return 0.0

            old_price = float(close.iloc[-(days + 1)])

            if old_price == 0:
                return 0.0

            return (current_price / old_price - 1) * 100

        try:
            ytd_series = close[close.index.year == current_year]

            if ytd_series.empty:
                ytd_return = 0.0
            else:
                ytd_price = float(ytd_series.iloc[0])

                ytd_return = (
                    (current_price / ytd_price - 1) * 100
                    if ytd_price != 0
                    else 0.0
                )

        except Exception:
            ytd_return = 0.0

        records.append({
            "ticker": ticker,
            "name": info.get("name", ticker),
            "type": info.get("type", info.get("category", "-")),
            "price": current_price,
            "1W": calc_return(5),
            "1M": calc_return(21),
            "3M": calc_return(63),
            "6M": calc_return(126),
            "YTD": ytd_return,
            "1Y": calc_return(252),
        })

    if not records:
        return pd.DataFrame(), history_map

    return pd.DataFrame(records), history_map


# ==============================================================================
# 2. 로테이션 모멘텀 계산 및 AI Context 포맷 변환 (RAG 전용)
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
