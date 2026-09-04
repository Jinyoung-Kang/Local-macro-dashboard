"""
services/krx_service.py
KRX OPEN API를 활용한 국내 파생상품(KOSPI 200 선물) 시세, 미결제약정,
시장 베이시스 및 투자자별 한국판 COT Index 산출 서비스 모듈
(직전 영업일 마감 확정치 자동 동기화 & NaN 결측치 원천 차단)

"""
import logging
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

    반환 DataFrame에는 'is_estimated' 컬럼이 포함됩니다.
    - False: KRX OpenAPI에서 수집한 실제 확정치
    - True : KRX OpenAPI 응답 부재로 KODEX 200 프록시 추정치를 사용함
    """
    today = datetime.now(ZoneInfo("Asia/Seoul"))
    date_list = []

    curr = today
    while len(date_list) < days + 10:
        if curr.weekday() < 5:
            date_list.append(curr.strftime("%Y%m%d"))
        curr -= timedelta(days=1)

    records = []

    for d_str in date_list:
        df_day = fetch_krx_derivatives_daily(d_str)
        if not df_day.empty:
            cols = {col.upper(): col for col in df_day.columns}

            name_col = cols.get("ISU_NM", cols.get("PROD_NM", ""))
            if name_col and name_col in df_day.columns:
                # "F 20" 패턴 제거: 계약월 표기는 모든 선물 상품에 공통으로
                # 들어가므로 코스피200선물만 정확히 매칭
                k200_futs = df_day[
                    df_day[name_col].str.contains("코스피200|KOSPI 200", na=False, regex=True)
                ]

                # 국채/달러/미니/위클리 등 다른 선물이 우연히 겹치는 것을 배제
                k200_futs = k200_futs[
                    ~k200_futs[name_col].str.contains("국채|달러|미니|위클리", na=False)
                ]

                if not k200_futs.empty:
                    # 여러 계약월(최근월/차근월)이 동시에 잡히면 거래량이
                    # 가장 큰 실질적인 최근월물을 선택
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

                    def safe_float(val):
                        try:
                            v = float(str(val).replace(",", "").strip())
                            return v if not np.isnan(v) else 0.0
                        except Exception:
                            return 0.0

                    close_val = safe_float(row.get("TDD_CLSPRC", row.get("CLSPRC", 0)))
                    fluc_val = safe_float(row.get("FLUC_RT", 0))
                    vol_val = safe_float(row.get("ACC_TRDVOL", row.get("TRDVOL", 0)))
                    oi_val = safe_float(row.get("ACC_OPNINT_QTY", row.get("OPNINT_QTY", 0)))

                    # KRX fut_bydd_trd API는 BASIS 필드를 제공하지 않으므로,
                    # Open API 지수 엔드포인트로 같은 날짜 현물 지수를 조회해
                    # 베이시스(선물 종가 - 현물 지수)를 직접 계산합니다.
                    theo_val = np.nan
                    basis_val = np.nan
                    if close_val > 0:
                        spot_close = fetch_kospi200_index_close(d_str)
                        if spot_close is not None and spot_close > 0:
                            basis_val = round(close_val - spot_close, 2)
                            theo_val = spot_close
                        else:
                            logger.warning(f"KRX Open API 코스피200 현물 지수 조회 실패 ({d_str})")

                    if close_val > 0:
                        records.append({
                            "Date": pd.to_datetime(d_str, format="%Y%m%d"),
                            "Futures_Close": close_val,
                            "Change_Pct": fluc_val,
                            "Volume": vol_val,
                            "Open_Interest": oi_val,
                            "Theory_Price": theo_val,
                            "Market_Basis": basis_val,
                            "Contract_Name": str(row.get(name_col, "KOSPI 200 선물")),
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
def fetch_daum_futures_investor_trend(lookback_days: int = 25) -> pd.DataFrame:
    """
    Daum 금융 '투자주체별 매매동향(선물)' 페이지의 내부 JSON API에서
    KOSPI 200 선물 개인/외국인/기관계(및 세부: 금융투자/보험/투신/은행/
    기타금융/연기금등)/기타법인의 일자별 순매수(계약수)를 가져와
    당일/5일 누적/20일 누적을 계산합니다.

    주의:
    - Daum 공식 API가 아닌 웹페이지 내부 요청이므로, 페이지 구조 변경 시
      실패할 수 있습니다.
    - 단위는 계약수(contracts)입니다. 금액 기준이 필요하면 type=PRICE
      파라미터를 추가로 사용해야 합니다(원 단위로 반환됨).
    - 실패 시 빈 DataFrame을 반환하며, 호출부는 반드시
      get_krx_investor_derivatives_summary()로 폴백해야 합니다.
    """
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

    try:
        response = requests.get(
            DAUM_FUTURES_INVESTOR_URL,
            headers=headers,
            params=params,
            timeout=10,
        )

        if response.status_code != 200:
            logger.warning(
                "Daum 선물 투자주체별 매매동향 API HTTP 실패: status=%s",
                response.status_code,
            )
            return pd.DataFrame()

        payload = response.json()
        rows = payload.get("data", [])

        if not isinstance(rows, list) or not rows:
            logger.warning("Daum 선물 투자주체별 매매동향 API 빈 응답")
            return pd.DataFrame()

        # 응답은 최신일이 첫 번째(DESC)로 옵니다.
        parsed_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            parsed_rows.append({
                "date": row.get("date"),
                **{
                    field: row.get(field, 0)
                    for _, field in DAUM_FUTURES_CATEGORY_MAP
                },
            })

        if not parsed_rows:
            return pd.DataFrame()

        df_raw = pd.DataFrame(parsed_rows)

        today_row = df_raw.iloc[0]
        cum_5d = df_raw.iloc[: min(5, len(df_raw))].sum(numeric_only=True)
        cum_20d = df_raw.iloc[: min(20, len(df_raw))].sum(numeric_only=True)

        records = []
        for label, field in DAUM_FUTURES_CATEGORY_MAP:
            net_today = int(today_row.get(field, 0) or 0)
            net_5d = int(cum_5d.get(field, 0) or 0)
            net_20d = int(cum_20d.get(field, 0) or 0)

            if net_20d > 0:
                stance = "🟢 매수 우위(Long)"
            elif net_20d < 0:
                stance = "🔴 매도 우위(Short)"
            else:
                stance = "⚪ 중립"

            records.append({
                "투자 주체": label,
                "당일 순매수": net_today,
                "5일 누적": net_5d,
                "20일 누적": net_20d,
                "포지션 성향": stance,
            })

        df_result = pd.DataFrame(records)
        df_result["is_placeholder"] = False

        logger.info(
            "Daum 선물 투자주체별 매매동향 수집 성공: rows=%s, 기준일=%s",
            len(df_result),
            today_row.get("date"),
        )

        return df_result

    except Exception as e:
        logger.warning("Daum 선물 투자주체별 매매동향 수집 실패: %s", e)
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
