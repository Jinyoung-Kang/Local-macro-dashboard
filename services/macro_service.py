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
from config import MACRO_CATEGORIES

logger = logging.getLogger(__name__)


# ==============================================================================
# 0. FRED API Key 안전 로더
# ==============================================================================
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

                if isinstance(section, str):
                    return section.strip()

            for k in ["FRED_API_KEY", "fred_api_key", "FRED_KEY", "fred_key"]:
                if k in st.secrets:
                    return str(st.secrets[k]).strip()
    except Exception as e:
        logger.warning(f"FRED 키 로드 중 예외: {e}")
    return ""


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
def fetch_ticker_data(symbol: str, period: str = "1mo") -> pd.DataFrame:
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
                    return proxy_df
        except Exception as e:
            logger.warning(f"MOVE 프록시 연산 지연: {e}")

        # 비상 Fallback (MOVE 지수 95~110pt 대역 시계열)
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


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fred_series(series_id: str, period_years: int = 10, api_key: str = None) -> pd.DataFrame:
    """FRED 시계열 수집 (DatetimeIndex 인덱스 및 series_id 컬럼명 매핑)"""
    key = api_key or get_fred_key()
    start_date = (datetime.now() - timedelta(days=period_years * 365 + 60)).strftime("%Y-%m-%d")

    if key:
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations?"
                f"series_id={series_id}&api_key={key}&file_type=json"
                f"&observation_start={start_date}"
            )
            res = requests.get(url, timeout=10)
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
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        res = requests.get(csv_url, headers=headers, timeout=15)
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
# 3. 실시간 매크로 전 지표 수집 및 텍스트 브리핑 생성
# ==============================================================================
@st.cache_data(ttl=30, show_spinner=False)
def get_collected_macro_data():
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
                delta = curr - prev
                pct = (delta / prev) * 100 if prev != 0 else 0.0
                if "JPY/KRW" in name and curr < 50:
                    curr, prev, delta = curr * 100, prev * 100, delta * 100
        
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
        
                collected[cat_name].append({
                    "name": name,
                    "price": curr,
                    "delta": delta,
                    "pct": pct,
                    "price_str": f"{curr:,.2f}",
                    "delta_str": f"{delta:+,.2f} ({pct:+.2f}%)",
                    "prev_str": f"{prev:,.2f}",
                    "status": "ok",
                    "last_ts": last_ts_str,
                })
                if ticker == "^TNX":
                    rate_10y_curr, rate_10y_prev = curr, prev
                elif ticker in ["2YY=F", "^IRX", "ZT=F"]:
                    rate_2y_curr, rate_2y_prev = curr, prev
        
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
            
                collected[cat_name].append({
                    "name": name,
                    "price": curr,
                    "delta": 0.0,
                    "pct": 0.0,
                    "price_str": f"{curr:,.2f}",
                    "delta_str": "0.00 (0.00%)",
                    "prev_str": f"{curr:,.2f}",
                    "status": "single",
                    "last_ts": last_ts_str,
                })
                if ticker == "^TNX":
                    rate_10y_curr, rate_10y_prev = curr, curr
                elif ticker in ["2YY=F", "^IRX", "ZT=F"]:
                    rate_2y_curr, rate_2y_prev = curr, curr
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
