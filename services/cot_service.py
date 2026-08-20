"""
services/cot_service.py
CFTC COT(Commitments of Traders) 데이터 수집 엔진
S&P 500 외 6대 주요 자산(주식, 채권, 환율, 원자재) 3년 시계열 병렬 수집 및 AI 요약기 추가
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
import streamlit as st

logger = logging.getLogger(__name__)

# ==============================================================================
# [신규] 다중 자산 COT 코드 매핑
# ==============================================================================
COT_ASSETS = {
    "S&P 500 E-Mini": {
        "code": "13874A",
        "category": "주식",
    },
    "NASDAQ 100 E-Mini": {
        "code": "209742",
        "category": "주식",
    },
    "미국 국채 10년물": {
        "code": "043602",
        "category": "채권",
    },
    "달러 인덱스": {
        "code": "098662",
        "category": "통화",
    },
    "WTI 원유": {
        "code": "067651",
        "category": "원자재",
    },
    "금": {
        "code": "088691",
        "category": "원자재",
    },
}

@st.cache_data(ttl=3600*12, show_spinner=False)
def fetch_cftc_cot_legacy(contract_code: str, limit: int = 300) -> tuple:
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "cftc_contract_market_code": contract_code,
        "$limit": limit,
        "$order": "report_date_as_yyyy_mm_dd DESC"
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if not data:
                return pd.DataFrame(), "결과 없음"
            
            records = []
            for row in data:
                try:
                    records.append({
                        "date": pd.to_datetime(row.get("report_date_as_yyyy_mm_dd")),
                        "nc_long": float(row.get("noncomm_positions_long_all", 0)),
                        "nc_short": float(row.get("noncomm_positions_short_all", 0)),
                        "comm_long": float(row.get("comm_positions_long_all", 0)),
                        "comm_short": float(row.get("comm_positions_short_all", 0)),
                        "nr_long": float(row.get("nonrept_positions_long_all", 0)),
                        "nr_short": float(row.get("nonrept_positions_short_all", 0)),
                    })
                except Exception:
                    continue
                    
            df = pd.DataFrame(records)
            if df.empty:
                return df, "파싱 오류"
            
            df["nc_net"] = df["nc_long"] - df["nc_short"]
            df["comm_net"] = df["comm_long"] - df["comm_short"]
            df["nr_net"] = df["nr_long"] - df["nr_short"]
            
            return df, None
        else:
            return pd.DataFrame(), f"HTTP {res.status_code}"
    except Exception as e:
        return pd.DataFrame(), str(e)


# ==============================================================================
# [신규] 다중 자산 3년 시계열 병렬 수집 및 AI Context 요약 헬퍼
# ==============================================================================
@st.cache_data(ttl=3600 * 12, show_spinner=False)
def fetch_cot_multi_asset_history(years: int = 3, max_workers: int = 4) -> dict:
    """
    6개 COT 자산의 최근 N년 주간 데이터를 병렬 수집.
    반환: {자산명: {"data": DataFrame, "error": str}}
    """
    weeks = int(years * 52 + 10)
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                fetch_cftc_cot_legacy,
                info["code"],
                weeks
            ): asset_name
            for asset_name, info in COT_ASSETS.items()
        }

        for future in as_completed(futures):
            asset_name = futures[future]

            try:
                df, error = future.result()
                if error:
                    results[asset_name] = {"data": pd.DataFrame(), "error": error}
                else:
                    results[asset_name] = {"data": df, "error": None}
            except Exception as e:
                results[asset_name] = {"data": pd.DataFrame(), "error": str(e)}

    return results


def summarize_cot_asset(asset_name: str, df: pd.DataFrame) -> str:
    """기본 리포트 모드용 1/4/13주 변화 및 백분위 요약"""
    if df is None or df.empty:
        return f"- {asset_name}: COT 데이터 없음"

    df = df.sort_values("date").copy()

    latest = df.iloc[-1]
    prev_1w = df.iloc[-2] if len(df) >= 2 else latest
    prev_4w = df.iloc[-5] if len(df) >= 5 else latest
    prev_13w = df.iloc[-14] if len(df) >= 14 else latest

    nc_net = float(latest["nc_net"])
    comm_net = float(latest["comm_net"])
    nr_net = float(latest["nr_net"])

    nc_1w = nc_net - float(prev_1w["nc_net"])
    nc_4w = nc_net - float(prev_4w["nc_net"])
    nc_13w = nc_net - float(prev_13w["nc_net"])

    nc_pctile = float(df["nc_net"].rank(pct=True).iloc[-1] * 100)
    date_text = latest["date"].strftime("%Y-%m-%d")

    return (
        f"- {asset_name} (기준일 {date_text})\n"
        f"  - 비상업/스마트머니 순포지션: {nc_net:+,.0f}계약\n"
        f"  - 상업/헤저 순포지션: {comm_net:+,.0f}계약\n"
        f"  - 소액/비보고 순포지션: {nr_net:+,.0f}계약\n"
        f"  - 스마트머니 변화: 1주 {nc_1w:+,.0f}, "
        f"4주 {nc_4w:+,.0f}, 13주 {nc_13w:+,.0f}\n"
        f"  - 3년 표본 내 스마트머니 순포지션 백분위: {nc_pctile:.1f}%"
    )


def cot_history_to_markdown(
    df: pd.DataFrame,
    asset_name: str,
    max_rows: int = 13,
) -> str:
    """
    AI Context용 COT 상세 데이터를 Markdown으로 변환합니다.
    max_rows=13: 약 3개월의 주간 COT 데이터만 넣어 컨텍스트 길이를 최적화합니다.
    """
    if df is None or df.empty:
        return f"\n##### {asset_name}\n데이터 없음\n"

    cols = ["date", "nc_net", "comm_net", "nr_net"]

    out = (
        df[cols]
        .sort_values("date")
        .tail(max_rows)
        .copy()
    )

    lines = [
        f"\n##### {asset_name}",
        "| 날짜 | 스마트머니 순포지션 | 상업 헤저 순포지션 | 소액/비보고 순포지션 |",
        "|---|---:|---:|---:|",
    ]

    for _, row in out.iterrows():
        lines.append(
            f"| {row['date'].strftime('%Y-%m-%d')} | "
            f"{row['nc_net']:+,.0f} | "
            f"{row['comm_net']:+,.0f} | "
            f"{row['nr_net']:+,.0f} |"
        )

    return "\n".join(lines)
