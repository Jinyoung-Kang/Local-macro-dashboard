"""
services/liquidity_service.py
연준 순유동성(Fed Net Liquidity) 지표 수집 및 분석 서비스 모듈
(WALCL, WTREGEN, RRPONTSYD 수집, 단위 정규화 및 무중단 Fallback 탑재 + 병렬 수집 최적화)


"""
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
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
    """
    Streamlit Secrets에서 FRED API 키를 안전하게 추출.
    dict, AttrDict, Mapping 등 어떤 타입으로 반환되든 .get()으로 시도하며,
    isinstance(val, dict) 검사에 의존하지 않음.
    """
    try:
        if hasattr(st, "secrets") and st.secrets:
            if "fred" in st.secrets:
                section = st.secrets["fred"]

                key = None
                try:
                    key = section.get("api_key")
                except AttributeError:
                    pass

                if key:
                    return str(key).strip()

                # section 자체가 문자열 하나로 등록된 경우 (예: fred = "xxxx")
                if isinstance(section, str):
                    return section.strip()

            for k in ["FRED_API_KEY", "fred_api_key", "FRED_KEY", "fred_key"]:
                if k in st.secrets:
                    return str(st.secrets[k]).strip()
    except Exception as e:
        logger.warning(f"FRED 키 로드 중 예외: {e}")
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


def _parse_fred_csv(csv_text: str, series_id: str) -> pd.DataFrame:
    """
    FRED 웹 CSV 응답을 안전하게 파싱합니다.
    FRED가 날짜 컬럼명을 'DATE'에서 'observation_date'로 변경한 이력이 있으므로,
    실제 컬럼명을 자동으로 탐색하여 향후 변경에도 유연하게 대응합니다.
    """
    raw_df = pd.read_csv(io.StringIO(csv_text))

    date_col = None
    for candidate in ["observation_date", "DATE", "date"]:
        if candidate in raw_df.columns:
            date_col = candidate
            break

    if date_col is None:
        raise ValueError(
            f"CSV에서 날짜 컬럼을 찾을 수 없습니다. 실제 컬럼: {list(raw_df.columns)}"
        )

    raw_df[date_col] = pd.to_datetime(raw_df[date_col])
    df = raw_df.set_index(date_col)
    df = df.replace(".", pd.NA)

    value_col = [c for c in df.columns if c != date_col][0]
    df = df[[value_col]].rename(columns={value_col: series_id})
    df[series_id] = pd.to_numeric(df[series_id], errors="coerce")

    return df.dropna()


def _build_emergency_fallback_series(series_id: str, period_years: int) -> pd.DataFrame:
    """
    ⚠️ 주의: 이 함수는 실제 FRED 데이터가 아닙니다.
    FRED API와 웹 CSV가 모두 실패했을 때만 호출되는 통계적 추정 시계열이며,
    실제 연준 대차대조표 수치와 다를 수 있습니다.
    """
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

    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_series_raw(series_id: str, period_years: int = 10) -> tuple[pd.DataFrame, bool]:
    """
    개별 FRED 시계열 수집 (API -> Web CSV -> 비상 Fallback)
    반환: (DataFrame, is_estimated)
    """
    fred_key = get_fred_key()
    start_date = (datetime.now() - timedelta(days=period_years * 365 + 90)).strftime("%Y-%m-%d")
    session = get_fred_session()

    # 1. FRED 공식 API 시도
    if fred_key:
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations?"
                f"series_id={series_id}&api_key={fred_key}&file_type=json"
                f"&observation_start={start_date}"
            )
            res = session.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json().get("observations", [])
                if data:
                    df = pd.DataFrame(data)[["date", "value"]]
                    df["date"] = pd.to_datetime(df["date"])
                    df["value"] = pd.to_numeric(df["value"], errors="coerce")
                    df = df.dropna().rename(columns={"value": series_id}).set_index("date")
                    if not df.empty and len(df) >= 2:
                        return df, False
            else:
                logger.warning(
                    f"FRED API 응답 실패 ({series_id}): "
                    f"HTTP {res.status_code} - {res.text[:300]}"
                )
        except Exception as e:
            logger.warning(f"FRED API 실패 ({series_id}): {e}")
    else:
        logger.info(f"{series_id}: FRED_API_KEY 미등록, 웹 CSV 경로로 진행합니다.")

    # 2. Web CSV 직접 다운로드 (403 방어 헤더 탑재, 컬럼명 자동 탐색 적용)
    try:
        csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        res = session.get(csv_url, timeout=10)
        if res.status_code == 200 and len(res.text) > 30:
            df = _parse_fred_csv(res.text, series_id)
            if not df.empty and len(df) >= 2:
                return df, False
        else:
            logger.warning(f"FRED CSV 응답 비정상 ({series_id}): HTTP {res.status_code}")
    except Exception as e:
        logger.warning(f"FRED CSV 다운로드 실패 ({series_id}): {e}")

    # 3. 비상 Fallback 시계열 (네트워크 완전 차단 시에만 사용, 반드시 is_estimated=True로 표시)
    logger.error(
        f"{series_id}: FRED API 및 CSV 모두 실패. "
        f"통계적 추정 Fallback 시계열을 사용하며, 화면에 반드시 경고가 표시되어야 합니다."
    )
    fallback_df = _build_emergency_fallback_series(series_id, period_years)

    if fallback_df.empty:
        return pd.DataFrame(), True

    return fallback_df, True


@st.cache_data(ttl=1800, show_spinner=False)
def get_fed_liquidity_data(period_years: int = 10) -> pd.DataFrame:
    """
    연준 순유동성(Net Liquidity = WALCL - WTREGEN - ON_RRP) 시계열 데이터프레임 생성
    단위: WALCL($M), WTREGEN($M), ON_RRP($B -> $M 변환 후 차감)
    3개 시계열을 ThreadPoolExecutor를 통해 병렬로 수집하여 로딩 속도를 최적화합니다.

    반환 DataFrame에는 'is_estimated' 컬럼이 포함됩니다.
    """
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_walcl = executor.submit(fetch_fred_series_raw, "WALCL", period_years)
        fut_wtre = executor.submit(fetch_fred_series_raw, "WTREGEN", period_years)
        fut_rrp = executor.submit(fetch_fred_series_raw, "RRPONTSYD", period_years)

        df_walcl, est_walcl = fut_walcl.result()
        df_wtre, est_wtre = fut_wtre.result()
        df_rrp, est_rrp = fut_rrp.result()

    if df_walcl is None or df_walcl.empty or df_wtre is None or df_wtre.empty or df_rrp is None or df_rrp.empty:
        return pd.DataFrame()

    any_estimated = bool(est_walcl or est_wtre or est_rrp)

    for df in [df_walcl, df_wtre, df_rrp]:
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()

    combined = pd.DataFrame(
        index=df_walcl.index.union(df_wtre.index).union(df_rrp.index)
    ).sort_index()
    combined['WALCL'] = df_walcl['WALCL']
    combined['WTREGEN'] = df_wtre['WTREGEN']
    combined['RRPONTSYD'] = df_rrp['RRPONTSYD']

    combined = combined.ffill().bfill().dropna()

    if combined.empty:
        return pd.DataFrame()

    rrp_max = combined['RRPONTSYD'].max()
    if rrp_max < 10000:
        combined['RRP_M'] = combined['RRPONTSYD'] * 1000.0
        combined['RRP_B'] = combined['RRPONTSYD']
    else:
        combined['RRP_M'] = combined['RRPONTSYD']
        combined['RRP_B'] = combined['RRPONTSYD'] / 1000.0

    combined['Net_Liquidity_M'] = combined['WALCL'] - combined['WTREGEN'] - combined['RRP_M']

    combined['Net_Liquidity_T'] = combined['Net_Liquidity_M'] / 1e6
    combined['WALCL_T'] = combined['WALCL'] / 1e6
    combined['WTREGEN_B'] = combined['WTREGEN'] / 1e3
    combined['WTREGEN_T'] = combined['WTREGEN'] / 1e6

    combined['Net_Liquidity'] = combined['Net_Liquidity_T']
    combined['Date'] = combined.index

    combined['is_estimated'] = any_estimated

    return combined


# 별칭 지원
fetch_fed_liquidity_data = get_fed_liquidity_data
