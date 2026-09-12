"""
services/cot_service.py
CFTC COT(Commitments of Traders) 데이터 수집 엔진
S&P 500 외 6대 주요 자산(주식, 채권, 환율, 원자재) 3년 시계열 병렬 수집, 재시도(Retry) 적용 및 AI 요약
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from services import datasets, store
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import requests

# 공용 커넥션 풀 세션을 사용해 요청마다 TCP/TLS 핸드셰이크를
# 반복하지 않습니다 (services/http_client.py).
from services.http_client import get_session
import streamlit as st

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. 다중 자산 COT 코드 매핑
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

class CFTCTransientError(RuntimeError):
    pass


def _cftc_get_with_retry(url: str, params: dict, max_attempts: int = 3):
    """일시적인 Connection Reset 방어를 위한 재시도 래퍼"""
    last_error = None
    for attempt in range(max_attempts):
        try:
            response = get_session().get(
                url,
                params=params,
                timeout=(5, 30),
                headers={
                    "User-Agent": (
                        "macro-dashboard-v2/1.0 "
                        "(data research contact: admin@example.com)"
                    )
                },
            )
            response.raise_for_status()
            return response, None
        except requests.RequestException as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
    return None, str(last_error)


def collect_cftc_cot_legacy(contract_code: str, limit: int = 300) -> pd.DataFrame:
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "cftc_contract_market_code": contract_code,
        "$limit": limit,
        "$order": "report_date_as_yyyy_mm_dd DESC"
    }
    
    res, error = _cftc_get_with_retry(url, params)
    
    if error:
        raise CFTCTransientError(f"CFTC 재시도 후 실패: {error}")
    
    try:
        data = res.json()
        if not data:
            raise CFTCTransientError("결과 없음")
        
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
            raise CFTCTransientError("파싱 오류")
        
        df["nc_net"] = df["nc_long"] - df["nc_short"]
        df["comm_net"] = df["comm_long"] - df["comm_short"]
        df["nr_net"] = df["nr_long"] - df["nr_short"]
        
        return df
    except CFTCTransientError:
        raise
    except Exception as e:
        raise CFTCTransientError(str(e))


# ==============================================================================
# 2. 다중 자산 3년 시계열 병렬 수집 및 AI Context 요약 헬퍼
# ==============================================================================
@st.cache_data(ttl=3600*12, show_spinner=False)
def fetch_cftc_cot_legacy(contract_code: str, limit: int = 300) -> pd.DataFrame:
    """
    화면용 진입점 (views/cot_view.py가 자산별로 직접 호출합니다).

    CFTC는 주 1회(금요일) 발표이므로 저장본으로 충분합니다. CFTC 공개 API는
    응답이 느리고 간헐적으로 막히는데(CFTCTransientError), 저장해 두면
    그 영향을 받지 않습니다.

    [중요] 원래 예외(CFTCTransientError)를 화면이 잡아 안내 문구를 띄우므로,
    저장본이 전혀 없을 때는 예외를 그대로 올려보내야 합니다.
    """
    snap_name = datasets.snap_cot_contract(contract_code, limit)
    mode = store.get_read_mode()

    if mode == store.READ_MODE_LIVE_ONLY:
        return collect_cftc_cot_legacy(contract_code, limit)

    snap = store.read_snapshot(snap_name)
    has_stored = (
        snap is not None
        and isinstance(snap.payload, pd.DataFrame)
        and not snap.payload.empty
    )

    if has_stored and snap.is_fresh(datasets.MAX_AGE_SLOW):
        return snap.payload

    if mode == store.READ_MODE_STORE_ONLY:
        if has_stored:
            return snap.payload
        return pd.DataFrame()

    try:
        df = collect_cftc_cot_legacy(contract_code, limit)
    except Exception:
        if has_stored:
            logger.info(
                "%s: CFTC 수집 실패, 저장본으로 대체합니다 (수집 시각 %s)",
                contract_code, snap.collected_at_kst_str(),
            )
            return snap.payload
        # 저장본도 없으면 화면이 안내를 띄울 수 있게 예외를 그대로 전달합니다.
        raise

    if df is not None and not df.empty:
        try:
            store.put_frame(snap_name, df)
        except Exception as e:
            logger.warning("COT 저장 실패 (%s): %s", contract_code, e)
    return df


def collect_cot_multi_asset_history(years: int = 3, max_workers: int = 4) -> dict:
    """
    6개 COT 자산의 최근 N년 주간 데이터를 병렬 수집.
    반환: {자산명: {"data": DataFrame, "error": str}}
    """
    weeks = int(years * 52 + 10)
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                collect_cftc_cot_legacy,
                info["code"],
                weeks
            ): asset_name
            for asset_name, info in COT_ASSETS.items()
        }

        for future in as_completed(futures):
            asset_name = futures[future]

            try:
                df = future.result()
                results[asset_name] = {"data": df, "error": None}
            except CFTCTransientError as e:
                results[asset_name] = {"data": pd.DataFrame(), "error": str(e)}
            except Exception as e:
                results[asset_name] = {"data": pd.DataFrame(), "error": str(e)}

    return results


# ==============================================================================
# 저장본 우선 읽기 경로
# ==============================================================================
def fetch_cot_multi_asset_history(years: int = 3, max_workers: int = 4) -> dict:
    """
    화면용 진입점. COT는 CFTC가 **주 1회(금요일)** 발표하므로 저장본으로
    충분하고, 자산마다 별도 요청이 필요해 수집이 느립니다.

    반환: {자산명: {"data": DataFrame | None, "error": str | None}}
    """
    def _collect():
        return collect_cot_multi_asset_history(years=years, max_workers=max_workers)

    payload = store.cached_or_live(
        datasets.SNAP_COT_HISTORY,
        _collect,
        max_age_seconds=datasets.MAX_AGE_SLOW,
        as_object=True,
        empty_value={},
    )
    return payload if isinstance(payload, dict) else {}


def summarize_cot_asset(asset_name: str, df: pd.DataFrame) -> str:
    """기본 리포트 모드용 1/4/13주 변화 및 백분위 요약 + 신선도(age_days) 표시 반영"""
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
    
    # COT 데이터 기준일 및 수집 시점 대비 지연 일수 계산
    date_val = latest["date"]
    cot_date = date_val.date() if hasattr(date_val, "date") else pd.to_datetime(date_val).date()
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    age_days = (today - cot_date).days

    date_text = cot_date.strftime("%Y-%m-%d")

    return (
        f"- {asset_name} (기준일 {date_text}, 수집 시점 대비 {age_days}일 전 주간 공시)\n"
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
