"""
services/macro_service.py
거시경제 지표, 금리, 환율, 원자재 데이터 수집 엔진
ThreadPoolExecutor 기반 I/O 병렬 처리, 원본 로직 완벽 보존 및 전 지표 출력 포맷터 탑재
"""
import io
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from config import MACRO_CATEGORIES, FRED_BASE_URL

logger = logging.getLogger(__name__)


# ==============================================================================
# 0. FRED API Key 안전 로더
# ==============================================================================
def get_fred_key() -> str:
    """Streamlit Secrets 및 환경변수에서 FRED API Key 안전 로드"""
    try:
        from config import get_secret
        val = get_secret("fred.api_key", get_secret("FRED_API_KEY", get_secret("fred_api_key", "")))
        if val:
            return str(val).strip()
    except Exception:
        pass

    try:
        if hasattr(st, "secrets") and st.secrets:
            if "fred" in st.secrets and hasattr(st.secrets["fred"], "get"):
                return str(st.secrets["fred"].get("api_key", "")).strip()
            if "fred_api_key" in st.secrets:
                return str(st.secrets["fred_api_key"]).strip()
            if "FRED_API_KEY" in st.secrets:
                return str(st.secrets["FRED_API_KEY"]).strip()
    except Exception:
        pass

    return os.environ.get("FRED_API_KEY", "")


# ==============================================================================
# 1. UI 헬퍼 및 텍스트 레이블 정제기
# ==============================================================================
def clean_tag_ui(tag_str: str) -> str:
    """UI 상에 지표 이름의 마크다운 스타일 태그(:gray[...], [[...]] 등)를 정제"""
    if not isinstance(tag_str, str):
        return str(tag_str)
    clean = re.sub(r':gray\[.*?\]', '', tag_str)
    clean = re.sub(r'\[\[.*?\]\]', '', clean)
    clean = re.sub(r'\[.*?\]', '', clean)
    return clean.strip()


def _clean_macro_label(text: str) -> str:
    if not isinstance(text, str):
        return str(text)
    text = re.sub(r":gray\[\[.*?\]\]", "", text)
    text = re.sub(r":gray\[.*?\]", "", text)
    text = re.sub(r"\[\[.*?\]\]", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# ==============================================================================
# 2. yfinance / FRED 데이터 수집 엔진 (DatetimeIndex 보존)
# ==============================================================================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_ticker_data(symbol: str, period: str = "1mo", interval: str = "1d") -> pd.DataFrame:
    """단일 티커 yfinance 데이터 수집 (DatetimeIndex 보존 및 ^MOVE 특수 처리)"""
    if not symbol:
        return pd.DataFrame()

    if symbol in ["^MOVE", "MOVE", "MOVE:INDEX"]:
        try:
            tk = yf.Ticker("^MOVE")
            df = tk.history(period="1mo", interval="1d")
            if df.empty:
                tk = yf.Ticker("MOVE")
                df = tk.history(period="1mo", interval="1d")
            if not df.empty:
                return df
        except Exception as e:
            logger.warning(f"MOVE 조회 실패: {e}")
        return pd.DataFrame()

    try:
        tk = yf.Ticker(symbol)
        df = tk.history(period=period, interval=interval)
        if df is not None and not df.empty:
            df = df.dropna(subset=['Close'])
            df = df[df['Close'] > 0]
            if len(df) >= 1:
                return df
    except Exception as e:
        logger.warning(f"yfinance 수집 실패 ({symbol}): {e}")
    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_series(series_id: str, period_years: int = 10, api_key: str = None) -> pd.DataFrame:
    """FRED 시계열 수집 (DatetimeIndex 인덱스 및 series_id 컬럼명 매핑)"""
    key = api_key or get_fred_key()
    start_date = (datetime.now() - timedelta(days=period_years * 365 + 60)).strftime("%Y-%m-%d")

    # 1차: 공식 FRED REST API
    if key:
        try:
            url = f"{FRED_BASE_URL}/series/observations"
            params = {
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "observation_start": start_date,
                "sort_order": "asc"
            }
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                data = res.json().get("observations", [])
                if data:
                    df = pd.DataFrame(data)[["date", "value"]]
                    df["date"] = pd.to_datetime(df["date"])
                    df["value"] = pd.to_numeric(df["value"], errors="coerce")
                    df = df.dropna().rename(columns={"value": series_id}).set_index("date")
                    if not df.empty and len(df) >= 2:
                        return df
        except Exception as e:
            logger.warning(f"FRED API 실패 ({series_id}): {e}")

    # 2차: FRED Direct CSV 다운로드 폴백
    try:
        csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res_csv = requests.get(csv_url, headers=headers, timeout=15)
        if res_csv.status_code == 200 and len(res_csv.text) > 30:
            df_csv = pd.read_csv(io.StringIO(res_csv.text), parse_dates=["DATE"], index_col="DATE", na_values=".")
            df_csv = df_csv.dropna()
            df_csv.columns = [series_id]
            if not df_csv.empty and len(df_csv) >= 2:
                return df_csv
    except Exception as e:
        logger.warning(f"FRED CSV 다운로드 실패 ({series_id}): {e}")

    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_cp_spread(api_key: str = None) -> pd.DataFrame:
    """FRED Commercial Paper Spread (30일물 CP vs 3개월 국채, 컬럼명: CP_SPREAD)"""
    key = api_key or get_fred_key()
    df_cp = fetch_fred_series("CPF3M", api_key=key)
    df_tb = fetch_fred_series("DGS3MO", api_key=key)
    if df_tb.empty:
        df_tb = fetch_fred_series("DFF", api_key=key)

    if not df_cp.empty and not df_tb.empty:
        combined = pd.DataFrame({"CP": df_cp.iloc[:, 0], "TB": df_tb.iloc[:, 0]}).ffill().dropna()
        combined["CP_SPREAD"] = (combined["CP"] - combined["TB"]).round(2)
        if not combined.empty and len(combined) >= 2:
            return combined[["CP_SPREAD"]]
    return pd.DataFrame()


# ==============================================================================
# 3. 매크로 데이터 병렬 수집
# ==============================================================================
@st.cache_data(ttl=30, show_spinner=False)
def get_collected_macro_data():
    """모든 거시경제 티커를 ThreadPoolExecutor로 병렬 수집한 후 카테고리별 리스트 구조로 반환"""
    collected = {}
    rate_10y_curr, rate_10y_prev = None, None
    rate_2y_curr, rate_2y_prev = None, None

    tasks = [
        (cat_name, name, ticker)
        for cat_name, items in MACRO_CATEGORIES.items()
        for name, ticker in items.items()
    ]

    def _fetch_one(cat_name, name, ticker):
        return cat_name, name, ticker, fetch_ticker_data(ticker, period="5d")

    raw = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_fetch_one, c, n, t) for c, n, t in tasks]
        for future in as_completed(futures):
            try:
                cat_name, name, ticker, df = future.result()
                raw.setdefault(cat_name, {})[name] = (ticker, df)
            except Exception:
                pass

    for cat_name, items in MACRO_CATEGORIES.items():
        collected[cat_name] = []
        for name, ticker in items.items():
            _, df = raw.get(cat_name, {}).get(name, (ticker, None))
            if df is not None and isinstance(df, pd.DataFrame) and len(df) >= 2:
                curr = float(df['Close'].iloc[-1])
                prev = float(df['Close'].iloc[-2])
                delta = curr - prev
                pct = (delta / prev) * 100 if prev != 0 else 0.0
                if "JPY/KRW" in name and curr < 50:
                    curr, prev, delta = curr * 100, prev * 100, delta * 100
                collected[cat_name].append({
                    "name": name,
                    "price": curr,
                    "delta": delta,
                    "pct": pct,
                    "price_str": f"{curr:,.2f}",
                    "delta_str": f"{delta:+,.2f} ({pct:+.2f}%)",
                    "prev_str": f"{prev:,.2f}",
                    "status": "ok"
                })
                if ticker == "^TNX":
                    rate_10y_curr, rate_10y_prev = curr, prev
                elif ticker in ["2YY=F", "^IRX", "ZT=F"]:
                    rate_2y_curr, rate_2y_prev = curr, prev
            elif df is not None and isinstance(df, pd.DataFrame) and len(df) == 1:
                curr = float(df['Close'].iloc[-1])
                collected[cat_name].append({
                    "name": name,
                    "price": curr,
                    "delta": 0.0,
                    "pct": 0.0,
                    "price_str": f"{curr:,.2f}",
                    "delta_str": "0.00 (0.00%)",
                    "prev_str": f"{curr:,.2f}",
                    "status": "single"
                })
                if ticker == "^TNX":
                    rate_10y_curr, rate_10y_prev = curr, curr
                elif ticker in ["2YY=F", "^IRX", "ZT=F"]:
                    rate_2y_curr, rate_2y_prev = curr, curr
            else:
                collected[cat_name].append({"name": name, "status": "fail"})

    return collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev


# ==============================================================================
# 4. 리스크 지표 요약 헬퍼 및 전체 매크로 원본 텍스트 생성기
# ==============================================================================
def summarize_series_for_ai(df: pd.DataFrame, value_col: str = None, label: str = "") -> str:
    """시계열을 AI Context용 최신값·직전 변화·백분위 문장으로 요약."""
    if df is None or df.empty:
        return f"- {label}: 데이터 수집 실패"

    try:
        if value_col and value_col in df.columns:
            series = df[value_col].dropna()
        else:
            series = df.iloc[:, 0].dropna()

        if len(series) < 2:
            return f"- {label}: 데이터 부족"

        current = float(series.iloc[-1])
        previous = float(series.iloc[-2])
        change = current - previous
        percentile = float(series.rank(pct=True).iloc[-1] * 100)

        return (
            f"- {label}: {current:,.2f} "
            f"(직전 대비 {change:+,.2f}, "
            f"최근 표본 내 백분위 {percentile:.1f}%)"
        )
    except Exception as e:
        return f"- {label}: 요약 실패 ({str(e)})"


@st.cache_data(ttl=1800, show_spinner=False)
def get_macro_risk_indicators_for_ai() -> dict:
    """AI Context용 금융 리스크·변동성 지표 수집."""
    return {
        "VIX": fetch_ticker_data("^VIX", period="3mo"),
        "MOVE": fetch_ticker_data("^MOVE", period="3mo"),
        "HY_OAS": fetch_fred_series("BAMLH0A0HYM2", period_years=3),
        "CP_SPREAD": fetch_fred_cp_spread(),
        "STLFSI4": fetch_fred_series("STLFSI4", period_years=3),
    }


def _append_macro_risk_section(lines: list[str], risk_data: dict | None) -> None:
    lines.append("## 금융 리스크·은행권·시장 변동성")

    if not isinstance(risk_data, dict):
        lines.append("- 금융 리스크 데이터 수집 실패")
        lines.append("")
        return

    indicators = [
        ("VIX", "Close", "CBOE VIX (주식 변동성)"),
        ("MOVE", "Close", "ICE BofA MOVE (채권 변동성)"),
        ("HY_OAS", "BAMLH0A0HYM2", "미국 하이일드 스프레드 (HY OAS)"),
        ("CP_SPREAD", "CP_SPREAD", "3M 금융 CP 스프레드"),
        ("STLFSI4", "STLFSI4", "세인트루이스 연준 금융스트레스"),
    ]

    for key, value_col, label in indicators:
        series_or_df = risk_data.get(key)
        if series_or_df is None:
            lines.append(f"- {label}: 데이터 수집 실패")
            continue
        lines.append(
            summarize_series_for_ai(
                series_or_df,
                value_col,
                label,
            )
        )
    lines.append("")


def generate_full_macro_text(
    collected_data: dict,
    rate_10y_curr=None,
    rate_10y_prev=None,
    rate_2y_curr=None,
    rate_2y_prev=None,
    risk_data: dict | None = None,
) -> str:
    """
    거시경제 매크로 메뉴에 표시된 모든 지표의 최신 원본값을
    카테고리 단위로 복사용 텍스트로 변환합니다.
    """
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST")

    lines = [
        "📋 [거시경제 매크로 지표 원본 브리핑]",
        f"수집 시각: {now_kst}",
        "표기: 최신값 | 전일/직전 대비 | 직전값",
        "=" * 72,
        "",
    ]

    if not isinstance(collected_data, dict):
        lines.append("- 매크로 데이터 수집에 실패했습니다.")
        return "\n".join(lines)

    for category_name, items in collected_data.items():
        lines.append(f"## {_clean_macro_label(category_name)}")

        if not isinstance(items, list) or not items:
            lines.append("- 수집된 지표가 없습니다.")
            lines.append("")
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            name = _clean_macro_label(item.get("name", "이름 없음"))
            status = item.get("status")

            if status in ("ok", "single"):
                lines.append(
                    f"- {name}: "
                    f"{item.get('price_str', 'N/A')} | "
                    f"{item.get('delta_str', 'N/A')} | "
                    f"직전: {item.get('prev_str', 'N/A')}"
                )
            else:
                lines.append(f"- {name}: 데이터 수집 실패")
        lines.append("")

    lines.append("## 장단기 금리차")

    if rate_10y_curr is not None and rate_2y_curr is not None:
        spread_curr = rate_10y_curr - rate_2y_curr

        lines.extend([
            f"- 미국채 10년물: {rate_10y_curr:.2f}%",
            f"- 미국채 2년물: {rate_2y_curr:.2f}%",
            f"- 10Y-2Y 스프레드: {spread_curr:+.3f}%p",
        ])

        if rate_10y_prev is not None and rate_2y_prev is not None:
            spread_prev = rate_10y_prev - rate_2y_prev
            lines.append(
                f"- 스프레드 직전 대비: "
                f"{spread_curr - spread_prev:+.3f}%p"
            )
    else:
        lines.append("- 장단기 금리차 데이터 수집 실패")

    lines.append("")

    _append_macro_risk_section(lines, risk_data)

    return "\n".join(lines)
