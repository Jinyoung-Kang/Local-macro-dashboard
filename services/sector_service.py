"""
services/sector_service.py
섹터 및 자산군 시계열/로테이션 수집 엔진
섹터/자산군 순위 평가 독립 분리 및 기존 calculate_returns_matrix 복원 적용 
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
    """
    여러 ETF의 일봉 시계열을 한 번에 수집해 {티커: DataFrame} 으로 반환합니다.

    [성능] 기존 구현은 티커마다 yf.Ticker(t).history()를 따로 호출해
    20개 티커면 HTTP 왕복이 20번 발생했습니다(스레드로 감췄을 뿐,
    Yahoo 레이트리밋에도 그만큼 더 노출됩니다). yf.download()는 여러
    심볼을 한 요청으로 묶어 받으므로 왕복이 사실상 1회로 줄어듭니다.

    배치 요청이 실패하면 기존처럼 티커별 개별 수집으로 폴백해
    "일부 티커만 실패" 상황에서도 화면이 비지 않게 합니다.
    """
    symbols = [t for t in dict.fromkeys(tickers) if t]
    if not symbols:
        return {}

    results: dict[str, pd.DataFrame] = {}

    try:
        raw = yf.download(
            tickers=" ".join(symbols),
            period=period,
            interval="1d",
            auto_adjust=True,
            actions=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as e:
        logger.warning(f"ETF 배치 수집 실패, 개별 수집으로 폴백합니다: {e}")
        raw = None

    if raw is not None and not raw.empty:
        for ticker in symbols:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    df = raw[ticker].dropna(how="all")
                else:
                    # 심볼이 1개면 yfinance가 단일 레벨 컬럼을 반환합니다.
                    df = raw.dropna(how="all")

                if not df.empty and "Close" in df.columns:
                    results[ticker] = df
            except Exception as e:
                logger.warning(f"ETF 배치 결과 분해 실패 ({ticker}): {e}")

    # 배치에서 빠진 티커만 개별로 재시도합니다.
    missing = [t for t in symbols if t not in results]
    if missing:
        logger.info(f"ETF 개별 재시도: {missing}")
        with ThreadPoolExecutor(max_workers=min(len(missing), 8)) as executor:
            future_to_ticker = {
                executor.submit(_fetch_single_history, t, period): t
                for t in missing
            }
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    results[ticker] = future.result()
                except Exception as e:
                    logger.warning(f"ETF 수집 실패 ({ticker}): {e}")
                    results[ticker] = pd.DataFrame()

    return results


def _fetch_single_history(ticker: str, period: str) -> pd.DataFrame:
    """배치 수집에서 누락된 개별 티커 폴백 수집."""
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    return df if df is not None else pd.DataFrame()


# ==============================================================================
# 1. 기존 sector_view.py 호환용 calculate_returns_matrix
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
            """
            days 거래일 전 대비 수익률(%).

            [버그 수정] 기존에는 표본이 부족하거나 과거 가격이 0이면 0.0을
            반환했습니다. 화면에서는 "0.00%"가 '데이터 없음'이 아니라
            '보합'으로 읽히므로, 신규 상장 ETF의 1Y 수익률이 실제로 보합인
            것처럼 표시되고 순위 계산에도 섞여 들어갔습니다.
            데이터가 없으면 NaN을 반환해 구분합니다.
            """
            if len(close) <= days:
                return float("nan")

            old_price = float(close.iloc[-(days + 1)])

            if old_price == 0:
                return float("nan")

            return (current_price / old_price - 1) * 100

        try:
            ytd_series = close[close.index.year == current_year]

            if ytd_series.empty:
                ytd_return = float("nan")
            else:
                ytd_price = float(ytd_series.iloc[0])

                ytd_return = (
                    (current_price / ytd_price - 1) * 100
                    if ytd_price != 0
                    else float("nan")
                )

        except Exception as e:
            logger.warning(f"YTD 수익률 계산 실패 ({ticker}): {e}")
            ytd_return = float("nan")

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
    각 그룹(섹터/자산군)별로 순위를 독립적으로 재평가합니다.
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
    
    if df_result.empty:
        return {
            "sector": pd.DataFrame(),
            "asset_class": pd.DataFrame(),
        }

    # 섹터와 자산군을 분리하여 각각 그룹 내에서 독립적 랭크 계산 적용
    sector_df = df_result[
        df_result["자산"].isin(ROTATION_SECTORS.keys())
    ].copy()
    
    asset_class_df = df_result[
        df_result["자산"].isin(ROTATION_ASSET_CLASSES.keys())
    ].copy()

    for target_df in [sector_df, asset_class_df]:
        if target_df.empty:
            continue
        for period_name in windows:
            target_df[f"{period_name}_순위"] = (
                target_df[period_name]
                .rank(ascending=False, method="min")
                .astype(int)
            )

    return {
        "sector": sector_df,
        "asset_class": asset_class_df,
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
