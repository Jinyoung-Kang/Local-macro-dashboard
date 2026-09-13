"""
services/macro_service.py
거시경제 지표, 금리, 환율, 원자재 데이터 수집 엔진
ThreadPoolExecutor 기반 I/O 병렬 처리, 원본 로직 완벽 보존 및 전 지표 출력 포맷터 탑재
"""
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

# FRED 키 로더는 config.get_fred_key() 하나만 사용합니다.
# (이 모듈과 liquidity_service에 동일 로직이 중복 정의돼 있었습니다.)
from config import MACRO_CATEGORIES, get_fred_key
from services.http_client import get_fred_session
from services import datasets, store

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. UI 헬퍼 및 텍스트 레이블 정제기
# ==============================================================================
def clean_tag_ui(tag_str: str) -> str:
    r"""
    지표 이름에서 UI 표시용 마크다운 태그를 제거합니다.

    [버그 수정] 기존에는 `:gray\[.*?\]`를 **먼저** 적용했습니다. config의
    실제 이름은 `달러 인덱스 (DXY) :gray[[실시간]]`처럼 대괄호가 중첩돼
    있는데, non-greedy `.*?\]`가 **첫 번째** `]`에서 멈춰
    `:gray[[실시간]` 까지만 지우고 닫는 `]` 하나를 남겼습니다.
    그 결과 차트 선택 목록에 "달러 인덱스 (DXY) ]" 처럼 표시됐습니다.

    중첩 패턴(`:gray[[...]]`)을 먼저 지운 뒤 단일 패턴을 처리해야 합니다.
    """
    if not isinstance(tag_str, str):
        return str(tag_str)

    clean = re.sub(r':gray\[\[.*?\]\]', '', tag_str)   # :gray[[...]]  (중첩)
    clean = re.sub(r':gray\[.*?\]', '', clean)         # :gray[...]
    clean = re.sub(r'\[\[.*?\]\]', '', clean)          # [[...]]
    clean = re.sub(r'\[.*?\]', '', clean)              # [...]
    return re.sub(r'\s{2,}', ' ', clean).strip()


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
def collect_ticker_data(symbol: str, period: str = "1mo") -> pd.DataFrame:
    """
    yfinance를 통해 티커 시계열 데이터를 수집합니다.

    [수정] 분봉(1m/5m) 수집 성공 여부를 df.attrs["is_intraday"]에 기록합니다.
    ^TNX/^TYX/2YY=F 같은 심볼은 분봉이 없어 일봉으로 폴백되는데, 일봉의
    타임스탬프는 신뢰할 수 있는 "시:분:초" 정보가 아니므로 이 플래그로
    구분해서 화면에서 다르게 표시해야 합니다.
    """
    if not symbol:
        return None

    if symbol in ["^MOVE", "MOVE", "MOVE:INDEX"]:
        # ----------------------------------------------------------------------
        # ⚠️ 중요: Yahoo Finance는 ICE BofA MOVE 지수를 제공하지 않습니다.
        # 아래 값은 실제 MOVE 지수가 아니라, 10년물 금리(^TNX)의 변동성에서
        # 역산한 **대용(proxy) 추정치**입니다. 실제 MOVE와 수치가 다릅니다.
        #
        # 따라서 df.attrs에 is_proxy/source_label을 반드시 심어서, 화면과
        # AI 리포트가 이 값을 "실제 공식 지표"로 오인하지 않게 합니다.
        # (MOVE 140 이상 = 채권 발작 같은 임계치 해석을 이 추정치에 그대로
        #  적용하면 잘못된 투자 판단으로 이어질 수 있습니다.)
        #
        # 실제 MOVE 지수가 필요하면 ICE/Bloomberg 등 유료 피드를 연결하고
        # 이 분기를 제거하세요.
        # ----------------------------------------------------------------------
        try:
            tnx_tk = yf.Ticker("^TNX")
            tnx_df = tnx_tk.history(period=period if period not in ["1d", "5d"] else "1mo")
            if tnx_df is not None and not tnx_df.empty:
                tnx_df = tnx_df.dropna(subset=['Close'])
                if len(tnx_df) >= 2:
                    rolling_bp_vol = tnx_df['Close'].diff().rolling(window=5, min_periods=1).std().fillna(0.05)
                    move_close = (88.0 + (rolling_bp_vol * 190.0) + (tnx_df['Close'] * 2.6)).round(2)

                    proxy_df = tnx_df.copy()
                    proxy_df['Close'] = move_close
                    proxy_df['Open'] = proxy_df['Close']
                    proxy_df['High'] = (proxy_df['Close'] * 1.01).round(2)
                    proxy_df['Low'] = (proxy_df['Close'] * 0.99).round(2)
                    proxy_df.attrs["is_intraday"] = False
                    proxy_df.attrs["is_proxy"] = True
                    proxy_df.attrs["source_label"] = (
                        "^TNX 변동성 기반 추정치 (실제 ICE BofA MOVE 아님)"
                    )
                    return proxy_df
        except Exception as e:
            logger.warning(f"MOVE 프록시 연산 지연: {e}")

        # 네트워크까지 실패한 경우의 자리표시용 합성 시계열입니다.
        # 값 자체에 정보가 전혀 없으므로(단순 사인파) is_synthetic으로
        # 표시해 화면에서 수치를 신뢰하지 않도록 합니다.
        today = datetime.now()
        dates = pd.date_range(end=today, periods=60, freq='B')
        vals = 98.5 + np.sin(np.linspace(0, 10, len(dates))) * 6.5
        fallback_df = pd.DataFrame({
            'Open': vals.round(2),
            'High': (vals * 1.01).round(2),
            'Low': (vals * 0.99).round(2),
            'Close': vals.round(2),
            'Volume': 0
        }, index=dates)
        fallback_df.attrs["is_intraday"] = False
        fallback_df.attrs["is_proxy"] = True
        fallback_df.attrs["is_synthetic"] = True
        fallback_df.attrs["source_label"] = (
            "수집 실패 시 자리표시용 합성 시계열 (실제 시장 데이터 아님)"
        )
        return fallback_df

    try:
        tk = yf.Ticker(symbol)
        intraday_periods = {"1d", "5d"}
        df = None
        is_intraday = False

        if period in intraday_periods:
            df = tk.history(period=period, interval="1m")
            if df is not None and not df.empty:
                is_intraday = True
            else:
                df = tk.history(period=period, interval="5m")
                if df is not None and not df.empty:
                    is_intraday = True
                else:
                    df = tk.history(period=period)
                    is_intraday = False
        else:
            df = tk.history(period=period)
            is_intraday = False

        if df is not None and not df.empty:
            df = df.dropna(subset=['Close'])
            df = df[df['Close'] > 0]
            if len(df) >= 1:
                df = df.copy()
                df.attrs["is_intraday"] = is_intraday
                return df
    except Exception as e:
        logger.warning(f"yfinance 수집 실패 ({symbol}): {e}")

    return None


def collect_fred_series(series_id: str, period_years: int = 10, api_key: str = None) -> pd.DataFrame:
    """
    FRED 시계열을 실제로 수집합니다 (DatetimeIndex 인덱스, series_id 컬럼명).

    화면은 fetch_fred_series()를 쓰세요. 이 함수는 항상 네트워크를 씁니다.
    """
    key = api_key or get_fred_key()
    start_date = (datetime.now() - timedelta(days=period_years * 365 + 60)).strftime("%Y-%m-%d")

    if key:
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations?"
                f"series_id={series_id}&api_key={key}&file_type=json"
                f"&observation_start={start_date}"
            )
            res = get_fred_session().get(url, timeout=10)
            if res.status_code == 200:
                data = res.json().get("observations", [])
                if data:
                    df = pd.DataFrame(data)[["date", "value"]]
                    df["date"] = pd.to_datetime(df["date"])
                    df["value"] = pd.to_numeric(df["value"], errors="coerce")
                    df = df.dropna().rename(columns={"value": series_id}).set_index("date")
                    if not df.empty and len(df) >= 2:
                        return df
            else:
                logger.warning(
                    f"FRED API 응답 실패 ({series_id}): "
                    f"HTTP {res.status_code} - {res.text[:300]}"
                )
        except Exception as e:
            logger.warning(f"FRED API 실패 ({series_id}): {e}")

    try:
        csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        res = get_fred_session().get(csv_url, timeout=15)
        if res.status_code == 200 and len(res.text) > 30:
            raw_df = pd.read_csv(io.StringIO(res.text))

            date_col = None
            for candidate in ["observation_date", "DATE", "date"]:
                if candidate in raw_df.columns:
                    date_col = candidate
                    break

            if date_col is not None:
                raw_df[date_col] = pd.to_datetime(raw_df[date_col])
                df = raw_df.set_index(date_col)
                df = df.replace(".", pd.NA)
                value_col = [c for c in df.columns if c != date_col][0]
                df = df[[value_col]].rename(columns={value_col: series_id})
                df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
                df = df.dropna()
                if not df.empty and len(df) >= 2:
                    return df
        else:
            logger.warning(f"FRED CSV 응답 비정상 ({series_id}): HTTP {res.status_code}")
    except Exception as e:
        logger.warning(f"FRED CSV 다운로드 실패 ({series_id}): {e}")

    logger.error(f"{series_id}: FRED API 및 CSV 모두 실패. 가짜 데이터를 생성하지 않고 빈 데이터를 반환합니다.")
    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_series(series_id: str, period_years: int = 10, api_key: str = None) -> pd.DataFrame:
    """
    화면용 진입점. 저장본 우선 + 누적 이력 병합.

    FRED는 과거 조회가 되는 소스지만 저장해 두는 이유가 둘 있습니다.
      1) API 키가 없거나 FRED가 장애일 때도 화면이 비지 않습니다.
      2) timeseries 테이블에 누적해 두면, 같은 시리즈를 다른 기간으로
         요청할 때 이미 받아 둔 구간을 재사용할 수 있습니다.
    """
    snap_name = datasets.snap_fred_series(series_id)

    def _collect():
        df = collect_fred_series(series_id, period_years=period_years, api_key=api_key)
        # 수집 성공 시에만 누적 테이블에 반영합니다(빈 결과로 덮어쓰지 않음).
        if df is not None and not df.empty:
            try:
                store.put_timeseries(datasets.TS_FRED, series_id, df, value_col=series_id)
            except Exception as e:
                logger.warning("FRED 누적 저장 실패 (%s): %s", series_id, e)
        return df

    df = store.cached_or_live(
        snap_name,
        _collect,
        max_age_seconds=datasets.MAX_AGE_DAILY,
        as_frame=True,
    )

    if df is not None and not df.empty:
        return df

    # 스냅샷이 비었더라도 과거에 누적해 둔 이력이 있으면 그것으로 복구합니다.
    accumulated = store.read_timeseries(
        datasets.TS_FRED, series_id, value_name=series_id,
    )
    if not accumulated.empty:
        logger.info("%s: 누적 이력 %d행으로 복구했습니다.", series_id, len(accumulated))
        return accumulated

    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_cp_spread(api_key: str = None) -> pd.DataFrame:
    """3M 금융 CP 스프레드 (CPF3M - 3M Treasury) 계산"""
    key = api_key or get_fred_key()
    df_cp = fetch_fred_series("CPF3M", api_key=key)
    df_tb = fetch_fred_series("DGS3MO", api_key=key)
    if df_tb is None or df_tb.empty:
        df_tb = fetch_fred_series("DFF", api_key=key)

    if df_cp is not None and df_tb is not None and not df_cp.empty and not df_tb.empty:
        combined = pd.DataFrame({'CP': df_cp['CPF3M'], 'TB': df_tb.iloc[:, 0]}).ffill().dropna()
        combined['CP_SPREAD'] = (combined['CP'] - combined['TB']).round(2)
        if not combined.empty and len(combined) >= 2:
            return combined[['CP_SPREAD']]

    logger.error("CP Spread 데이터 합산 실패. 빈 데이터를 반환합니다.")
    return pd.DataFrame()


# ==============================================================================
# 2-0. 저장본 우선 읽기 경로 (변동성 지수만 해당)
# ==============================================================================
# ^VIX / ^MOVE는 화면 여러 곳에서 서로 다른 기간으로 요청됩니다. 기간마다
# 스냅샷을 만들면 저장본이 난립하므로, 가장 긴 기간(5y)으로 한 번만 저장하고
# 짧은 기간 요청은 꼬리를 잘라 씁니다 (13F에서 q1을 q8에서 유도하는 것과
# 같은 방식). 덕분에 store_only 모드에서 이 두 심볼은 네트워크를 타지 않습니다.
_STORE_BACKED_TICKERS = {"^VIX", "^MOVE", "MOVE", "MOVE:INDEX"}

_PERIOD_DAYS = {
    "1d": 1, "5d": 5, "1mo": 31, "3mo": 92, "6mo": 183,
    "1y": 366, "2y": 731, "5y": 1827,
}


def _slice_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """저장된 긴 시계열에서 요청 기간만큼 최근 구간을 잘라 냅니다."""
    days = _PERIOD_DAYS.get(period)
    if not days or df is None or df.empty:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        return df

    cutoff = df.index.max() - pd.Timedelta(days=days)
    sliced = df[df.index >= cutoff]
    out = sliced if len(sliced) >= 2 else df
    # attrs(is_proxy 등)는 슬라이싱에서 보존되지 않으므로 직접 옮깁니다.
    out.attrs.update(df.attrs)
    return out


@st.cache_data(ttl=60, show_spinner=False)
def fetch_ticker_data(symbol: str, period: str = "1mo") -> pd.DataFrame:
    """
    화면용 진입점. 변동성 지수는 저장본을 우선 사용하고, 나머지 심볼은
    기존처럼 직접 수집합니다(티커가 많아 전부 저장할 이유가 없습니다).
    """
    if symbol not in _STORE_BACKED_TICKERS:
        return collect_ticker_data(symbol, period)

    store_period = datasets.VOLATILITY_STORE_PERIOD
    snap_name = datasets.snap_ticker_history(symbol, store_period)

    def _collect():
        return collect_ticker_data(symbol, store_period)

    full = store.cached_or_live(
        snap_name,
        _collect,
        max_age_seconds=datasets.MAX_AGE_DAILY,
        as_frame=True,
    )

    if full is None or (isinstance(full, pd.DataFrame) and full.empty):
        # 저장본도 없고 수집도 실패 → 요청 기간 그대로 한 번 더 시도
        return collect_ticker_data(symbol, period)

    return _slice_period(full, period)


# ==============================================================================
# 2-1. TradingView Scanner 기반 미국채 수익률 보정
# ==============================================================================
# config.py의 미국채 2년물 티커는 "ZT=F"(CBOT 2년 국채 선물 가격, ~100pt대)로
# 지정되어 있어 "수익률(%)" 라벨과 단위가 맞지 않습니다. ^TNX/^TYX는 Yahoo가
# 실제 수익률(%)을 제공하므로 정상이지만, 2년물만 선물 가격을 수익률처럼
# 잘못 표시하는 문제가 있습니다.
#
# 화면 카드(views/macro_view.py)는 자체적으로 market_scraper_service의
# TradingView Scanner 결과로 이 값을 덮어써서 정상으로 보이지만, 이 함수가
# 반환하는 collected_data / rate_2y_curr / rate_2y_prev 자체는 보정되지
# 않은 원시값이었습니다. 그 결과 "AI 분석 없이 수집한 전체 대시보드 최신
# 원본 데이터" 텍스트와 10Y-2Y 스프레드 계산에는 잘못된 선물가격이 그대로
# 사용되는 불일치가 발생했습니다.
#
# 아래 보정은 데이터 "수집" 시점에 한 번만 적용하여, 이후 이 데이터를
# 참조하는 모든 화면·텍스트·계산이 항상 같은 값을 보도록 통일합니다.
BOND_SCRAPER_KEY_MAP = {
    "미국채 2년물 수익률(%) :gray[[TradingView 참고]]": "us02y",
    "미국채 10년물 수익률(%) :gray[[TradingView 참고]]": "us10y",
    "미국채 30년물 수익률(%) :gray[[TradingView 참고]]": "us30y",
}


def _apply_bond_scanner_override(
    collected: dict,
    rate_10y_curr,
    rate_10y_prev,
    rate_2y_curr,
    rate_2y_prev,
):
    """
    TradingView Scanner(us02y/us10y/us30y)의 실제 수익률로 collected_data와
    10Y/2Y 스프레드 계산용 변수를 함께 덮어씁니다.

    Scanner 조회가 실패하면 원본 값을 그대로 유지하여 데이터 공백을
    만들지 않습니다.
    """
    try:
        from services.market_scraper_service import get_scraped_macro_markets
    except Exception as e:
        logger.warning(f"market_scraper_service 임포트 실패로 국채 보정을 건너뜁니다: {e}")
        return collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev

    try:
        # [성능] 수집기(live_only 모드)에서는 scraper_markets 태스크가 방금
        # 같은 데이터를 받아 저장해 뒀습니다. 여기서 래퍼를 그대로 부르면
        # 같은 실행 안에서 외부 스크래핑이 두 번 일어납니다(요청 20여 건 낭비).
        # 갓 저장된 스냅샷이 있으면 그것을 씁니다.
        scraper_result = None
        snap = store.read_snapshot(datasets.SNAP_SCRAPER_MARKETS)
        if snap is not None and snap.is_fresh(300) and snap.payload:
            scraper_result = snap.payload
            logger.debug("국채 보정: 방금 저장된 스크래핑 스냅샷을 재사용합니다.")

        if not scraper_result:
            scraper_result = get_scraped_macro_markets()

        scraper_items = {
            item.get("key"): item
            for item in scraper_result.get("items", [])
            if isinstance(item, dict)
        }
    except Exception as e:
        logger.warning(f"TradingView Scanner 국채 보정 조회 실패: {e}")
        return collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev

    if not scraper_items:
        return collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev

    for items in collected.values():
        for item in items:
            if not isinstance(item, dict):
                continue

            scraper_key = BOND_SCRAPER_KEY_MAP.get(item.get("name"))
            if not scraper_key:
                continue

            scraped = scraper_items.get(scraper_key)
            if not scraped or scraped.get("status") != "ok":
                continue

            price = scraped.get("price")
            if price is None:
                continue

            price = float(price)
            previous_close = scraped.get("previous_close")

            item["price"] = price
            item["price_str"] = f"{price:,.3f}"
            item["status"] = "ok"
            item["source"] = scraped.get("provider", "TradingView Scanner")
            # Scanner 응답에는 체결 시각이 없으므로 "수집 시각"임을 밝혀 둡니다.
            item["last_ts"] = (
                datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S KST")
                + " (TradingView 수집 시각)"
            )

            prev_source = "TradingView"
            if previous_close is None or float(previous_close) == 0:
                # 스크래핑이 전일값을 못 주면 FRED 공식 확정치로 보완합니다.
                fred_prev = get_bond_previous_close_from_fred(scraper_key)
                if fred_prev is not None:
                    previous_close = fred_prev
                    prev_source = "FRED 공식 확정치"

            if previous_close is not None and float(previous_close) != 0:
                previous_close = float(previous_close)
                delta = price - previous_close
                pct = (delta / previous_close) * 100.0
                item["delta"] = delta
                item["pct"] = pct
                item["prev_str"] = f"{previous_close:,.3f}"
                item["delta_str"] = f"{delta:+,.3f} ({pct:+.2f}%)"
                item["prev_source"] = prev_source
            else:
                # 어느 출처도 전일값을 주지 못하면 "변화 없음(0.00%)"으로
                # 위장하지 않고 명시적으로 N/A 처리합니다.
                item["delta"] = None
                item["pct"] = None
                item["prev_str"] = "N/A"
                item["delta_str"] = "N/A"

            if scraper_key == "us10y":
                rate_10y_curr = price
                rate_10y_prev = previous_close if previous_close else rate_10y_prev
            elif scraper_key == "us02y":
                rate_2y_curr = price
                rate_2y_prev = previous_close if previous_close else rate_2y_prev

    return collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev


# ==============================================================================
# 2-2. 미국채 전일 종가 폴백 (FRED 공식 일별)
# ==============================================================================
# TradingView bonds scanner는 현재 수익률만 주고, Symbol Scanner·HTML 파서도
# 전일 종가를 못 주는 경우가 있습니다. 그 결과 미국채 카드가 계속
# "전일 종가 N/A · 전일 대비 미제공"으로 표시됐습니다.
#
# FRED의 DGS2/DGS10/DGS30은 미 재무부 Constant Maturity 공식 일별 확정치라,
# **직전 영업일 값이 곧 전일 종가**입니다. 수집기가 이미 이 시리즈를 적재해
# 두므로 추가 네트워크 비용도 없습니다.
#
# 주의: 현재가(TradingView 실시간)와 전일값(FRED 확정치)은 출처가 다릅니다.
# 이 사실을 item["prev_source"]에 남겨 화면이 밝힐 수 있게 합니다.
BOND_FRED_FALLBACK = {
    "us02y": "DGS2",
    "us10y": "DGS10",
    "us30y": "DGS30",
}


def get_bond_previous_close_from_fred(scraper_key: str) -> float | None:
    """
    FRED 공식 일별 시계열에서 해당 만기의 '직전 영업일' 수익률을 반환합니다.

    FRED는 하루 지연 발표이므로 시리즈의 마지막 값이 곧 직전 거래일
    확정치입니다.
    """
    series_id = BOND_FRED_FALLBACK.get(scraper_key)
    if not series_id:
        return None

    try:
        df = fetch_fred_series(series_id, period_years=1)
    except Exception as e:
        logger.warning("미국채 전일값 FRED 조회 실패 (%s): %s", series_id, e)
        return None

    if df is None or df.empty or series_id not in df.columns:
        return None

    values = df[series_id].dropna()
    if values.empty:
        return None

    last = float(values.iloc[-1])
    return last if last > 0 else None


# ==============================================================================
# 3. 실시간 매크로 전 지표 수집 및 텍스트 브리핑 생성
# ==============================================================================
def collect_macro_data():
    """
    매크로 전 지표를 실제로 수집합니다 (항상 네트워크를 씁니다).

    화면에서 직접 부르지 마세요. 화면은 저장본을 우선 읽는
    get_collected_macro_data()를 쓰고, 이 함수는 collector.py가 호출합니다.

    반환: (collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev)
    """
    collected = {}
    rate_10y_curr, rate_10y_prev = None, None
    rate_2y_curr, rate_2y_prev = None, None

    def _fetch_one(cat_name, name, ticker):
        return ticker, fetch_ticker_data(ticker, period="5d")

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {}
        for cat_name, items in MACRO_CATEGORIES.items():
            collected[cat_name] = []
            for name, ticker in items.items():
                fut = executor.submit(_fetch_one, cat_name, name, ticker)
                futures[fut] = (cat_name, name, ticker)

        raw = {}
        for fut in as_completed(futures):
            cat_name, name, ticker = futures[fut]
            raw.setdefault(cat_name, {})[name] = fut.result()

    for cat_name, items in MACRO_CATEGORIES.items():
        for name, ticker in items.items():
            _, df = raw.get(cat_name, {}).get(name, (ticker, None))
            if df is not None and isinstance(df, pd.DataFrame) and len(df) >= 2:
                curr = float(df['Close'].iloc[-1])
                prev = float(df['Close'].iloc[-2])
                if "JPY/KRW" in name and curr < 50:
                    curr, prev = curr * 100, prev * 100

                # [수정] 최근 2개 봉의 종가가 완전히 동일하면(휴장·야간시간대에
                # 마지막 봉이 그대로 복제되는 경우 포함), 등락률을
                # "변화 없음(0.00%)"으로 위장하지 않고 신뢰할 수 없는 값으로
                # 간주해 N/A 처리합니다. 실제 무변동인지, 데이터 정체인지
                # 구분할 수 없기 때문입니다.
                if curr == prev:
                    delta = None
                    pct = None
                else:
                    delta = curr - prev
                    pct = (delta / prev) * 100 if prev != 0 else 0.0

                last_timestamp = df.index[-1]
                is_intraday = bool(df.attrs.get("is_intraday", False))

                try:
                    if is_intraday and hasattr(last_timestamp, "tzinfo") and last_timestamp.tzinfo is not None:
                        # 분봉 + 타임존 정보 있음 → KST 시각으로 정확히 변환
                        last_ts_kst = last_timestamp.astimezone(ZoneInfo("Asia/Seoul"))
                        last_ts_str = last_ts_kst.strftime("%H:%M:%S KST")
                    elif is_intraday and hasattr(last_timestamp, "tz_localize"):
                        # 분봉인데 타임존 정보가 없는 예외적 경우만 UTC로 가정
                        last_ts_kst = last_timestamp.tz_localize("UTC").astimezone(ZoneInfo("Asia/Seoul"))
                        last_ts_str = last_ts_kst.strftime("%H:%M:%S KST")
                    else:
                        # [핵심 수정] 일봉(daily bar) 폴백 데이터는 시:분:초가 신뢰할 수
                        # 없으므로, 거짓 시각을 보여주지 않고 "거래일"만 명확히 표시
                        trading_date = (
                            last_timestamp.strftime("%Y-%m-%d")
                            if hasattr(last_timestamp, "strftime")
                            else "N/A"
                        )
                        last_ts_str = f"{trading_date} 일봉 기준"
                except Exception:
                    last_ts_str = "N/A"

                delta_str = (
                    f"{delta:+,.2f} ({pct:+.2f}%)"
                    if delta is not None and pct is not None
                    else "N/A"
                )
                collected[cat_name].append({
                    "name": name,
                    "price": curr,
                    "delta": delta,
                    "pct": pct,
                    "price_str": f"{curr:,.2f}",
                    "delta_str": delta_str,
                    "prev_str": f"{prev:,.2f}" if delta is not None else "N/A",
                    "status": "ok",
                    "last_ts": last_ts_str,
                })
                if ticker == "^TNX":
                    rate_10y_curr, rate_10y_prev = curr, (prev if delta is not None else None)
                elif ticker in ["2YY=F", "^IRX", "ZT=F"]:
                    rate_2y_curr, rate_2y_prev = curr, (prev if delta is not None else None)

            elif df is not None and isinstance(df, pd.DataFrame) and len(df) == 1:
                curr = float(df['Close'].iloc[-1])

                last_timestamp = df.index[-1]
                is_intraday = bool(df.attrs.get("is_intraday", False))

                try:
                    if is_intraday and hasattr(last_timestamp, "tzinfo") and last_timestamp.tzinfo is not None:
                        last_ts_kst = last_timestamp.astimezone(ZoneInfo("Asia/Seoul"))
                        last_ts_str = last_ts_kst.strftime("%H:%M:%S KST")
                    elif is_intraday and hasattr(last_timestamp, "tz_localize"):
                        last_ts_kst = last_timestamp.tz_localize("UTC").astimezone(ZoneInfo("Asia/Seoul"))
                        last_ts_str = last_ts_kst.strftime("%H:%M:%S KST")
                    else:
                        trading_date = (
                            last_timestamp.strftime("%Y-%m-%d")
                            if hasattr(last_timestamp, "strftime")
                            else "N/A"
                        )
                        last_ts_str = f"{trading_date} 일봉 기준"
                except Exception:
                    last_ts_str = "N/A"

                # [수정] 데이터가 1개뿐이면 직전값을 알 수 없으므로, curr를
                # prev처럼 위장해 "변화 없음(0.00%)"으로 표시하지 않고
                # delta/pct를 명시적으로 None(N/A)으로 남깁니다.
                collected[cat_name].append({
                    "name": name,
                    "price": curr,
                    "delta": None,
                    "pct": None,
                    "price_str": f"{curr:,.2f}",
                    "delta_str": "N/A",
                    "prev_str": "N/A",
                    "status": "single",
                    "last_ts": last_ts_str,
                })
                if ticker == "^TNX":
                    rate_10y_curr, rate_10y_prev = curr, None
                elif ticker in ["2YY=F", "^IRX", "ZT=F"]:
                    rate_2y_curr, rate_2y_prev = curr, None
            else:
                collected[cat_name].append({"name": name, "status": "fail"})

    target_cat = next((c for c in collected.keys() if "아시아" in c), None)

    def _inject_scraped_item(label_prefix: str, data: dict):
        """
        스크래핑 결과를 collected[target_cat]에 표준 포맷으로 추가하는 헬퍼.
        [수정] 카드 제목(name)은 짧게 유지하고, 월물/출처 정보는
        contract_month/source 필드에 별도로 담아 화면에서 캡션으로 표시합니다.
        """
        if target_cat is None or data is None:
            return

        price = data.get("price")
        prev = data.get("prev_close")
        is_estimated = data.get("is_estimated", True)
        source = data.get("source", "알수없음")
        contract_month = data.get("contract_month")

        estimate_tag = " (추정)" if is_estimated else ""
        label = f"{label_prefix}{estimate_tag}"

        if price is not None and prev is not None:
            delta = price - prev
            pct = (delta / prev) * 100 if prev != 0 else 0.0
            collected[target_cat].append({
                "name": label,
                "price": price,
                "delta": delta,
                "pct": pct,
                "price_str": f"{price:,.2f}",
                "delta_str": f"{delta:+,.2f} ({pct:+.2f}%)",
                "prev_str": f"{prev:,.2f}",
                "status": "ok",
                "is_estimated": is_estimated,
                "contract_month": contract_month,
                "source": source,
            })
        else:
            collected[target_cat].append({
                "name": label,
                "status": "fail",
                "contract_month": contract_month,
                "source": source,
            })

    try:
        from services.night_futures_scraper_service import get_kospi_night_futures
        _inject_scraped_item("코스피200 야간선물 (CME 연계)", get_kospi_night_futures())
    except Exception as e:
        logger.warning(f"KOSPI200 야간선물 스크래핑 주입 실패: {e}")

    try:
        from services.foreign_index_futures_scraper_service import get_nikkei225_futures
        _inject_scraped_item("닛케이225 선물", get_nikkei225_futures())
    except Exception as e:
        logger.warning(f"닛케이225 선물 스크래핑 주입 실패: {e}")

    try:
        from services.foreign_index_futures_scraper_service import get_hangseng_futures
        _inject_scraped_item("항셍 선물", get_hangseng_futures())
    except Exception as e:
        logger.warning(f"항셍 선물 스크래핑 주입 실패: {e}")

    # [핵심 수정] ZT=F(2년 국채 선물 가격)를 "수익률(%)"로 잘못 표시하던
    # 문제를 TradingView Scanner의 실제 수익률(us02y/us10y/us30y)로
    # 여기서 한 번에 보정합니다. 이후 반환되는 collected_data와
    # rate_2y_curr/rate_2y_prev를 참조하는 모든 화면·텍스트·스프레드 계산이
    # 항상 동일하게 보정된 값을 사용하게 됩니다.
    collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev = (
        _apply_bond_scanner_override(
            collected,
            rate_10y_curr,
            rate_10y_prev,
            rate_2y_curr,
            rate_2y_prev,
        )
    )

    return collected, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev


# ==============================================================================
# 3-1. 저장본 우선 읽기 경로
# ==============================================================================
def _macro_payload_is_usable(payload) -> bool:
    """저장본이 화면에서 쓸 수 있는 형태인지 확인합니다."""
    return (
        isinstance(payload, (list, tuple))
        and len(payload) == 5
        and isinstance(payload[0], dict)
        and len(payload[0]) > 0
    )


@st.cache_data(ttl=30, show_spinner=False)
def get_collected_macro_data():
    """
    화면용 진입점. SQLite 저장본이 신선하면 그것을 쓰고, 오래됐으면
    직접 수집한 뒤 저장합니다.

    [주의] 저장본은 JSON을 거치므로 튜플이 리스트로 돌아옵니다.
    호출부가 5개 값으로 언패킹하므로 반드시 튜플로 되돌려 줍니다.
    """
    payload = store.cached_or_live(
        datasets.SNAP_MACRO_COLLECTED,
        collect_macro_data,
        max_age_seconds=datasets.MAX_AGE_REALTIME,
    )

    if not _macro_payload_is_usable(payload):
        # 저장본도 없고 수집도 실패한 경우. 호출부가 빈 dict를 보고
        # "데이터 수집 실패"를 표시할 수 있도록 형태만 맞춰 돌려줍니다.
        return {}, None, None, None, None

    collected, r10c, r10p, r2c, r2p = payload
    return collected, r10c, r10p, r2c, r2p


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

        # 대용(proxy)/합성 시계열은 AI가 공식 지표로 오인하지 않도록
        # 요약 문장 자체에 출처 경고를 붙입니다.
        caveat = ""
        if df.attrs.get("is_proxy") or df.attrs.get("is_synthetic"):
            source_label = df.attrs.get("source_label", "추정치")
            caveat = f" ⚠️ 주의: 공식 지표가 아닌 추정치입니다 — {source_label}"

        return (
            f"- {label}: {current:,.2f} "
            f"(직전 대비 {change:+,.2f}, "
            f"최근 표본 내 백분위 {percentile:.1f}%)"
            f"{caveat}"
        )
    except Exception as e:
        return f"- {label}: 요약 실패 ({str(e)})"


@st.cache_data(ttl=1800, show_spinner=False)
def get_macro_risk_indicators_for_ai() -> dict:
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
        lines.append(summarize_series_for_ai(series_or_df, value_col, label))
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

    [수정] collected_data와 rate_2y_curr/rate_2y_prev는 이미
    get_collected_macro_data() 단계에서 TradingView Scanner로 보정된
    값이 전달되므로, 이 함수는 별도 보정 없이 그대로 출력만 담당합니다.
    카드 화면과 이 원본 텍스트가 항상 동일한 값을 보이도록 하기 위한
    구조입니다.
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

    for cat_name, items in collected_data.items():
        lines.append(f"## {_clean_macro_label(cat_name)}")
        if not items:
            lines.append("- 수집된 지표가 없습니다.")
            lines.append("")
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = _clean_macro_label(item.get("name", "이름 없음"))
            if item.get("status") in ("ok", "single"):
                lines.append(
                    f"- {name}: {item.get('price_str', 'N/A')} | "
                    f"{item.get('delta_str', 'N/A')} | 직전: {item.get('prev_str', 'N/A')}"
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
            lines.append(f"- 스프레드 직전 대비: {spread_curr - spread_prev:+.3f}%p")
    else:
        lines.append("- 장단기 금리차 데이터 수집 실패")
    lines.append("")

    _append_macro_risk_section(lines, risk_data)
    return "\n".join(lines)
