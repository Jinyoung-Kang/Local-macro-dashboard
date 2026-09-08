"""
services/krx_service.py
KRX OPEN API를 활용한 국내 파생상품(KOSPI 200 선물) 시세, 미결제약정,
시장 베이시스 및 투자자별 한국판 COT Index 산출 서비스 모듈
(직전 영업일 마감 확정치 자동 동기화 & NaN 결측치 원천 차단)

"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from config import get_krx_key, KRX_BASE_URL

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. KRX OPEN API 통신 엔진
# ==============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_krx_derivatives_daily(date_str: str) -> pd.DataFrame:
    """
    KRX OPEN API: 선물 일별매매정보 (fut_bydd_trd)
    date_str: YYYYMMDD 포맷
    """
    auth_key = get_krx_key()
    if not auth_key:
        return pd.DataFrame()

    url = f"{KRX_BASE_URL}/drv/fut_bydd_trd"
    headers = {
        "AUTH_KEY": auth_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    params = {"basDd": date_str}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=8)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                for key in ["OutBlock_1", "output", "block1", "items"]:
                    if key in data and isinstance(data[key], list) and len(data[key]) > 0:
                        return pd.DataFrame(data[key])
                for v in data.values():
                    if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                        return pd.DataFrame(v)
            elif isinstance(data, list):
                return pd.DataFrame(data)
    except Exception as e:
        logger.warning(f"KRX Derivatives API fetch failed for {date_str}: {e}")
    return pd.DataFrame()


# ==============================================================================
# 2. KRX Open API 지수 엔드포인트로 코스피200 현물 지수 조회
# ==============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_kospi200_index_close(date_str: str) -> float:
    """
    KRX Open API 지수 서비스(idx/kospi_dd_trd)로 코스피200 현물 지수
    종가를 조회합니다. pykrx 웹 스크래핑 대신 정식 AUTH_KEY 기반
    엔드포인트를 사용해 클라우드 환경에서의 빈 응답/차단 문제를 회피합니다.
    """
    auth_key = get_krx_key()
    if not auth_key:
        return None

    url = f"{KRX_BASE_URL}/idx/kospi_dd_trd"
    headers = {
        "AUTH_KEY": auth_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    params = {"basDd": date_str}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=8)
        if response.status_code != 200:
            return None

        data = response.json()
        items = []
        if isinstance(data, dict):
            for key in ["OutBlock_1", "output", "block1"]:
                if key in data and isinstance(data[key], list) and len(data[key]) > 0:
                    items = data[key]
                    break

        if not items:
            return None

        df = pd.DataFrame(items)
        cols = {c.upper(): c for c in df.columns}
        name_col = cols.get("IDX_NM", "")
        close_col = cols.get("CLSPRC_IDX", cols.get("TDD_CLSPRC", ""))

        if not name_col or not close_col:
            return None

        k200_row = df[df[name_col].str.contains("코스피200|코스피 200", na=False)]
        if k200_row.empty:
            return None

        close_str = str(k200_row.iloc[0][close_col]).replace(",", "").strip()
        return float(close_str) if close_str else None
    except Exception as e:
        logger.warning(f"KRX 지수 Open API 코스피200 조회 실패 ({date_str}): {e}")
        return None


# ==============================================================================
# 3. 최근 N영업일 파생 시계열 수집 및 동기화 (NaN 결측치 완벽 방어)
# ==============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def get_krx_futures_history(days: int = 40) -> pd.DataFrame:
    """
    최근 N영업일 동안의 KOSPI 200 선물 최근월물 종가, 거래량, 미결제약정 시계열을 수집.
    미확정/야간 데이터는 자동으로 직전 영업일 마감 확정치로 정제.

    """
    today = datetime.now(ZoneInfo("Asia/Seoul"))
    date_list = []

    curr = today
    while len(date_list) < days + 10:
        if curr.weekday() < 5:
            date_list.append(curr.strftime("%Y%m%d"))
        curr -= timedelta(days=1)

    def safe_float(val):
        try:
            v = float(str(val).replace(",", "").strip())
            return v if not np.isnan(v) else 0.0
        except Exception:
            return 0.0

    def parse_one_day(d_str: str, df_day: pd.DataFrame):
        """단일 날짜의 선물 일별매매정보를 파싱합니다. (네트워크 호출 없음)"""
        if df_day is None or df_day.empty:
            return None

        cols = {col.upper(): col for col in df_day.columns}
        name_col = cols.get("ISU_NM", cols.get("PROD_NM", ""))
        if not name_col or name_col not in df_day.columns:
            return None

        k200_futs = df_day[
            df_day[name_col].str.contains("코스피200|KOSPI 200", na=False, regex=True)
        ]
        k200_futs = k200_futs[
            ~k200_futs[name_col].str.contains("국채|달러|미니|위클리", na=False)
        ]

        if k200_futs.empty:
            return None

        if len(k200_futs) > 1:
            vol_col = cols.get("ACC_TRDVOL", cols.get("TRDVOL", ""))
            if vol_col and vol_col in k200_futs.columns:
                k200_futs = k200_futs.copy()
                k200_futs["_vol_sort"] = pd.to_numeric(
                    k200_futs[vol_col].astype(str).str.replace(",", ""),
                    errors="coerce",
                ).fillna(0)
                k200_futs = k200_futs.sort_values("_vol_sort", ascending=False)

        row = k200_futs.iloc[0]

        close_val = safe_float(row.get("TDD_CLSPRC", row.get("CLSPRC", 0)))
        if close_val <= 0:
            return None

        return {            
            "Date": pd.to_datetime(d_str, format="%Y%m%d"),
            "Futures_Close": close_val,
            "Change_Pct": safe_float(row.get("FLUC_RT", 0)),
            "Volume": safe_float(row.get("ACC_TRDVOL", row.get("TRDVOL", 0))),
            "Open_Interest": safe_float(row.get("ACC_OPNINT_QTY", row.get("OPNINT_QTY", 0))),
            "Contract_Name": str(row.get(name_col, "KOSPI 200 선물")),
        }

    # ------------------------------------------------------------------
    # 1단계: 날짜별 선물 일별매매정보를 병렬로 조회
    # ------------------------------------------------------------------
    parsed_records = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {
            executor.submit(fetch_krx_derivatives_daily, d_str): d_str
            for d_str in date_list
        }
        for future in as_completed(future_map):
            d_str = future_map[future]
            try:
                df_day = future.result()
            except Exception as e:
                logger.warning(f"KRX 선물 일별매매정보 병렬 조회 실패 ({d_str}): {e}")
                continue

            parsed = parse_one_day(d_str, df_day)
            if parsed is not None:
                parsed_records[d_str] = parsed

    # ------------------------------------------------------------------
    # 2단계: 유효한 날짜에 한해 코스피200 현물 지수를 병렬로 조회
    # (베이시스 = 선물 종가 - 현물 지수 계산용)
    # ------------------------------------------------------------------
    spot_closes = {}
    if parsed_records:
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_map = {
                executor.submit(fetch_kospi200_index_close, d_str): d_str
                for d_str in parsed_records.keys()
            }
            for future in as_completed(future_map):
                d_str = future_map[future]
                try:
                    spot_closes[d_str] = future.result()
                except Exception as e:
                    logger.warning(f"KRX 코스피200 현물 지수 병렬 조회 실패 ({d_str}): {e}")
                    spot_closes[d_str] = None

    # ------------------------------------------------------------------
    # 3단계: 병합 및 베이시스 계산 (네트워크 호출 없음, 순수 연산)
    # ------------------------------------------------------------------
    records = []
    for d_str, rec in parsed_records.items():
        spot_close = spot_closes.get(d_str)
        theo_val = np.nan
        basis_val = np.nan
        if spot_close is not None and spot_close > 0:
            basis_val = round(rec["Futures_Close"] - spot_close, 2)
            theo_val = spot_close
        else:
            logger.warning(f"KRX Open API 코스피200 현물 지수 조회 실패 ({d_str})")

        records.append({
            "Date": rec["Date"],
            "Futures_Close": rec["Futures_Close"],
            "Change_Pct": rec["Change_Pct"],
            "Volume": rec["Volume"],
            "Open_Interest": rec["Open_Interest"],
            "Theory_Price": theo_val,
            "Market_Basis": basis_val,
            "Contract_Name": rec["Contract_Name"],
        })

    # KRX 응답 부재 시 Fallback (KODEX 200 및 코스피 200 지수 프록시, is_estimated=True로 명시)
    if len(records) < 5:
        logger.warning(
            "KRX OpenAPI 파생상품 시계열 수집 부족(records<5). "
            "KODEX 200 프록시 추정치로 대체하며 is_estimated=True로 표시합니다."
        )
        return _generate_fallback_derivatives_data(days)

    df_hist = pd.DataFrame(records).sort_values("Date").reset_index(drop=True)

    # NaN 및 0 결측치 보정 (베이시스/이론가는 결측 그대로 유지, 임의 대체하지 않음)
    df_hist["Futures_Close"] = df_hist["Futures_Close"].replace(0, np.nan).ffill().bfill()
    df_hist["Open_Interest"] = df_hist["Open_Interest"].replace(0, np.nan).ffill().bfill()

    # 미결제약정 증감
    df_hist["OI_Change"] = df_hist["Open_Interest"].diff().fillna(0)

    # 4대 국면 판별
    def diagnose_phase(row):
        p_up = row["Change_Pct"] >= 0
        oi_up = row["OI_Change"] >= 0
        if p_up and oi_up:
            return "신규 롱 (Long Accumulation)"
        elif p_up and not oi_up:
            return "숏 커버링 (Short Covering)"
        elif not p_up and oi_up:
            return "신규 숏 (Short Accumulation)"
        else:
            return "롱 청산 (Long Liquidation)"

    df_hist["Market_Phase"] = df_hist.apply(diagnose_phase, axis=1)

    # 한국판 선물 COT Index (0~100%)
    min_oi = df_hist["Open_Interest"].rolling(window=min(20, len(df_hist)), min_periods=1).min()
    max_oi = df_hist["Open_Interest"].rolling(window=min(20, len(df_hist)), min_periods=1).max()
    denom = (max_oi - min_oi).replace(0, 1)
    df_hist["COT_OI_Index"] = ((df_hist["Open_Interest"] - min_oi) / denom * 100).round(1)

    # 베이시스 계산이 전부 실패했다면 경고 로그
    if df_hist["Market_Basis"].isna().all():
        logger.warning(
            "전체 구간에서 베이시스 계산이 실패했습니다 (KRX Open API 지수 조회 불가). "
            "Market_Basis는 NaN으로 유지되며 화면에서 '데이터 미제공'으로 표시해야 합니다."
        )

    # 실제 KRX 확정 데이터임을 명시
    df_hist["is_estimated"] = False

    return df_hist.tail(days).reset_index(drop=True)


def _generate_fallback_derivatives_data(days: int) -> pd.DataFrame:
    """
    ⚠️ 주의: 이 함수는 실제 KRX 확정 데이터가 아닙니다.
    KRX API 연결 실패 시 KODEX 200(069500.KS) 또는 ^KS200 가격을 기반으로 만든
    통계적 추정치이며, 실제 선물 종가·미결제약정·베이시스와 다를 수 있습니다.
    반환 DataFrame에는 반드시 is_estimated=True가 포함됩니다.
    """
    try:
        hist = None
        for sym in ["069500.KS", "^KS200"]:
            try:
                tk = yf.Ticker(sym)
                h = tk.history(period=f"{days + 30}d")
                if h is not None and not h.empty and len(h) >= 5:
                    hist = h
                    break
            except Exception:
                pass

        if hist is not None and not hist.empty:
            hist = hist.dropna(subset=["Close"])
            hist = hist[hist["Close"] > 0]
            hist["Close"] = hist["Close"].ffill().bfill()

            df = hist.tail(days).reset_index()
            df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)

            last_close = df["Close"].iloc[-1]
            scale_factor = 0.01 if last_close > 1000 else 1.0

            df["Futures_Close"] = (df["Close"] * scale_factor).round(2)
            df["Change_Pct"] = df["Futures_Close"].pct_change().fillna(0.0).round(2) * 100.0

            vol = df["Volume"] if "Volume" in df.columns else 150000
            df["Volume"] = pd.to_numeric(vol, errors='coerce').fillna(150000).replace(0, 150000).astype(int)

            rolling_std = df["Futures_Close"].rolling(5, min_periods=1).std().fillna(1.0)
            df["Open_Interest"] = (280000 + (rolling_std * 4500) + np.linspace(500, 5000, len(df))).astype(int)
            df["OI_Change"] = df["Open_Interest"].diff().fillna(0).astype(int)

            df["Theory_Price"] = (df["Futures_Close"] * 1.0015).round(2)
            df["Market_Basis"] = (0.85 + np.sin(np.linspace(0, 6, len(df))) * 0.65).round(2)
            df["Contract_Name"] = "KOSPI 200 최근월물 (프록시 추정 모드)"

            def diagnose_phase(row):
                p_up = row["Change_Pct"] >= 0
                oi_up = row["OI_Change"] >= 0
                if p_up and oi_up:
                    return "신규 롱 (Long Accumulation)"
                elif p_up and not oi_up:
                    return "숏 커버링 (Short Covering)"
                elif not p_up and oi_up:
                    return "신규 숏 (Short Accumulation)"
                else:
                    return "롱 청산 (Long Liquidation)"

            df["Market_Phase"] = df.apply(diagnose_phase, axis=1)
            min_oi = df["Open_Interest"].min()
            max_oi = df["Open_Interest"].max()
            denom = (max_oi - min_oi) if max_oi != min_oi else 1
            df["COT_OI_Index"] = (((df["Open_Interest"] - min_oi) / denom) * 100.0).round(1)

            if pd.isna(df["Futures_Close"].iloc[-1]) or df["Futures_Close"].iloc[-1] == 0:
                df.loc[df.index[-1], "Futures_Close"] = (
                    df["Futures_Close"].iloc[-2] if len(df) > 1 else 365.50
                )

            df["is_estimated"] = True

            return df[[
                "Date", "Futures_Close", "Change_Pct", "Volume", "Open_Interest",
                "OI_Change", "Theory_Price", "Market_Basis", "Contract_Name",
                "Market_Phase", "COT_OI_Index", "is_estimated"
            ]]
    except Exception as e:
        logger.error(f"Fallback generation error: {e}")

    today = datetime.now(ZoneInfo("Asia/Seoul"))
    dates = [today - timedelta(days=i) for i in range(days, 0, -1)]
    return pd.DataFrame({
        "Date": dates,
        "Futures_Close": [365.0 + (i * 0.2) for i in range(days)],
        "Change_Pct": [0.20] * days,
        "Volume": [150000] * days,
        "Open_Interest": [280000 + (i * 150) for i in range(days)],
        "OI_Change": [150] * days,
        "Theory_Price": [365.5 + (i * 0.2) for i in range(days)],
        "Market_Basis": [0.75] * days,
        "Contract_Name": "KOSPI 200 최근월물 (프록시 추정 모드)",
        "Market_Phase": ["신규 롱 (Long Accumulation)"] * days,
        "COT_OI_Index": [55.0] * days,
        "is_estimated": [True] * days,
    })


# ==============================================================================
# 4. [신규] Daum 금융 선물(KOSPI 200) 투자주체별 매매동향 실제 데이터 수집
# ==============================================================================
DAUM_FUTURES_INVESTOR_URL = "https://finance.daum.net/api/investor/future/days"

# Daum 응답 필드 -> 화면 표시용 (투자 주체, 원본 필드명) 매핑.
# 순서는 Daum 원본 화면(개인 -> 외국인 -> 기관계 -> 세부기관 -> 기타법인)과
# 유사하게 배치하되, 스마트머니 관점에서 외국인을 최상단에 둡니다.
DAUM_FUTURES_CATEGORY_MAP = [
    ("외국인 (스마트머니)", "foreignSettlement"),
    ("기관계", "institutionalSettlement"),
    ("금융투자 (차익거래)", "financialInvestment"),
    ("보험", "insuranceInvestment"),
    ("투신", "trustInvestment"),
    ("은행", "bankInvestment"),
    ("기타금융", "etcInvestment"),
    ("연기금등", "pensionFundInvestment"),
    ("기타법인", "etcCorporationSettlement"),
    ("개인 (리테일)", "privateSettlement"),
]


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_daum_futures_investor_trend(
    lookback_days: int = 25,
    measure: str = "CONTRACT",
) -> pd.DataFrame:
    """
    Daum 금융 '투자주체별 매매동향(선물)' 내부 JSON API에서
    KOSPI 200 선물의 투자자별 일자별 순매수 데이터를 가져옵니다.

    measure:
    - CONTRACT: 계약수 기준 (기본값)
    - PRICE: 금액 기준. Daum 원 단위 응답을 억 원 단위로 변환합니다.

    반환 컬럼:
    - 투자 주체
    - 당일 순매수
    - 5일 누적
    - 20일 누적
    - 포지션 성향
    - is_placeholder
    - data_measure
    - data_unit
    """
    valid_measures = {"CONTRACT", "PRICE"}
    measure = str(measure).upper().strip()

    if measure not in valid_measures:
        logger.warning(
            "Daum 선물 수급 지원하지 않는 measure=%s. CONTRACT로 변경합니다.",
            measure,
        )
        measure = "CONTRACT"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://finance.daum.net/domestic/investors/DERIVATIVES",
        "X-Requested-With": "XMLHttpRequest",
    }

    params = {
        "page": 1,
        "perPage": max(lookback_days, 20),
        "terms": "days",
        "pagination": "true",
    }

    # Daum API는 계약수 모드일 때 type 파라미터가 없고,
    # 금액 모드일 때만 type=PRICE를 사용합니다.
    if measure == "PRICE":
        params["type"] = "PRICE"

    try:
        response = requests.get(
            DAUM_FUTURES_INVESTOR_URL,
            headers=headers,
            params=params,
            timeout=10,
        )

        if response.status_code != 200:
            logger.warning(
                "Daum 선물 투자주체별 매매동향 API HTTP 실패: "
                "measure=%s, status=%s",
                measure,
                response.status_code,
            )
            return pd.DataFrame()

        payload = response.json()
        rows = payload.get("data", [])

        if not isinstance(rows, list) or not rows:
            logger.warning(
                "Daum 선물 투자주체별 매매동향 API 빈 응답: measure=%s",
                measure,
            )
            return pd.DataFrame()

        parsed_rows = []

        for row in rows:
            if not isinstance(row, dict):
                continue

            parsed_rows.append({
                "date": row.get("date"),
                **{
                    field: pd.to_numeric(
                        row.get(field, 0),
                        errors="coerce",
                    )
                    for _, field in DAUM_FUTURES_CATEGORY_MAP
                },
            })

        if not parsed_rows:
            logger.warning(
                "Daum 선물 수급 API 파싱 결과가 비어 있습니다: measure=%s",
                measure,
            )
            return pd.DataFrame()

        df_raw = pd.DataFrame(parsed_rows)

        numeric_columns = [
            field
            for _, field in DAUM_FUTURES_CATEGORY_MAP
        ]

        for column in numeric_columns:
            df_raw[column] = pd.to_numeric(
                df_raw[column],
                errors="coerce",
            ).fillna(0.0)

        # API 응답은 최신 거래일이 첫 행인 DESC 순서입니다.
        today_row = df_raw.iloc[0]
        cum_5d = df_raw.iloc[: min(5, len(df_raw))].sum(
            numeric_only=True
        )
        cum_20d = df_raw.iloc[: min(20, len(df_raw))].sum(
            numeric_only=True
        )

        # type=PRICE 응답은 원 단위이므로 억 원 단위로 변환합니다.
        divisor = 100_000_000 if measure == "PRICE" else 1
        unit = "억 원" if measure == "PRICE" else "계약"
        measure_label = "금액" if measure == "PRICE" else "계약수"

        records = []

        for label, field in DAUM_FUTURES_CATEGORY_MAP:
            raw_today = float(today_row.get(field, 0.0) or 0.0)
            raw_5d = float(cum_5d.get(field, 0.0) or 0.0)
            raw_20d = float(cum_20d.get(field, 0.0) or 0.0)

            net_today = raw_today / divisor
            net_5d = raw_5d / divisor
            net_20d = raw_20d / divisor

            # 포지션 성향은 최근 20거래일 누적값을 기준으로 판단합니다.
            if net_20d > 0:
                stance = "🟢 매수 우위(Long)"
            elif net_20d < 0:
                stance = "🔴 매도 우위(Short)"
            else:
                stance = "⚪ 중립"

            if measure == "PRICE":
                net_today = round(net_today, 1)
                net_5d = round(net_5d, 1)
                net_20d = round(net_20d, 1)
            else:
                net_today = int(net_today)
                net_5d = int(net_5d)
                net_20d = int(net_20d)

            records.append({
                "투자 주체": label,
                "당일 순매수": net_today,
                "5일 누적": net_5d,
                "20일 누적": net_20d,
                "포지션 성향": stance,
            })

        df_result = pd.DataFrame(records)

        # 뷰에서 토글별 표기·포맷을 결정하는 데 사용합니다.
        df_result["is_placeholder"] = False
        df_result["data_measure"] = measure
        df_result["data_unit"] = unit
        df_result["data_date"] = str(
            today_row.get("date", "")
        )[:10]

        logger.info(
            "Daum 선물 투자주체별 매매동향 수집 성공: "
            "measure=%s, rows=%s, 기준일=%s",
            measure_label,
            len(df_result),
            today_row.get("date"),
        )

        return df_result

    except Exception as e:
        logger.warning(
            "Daum 선물 투자주체별 매매동향 수집 실패: "
            "measure=%s, error=%s",
            measure,
            e,
        )
        return pd.DataFrame()


# ==============================================================================
# 5. 주체별(외인/기관/개인) 선물 수급 요약 — Daum 실데이터 실패 시 폴백 placeholder
# ==============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def get_krx_investor_derivatives_summary() -> pd.DataFrame:
    """
    ⚠️ 중요 안내: 이 함수는 KRX 실제 투자자별 선물 거래 API와 연동되지 않은
    고정 예시(placeholder) 데이터입니다. fetch_daum_futures_investor_trend()가
    Daum 실데이터 수집에 실패했을 때의 최종 폴백으로만 사용해야 합니다.

    반환 DataFrame에는 'is_placeholder' 컬럼이 항상 True로 포함되며,
    호출하는 화면(views/krx_cot_view.py)은 이 값을 반드시 확인하여
    사용자에게 경고를 표시해야 합니다.
    """
    logger.warning(
        "get_krx_investor_derivatives_summary(): Daum 실데이터 수집 실패로 "
        "고정 예시(placeholder) 데이터를 반환합니다."
    )

    categories = ["외국인 (스마트머니)", "금융투자 (차익거래)", "투신/사모 (기관)", "개인 (리테일)"]
    net_today = [3450, -2100, -850, -500]
    net_5d = [14200, -8900, -3100, -2200]
    net_20d = [38500, -24100, -6800, -7600]

    short_stance = [
        "🟢 강한 상방(Long)",
        "🔴 매도/차익 헤지",
        "⚪ 중립/분할 헤지",
        "🔵 하방(Short) 베팅",
    ]

    df = pd.DataFrame({
        "투자 주체": categories,
        "당일 순매수": net_today,
        "5일 누적": net_5d,
        "20일 누적": net_20d,
        "포지션 성향": short_stance,
        "is_placeholder": [True] * len(categories),
    })
    return df

# ==============================================================================
# 6. Daum 선물 시간별 투자자 수급 가속도
# ==============================================================================
DAUM_FUTURES_INVESTOR_TIMES_URL = (
    "https://finance.daum.net/api/investor/future/times"
)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_daum_futures_intraday_acceleration(
    lookback_minutes: int = 30,
) -> dict:
    """
    Daum 금융의 KOSPI 200 선물 시간별 투자주체 수급 API를 사용하여
    외국인·기관계의 최신 누적 순매수와 최근 N분 수급 변화량을 계산합니다.

    실제 확인된 API:
        https://finance.daum.net/api/investor/future/times
        ?page=1&perPage=10&terms=times&pagination=true

    주요 원본 필드:
        privateSettlement       : 개인 누적 순매수 계약수
        foreignSettlement       : 외국인 누적 순매수 계약수
        institutionalSettlement : 기관계 누적 순매수 계약수
        financialInvestment     : 금융투자 누적 순매수 계약수
        insuranceInvestment     : 보험 누적 순매수 계약수
        trustInvestment         : 투신 누적 순매수 계약수
        bankInvestment          : 은행 누적 순매수 계약수
        etcInvestment           : 기타금융 누적 순매수 계약수
        pensionFundInvestment   : 연기금등 누적 순매수 계약수

    반환:
        {
            "available": bool,
            "data_date": "YYYY-MM-DD",
            "latest_time": "HH:MM:SS",
            "reference_time": "HH:MM:SS",
            "lookback_minutes": int,
            "foreign_current": int,
            "foreign_change": int,
            "institution_current": int,
            "institution_change": int,
            "private_current": int,
            "private_change": int,
            "financial_current": int,
            "financial_change": int,
            "pension_current": int,
            "pension_change": int,
            "flow_status": str,
            "flow_status_color": str,
            "source": str,
            "error": str | None,
        }

    주의:
    - 이 데이터는 장중 시간별 누적 수급이며, 장 마감 후 정산/집계 갱신에 따라
      값이 달라질 수 있습니다.
    - Daum 웹사이트 내부 API 기반 비공식 데이터입니다.
    - N분 전 정확히 같은 시각이 없을 수 있으므로, 기준시각 이전의 가장 가까운
      수집값을 사용합니다.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": (
            "https://finance.daum.net/domestic/investors/DERIVATIVES"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }

    # 시간별 API는 일반적으로 장중 수백 건의 행을 반환합니다.
    # 최근 30~60분 분석에 충분한 범위를 한 번에 가져옵니다.
    params = {
        "page": 1,
        "perPage": 500,
        "terms": "times",
        "pagination": "true",
    }

    default_result = {
        "available": False,
        "data_date": None,
        "latest_time": None,
        "reference_time": None,
        "lookback_minutes": lookback_minutes,
        "foreign_current": None,
        "foreign_change": None,
        "institution_current": None,
        "institution_change": None,
        "private_current": None,
        "private_change": None,
        "financial_current": None,
        "financial_change": None,
        "pension_current": None,
        "pension_change": None,
        "flow_status": "시간별 수급 데이터 미제공",
        "flow_status_color": "gray",
        "source": "Daum 금융 시간별 선물 수급 (비공식)",
        "error": None,
    }

    try:
        response = requests.get(
            DAUM_FUTURES_INVESTOR_TIMES_URL,
            headers=headers,
            params=params,
            timeout=10,
        )

        if response.status_code != 200:
            default_result["error"] = (
                f"Daum API HTTP {response.status_code}"
            )
            logger.warning(
                "Daum 선물 시간별 수급 API HTTP 실패: status=%s",
                response.status_code,
            )
            return default_result

        payload = response.json()
        rows = payload.get("data", [])

        if not isinstance(rows, list) or not rows:
            default_result["error"] = "응답 data가 비어 있습니다."
            logger.warning("Daum 선물 시간별 수급 API 빈 응답")
            return default_result

        df = pd.DataFrame(rows)

        if "date" not in df.columns:
            default_result["error"] = "응답에 date 필드가 없습니다."
            logger.warning(
                "Daum 선물 시간별 수급 API date 필드 없음: columns=%s",
                list(df.columns),
            )
            return default_result

        df["DateTime"] = pd.to_datetime(
            df["date"],
            errors="coerce",
        )
        df = df.dropna(subset=["DateTime"]).copy()

        if df.empty:
            default_result["error"] = "유효한 시간 데이터가 없습니다."
            return default_result

        numeric_columns = [
            "privateSettlement",
            "foreignSettlement",
            "institutionalSettlement",
            "financialInvestment",
            "insuranceInvestment",
            "trustInvestment",
            "bankInvestment",
            "etcInvestment",
            "pensionFundInvestment",
        ]

        for column in numeric_columns:
            if column not in df.columns:
                df[column] = 0
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            ).fillna(0.0)

        # API 응답은 최신 시점부터 DESC로 오지만, 시간 계산과 기준행 선택을 위해
        # 과거 -> 최신 오름차순으로 정렬합니다.
        df = (
            df.sort_values("DateTime")
            .drop_duplicates(subset=["DateTime"], keep="last")
            .reset_index(drop=True)
        )

        latest_dt = df["DateTime"].iloc[-1]
        latest_day = latest_dt.date()

        # 최신 날짜의 장중 데이터만 분석합니다.
        # 페이지 경계를 넘어 전일 데이터가 함께 들어와도 섞이지 않게 막습니다.
        intraday_df = df[
            df["DateTime"].dt.date == latest_day
        ].copy()

        if intraday_df.empty:
            default_result["error"] = "최신 거래일 시간별 데이터가 없습니다."
            return default_result

        latest_row = intraday_df.iloc[-1]
        reference_target_dt = latest_dt - pd.Timedelta(
            minutes=lookback_minutes
        )

        # 정확히 N분 전 시점이 없을 경우, 기준시각보다 이전인 가장 가까운 데이터를 사용합니다.
        reference_candidates = intraday_df[
            intraday_df["DateTime"] <= reference_target_dt
        ]

        # 장 시작 직후처럼 N분 이전 데이터가 없으면, 가장 오래된 유효 시점을 사용합니다.
        if reference_candidates.empty:
            reference_row = intraday_df.iloc[0]
        else:
            reference_row = reference_candidates.iloc[-1]

        def to_int(value) -> int:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0

        foreign_current = to_int(
            latest_row["foreignSettlement"]
        )
        foreign_change = (
            foreign_current
            - to_int(reference_row["foreignSettlement"])
        )

        institution_current = to_int(
            latest_row["institutionalSettlement"]
        )
        institution_change = (
            institution_current
            - to_int(reference_row["institutionalSettlement"])
        )

        private_current = to_int(
            latest_row["privateSettlement"]
        )
        private_change = (
            private_current
            - to_int(reference_row["privateSettlement"])
        )

        financial_current = to_int(
            latest_row["financialInvestment"]
        )
        financial_change = (
            financial_current
            - to_int(reference_row["financialInvestment"])
        )

        pension_current = to_int(
            latest_row["pensionFundInvestment"]
        )
        pension_change = (
            pension_current
            - to_int(reference_row["pensionFundInvestment"])
        )

        # 외국인과 기관계의 최근 N분 변화 방향을 비교합니다.
        if foreign_change > 0 and institution_change > 0:
            flow_status = "외국인·기관 동반 매수"
            flow_status_color = "green"
        elif foreign_change < 0 and institution_change < 0:
            flow_status = "외국인·기관 동반 매도"
            flow_status_color = "red"
        elif foreign_change > 0 and institution_change < 0:
            flow_status = "외국인 매수 · 기관 매도"
            flow_status_color = "blue"
        elif foreign_change < 0 and institution_change > 0:
            flow_status = "외국인 매도 · 기관 매수"
            flow_status_color = "orange"
        else:
            flow_status = "수급 방향 중립 또는 혼조"
            flow_status_color = "gray"

        result = {
            "available": True,
            "data_date": latest_dt.strftime("%Y-%m-%d"),
            "latest_time": latest_dt.strftime("%H:%M:%S"),
            "reference_time": reference_row["DateTime"].strftime(
                "%H:%M:%S"
            ),
            "lookback_minutes": lookback_minutes,
            "foreign_current": foreign_current,
            "foreign_change": foreign_change,
            "institution_current": institution_current,
            "institution_change": institution_change,
            "private_current": private_current,
            "private_change": private_change,
            "financial_current": financial_current,
            "financial_change": financial_change,
            "pension_current": pension_current,
            "pension_change": pension_change,
            "flow_status": flow_status,
            "flow_status_color": flow_status_color,
            "source": "Daum 금융 시간별 선물 수급 (비공식)",
            "error": None,
        }

        logger.info(
            "Daum 선물 시간별 수급 가속도 수집 성공: "
            "date=%s, latest=%s, reference=%s, "
            "foreign_change=%s, institution_change=%s",
            result["data_date"],
            result["latest_time"],
            result["reference_time"],
            result["foreign_change"],
            result["institution_change"],
        )

        return result

    except Exception as e:
        logger.warning(
            "Daum 선물 시간별 수급 가속도 수집 실패: %s",
            e,
        )
        default_result["error"] = str(e)
        return default_result
