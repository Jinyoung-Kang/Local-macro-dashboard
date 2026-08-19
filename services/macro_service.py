"""
services/macro_service.py
거시경제 지표, 금리, 환율, 원자재 데이터 수집 엔진
ThreadPoolExecutor 기반 I/O 병렬 처리 및 macro_view 계약(Contract) 100% 준수
"""
import io
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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
# 1. UI 헬퍼 및 브리핑 생성기 (views/macro_view.py 필수 의존성)
# ==============================================================================
def clean_tag_ui(tag_str: str) -> str:
    """UI 상에 지표 이름의 마크다운 스타일 태그(:gray[...], [[...]] 등)를 정제"""
    if not isinstance(tag_str, str):
        return str(tag_str)
    clean = re.sub(r':gray\[.*?\]', '', tag_str)
    clean = re.sub(r'\[\[.*?\]\]', '', clean)
    clean = re.sub(r'\[.*?\]', '', clean)
    return clean.strip()


def generate_briefing_text(
    collected_data,
    rate_10y_curr=None,
    rate_10y_prev=None,
    rate_2y_curr=None,
    rate_2y_prev=None,
    vix_hist=None,
    move_hist=None,
    hy_df=None,
    cp_spread_df=None,
    stlfsi_df=None,
    now_str_kst=None,
    *args,
    **kwargs
) -> str:
    """수집된 매크로 데이터를 바탕으로 시장 국면 자동 브리핑 요약문 생성 (11개 인자 완전 수용)"""
    lines = []
    ts = now_str_kst or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"📌 **[{ts} KST] 글로벌 매크로 및 금융시장 동향 브리핑**\n")

    # 1. 금리 및 장단기 스프레드
    if rate_10y_curr is not None and rate_2y_curr is not None:
        spread = rate_10y_curr - rate_2y_curr
        spread_status = "정상화(확대)" if spread > 0 else "역전(경기침체 경고)"
        lines.append(
            f"- **미국 국채 금리**: 10Y `{rate_10y_curr:.2f}%`, 2Y `{rate_2y_curr:.2f}%` "
            f"(10Y-2Y 스프레드: `{spread:+.3f}%p` - **{spread_status}**)"
        )

    # 2. 시장 리스크 및 변동성 지표
    risk_items = []
    if vix_hist is not None and isinstance(vix_hist, pd.DataFrame) and not vix_hist.empty:
        vix_val = vix_hist["Close"].iloc[-1]
        risk_items.append(f"VIX `{vix_val:.2f}`")
    if move_hist is not None and isinstance(move_hist, pd.DataFrame) and not move_hist.empty:
        move_val = move_hist["Close"].iloc[-1]
        risk_items.append(f"MOVE `{move_val:.2f}`")
    if hy_df is not None and isinstance(hy_df, pd.DataFrame) and not hy_df.empty:
        hy_val = hy_df["Close"].iloc[-1]
        risk_items.append(f"HY 스프레드 `{hy_val:.2f}%`")
    if cp_spread_df is not None and isinstance(cp_spread_df, pd.DataFrame) and not cp_spread_df.empty:
        col = "Spread" if "Spread" in cp_spread_df.columns else "Close"
        cp_val = cp_spread_df[col].iloc[-1]
        risk_items.append(f"3M CP 스프레드 `{cp_val:.2f}%p`")
    if stlfsi_df is not None and isinstance(stlfsi_df, pd.DataFrame) and not stlfsi_df.empty:
        stlfsi_val = stlfsi_df["Close"].iloc[-1]
        risk_items.append(f"STLFSI4 `{stlfsi_val:.3f}`")

    if risk_items:
        lines.append(f"- **금융 스트레스 & 변동성**: {', '.join(risk_items)}")

    # 3. 주요 자산 변동 하이라이트 (±1.0% 이상)
    movers = []
    if isinstance(collected_data, dict):
        for cat_name, items in collected_data.items():
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("status") == "ok":
                        pct = abs(it.get("pct", 0))
                        if pct >= 1.0:
                            movers.append(f"{it.get('name', '')} ({it.get('delta_str', '')})")

    if movers:
        lines.append(f"- **주요 시장 변동 자산(±1% 이상)**: {', '.join(movers[:6])}")

    lines.append("\n💡 *본 데이터는 FRED 및 실시간 시장 지표를 기반으로 자동 집계된 브리핑입니다.*")
    return "\n".join(lines)


# ==============================================================================
# 2. yfinance / FRED 데이터 수집 엔진
# ==============================================================================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_ticker_data(symbol: str, period: str = "5d", interval: str = "1d") -> pd.DataFrame:
    """단일 티커 yfinance 데이터 수집 (^MOVE 특수 처리 포함)"""
    try:
        sym = symbol
        if sym == "^MOVE":
            tk = yf.Ticker("^MOVE")
            df = tk.history(period="1mo", interval="1d")
            if df.empty:
                tk = yf.Ticker("MOVE")
                df = tk.history(period="1mo", interval="1d")
        else:
            tk = yf.Ticker(sym)
            df = tk.history(period=period, interval=interval)

        if df.empty or len(df) == 0:
            return pd.DataFrame()
        df = df.reset_index()
        df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
        return df.sort_values("Date").reset_index(drop=True)
    except Exception as e:
        logger.warning(f"티커({symbol}) 조회 실패: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_series(series_id: str, start_date: str = None, api_key: str = None) -> pd.DataFrame:
    """FRED 시계열 수집 (1차: REST API -> 2차: Direct CSV 다운로드)"""
    key = api_key or get_fred_key()

    if not start_date:
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    if key:
        url = f"{FRED_BASE_URL}/series/observations"
        params = {
            "series_id": series_id,
            "api_key": key,
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
                    if not df.empty:
                        return df.sort_values("Date").reset_index(drop=True)
        except Exception:
            pass

    try:
        csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res_csv = requests.get(csv_url, headers=headers, timeout=5)
        if res_csv.status_code == 200:
            df_csv = pd.read_csv(io.StringIO(res_csv.text))
            df_csv.columns = ["Date", "Close"]
            df_csv["Close"] = pd.to_numeric(df_csv["Close"], errors="coerce")
            df_csv = df_csv.dropna()
            df_csv["Date"] = pd.to_datetime(df_csv["Date"])
            if not df_csv.empty:
                return df_csv.sort_values("Date").reset_index(drop=True)
    except Exception as e:
        logger.warning(f"FRED CSV({series_id}) 수집 실패: {e}")

    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_cp_spread(api_key: str = None) -> pd.DataFrame:
    """FRED Commercial Paper Spread (30일물 CP vs 3개월 국채) 산출"""
    key = api_key or get_fred_key()
    df_cp = fetch_fred_series("RIFSPPNA30NB", api_key=key)
    df_tb = fetch_fred_series("DTB3", api_key=key)
    if not df_cp.empty and not df_tb.empty:
        df = pd.merge(df_cp, df_tb, on="Date", suffixes=("_CP", "_TB")).dropna()
        df["Spread"] = df["Close_CP"] - df["Close_TB"]
        return df[["Date", "Spread"]]
    return pd.DataFrame()


# ==============================================================================
# 3. 매크로 데이터 병렬 수집 (views/macro_view.py 원본 계약 완전 일치)
# ==============================================================================
@st.cache_data(ttl=30, show_spinner=False)
def get_collected_macro_data():
    """
    모든 거시경제 티커를 ThreadPoolExecutor로 병렬 수집한 후
    views/macro_view.py가 요구하는 카테고리별 리스트 구조로 반환
    """
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
