"""
services/krx_service.py
KRX OPEN API를 활용한 국내 파생상품(KOSPI 200 선물) 시세, 미결제약정,
시장 베이시스 및 투자자별 한국판 COT Index 산출 서비스 모듈
(직전 영업일 마감 확정치 자동 동기화 & NaN 결측치 원천 차단)

[수정 사항]
1. get_krx_futures_history()의 반환 DataFrame에 'is_estimated' 컬럼 추가
   (KODEX 200 프록시 Fallback을 사용한 경우 True로 표시)
2. get_krx_investor_derivatives_summary()가 하드코딩된 예시 수치를 반환한다는 사실을
   함수명·docstring·반환 튜플의 is_placeholder 플래그로 명확히 표시
   (실제 KRX 투자자별 선물 데이터 API가 연동되기 전까지의 임시 조치)
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
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
# 2. 최근 N영업일 파생 시계열 수집 및 동기화 (NaN 결측치 완벽 방어)
# ==============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def get_krx_futures_history(days: int = 40) -> pd.DataFrame:
    """
    최근 N영업일 동안의 KOSPI 200 선물 최근월물 종가, 거래량, 미결제약정 시계열을 수집.
    미확정/야간 데이터는 자동으로 직전 영업일 마감 확정치로 정제.

    반환 DataFrame에는 'is_estimated' 컬럼이 포함됩니다.
    - False: KRX OpenAPI에서 수집한 실제 확정치
    - True : KRX OpenAPI 응답 부재로 KODEX 200 프록시 추정치를 사용함

    [수정 사항] 상품명 필터를 "코스피200|KOSPI 200|F 20" → "코스피200|KOSPI 200"로
    좁히고, 국채/달러/미니 등 다른 선물이 잘못 매칭되는 것을 차단했습니다.
    "F 20"이라는 계약월 표기는 모든 선물 상품에 공통으로 들어가므로, 이 패턴만으로
    필터링하면 코스피200선물이 아닌 10년국채선물 등이 잘못 선택될 수 있습니다.
    또한 동일 상품의 여러 계약월(최근월/차근월)이 함께 잡힐 경우, 거래량이 가장 큰
    실질적인 "최근월물"을 명시적으로 선택하도록 정렬 로직을 추가했습니다.
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
                # [수정] "F 20" 패턴 제거: 계약월 표기는 모든 선물 상품에
                # 공통으로 들어가므로 코스피200선물만 정확히 매칭
                k200_futs = df_day[
                    df_day[name_col].str.contains("코스피200|KOSPI 200", na=False, regex=True)
                ]

                # [수정] 국채/달러/미니/위클리 등 상품명에 "코스피200" 문자열이
                # 우연히 겹치는 다른 선물을 한 번 더 명시적으로 배제
                k200_futs = k200_futs[
                    ~k200_futs[name_col].str.contains("국채|달러|미니|위클리", na=False)
                ]

                if not k200_futs.empty:
                    # [수정] 여러 계약월(최근월/차근월)이 동시에 잡히면,
                    # 실제 거래가 집중된 최근월물(거래량 최대)을 선택
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
                    theo_val = safe_float(row.get("THEO_PRC", 0))
                    basis_val = safe_float(row.get("BASIS", 0))

                    if close_val > 0:
                        records.append({
                            "Date": pd.to_datetime(d_str, format="%Y%m%d"),
                            "Futures_Close": close_val,
                            "Change_Pct": fluc_val,
                            "Volume": vol_val,
                            "Open_Interest": oi_val,
                            "Theory_Price": theo_val,
                            "Market_Basis": basis_val,
                            "Contract_Name": str(row.get(name_col, "KOSPI 200 선물"))
                        })

    # KRX 응답 부재 시 Fallback (KODEX 200 및 코스피 200 지수 프록시, is_estimated=True로 명시)
    if len(records) < 5:
        logger.warning(
            "KRX OpenAPI 파생상품 시계열 수집 부족(records<5). "
            "KODEX 200 프록시 추정치로 대체하며 is_estimated=True로 표시합니다."
        )
        return _generate_fallback_derivatives_data(days)

    df_hist = pd.DataFrame(records).sort_values("Date").reset_index(drop=True)

    # NaN 및 0 결측치 보정
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
        # 코스피 200 프록시 수집 (069500.KS 및 ^KS200)
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
            # 1. NaN이거나 0 이하인 최근 미확정 캔들 완전 제거
            hist = hist.dropna(subset=["Close"])
            hist = hist[hist["Close"] > 0]
            hist["Close"] = hist["Close"].ffill().bfill()

            df = hist.tail(days).reset_index()
            df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)

            # KODEX 200(원화 가격 약 36,000원) vs 코스피 200 지수(포인트 약 360pt) 스케일링
            last_close = df["Close"].iloc[-1]
            scale_factor = 0.01 if last_close > 1000 else 1.0

            df["Futures_Close"] = (df["Close"] * scale_factor).round(2)
            df["Change_Pct"] = df["Futures_Close"].pct_change().fillna(0.0).round(2) * 100.0

            # 거래량 및 미결제약정 결측 방어
            vol = df["Volume"] if "Volume" in df.columns else 150000
            df["Volume"] = pd.to_numeric(vol, errors='coerce').fillna(150000).replace(0, 150000).astype(int)

            # 추정 베이시스 및 미결제약정 모델링 (NaN 완전 배제, 실제 값이 아님)
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

            # 최종 NaN 방어 검증
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

    # 비상 더미 데이터 (주말/서버 점검 시에도 절대 NaN 미발생, 반드시 is_estimated=True)
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
# 3. 주체별(외인/기관/개인) 선물 수급 요약
# ==============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def get_krx_investor_derivatives_summary() -> pd.DataFrame:
    """
    ⚠️ 중요 안내: 이 함수는 아직 KRX 실제 투자자별 선물 거래 API와 연동되지 않았습니다.
    아래 수치는 화면 레이아웃 검증을 위한 예시(placeholder) 데이터이며,
    날짜가 바뀌어도 값이 변하지 않는 고정값입니다. 실제 KRX 공시 투자자별 순매수
    데이터가 아니므로 투자 판단에 절대 사용하지 마십시오.

    반환 DataFrame에는 'is_placeholder' 컬럼이 항상 True로 포함되며,
    호출하는 화면(views/krx_cot_view.py)은 이 값을 반드시 확인하여
    사용자에게 경고를 표시해야 합니다.

    TODO: KRX Data Marketplace 또는 자체 보유 증권사 API(KIS/LS)의
    투자자별 선물 거래 실적 엔드포인트가 확보되면 이 함수를 실제 데이터 수집
    로직으로 교체해야 합니다.
    """
    logger.warning(
        "get_krx_investor_derivatives_summary(): 실제 API 미연동, "
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
        "🔵 하방(Short) 베팅"
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
