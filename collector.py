#!/usr/bin/env python3
"""
collector.py
대시보드 데이터 수집기 (Streamlit과 분리된 독립 프로세스).

[왜 분리하는가]
기존에는 사용자가 화면을 열 때 수집이 시작돼, TTL이 만료된 순간 접속한
사람이 전체 수집 시간을 그대로 기다렸습니다. 이 스크립트가 미리 수집해
SQLite에 적재하면 Streamlit은 읽기만 하므로 체감 로딩이 수 ms로 떨어집니다.

[사용법]

    # 1회 수집 (전체)
    python collector.py

    # 빠른 것만 / 느린 것만
    python collector.py --only fast
    python collector.py --only slow

    # 상주 모드 (5분마다 fast, 1시간마다 slow)
    python collector.py --loop

    # 저장 상태 확인
    python collector.py --status

[주기 권장값]
  fast (시세·수급): 5분    — 장중에 의미 있는 갱신 주기
  slow (FRED·KRX 마감·13F): 1시간 — 일별/주별 확정치라 더 자주 받을 이유가 없음

[macOS 자동 실행 (launchd)]
  python collector.py --install-launchd  로 plist 예시를 출력합니다.

[중요]
이 스크립트는 Streamlit 런타임 밖에서 돕니다. services/*.py가 @st.cache_data를
쓰고 있어 "No runtime found" 경고가 나오지만 동작에는 문제가 없습니다
(메모리 캐시로 자동 폴백). 경고는 기본적으로 숨깁니다.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time as time_module
import traceback
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

# 이 스크립트는 Streamlit 런타임이 없으므로, 수집 함수들이 저장 계층을
# 거치지 않고 항상 실제 수집을 하도록 강제합니다.
os.environ.setdefault("DASHBOARD_READ_MODE", "live_only")

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Streamlit의 "No runtime found" 경고는 이 맥락에서 정상이므로 낮춥니다.
logging.getLogger("streamlit").setLevel(logging.ERROR)

logger = logging.getLogger("collector")

KST = ZoneInfo("Asia/Seoul")

DEFAULT_FAST_INTERVAL = 5 * 60
DEFAULT_SLOW_INTERVAL = 60 * 60
DEFAULT_WEEKLY_INTERVAL = 12 * 60 * 60


# ==============================================================================
# 수집 작업 정의
# ==============================================================================
class EmptyResult(Exception):
    """
    수집 자체는 예외 없이 끝났지만 쓸 수 있는 데이터가 없을 때 사용합니다.

    이 경우를 성공으로 보고하면 "✅ 0/10 수집"처럼 모순된 로그가 남고,
    cron/launchd가 장애를 알아채지 못합니다. 반드시 실패로 집계합니다.
    """


class Task:
    """수집 작업 하나. 실패해도 다른 작업을 막지 않습니다."""

    def __init__(self, name: str, speed: str, fn, description: str = ""):
        self.name = name
        self.speed = speed          # "fast" | "slow"
        self.fn = fn
        self.description = description

    def run(self, run_id: int | None = None) -> tuple[bool, str]:
        """
        태스크를 1회 실행하고 결과를 DB(collector_task_runs)에 남깁니다.

        태스크 단위 기록이 없으면 "성공 0 · 실패 3"만 보이고 어떤 작업이 왜
        실패했는지 알 수 없습니다. 로그는 터미널을 닫으면 사라지므로,
        --status로 언제든 다시 볼 수 있게 DB에 남깁니다.
        """
        from services import store

        started_wall = datetime.now(timezone.utc)
        started = time_module.perf_counter()

        status = "ok"
        ok = True
        detail = ""

        try:
            detail = self.fn() or ""
        except EmptyResult as e:
            # 예외 없이 끝났지만 데이터가 없음 → 실패로 집계(기존 저장본은 보존)
            status, ok, detail = "empty", False, f"수집 결과 없음: {e}"
        except Exception as e:
            status, ok = "error", False
            detail = f"{type(e).__name__}: {e}"
            logger.debug(traceback.format_exc())

        elapsed = time_module.perf_counter() - started
        icon = {"ok": "✅", "empty": "⚠️ ", "error": "❌"}[status]
        log = logger.info if ok else logger.warning
        log("  %s %-26s %6.2fs  %s", icon, self.name, elapsed, detail)

        store.record_task_run(
            run_id, self.name,
            speed=self.speed,
            status=status,
            started_at=started_wall,
            duration_ms=int(elapsed * 1000),
            detail=detail,
        )
        return ok, detail


# ------------------------------------------------------------------ fast tasks
def _task_scraper_markets() -> str:
    from services import datasets, store
    from services.market_scraper_service import collect_scraped_macro_markets

    result = collect_scraped_macro_markets()
    items = result.get("items", [])
    ok = sum(1 for i in items if i.get("status") == "ok")

    if not ok:
        # 한 건도 못 받았으면 기존 저장본을 빈 값으로 덮어쓰지 않습니다.
        # (네트워크 일시 장애로 어제 받아 둔 데이터를 날리면 안 됩니다.)
        raise EmptyResult(f"0/{len(items)} 소스 — 기존 저장본 유지")

    store.put_snapshot(datasets.SNAP_SCRAPER_MARKETS, result, status="ok")
    return f"{ok}/{len(items)} 소스 수집"


def _task_macro_collected() -> str:
    from services import datasets, store
    from services.macro_service import collect_macro_data

    payload = collect_macro_data()
    collected = payload[0] if payload else {}

    if not isinstance(collected, dict):
        raise EmptyResult("예상치 못한 반환 형태")

    total = sum(len(v) for v in collected.values())
    # collected에는 수집 실패 항목도 status="fail"로 들어옵니다.
    # 전체 개수를 세면 "21개 지표 수집"처럼 잘못된 성공 보고가 됩니다.
    usable = sum(
        1 for items in collected.values()
        for item in items
        if isinstance(item, dict) and item.get("status") in ("ok", "single")
    )

    if not usable:
        raise EmptyResult(f"0/{total} 지표 — 기존 저장본 유지")

    store.put_snapshot(datasets.SNAP_MACRO_COLLECTED, list(payload), status="ok")
    return f"{usable}/{total} 지표 수집"


def _task_radar_rankings() -> str:
    """
    수급 레이더는 조건 조합이 많습니다. 화면 기본값 조합만 미리 받아 둡니다.
    (전부 받으면 수집 시간이 과도하게 길어집니다.)
    """
    from services import datasets, store
    from services.radar_service import (
        collect_market_radar_scanner,
        _accumulate_radar_history,
    )

    combos = [
        ("KOSPI", "외국인", "순매수", "TODAY"),
        ("KOSPI", "기관", "순매수", "TODAY"),
        ("KOSPI", "외국인", "순매도", "TODAY"),
    ]

    today = datetime.now(KST).date()
    ok = 0
    for market, investor, trade_type, interval in combos:
        df = collect_market_radar_scanner(
            today, market, investor, trade_type, 30, interval,
        )
        if df is None or df.empty:
            # 빈 결과로 저장본을 덮지 않습니다.
            logger.info(
                "    수급 조합 빈 결과(저장본 유지): %s/%s/%s/%s",
                market, investor, trade_type, interval,
            )
            continue

        name = datasets.snap_radar_scanner(market, investor, trade_type, interval)
        store.put_frame(name, df)
        _accumulate_radar_history(df, market, investor, trade_type, interval)
        ok += 1

    if not ok:
        raise EmptyResult(f"0/{len(combos)} 조합 — 기존 저장본 유지")

    return f"{ok}/{len(combos)} 조합 수집"


# ------------------------------------------------------------------ slow tasks
def _task_fred_series() -> str:
    from services import datasets, store
    from services.macro_service import collect_fred_series

    from services.advanced_macro_service import ADVANCED_SERIES_IDS

    # 화면이 실제로 쓰는 시리즈들
    series_ids = [
        "DGS2", "DGS10", "DGS30", "DGS3MO",
        "BAMLH0A0HYM2",     # 하이일드 OAS
        "STLFSI4",          # 금융스트레스
        "CPF3M",            # 3M 금융 CP
        # 심화 지표: T10Y3M(10Y-3M) / DFII10(실질금리) / T10YIE(기대인플레)
        #            BAMLC0A0CM(IG 스프레드) / NFCI(시카고 금융상황)
        *ADVANCED_SERIES_IDS,
    ]

    ok = 0
    rows = 0
    for sid in series_ids:
        df = collect_fred_series(sid, period_years=10)
        if df is None or df.empty:
            # 개별 시리즈가 비면 그 시리즈의 저장본만 건드리지 않고 넘어갑니다.
            logger.info("    FRED 빈 결과(저장본 유지): %s", sid)
            continue
        store.put_frame(datasets.snap_fred_series(sid), df)
        rows += store.put_timeseries(datasets.TS_FRED, sid, df, value_col=sid)
        ok += 1

    if not ok:
        raise EmptyResult(f"0/{len(series_ids)} 시리즈 — 기존 저장본 유지")

    return f"{ok}/{len(series_ids)} 시리즈, 누적 {rows}행"


def _task_fed_liquidity() -> str:
    from services import datasets, store
    from services.liquidity_service import collect_fed_liquidity_data

    df = collect_fed_liquidity_data(10)
    if df is None or df.empty:
        raise EmptyResult("순유동성 빈 결과 — 기존 저장본 유지")

    estimated = "is_estimated" in df.columns and bool(df["is_estimated"].any())
    store.put_frame(
        datasets.SNAP_FED_LIQUIDITY, df,
        status="estimated" if estimated else "ok",
    )

    if estimated:
        # 추정치는 누적 이력에 절대 넣지 않습니다.
        return f"{len(df)}행 (⚠️ 추정치 — 누적 제외)"

    rows = store.put_frame_as_timeseries(
        datasets.TS_LIQUIDITY, df,
        columns=["WALCL", "WTREGEN", "RRP_M", "Net_Liquidity_M"],
    )
    return f"{len(df)}행, 누적 {rows}행"


def _task_krx_futures() -> str:
    from services import datasets, store
    from services.krx_service import collect_krx_futures_history

    df = collect_krx_futures_history(40)
    if df is None or df.empty:
        raise EmptyResult("KRX 선물 빈 결과 — 기존 저장본 유지")

    estimated = "is_estimated" in df.columns and bool(df["is_estimated"].any())
    store.put_frame(
        datasets.SNAP_KRX_FUTURES, df,
        status="estimated" if estimated else "ok",
    )

    if estimated:
        return f"{len(df)}행 (⚠️ 추정치 — 누적 제외)"

    indexed = df.set_index("Date") if "Date" in df.columns else df
    rows = store.put_frame_as_timeseries(datasets.TS_KRX_FUTURES, indexed)
    return f"{len(df)}행, 누적 {rows}행"


def _task_daum_futures_trend() -> str:
    """KRX 화면이 페이지 로드 시 바로 쓰는 Daum 선물 수급을 미리 받습니다."""
    from services import datasets, store
    from services.krx_service import collect_daum_futures_investor_trend

    # views/krx_cot_view.py가 쓰는 기간. 금액(억원) 기준은 Daum이 제공하지
    # 않아 제거됐으므로, 예전의 CONTRACT/PRICE 2회 요청이 1회로 줄었습니다.
    lookback = 25

    df = collect_daum_futures_investor_trend(lookback)
    if df is None or df.empty:
        raise EmptyResult("빈 결과 — 기존 저장본 유지")

    store.put_frame(datasets.snap_daum_futures_trend(lookback), df)
    return f"{len(df)}행"


def _task_volatility_history() -> str:
    """
    ^VIX / ^MOVE 시계열.

    화면이 여러 기간으로 요청하므로 가장 긴 기간(5y)으로 한 번만 저장하고,
    짧은 기간은 화면에서 잘라 씁니다.

    ⚠️ ^MOVE는 Yahoo가 제공하지 않아 ^TNX 변동성에서 역산한 추정치입니다.
    df.attrs["is_proxy"]로 표시되며, 저장 계층이 이 표시를 보존합니다.
    """
    from services import datasets, store
    from services.macro_service import collect_ticker_data

    period = datasets.VOLATILITY_STORE_PERIOD
    ok = 0
    for symbol in ("^VIX", "^MOVE"):
        df = collect_ticker_data(symbol, period)
        if df is None or df.empty:
            logger.info("    변동성 빈 결과(저장본 유지): %s", symbol)
            continue
        store.put_frame(
            datasets.snap_ticker_history(symbol, period), df,
            status="estimated" if df.attrs.get("is_proxy") else "ok",
        )
        ok += 1

    if not ok:
        raise EmptyResult("0/2 지수 — 기존 저장본 유지")

    return f"{ok}/2 지수 ({period})"


def _task_sector_history() -> str:
    from services import datasets, store
    from services.sector_service import (
        ROTATION_ASSET_CLASSES,
        ROTATION_SECTORS,
        collect_etf_history_map,
    )
    from config import ASSET_CLASS_ETFS, SECTOR_ETFS

    tickers = tuple(sorted(set(
        list(SECTOR_ETFS)
        + list(ASSET_CLASS_ETFS)
        + list(ROTATION_SECTORS.values())
        + list(ROTATION_ASSET_CLASSES.values())
        + ["SPY"]
    )))

    # 래퍼(fetch_etf_history_map)가 아니라 실수집 함수를 직접 부릅니다.
    hist = collect_etf_history_map(tickers, period="2y")
    usable = {
        t: df for t, df in (hist or {}).items()
        if df is not None and not df.empty and "Close" in df.columns
    }

    # 종가만 저장합니다(원본 OHLCV 전체는 불필요하게 큽니다).
    payload = {
        t: {
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "close": [
                None if v != v else float(v)      # NaN -> None
                for v in df["Close"].tolist()
            ],
        }
        for t, df in usable.items()
    }
    if not payload:
        raise EmptyResult(f"0/{len(tickers)} 티커 — 기존 저장본 유지")

    store.put_snapshot(datasets.SNAP_SECTOR_HISTORY, payload, status="ok")
    return f"{len(usable)}/{len(tickers)} 티커"


def _task_cot_history() -> str:
    from services import datasets, store
    from services.cot_service import collect_cot_multi_asset_history

    from services.cot_service import COT_ASSETS

    years = 3
    weeks = int(years * 52 + 10)

    result = collect_cot_multi_asset_history(years=years)
    usable = {
        k: v for k, v in (result or {}).items()
        if isinstance(v, dict) and v.get("data") is not None
        and not v["data"].empty
    }

    if not usable:
        raise EmptyResult(f"0/{len(result or {})} 자산 — 기존 저장본 유지")

    # (a) AI 스냅샷 경로용 통합 저장
    store.put_object(datasets.SNAP_COT_HISTORY, result, status="ok")

    # (b) COT 화면은 자산별로 fetch_cftc_cot_legacy()를 호출하므로,
    #     계약 코드별 스냅샷도 함께 적재해야 화면이 빨라집니다.
    per_contract = 0
    for asset_name, info in COT_ASSETS.items():
        entry = usable.get(asset_name)
        if not entry:
            continue
        df = entry.get("data")
        if df is None or df.empty:
            continue
        store.put_frame(
            datasets.snap_cot_contract(info["code"], weeks), df,
        )
        per_contract += 1

    return f"{len(usable)}/{len(result)} 자산 (계약별 {per_contract}건)"


def _task_sec_13f() -> str:
    """
    SEC 13F 수집 — 전체 수집에서 가장 오래 걸리는 작업입니다.

    [최적화 2가지]
    1) q1은 q8의 부분집합입니다. collect_sec_13f_multi_quarters()는 공시를
       최신순으로 훑어 max_quarters개만 자르므로, q8[:1] == q1 입니다.
       따라서 q8만 수집하고 q1은 잘라서 저장합니다 (수집 24건 → 12건).
    2) 기존에는 기관을 순차 처리했습니다. SEC 한도는 초당 10건인데
       time.sleep(0.2) 직렬 방식으로는 초당 5건도 못 썼습니다.
       _sec_rate_limit() 토큰 버킷으로 바꿨으므로 기관을 병렬 처리해도
       전체 합계 한도는 지켜집니다.
    """
    from services import datasets, store
    from services.sec_service import collect_sec_13f_multi_quarters
    from config import INSTITUTIONS

    targets = [
        (name, info["cik"])
        for name, info in INSTITUTIONS.items()
        if info.get("cik")
    ]
    if not targets:
        raise EmptyResult("수집 대상 기관이 없습니다")

    QUARTERS = 8

    def one(item):
        name, cik = item
        history, err = collect_sec_13f_multi_quarters(cik, QUARTERS)
        return name, cik, history, err

    ok = 0
    saved = 0
    errors: list[str] = []

    # SEC 한도는 토큰 버킷이 전역으로 지키므로, 워커 수는 지연 숨기기 용도만
    # 입니다. 과도하게 늘리면 커넥션만 낭비합니다.
    with ThreadPoolExecutor(max_workers=4) as executor:
        for name, cik, history, err in executor.map(one, targets):
            if not history:
                logger.info("    13F 빈 결과(저장본 유지): %s (%s)", name, err)
                errors.append(f"{name}: {err or '빈 결과'}")
                continue

            # q8 전체
            store.put_object(
                datasets.snap_sec_13f(cik, QUARTERS), (history, err), status="ok",
            )
            saved += 1

            # q1은 q8의 첫 분기 = 같은 데이터. 재수집하지 않습니다.
            store.put_object(
                datasets.snap_sec_13f(cik, 1), (history[:1], err), status="ok",
            )
            saved += 1
            ok += 1

    if not ok:
        raise EmptyResult(
            f"0/{len(targets)} 기관 — 기존 저장본 유지"
            + (f" ({errors[0]})" if errors else "")
        )

    detail = f"{ok}/{len(targets)} 기관, 스냅샷 {saved}건 (q8 수집 → q1 유도)"
    if errors:
        detail += f", 실패 {len(errors)}곳"
    return detail


ALL_TASKS: list[Task] = [
    # fast
    Task("scraper_markets", "fast", _task_scraper_markets,
         "TradingView/Yahoo 참고 시세"),
    Task("macro_collected", "fast", _task_macro_collected,
         "매크로 카드 전 지표"),
    Task("radar_rankings", "fast", _task_radar_rankings,
         "국내 수급 랭킹 (이력 누적)"),
    # slow
    Task("fred_series", "slow", _task_fred_series,
         "FRED 금리/신용 시계열 (이력 누적)"),
    Task("fed_liquidity", "slow", _task_fed_liquidity,
         "연준 순유동성 (이력 누적)"),
    Task("krx_futures", "slow", _task_krx_futures,
         "KRX 선물/미결제약정 (이력 누적)"),
    Task("sector_history", "slow", _task_sector_history,
         "섹터·자산군 ETF 종가"),
    Task("volatility_history", "slow", _task_volatility_history,
         "VIX·MOVE 변동성 시계열"),
    Task("cot_history", "slow", _task_cot_history,
         "CFTC COT (주 1회 발표)"),
    Task("daum_futures_trend", "slow", _task_daum_futures_trend,
         "Daum 선물 투자주체별 수급"),
    # 13F는 수집이 가장 오래 걸리므로 별도 군(weekly)으로 분리했습니다.
    Task("sec_13f", "weekly", _task_sec_13f,
         "SEC 13F 기관 포트폴리오 (분기 공시)"),
]


# ==============================================================================
# 실행
# ==============================================================================
# ==============================================================================
# 중복 실행 방지
# ==============================================================================
class AlreadyRunning(Exception):
    """다른 수집기 프로세스가 이미 돌고 있을 때."""


def _lock_path() -> Path:
    from services import store

    return store.get_db_path().parent / "collector.lock"


@contextmanager
def process_lock(force: bool = False):
    """
    수집기 중복 실행을 막습니다.

    두 프로세스가 동시에 돌면 같은 외부 소스를 두 배로 호출하고(레이트리밋
    위험), SQLite 쓰기 경쟁도 늘어납니다. 무엇보다 --status 출력이 뒤섞여
    원인 파악이 어려워집니다.

    락 파일에 PID를 적고, 죽은 프로세스의 락은 자동으로 회수합니다
    (절전/강제종료로 락이 남는 것을 방지).
    """
    from services import store

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            holder = int(path.read_text().split()[0])
        except (ValueError, IndexError, OSError):
            holder = None

        if holder and holder != os.getpid() and store._pid_is_alive(holder):
            if not force:
                raise AlreadyRunning(
                    f"다른 수집기가 이미 실행 중입니다 (PID {holder}).\n"
                    f"  락 파일: {path}\n"
                    f"  그 프로세스를 끝내거나, --force로 무시할 수 있습니다."
                )
            logger.warning("--force: 기존 락(PID %s)을 무시합니다.", holder)
        elif holder:
            logger.info("죽은 프로세스의 락을 회수합니다 (PID %s).", holder)

    path.write_text(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
    try:
        yield
    finally:
        try:
            # 내 락만 지웁니다 (--force로 빼앗긴 경우 남의 락을 지우지 않도록)
            if path.exists() and path.read_text().split()[0] == str(os.getpid()):
                path.unlink()
        except (OSError, IndexError):
            pass


def run_once(
    only: str | None = None,
    task_name: str | None = None,
) -> tuple[int, int]:
    """선택된 작업을 1회 실행합니다. (성공 수, 실패 수)를 반환합니다."""
    from services import store

    if task_name:
        tasks = [t for t in ALL_TASKS if t.name == task_name]
    else:
        tasks = [t for t in ALL_TASKS if only in (None, "all") or t.speed == only]
    if not tasks:
        logger.warning("실행할 작업이 없습니다 (--only %s)", only)
        return 0, 0

    label = task_name or only or "all"
    logger.info("수집 시작 (%s): %d개 작업", label, len(tasks))

    run_id = None
    try:
        run_id = store.start_run(group_name=label)
    except Exception as e:
        logger.warning("실행 로그 기록 실패(수집은 계속합니다): %s", e)

    started = time_module.perf_counter()
    ok_count = 0
    failures: list[str] = []

    for task in tasks:
        success, detail = task.run(run_id)
        if success:
            ok_count += 1
        else:
            failures.append(f"{task.name}: {detail}")
        # 태스크마다 heartbeat를 찍어, 죽은 수집기를 "진행 중"으로
        # 오인하지 않게 합니다 (13F는 한 태스크가 10분 넘게 걸립니다).
        if run_id is not None:
            store.heartbeat_run(run_id)

    elapsed = time_module.perf_counter() - started
    fail_count = len(failures)

    if run_id is not None:
        try:
            store.finish_run(
                run_id,
                status="ok" if fail_count == 0 else (
                    "partial" if ok_count else "fail"
                ),
                ok_count=ok_count,
                fail_count=fail_count,
                detail="; ".join(failures)[:2000] or None,
            )
        except Exception as e:
            logger.warning("실행 로그 마감 실패: %s", e)

    logger.info(
        "수집 완료 (%s): 성공 %d · 실패 %d · %.1fs",
        label, ok_count, fail_count, elapsed,
    )
    return ok_count, fail_count


def run_loop(
    fast_interval: int,
    slow_interval: int,
    weekly_interval: int,
) -> None:
    """상주 모드. 작업군별로 각자의 주기로 실행합니다."""
    logger.info(
        "상주 모드 시작: fast %d초 · slow %d초 · weekly %d초 (Ctrl+C로 종료)",
        fast_interval, slow_interval, weekly_interval,
    )

    # 0.0으로 시작해 기동 직후 전부 한 번 수집합니다.
    next_run = {"fast": 0.0, "slow": 0.0, "weekly": 0.0}
    intervals = {
        "fast": fast_interval,
        "slow": slow_interval,
        "weekly": weekly_interval,
    }

    try:
        while True:
            now = time_module.monotonic()

            # [수정] 예전에는 weekly를 먼저 돌렸습니다. 그런데 13F(weekly)는
            # 10분 이상 걸려서, 기동 직후 가장 자주 보는 fast 데이터가
            # 그만큼 늦게 채워졌습니다. 싼 것부터 처리합니다.
            for group in ("fast", "slow", "weekly"):
                if now >= next_run[group]:
                    run_once(group)
                    next_run[group] = time_module.monotonic() + intervals[group]

            sleep_for = max(
                1.0, min(next_run.values()) - time_module.monotonic()
            )
            time_module.sleep(sleep_for)
    except KeyboardInterrupt:
        logger.info("상주 모드를 종료합니다.")


def print_status(verbose: bool = False) -> None:
    """저장 상태를 출력합니다. '무엇이 왜 실패했는지'가 핵심입니다."""
    from services import store

    stats = store.store_stats()

    print(f"\nDB 경로   : {stats['db_path']}")
    if not stats["exists"]:
        print("상태      : 아직 생성되지 않았습니다. "
              "`python collector.py`를 먼저 실행하세요.\n")
        return

    print(f"DB 크기   : {stats['size_bytes'] / 1024:,.1f} KB")
    print(f"누적 시계열: {stats['timeseries_rows']:,}행")
    print(f"누적 레코드: {stats['observation_rows']:,}행")

    # ----------------------------------------------------------- 최근 수집
    last = stats.get("last_run")
    resolved = stats.get("last_run_status", "none")
    if last:
        started = _fmt_kst(last.get("started_at"))
        finished = _fmt_kst(last.get("finished_at"))

        if resolved == "running":
            beat = store._parse_iso(
                last.get("heartbeat_at") or last.get("started_at")
            )
            mins = (
                (datetime.now(timezone.utc) - beat).total_seconds() / 60
                if beat else 0
            )
            tail = f"진행 중 (마지막 신호 {mins:.1f}분 전, PID {last.get('pid')})"
        elif resolved == "interrupted":
            tail = "⚠️ 비정상 종료 (프로세스가 사라졌거나 신호가 끊겼습니다)"
        else:
            tail = f"{finished or '?'} [{resolved}]"

        print(
            f"최근 수집 : {started} → {tail}  "
            f"성공 {last.get('ok_count', 0)} · 실패 {last.get('fail_count', 0)}"
            + (f" · 대상 {last.get('group_name')}" if last.get("group_name") else "")
        )

    # ------------------------------------------------------ 태스크별 결과
    summary = stats.get("task_summary") or []
    print("\n태스크별 최근 결과:")
    if not summary:
        print("  (기록 없음 — 이 버전 이전에 수집했다면 다시 한 번 실행하세요)")
    else:
        icons = {"ok": "✅", "empty": "⚠️ ", "error": "❌"}
        for t in summary:
            secs = (t.get("duration_ms") or 0) / 1000
            print(
                f"  {icons.get(t['status'], '? ')} "
                f"[{(t.get('speed') or '?'):<6}] {t['task']:<22} "
                f"{secs:7.1f}s  {_fmt_kst(t.get('started_at')) or ''}"
            )
            if t["status"] != "ok" and t.get("detail"):
                print(f"       └─ {t['detail'][:160]}")

    # -------------------------------------------------- 누락된 데이터셋
    missing = store.missing_datasets()
    print(f"\n저장된 스냅샷: {len(stats['snapshots'])}개"
          f" / 누락: {len(missing)}개")

    if missing:
        print("\n⚠️  있어야 하는데 없는 데이터셋:")
        for m in missing[: (None if verbose else 15)]:
            print(f"  - {m['label']:<28} ({m['name']})")
        if not verbose and len(missing) > 15:
            print(f"  ... 외 {len(missing) - 15}개 (--status -v 로 전체 보기)")
        print("\n  해당 태스크가 실패했거나 아직 실행되지 않았습니다.")
        print("  위 '태스크별 최근 결과'에서 ❌/⚠️ 항목을 확인하세요.")

    if verbose and stats["snapshots"]:
        print("\n저장된 스냅샷 상세:")
        now = datetime.now(tz=timezone.utc)
        for snap in stats["snapshots"]:
            collected = snap.get("collected_at") or ""
            try:
                dt = store._parse_iso(collected)
                mins = (now - dt).total_seconds() / 60
                age = f"{mins:8.1f}분 전"
            except (ValueError, TypeError):
                age = "        ?"
            print(f"  {snap['name']:<40} [{snap['status']:<9}] {age}")

    print()


def _fmt_kst(iso: str | None) -> str | None:
    """UTC ISO 문자열을 KST 표시 문자열로."""
    from services import store

    dt = store._parse_iso(iso)
    if dt is None:
        return None
    return dt.astimezone(KST).strftime("%m-%d %H:%M:%S")


def print_launchd_plist(fast_interval: int) -> None:
    """macOS launchd 설정 예시를 출력합니다."""
    project = Path(__file__).resolve().parent
    python_bin = project / "venv" / "bin" / "python"
    label = "com.local.macro-dashboard.collector"

    print(f"""
# ── macOS 자동 실행 설정 (launchd) ──────────────────────────────────────
# 1) 아래 내용을 다음 경로에 저장하세요:
#      ~/Library/LaunchAgents/{label}.plist
# 2) 등록:
#      launchctl load -w ~/Library/LaunchAgents/{label}.plist
# 3) 해제:
#      launchctl unload -w ~/Library/LaunchAgents/{label}.plist
# 4) 로그 확인:
#      tail -f {project}/data/collector.log

<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{project}/collector.py</string>
        <string>--loop</string>
        <string>--fast-interval</string>
        <string>{fast_interval}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{project}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{project}/data/collector.log</string>
    <key>StandardErrorPath</key>
    <string>{project}/data/collector.log</string>
</dict>
</plist>
""")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="대시보드 데이터 수집기 (Streamlit과 분리 실행)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only", choices=["fast", "slow", "weekly", "all"], default="all",
        help=(
            "실행할 작업군 "
            "(fast=시세·수급, slow=FRED·KRX·COT, weekly=SEC 13F)"
        ),
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="상주 모드로 주기 실행",
    )
    parser.add_argument(
        "--fast-interval", type=int, default=DEFAULT_FAST_INTERVAL,
        help=f"fast 작업 주기(초). 기본 {DEFAULT_FAST_INTERVAL}",
    )
    parser.add_argument(
        "--slow-interval", type=int, default=DEFAULT_SLOW_INTERVAL,
        help=f"slow 작업 주기(초). 기본 {DEFAULT_SLOW_INTERVAL}",
    )
    parser.add_argument(
        "--weekly-interval", type=int, default=DEFAULT_WEEKLY_INTERVAL,
        help=(
            "weekly 작업(13F) 주기(초). 기본 "
            f"{DEFAULT_WEEKLY_INTERVAL} (=12시간). 13F는 분기 공시라 "
            "자주 받을 이유가 없습니다."
        ),
    )
    parser.add_argument(
        "--status", action="store_true",
        help="저장 상태만 출력하고 종료",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="--status 출력에 스냅샷 상세와 전체 누락 목록 포함",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="다른 수집기가 실행 중이어도 강제로 진행",
    )
    parser.add_argument(
        "--task", default=None,
        help="특정 태스크만 실행 (--list로 이름 확인)",
    )
    parser.add_argument(
        "--history", default=None, metavar="TASK",
        help="해당 태스크의 실행 이력을 출력하고 종료 ('all'이면 전체)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="수집 작업 목록 출력",
    )
    parser.add_argument(
        "--install-launchd", action="store_true",
        help="macOS launchd plist 예시 출력",
    )
    parser.add_argument(
        "--purge-days", type=int, default=None,
        help="지정 일수보다 오래된 누적 이력 삭제",
    )
    args = parser.parse_args()

    if args.list:
        print("\n수집 작업 목록:")
        for t in ALL_TASKS:
            print(f"  [{t.speed:<4}] {t.name:<20} {t.description}")
        print()
        return 0

    if args.install_launchd:
        print_launchd_plist(args.fast_interval)
        return 0

    if args.status:
        print_status(verbose=args.verbose)
        return 0

    if args.history:
        from services import store

        task = None if args.history == "all" else args.history
        rows = store.read_task_history(task, limit=40)
        if not rows:
            print(f"\n실행 이력이 없습니다: {args.history}\n")
            return 0
        print(f"\n태스크 실행 이력 ({args.history}, 최신순):")
        icons = {"ok": "✅", "empty": "⚠️ ", "error": "❌"}
        for r in rows:
            secs = (r.get("duration_ms") or 0) / 1000
            print(
                f"  {icons.get(r['status'], '? ')} {_fmt_kst(r['started_at'])}  "
                f"{r['task']:<22} {secs:7.1f}s  {(r.get('detail') or '')[:90]}"
            )
        print()
        return 0

    if args.purge_days is not None:
        from services import store
        removed = store.purge_older_than(args.purge_days)
        logger.info("정리 완료: %s", removed)
        return 0

    from services import store

    # 비정상 종료로 'running'에 남아 있던 기록을 먼저 정리합니다.
    try:
        store.mark_stale_runs_interrupted()
    except Exception as e:
        logger.debug("오래된 실행 기록 정리 실패: %s", e)

    try:
        with process_lock(force=args.force):
            if args.loop:
                run_loop(
                    args.fast_interval, args.slow_interval, args.weekly_interval,
                )
                return 0

            if args.task:
                names = {t.name for t in ALL_TASKS}
                if args.task not in names:
                    logger.error(
                        "알 수 없는 태스크: %s (가능: %s)",
                        args.task, ", ".join(sorted(names)),
                    )
                    return 2
                ok, fail = run_once(only=None, task_name=args.task)
            else:
                ok, fail = run_once(args.only)
    except AlreadyRunning as e:
        logger.error("%s", e)
        return 3

    # 전부 실패하면 0이 아닌 종료코드를 줘서 cron/launchd가 알아챌 수 있게 합니다.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
