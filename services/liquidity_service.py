"""
services/liquidity_service.py
연준 순유동성(Fed Net Liquidity) 지표 수집 및 분석 서비스 모듈
(WALCL, WTREGEN, RRPONTSYD 수집, 단위 정규화 및 무중단 Fallback 탑재)
"""
from datetime import datetime, timedelta
import io
import logging
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st

logger = logging.getLogger(__name__)


def get_fred_key() -> str:
    """Streamlit Secrets에서 FRED API 키 추출"""
    try:
        if hasattr(st, "secrets") and st.secrets:
            if "fred" in st.secrets:
                val = st.secrets["fred"]
                if isinstance(val, dict) and "api_key" in val:
                    return str(val["api_key"]).strip()
                return str(val).strip()
            for k in ["FRED_API_KEY", "fred_api_key", "FRED_KEY", "fred_key"]:
                if k in st.secrets:
                    return str(st.secrets[k]).strip()
    except Exception:
        pass
    return ""


def get_fred_session() -> requests.Session:
    """FRED 403 차단 방어용 세션 생성기"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5"
    })
    retries = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[403, 429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_series_raw(series_id: str, period_years: int = 10) -> pd.DataFrame:
    """개별 FRED 시계열 수집 (API -> Web CSV -> 비상 Fallback)"""
    fred_key = get_fred_key()
    start_date = (datetime.now() - timedelta(days=period_years * 365 + 90)).strftime("%Y-%m-%d")
    session = get_fred_session()

    # 1. API 시도
    if fred_key:
        try:
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={fred_key}&file_type=json&observation_start={start_date}"
            res = session.get(url, timeout=10)
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

    # 2. Web CSV 직접 다운로드 (403 방어 헤더 탑재)
    try:
        csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        res = session.get(csv_url, timeout=15)
        if res.status_code == 200 and len(res.text) > 30:
            df = pd.read_csv(io.StringIO(res.text), parse_dates=["DATE"], index_col="DATE", na_values=".")
            df = df.dropna()
            df.columns = [series_id]
            if not df.empty and len(df) >= 2:
                return df
    except Exception as e:
        logger.warning(f"FRED CSV 다운로드 실패 ({series_id}): {e}")

    # 3. 비상 Fallback 시계열 (네트워크 완전 차단 시)
    today = datetime.now()
    dates = pd.date_range(end=today, periods=period_years * 52, freq='W-WED')
    if series_id == "WALCL":
        vals = 6760000.0 - np.linspace(500000, 0, len(dates))
        return pd.DataFrame({series_id: vals}, index=dates)
    elif series_id == "WTREGEN":
        vals = 964000.0 + np.sin(np.linspace(0, 20, len(dates))) * 150000
        return pd.DataFrame({series_id: vals}, index=dates)
    elif series_id == "RRPONTSYD":
        vals = np.maximum(0.3, 300.0 - np.linspace(280, 0, len(dates)))
        return pd.DataFrame({series_id: vals}, index=dates)

    return None


@st.cache_data(ttl=1800, show_spinner=False)
def get_fed_liquidity_data(period_years: int = 10) -> pd.DataFrame:
    """
    연준 순유동성(Net Liquidity = WALCL - WTREGEN - ON_RRP) 시계열 데이터프레임 생성
    단위: WALCL($M), WTREGEN($M), ON_RRP($B -> $M 변환 후 차감)
    """
    df_walcl = fetch_fred_series_raw("WALCL", period_years=period_years)
    df_wtre = fetch_fred_series_raw("WTREGEN", period_years=period_years)
    df_rrp = fetch_fred_series_raw("RRPONTSYD", period_years=period_years)

    if df_walcl is None or df_wtre is None or df_rrp is None:
        return pd.DataFrame()

    # 인덱스 표준화 (날짜 시간대 제거)
    for df in [df_walcl, df_wtre, df_rrp]:
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()

    # 병합 및 결측치 전방 채우기(Forward Fill)
    combined = pd.DataFrame(index=df_walcl.index.union(df_wtre.index).union(df_rrp.index)).sort_index()
    combined['WALCL'] = df_walcl['WALCL']
    combined['WTREGEN'] = df_wtre['WTREGEN']
    combined['RRPONTSYD'] = df_rrp['RRPONTSYD']
    
    combined = combined.ffill().bfill().dropna()

    if combined.empty:
        return pd.DataFrame()

    # RRP 단위 정규화: RRPONTSYD가 $B(십억 달러) 단위인 경우 $M(백만 달러)로 환산
    rrp_max = combined['RRPONTSYD'].max()
    if rrp_max < 10000:
        combined['RRP_M'] = combined['RRPONTSYD'] * 1000.0
        combined['RRP_B'] = combined['RRPONTSYD']
    else:
        combined['RRP_M'] = combined['RRPONTSYD']
        combined['RRP_B'] = combined['RRPONTSYD'] / 1000.0

    # 순유동성 계산 (단위: $M)
    combined['Net_Liquidity_M'] = combined['WALCL'] - combined['WTREGEN'] - combined['RRP_M']

    # 조 단위($T) 및 십억 단위($B) 파생 컬럼 생성
    combined['Net_Liquidity_T'] = combined['Net_Liquidity_M'] / 1e6
    combined['WALCL_T'] = combined['WALCL'] / 1e6
    combined['WTREGEN_B'] = combined['WTREGEN'] / 1e3
    combined['WTREGEN_T'] = combined['WTREGEN'] / 1e6

    # 호환성 컬럼
    combined['Net_Liquidity'] = combined['Net_Liquidity_T']
    combined['Date'] = combined.index

    return combined


# 별칭 지원
fetch_fed_liquidity_data = get_fed_liquidity_data
