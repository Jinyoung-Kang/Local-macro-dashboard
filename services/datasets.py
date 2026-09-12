"""
services/datasets.py
저장 계층에서 쓰는 데이터셋 이름과 신선도 기준의 단일 출처.

수집기(collector.py)와 읽기 측(services/*.py)이 **같은 문자열**을 써야
하므로, 양쪽에서 문자열을 직접 타이핑하지 않고 여기서 가져갑니다.
(한쪽 오타 때문에 "수집은 되는데 화면에는 안 보이는" 상황을 막습니다.)

MAX_AGE는 "이 시간보다 오래된 저장본이면 직접 수집도 허용한다"는 기준입니다.
수집 주기보다 넉넉하게 잡아야 수집기가 막 돌기 직전에 화면이 불필요하게
직접 수집하는 일이 없습니다. (권장: 수집 주기의 2~3배)
"""
from __future__ import annotations

# ==============================================================================
# 스냅샷 (최신 상태 1건)
# ==============================================================================
SNAP_MACRO_COLLECTED = "macro.collected"          # 매크로 카드 + 2Y/10Y 금리
SNAP_SCRAPER_MARKETS = "macro.scraper_markets"    # TradingView/Yahoo 참고 시세
SNAP_FED_LIQUIDITY = "liquidity.fed_net"          # 연준 순유동성 (DataFrame)
SNAP_KRX_FUTURES = "krx.futures_history"          # KOSPI200 선물 시계열 (DataFrame)
SNAP_SECTOR_HISTORY = "sector.etf_history"        # 섹터/자산군 ETF 종가 (dict)
SNAP_COT_HISTORY = "cot.multi_asset"              # CFTC COT (DataFrame 중첩)


# Daum 선물 투자주체별 매매동향 (조회 조건별)
def snap_daum_futures_trend(lookback_days: int, measure: str) -> str:
    return f"krx.daum_futures_trend.d{lookback_days}.{measure}"


# CFTC COT는 계약 코드별로 저장합니다 (views/cot_view.py가 자산별로 조회).
def snap_cot_contract(contract_code: str, limit: int) -> str:
    return f"cot.contract.{contract_code}.l{limit}"


# SEC 13F는 기관(CIK)·분기수 조합마다 결과가 다릅니다.
def snap_sec_13f(cik: str, max_quarters: int) -> str:
    return f"sec.13f.{cik}.q{max_quarters}"

# FRED 개별 시계열은 series_id별로 스냅샷을 따로 둡니다.
def snap_fred_series(series_id: str) -> str:
    return f"fred.series.{series_id}"


# 수급 레이더는 조회 조건 조합마다 결과가 달라지므로 키에 조건을 포함합니다.
def snap_radar_scanner(
    market: str,
    investor: str,
    trade_type: str,
    interval_type: str,
) -> str:
    return f"radar.scanner.{market}.{investor}.{trade_type}.{interval_type}"


# ==============================================================================
# 누적 이력 (timeseries / observations)
# ==============================================================================
TS_FRED = "fred"                    # series_id = FRED 시리즈 ID
TS_KRX_FUTURES = "krx_futures"      # series_id = 종가/거래량/미결제약정
TS_LIQUIDITY = "fed_liquidity"      # series_id = WALCL/WTREGEN/RRP_M/Net_Liquidity_M

OBS_RADAR = "radar_ranking"         # 날짜별 수급 상위 종목 (과거 조회 불가 소스)


# ==============================================================================
# 신선도 기준 (초)
# ==============================================================================
# 장중 시세성 데이터: 수집기를 5분 주기로 돌리는 것을 전제로 15분
MAX_AGE_REALTIME = 15 * 60

# 일별 확정치(FRED/KRX 마감): 수집기를 1시간 주기로 돌려도 충분
MAX_AGE_DAILY = 6 * 60 * 60

# 분기 공시(13F) 등 거의 변하지 않는 데이터
MAX_AGE_SLOW = 24 * 60 * 60


__all__ = [
    "SNAP_MACRO_COLLECTED",
    "SNAP_SCRAPER_MARKETS",
    "SNAP_FED_LIQUIDITY",
    "SNAP_KRX_FUTURES",
    "SNAP_SECTOR_HISTORY",
    "SNAP_COT_HISTORY",
    "snap_sec_13f",
    "snap_cot_contract",
    "snap_daum_futures_trend",
    "snap_fred_series",
    "snap_radar_scanner",
    "TS_FRED",
    "TS_KRX_FUTURES",
    "TS_LIQUIDITY",
    "OBS_RADAR",
    "MAX_AGE_REALTIME",
    "MAX_AGE_DAILY",
    "MAX_AGE_SLOW",
]
