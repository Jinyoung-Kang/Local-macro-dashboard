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
from datetime import datetime
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

    def run(self) -> tuple[bool, str]:
        started = time_module.perf_counter()
        try:
            detail = self.fn()
        except EmptyResult as e:
            elapsed = time_module.perf_counter() - started
            # 예외 없이 끝났지만 데이터가 없음 → 실패로 집계(기존 저장본은 보존)
            logger.warning("  ⚠️  %-26s %6.2fs  수집 결과 없음: %s",
                           self.name, elapsed, e)
            return False, f"수집 결과 없음: {e}"
        except Exception as e:
            elapsed = time_module.perf_counter() - started
            logger.warning("  ❌ %-26s %6.2fs  %s: %s",
                           self.name, elapsed, type(e).__name__, e)
            logger.debug(traceback.format_exc())
            return False, f"{type(e).__name__}: {e}"

        elapsed = time_module.perf_counter() - started
        logger.info("  ✅ %-26s %6.2fs  %s", self.name, elapsed, detail or "")
        return True, detail or ""


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

    # 화면이 실제로 쓰는 시리즈들
    series_ids = [
        "DGS2", "DGS10", "DGS30", "DGS3MO",
        "BAMLH0A0HYM2",     # 하이일드 OAS
        "STLFSI4",          # 금융스트레스
        "CPF3M",            # 3M 금융 CP
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
    """KRX 화면이 페이지 로드 시 바로 쓰는 Daum 선물 수급 조합을 미리 받습니다."""
    from services import datasets, store
    from services.krx_service import collect_daum_futures_investor_trend

    # views/krx_cot_view.py의 기본 선택값 조합
    combos = [(25, "CONTRACT"), (25, "PRICE")]

    ok = 0
    for lookback, measure in combos:
        df = collect_daum_futures_investor_trend(lookback, measure)
        if df is None or df.empty:
            logger.info("    Daum 선물 빈 결과(저장본 유지): d%s/%s", lookback, measure)
            continue
        store.put_frame(
            datasets.snap_daum_futures_trend(lookback, measure), df,
        )
        ok += 1

    if not ok:
        raise EmptyResult(f"0/{len(combos)} 조합 — 기존 저장본 유지")

    return f"{ok}/{len(combos)} 조합"


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
    13F는 기관 12곳 × 분기별 문서 추적이라 가장 느린 작업입니다
    (SEC 초당 10건 제한 준수 때문에 더 느립니다). 화면이 쓰는
    max_quarters 조합(1, 8)을 미리 받아 둡니다.
    """
    from services import datasets, store
    from services.sec_service import collect_sec_13f_multi_quarters
    from config import INSTITUTIONS

    ok = 0
    attempted = 0
    for name, info in INSTITUTIONS.items():
        cik = info.get("cik")
        if not cik:
            continue
        for quarters in (1, 8):
            attempted += 1
            history, err = collect_sec_13f_multi_quarters(cik, quarters)
            if not history:
                logger.info("    13F 빈 결과(저장본 유지): %s q%s (%s)",
                            name, quarters, err)
                continue
            store.put_object(
                datasets.snap_sec_13f(cik, quarters), (history, err), status="ok",
            )
            ok += 1

    if not ok:
        raise EmptyResult(f"0/{attempted} 건 — 기존 저장본 유지")

    return f"{ok}/{attempted} 건 (기관 {len(INSTITUTIONS)}곳)"


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
def run_once(only: str | None = None) -> tuple[int, int]:
    """선택된 작업을 1회 실행합니다. (성공 수, 실패 수)를 반환합니다."""
    from services import store

    tasks = [t for t in ALL_TASKS if only in (None, "all") or t.speed == only]
    if not tasks:
        logger.warning("실행할 작업이 없습니다 (--only %s)", only)
        return 0, 0

    label = only or "all"
    logger.info("수집 시작 (%s): %d개 작업", label, len(tasks))

    run_id = None
    try:
        run_id = store.start_run()
    except Exception as e:
        logger.warning("실행 로그 기록 실패(수집은 계속합니다): %s", e)

    started = time_module.perf_counter()
    ok_count = 0
    failures: list[str] = []

    for task in tasks:
        success, detail = task.run()
        if success:
            ok_count += 1
        else:
            failures.append(f"{task.name}: {detail}")

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

            # 무거운 군을 먼저 처리해, 가벼운 fast가 뒤에서 밀리지 않게 합니다.
            for group in ("weekly", "slow", "fast"):
                if now >= next_run[group]:
                    run_once(group)
                    next_run[group] = time_module.monotonic() + intervals[group]

            sleep_for = max(
                1.0, min(next_run.values()) - time_module.monotonic()
            )
            time_module.sleep(sleep_for)
    except KeyboardInterrupt:
        logger.info("상주 모드를 종료합니다.")


def print_status() -> None:
    """저장 상태를 출력합니다."""
    from services import store

    stats = store.store_stats()

    print(f"\nDB 경로   : {stats['db_path']}")
    if not stats["exists"]:
        print("상태      : 아직 생성되지 않았습니다. `python collector.py`를 먼저 실행하세요.\n")
        return

    print(f"DB 크기   : {stats['size_bytes'] / 1024:.1f} KB")
    print(f"누적 시계열: {stats['timeseries_rows']:,}행")
    print(f"누적 레코드: {stats['observation_rows']:,}행")

    last = stats["last_run"]
    if last:
        print(
            f"최근 수집 : {last['started_at']} → {last.get('finished_at') or '진행 중'} "
            f"[{last['status']}] 성공 {last['ok_count']} · 실패 {last['fail_count']}"
        )
        if last.get("detail"):
            print(f"            실패 상세: {last['detail'][:300]}")

    print("\n저장된 스냅샷:")
    if not stats["snapshots"]:
        print("  (없음)")
    else:
        now = datetime.now(tz=ZoneInfo("UTC"))
        for snap in stats["snapshots"]:
            collected = snap.get("collected_at") or ""
            age = ""
            try:
                dt = datetime.fromisoformat(collected)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                mins = (now - dt).total_seconds() / 60
                age = f"{mins:6.1f}분 전"
            except ValueError:
                age = "        ?"
            print(f"  {snap['name']:<34} [{snap['status']:<9}] {age}")
    print()


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
        print_status()
        return 0

    if args.purge_days is not None:
        from services import store
        removed = store.purge_older_than(args.purge_days)
        logger.info("정리 완료: %s", removed)
        return 0

    if args.loop:
        run_loop(args.fast_interval, args.slow_interval, args.weekly_interval)
        return 0

    ok, fail = run_once(args.only)
    # 전부 실패하면 0이 아닌 종료코드를 줘서 cron/launchd가 알아챌 수 있게 합니다.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
