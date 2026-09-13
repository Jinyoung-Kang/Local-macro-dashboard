"""
tests/test_store.py
저장 계층(services/store.py)과 수집/표시 분리 동작에 대한 테스트.

네트워크를 쓰지 않습니다. DB는 tmp_path에 만들어 테스트 간 격리합니다.
"""
import os
import sys

import pathlib
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """테스트 전용 DB 경로를 환경변수로 지정하고 초기화 캐시를 비웁니다."""
    from services import store

    path = tmp_path / "test.db"
    monkeypatch.setenv("DASHBOARD_DB", str(path))
    monkeypatch.setenv("DASHBOARD_READ_MODE", "auto")
    store._initialized_paths.clear()
    store.init_db()
    yield path
    store._initialized_paths.clear()


# ==============================================================================
# 1. 스냅샷
# ==============================================================================
def test_snapshot_roundtrip(db):
    from services import store

    store.put_snapshot("x", {"a": [1, 2], "ko": "한글"})
    snap = store.read_snapshot("x")

    assert snap is not None
    assert snap.payload == {"a": [1, 2], "ko": "한글"}
    assert snap.age_seconds < 60
    assert snap.is_fresh(60)
    assert "KST" in snap.collected_at_kst_str()


def test_read_missing_snapshot_returns_none(db):
    from services import store

    assert store.read_snapshot("없는키") is None


def test_snapshot_upsert_overwrites(db):
    from services import store

    store.put_snapshot("x", {"v": 1})
    store.put_snapshot("x", {"v": 2})
    assert store.read_snapshot("x").payload == {"v": 2}


def test_frame_roundtrip_preserves_index_and_nan(db):
    from services import store

    idx = pd.date_range("2026-01-01", periods=4, freq="D")
    df = pd.DataFrame({"Close": [1.5, None, 3.5, 4.0], "n": [1, 2, 3, 4]}, index=idx)

    store.put_frame("f", df)
    back = store.read_snapshot("f").payload

    assert isinstance(back, pd.DataFrame)
    assert isinstance(back.index, pd.DatetimeIndex)
    assert bool(back["Close"].isna().iloc[1])
    assert back["Close"].iloc[0] == pytest.approx(1.5)


# ==============================================================================
# 2. 중첩 구조 코덱 (13F / COT)
# ==============================================================================
def test_object_codec_preserves_nested_dataframes_and_tuples(db):
    from services import store

    df = pd.DataFrame(
        {"v": [1.0, None, 3.0]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
    )
    payload = ([(df, {"report_date": "2026-06-30", "total_value": 1.0})], None)

    store.put_object("sec", payload)
    back = store.read_snapshot("sec").payload

    assert isinstance(back, tuple) and len(back) == 2
    history, err = back
    assert err is None
    inner_df, meta = history[0]
    assert isinstance(inner_df, pd.DataFrame)
    assert isinstance(inner_df.index, pd.DatetimeIndex)
    assert bool(inner_df["v"].isna().iloc[1])
    assert meta["report_date"] == "2026-06-30"


def test_object_codec_does_not_use_pickle(db):
    """
    역직렬화 시 임의 코드가 실행되면 안 됩니다. 저장 포맷은 순수 JSON이어야
    하므로, 저장된 payload가 JSON으로 파싱 가능한지 확인합니다.
    """
    import json

    from services import store

    store.put_object("o", {"df": pd.DataFrame({"a": [1]})})
    with store.connect(readonly=True) as conn:
        raw = conn.execute(
            "SELECT payload FROM snapshots WHERE name = 'o'"
        ).fetchone()["payload"]

    json.loads(raw)  # 예외가 나면 실패


# ==============================================================================
# 3. 시계열 누적
# ==============================================================================
def test_timeseries_accumulates_and_upserts(db):
    from services import store

    store.put_timeseries(
        "fred", "DGS10",
        pd.Series([4.1, 4.2], index=pd.to_datetime(["2026-01-02", "2026-01-03"])),
    )
    # 겹치는 날짜는 갱신, 새 날짜는 추가되어야 합니다.
    store.put_timeseries(
        "fred", "DGS10",
        pd.Series([9.9, 4.3], index=pd.to_datetime(["2026-01-03", "2026-01-06"])),
    )

    out = store.read_timeseries("fred", "DGS10")
    assert list(out.index.strftime("%Y-%m-%d")) == [
        "2026-01-02", "2026-01-03", "2026-01-06",
    ]
    assert out["DGS10"].tolist() == [4.1, 9.9, 4.3]


def test_timeseries_start_date_filter(db):
    from services import store

    idx = pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"])
    store.put_timeseries("d", "s", pd.Series([1.0, 2.0, 3.0], index=idx))

    out = store.read_timeseries("d", "s", start_date="2026-02-01")
    assert len(out) == 2


def test_put_frame_as_timeseries_splits_numeric_columns(db):
    from services import store

    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    df = pd.DataFrame(
        {"WALCL": [1.0, 2.0, 3.0], "label": ["a", "b", "c"]}, index=idx,
    )
    rows = store.put_frame_as_timeseries("liq", df, columns=["WALCL"])

    assert rows == 3
    assert store.read_timeseries("liq", "WALCL")["WALCL"].tolist() == [1.0, 2.0, 3.0]


# ==============================================================================
# 4. 날짜별 레코드 누적 (Naver/Daum 과거 조회 불가 소스의 이력)
# ==============================================================================
def test_observations_accumulate_across_dates(db):
    from services import store

    for d, amount in [("2026-09-11", 120.5), ("2026-09-12", -40.0)]:
        store.put_observations(
            "radar", d,
            [{"종목코드": "005930", "순매수대금(억)": amount, "_entity": "k|005930"}],
            entity_key="_entity",
        )

    assert store.list_observation_dates("radar") == ["2026-09-11", "2026-09-12"]

    hist = store.read_observations("radar")
    assert len(hist) == 2
    assert set(hist["수집일자"]) == {"2026-09-11", "2026-09-12"}


def test_observations_same_entity_same_date_is_updated_not_duplicated(db):
    from services import store

    for amount in (100.0, 200.0):
        store.put_observations(
            "radar", "2026-09-12",
            [{"종목코드": "005930", "v": amount, "_entity": "k|005930"}],
            entity_key="_entity",
        )

    hist = store.read_observations("radar", obs_date="2026-09-12")
    assert len(hist) == 1
    assert hist.iloc[0]["v"] == 200.0


# ==============================================================================
# 5. 읽기 경로: 저장본 우선 / 모드별 동작
# ==============================================================================
def test_cached_or_live_uses_store_and_skips_live(db):
    from services import store

    calls = []

    def live():
        calls.append(1)
        return {"from": "live"}

    first = store.cached_or_live("k", live, max_age_seconds=600)
    assert first == {"from": "live"}
    assert len(calls) == 1

    # 두 번째 호출은 저장본이 신선하므로 live를 부르지 않아야 합니다.
    second = store.cached_or_live("k", live, max_age_seconds=600)
    assert second == {"from": "live"}
    assert len(calls) == 1, "저장본이 신선한데 다시 수집했습니다"


def test_cached_or_live_refetches_when_stale(db):
    from services import store

    calls = []

    def live():
        calls.append(1)
        return {"n": len(calls)}

    store.cached_or_live("k", live, max_age_seconds=600)
    # max_age=0 이면 항상 오래된 것으로 간주됩니다.
    store.cached_or_live("k", live, max_age_seconds=0)
    assert len(calls) == 2


def test_store_only_mode_never_calls_live(db, monkeypatch):
    from services import store

    monkeypatch.setenv("DASHBOARD_READ_MODE", "store_only")

    def live():
        raise AssertionError("store_only 모드에서 수집이 호출되면 안 됩니다")

    assert store.cached_or_live(
        "missing", live, max_age_seconds=600, empty_value={"e": True},
    ) == {"e": True}


def test_live_only_mode_ignores_store(db, monkeypatch):
    from services import store

    store.put_snapshot("k", {"from": "store"})
    monkeypatch.setenv("DASHBOARD_READ_MODE", "live_only")

    assert store.cached_or_live(
        "k", lambda: {"from": "live"}, max_age_seconds=600,
    ) == {"from": "live"}


def test_stale_store_is_served_when_live_fails(db):
    """외부 장애 시 화면이 비는 것보다 오래된 값을 보여주는 편이 낫습니다."""
    from services import store

    store.put_snapshot("k", {"from": "store"})

    def boom():
        raise RuntimeError("외부 API 장애")

    out = store.cached_or_live("k", boom, max_age_seconds=0)
    assert out == {"from": "store"}


def test_live_failure_with_no_store_returns_empty_value(db):
    from services import store

    def boom():
        raise RuntimeError("외부 API 장애")

    assert store.cached_or_live(
        "k", boom, max_age_seconds=0, empty_value=[],
    ) == []


def test_invalid_read_mode_falls_back_to_auto(db, monkeypatch):
    from services import store

    monkeypatch.setenv("DASHBOARD_READ_MODE", "오타모드")
    assert store.get_read_mode() == store.READ_MODE_AUTO


# ==============================================================================
# 6. 스키마 검증 (예전 버전이 저장한 스냅샷이 화면을 죽이지 않아야 한다)
# ==============================================================================
def test_schema_mismatch_discards_snapshot_and_refetches(db):
    from services import store

    # 컬럼이 빠진 "예전 버전" 저장본
    store.put_frame("krx", pd.DataFrame({"Date": [1], "Futures_Close": [2.0]}))

    good = pd.DataFrame({
        "Date": [1], "Futures_Close": [2.0], "Market_Basis": [0.5],
    })
    out = store.cached_or_live(
        "krx", lambda: good,
        max_age_seconds=600,
        as_frame=True,
        required_columns=("Date", "Futures_Close", "Market_Basis"),
    )
    assert "Market_Basis" in out.columns


def test_schema_mismatch_in_store_only_returns_empty(db, monkeypatch):
    from services import store

    store.put_frame("krx", pd.DataFrame({"Date": [1]}))
    monkeypatch.setenv("DASHBOARD_READ_MODE", "store_only")

    out = store.cached_or_live(
        "krx", lambda: pd.DataFrame(),
        max_age_seconds=600,
        as_frame=True,
        required_columns=("Date", "Market_Basis"),
        empty_value=pd.DataFrame(),
    )
    assert out.empty


def test_sec_13f_schema_validator():
    from services.sec_service import _history_schema_ok

    cols = ["name", "cusip", "class", "value", "shares", "weight"]
    good = pd.DataFrame([[*["x", "y", "COM", 1.0, 1, 100.0]]], columns=cols)
    assert _history_schema_ok(([(good, {})], None))

    bad = good.drop(columns=["weight"])
    assert not _history_schema_ok(([(bad, {})], None))

    # 빈 이력은 '데이터 없음'이지 스키마 오류가 아닙니다.
    assert _history_schema_ok(([], None))
    assert not _history_schema_ok("형태가 아예 다름")


# ==============================================================================
# 7. 수집 실행 로그
# ==============================================================================
def test_run_log_records_failures(db):
    from services import store

    run_id = store.start_run()
    store.finish_run(run_id, status="partial", ok_count=2, fail_count=1,
                     detail="scraper: 빈 결과")

    last = store.read_last_run()
    assert last["status"] == "partial"
    assert last["ok_count"] == 2
    assert last["fail_count"] == 1
    assert "scraper" in last["detail"]


def test_store_stats_reports_counts(db):
    from services import store

    store.put_snapshot("a", {"x": 1})
    store.put_timeseries("d", "s", pd.Series([1.0], index=pd.to_datetime(["2026-01-01"])))
    store.put_observations("o", "2026-01-01", [{"_entity": "e", "v": 1}], entity_key="_entity")

    stats = store.store_stats()
    assert stats["exists"]
    assert stats["timeseries_rows"] == 1
    assert stats["observation_rows"] == 1
    assert any(s["name"] == "a" for s in stats["snapshots"])


def test_purge_removes_old_rows_only(db):
    from services import store

    store.put_timeseries(
        "d", "s",
        pd.Series([1.0, 2.0], index=pd.to_datetime(["2000-01-01", "2026-01-01"])),
    )
    store.purge_older_than(365)

    left = store.read_timeseries("d", "s")
    assert len(left) == 1
    assert left.index[0].year == 2026


# ==============================================================================
# 8. 수집기 작업 정의 정합성
# ==============================================================================
def test_collector_tasks_have_valid_groups():
    import collector

    valid = {"fast", "slow", "weekly"}
    assert collector.ALL_TASKS
    for task in collector.ALL_TASKS:
        assert task.speed in valid, f"{task.name}: 알 수 없는 작업군 {task.speed}"
        assert callable(task.fn)
        assert task.description


def test_collector_task_names_are_unique():
    import collector

    names = [t.name for t in collector.ALL_TASKS]
    assert len(names) == len(set(names))


def test_empty_result_is_counted_as_failure():
    """'수집은 됐지만 데이터가 없음'을 성공으로 보고하면 안 됩니다."""
    import collector

    task = collector.Task(
        "t", "fast", lambda: (_ for _ in ()).throw(collector.EmptyResult("없음")),
    )
    ok, detail = task.run()
    assert ok is False
    assert "없음" in detail


def test_dataset_names_are_unique():
    """수집기와 읽기 측이 같은 키를 쓰도록, 이름이 겹치면 안 됩니다."""
    from services import datasets

    fixed = [
        datasets.SNAP_MACRO_COLLECTED,
        datasets.SNAP_SCRAPER_MARKETS,
        datasets.SNAP_FED_LIQUIDITY,
        datasets.SNAP_KRX_FUTURES,
        datasets.SNAP_SECTOR_HISTORY,
        datasets.SNAP_COT_HISTORY,
    ]
    assert len(fixed) == len(set(fixed))

    generated = [
        datasets.snap_fred_series("DGS10"),
        datasets.snap_radar_scanner("KOSPI", "외국인", "순매수", "TODAY"),
        datasets.snap_sec_13f("0001067983", 8),
        datasets.snap_cot_contract("13874A", 166),
        datasets.snap_daum_futures_trend(25),
    ]
    assert len(generated) == len(set(generated))
    assert not set(fixed) & set(generated)


# ==============================================================================
# 9. 수집 실행 상태 판정 (죽은 수집기를 '진행 중'으로 보고하면 안 된다)
# ==============================================================================
def test_dead_pid_running_run_is_reported_interrupted(db):
    """
    [회귀] 수집기가 Ctrl+C·절전·강제종료로 죽으면 status가 'running'에
    영구히 남아 --status가 "진행 중"이라고 거짓 보고했습니다.
    """
    import socket

    from services import store

    run = {
        "status": "running",
        "pid": 999_999_999,                 # 존재하지 않는 PID
        "host": socket.gethostname(),
        "heartbeat_at": store._utc_now_iso(),
        "started_at": store._utc_now_iso(),
    }
    assert store.resolve_run_status(run) == "interrupted"


def test_live_pid_with_fresh_heartbeat_is_running(db):
    import os
    import socket

    from services import store

    run = {
        "status": "running",
        "pid": os.getpid(),                 # 살아 있는 PID (이 테스트 프로세스)
        "host": socket.gethostname(),
        "heartbeat_at": store._utc_now_iso(),
        "started_at": store._utc_now_iso(),
    }
    assert store.resolve_run_status(run) == "running"


def test_stale_heartbeat_is_interrupted_even_if_pid_alive(db):
    """PID가 재사용됐거나 프로세스가 멈춘 경우도 잡아야 합니다."""
    import os
    import socket
    from datetime import datetime, timedelta, timezone

    from services import store

    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=store.STALE_RUN_SECONDS + 60)
    ).isoformat(timespec="seconds")

    run = {
        "status": "running",
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "heartbeat_at": old,
        "started_at": old,
    }
    assert store.resolve_run_status(run) == "interrupted"


def test_finished_run_status_is_passed_through(db):
    from services import store

    assert store.resolve_run_status({"status": "ok"}) == "ok"
    assert store.resolve_run_status({"status": "partial"}) == "partial"
    assert store.resolve_run_status(None) == "none"


def test_mark_stale_runs_interrupted_cleans_db(db):
    import socket

    from services import store

    with store.connect() as conn:
        conn.execute(
            "INSERT INTO collector_runs "
            "(started_at, status, pid, host, heartbeat_at) VALUES (?,?,?,?,?)",
            (store._utc_now_iso(), "running", 999_999_999,
             socket.gethostname(), store._utc_now_iso()),
        )

    assert store.mark_stale_runs_interrupted() == 1
    assert store.read_last_run()["status"] == "interrupted"
    # 멱등: 두 번째 호출은 아무것도 바꾸지 않습니다.
    assert store.mark_stale_runs_interrupted() == 0


# ==============================================================================
# 10. 태스크별 실행 로그
# ==============================================================================
def test_task_run_log_records_each_task(db):
    from datetime import datetime, timezone

    from services import store

    run_id = store.start_run(group_name="fast")
    for task, status, detail in [
        ("scraper_markets", "ok", "10/10 소스"),
        ("krx_futures", "error", "ConnectionError: 끊김"),
        ("sector_history", "empty", "0/20 티커"),
    ]:
        store.record_task_run(
            run_id, task, speed="fast", status=status,
            started_at=datetime.now(timezone.utc), duration_ms=1234,
            detail=detail,
        )

    summary = {t["task"]: t for t in store.read_task_summary()}
    assert summary["krx_futures"]["status"] == "error"
    assert "ConnectionError" in summary["krx_futures"]["detail"]
    assert summary["sector_history"]["status"] == "empty"
    assert summary["scraper_markets"]["status"] == "ok"


def test_task_summary_returns_only_latest_per_task(db):
    from datetime import datetime, timezone

    from services import store

    for status in ("error", "ok"):
        store.record_task_run(
            None, "krx_futures", speed="slow", status=status,
            started_at=datetime.now(timezone.utc), duration_ms=10,
            detail=status,
        )

    rows = [t for t in store.read_task_summary() if t["task"] == "krx_futures"]
    assert len(rows) == 1
    assert rows[0]["status"] == "ok", "가장 최근 결과를 써야 합니다"


def test_task_history_filters_by_task(db):
    from datetime import datetime, timezone

    from services import store

    for task in ("a", "b", "a"):
        store.record_task_run(
            None, task, speed="fast", status="ok",
            started_at=datetime.now(timezone.utc), duration_ms=1,
        )

    assert len(store.read_task_history("a")) == 2
    assert len(store.read_task_history()) == 3


# ==============================================================================
# 11. 누락 데이터셋 탐지 (있는 것만 보여주면 빠진 걸 알 수 없다)
# ==============================================================================
def test_missing_datasets_reports_absent_expected_keys(db):
    from services import datasets, store

    # 아무것도 없으면 기대 목록 전체가 누락으로 나와야 합니다.
    all_missing = store.missing_datasets()
    assert len(all_missing) > 20
    names = {m["name"] for m in all_missing}
    assert datasets.SNAP_KRX_FUTURES in names
    assert datasets.SNAP_SECTOR_HISTORY in names
    assert datasets.SNAP_COT_HISTORY in names

    # 하나 채우면 그 항목만 목록에서 빠집니다.
    store.put_snapshot(datasets.SNAP_KRX_FUTURES, {"x": 1})
    after = {m["name"] for m in store.missing_datasets()}
    assert datasets.SNAP_KRX_FUTURES not in after
    assert datasets.SNAP_SECTOR_HISTORY in after


def test_missing_datasets_entries_have_human_labels(db):
    from services import store

    for m in store.missing_datasets():
        assert m["label"] and not m["label"].startswith("krx.")


# ==============================================================================
# 12. 스키마 마이그레이션 (기존 사용자 DB에 컬럼 추가)
# ==============================================================================
def test_migration_adds_columns_to_existing_db(tmp_path, monkeypatch):
    """
    이전 버전이 만든 collector_runs(pid/heartbeat 없음)에도
    컬럼이 추가돼야 합니다. CREATE TABLE IF NOT EXISTS는 컬럼을
    추가해 주지 않습니다.
    """
    import sqlite3

    from services import store

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE collector_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            ok_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            detail TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO collector_runs (started_at, status) VALUES ('x', 'ok')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DASHBOARD_DB", str(path))
    store._initialized_paths.clear()
    store.init_db()

    with store.connect(readonly=True) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(collector_runs)")}
    store._initialized_paths.clear()

    assert {"pid", "host", "heartbeat_at", "group_name"} <= cols
    # 기존 데이터는 보존돼야 합니다.
    assert cols >= {"started_at", "status"}


# ==============================================================================
# 13. SEC 13F 최적화
# ==============================================================================
def test_sec_rate_limiter_respects_cap_under_concurrency():
    """SEC 초당 요청 한도를 병렬 상황에서도 넘지 않아야 합니다."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from services.sec_service import _SEC_MAX_RPS, _sec_rate_limit

    n = 24
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: _sec_rate_limit(), range(n)))
    rps = n / (time.perf_counter() - start)

    assert rps <= _SEC_MAX_RPS * 1.25, f"한도 초과: {rps:.1f} req/s"


def test_q1_is_derived_from_q8_snapshot_without_network(db, monkeypatch):
    """
    q1은 q8의 앞부분과 동일하므로 재수집하면 안 됩니다
    (수집기 기준 기관당 요청 2회 절약).
    """
    import pandas as pd

    from services import datasets, store
    import services.sec_service as sec

    cols = ["name", "cusip", "class", "value", "shares", "weight"]
    hist8 = [
        (pd.DataFrame([[f"N{i}", "c", "COM", 1.0, 1, 100.0]], columns=cols),
         {"report_date": f"2026-0{i}-30"})
        for i in range(1, 9)
    ]
    store.put_object(datasets.snap_sec_13f("CIK1", 8), (hist8, None))

    called = []
    monkeypatch.setattr(
        sec, "collect_sec_13f_multi_quarters",
        lambda c, q: (called.append((c, q)), ([], "네트워크"))[1],
    )

    sec.fetch_sec_13f_multi_quarters.clear()
    history, err = sec.fetch_sec_13f_multi_quarters("CIK1", 1)
    sec.fetch_sec_13f_multi_quarters.clear()

    assert len(history) == 1
    assert history[0][1]["report_date"] == "2026-01-30"
    assert called == [], "q8 저장본이 있으면 네트워크를 타지 않아야 합니다"


def test_full_quarters_request_does_not_self_derive(db, monkeypatch):
    """q8 요청은 q8에서 유도할 수 없으므로 정상 경로를 타야 합니다."""
    import services.sec_service as sec

    assert sec._derive_from_longer_snapshot("CIK1", 8) is None


# ==============================================================================
# 14. 수집기 루프 순서 / 락
# ==============================================================================
def test_loop_runs_fast_group_before_weekly():
    """
    [회귀] weekly(13F, 10분+)를 먼저 돌려 fast 데이터가 늦게 채워졌습니다.
    싼 것부터 처리해야 기동 직후 화면이 빨리 쓸만해집니다.
    """
    import inspect

    import collector

    src = inspect.getsource(collector.run_loop)
    assert 'for group in ("fast", "slow", "weekly")' in src


def test_process_lock_blocks_second_instance(tmp_path, monkeypatch):
    """수집기 중복 실행은 외부 소스를 두 배로 호출하므로 막아야 합니다."""
    import collector
    from services import store

    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "x.db"))
    store._initialized_paths.clear()

    with collector.process_lock():
        # 같은 프로세스 재진입은 허용 (PID가 같으므로)
        lock_file = collector._lock_path()
        assert lock_file.exists()

        # 다른 살아 있는 PID가 들고 있는 것처럼 위조
        import os

        lock_file.write_text(f"{os.getppid()} now\n")
        with pytest.raises(collector.AlreadyRunning):
            with collector.process_lock():
                pass

        # --force 면 통과해야 합니다
        lock_file.write_text(f"{os.getppid()} now\n")
        with collector.process_lock(force=True):
            pass

    store._initialized_paths.clear()


def test_process_lock_reclaims_dead_holder(tmp_path, monkeypatch):
    import collector
    from services import store

    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "y.db"))
    store._initialized_paths.clear()

    lock_file = collector._lock_path()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text("999999999 stale\n")      # 죽은 PID

    with collector.process_lock():
        pass                                        # 예외 없이 회수돼야 함

    store._initialized_paths.clear()


# ==============================================================================
# 15. df.attrs 보존 (추정치 경고가 저장 왕복에서 사라지면 안 된다)
# ==============================================================================
def test_frame_attrs_survive_store_roundtrip(db):
    """
    [회귀] put_frame은 to_json(orient="split")을 쓰는데 이 포맷은 df.attrs를
    보존하지 않습니다. attrs에는 is_proxy / source_label 같은 "이 값은 실제
    지표가 아니다" 표시가 들어 있어서, 잃어버리면 추정치가 공식 데이터처럼
    화면과 AI 리포트에 나갑니다.
    """
    from services import store

    df = pd.DataFrame(
        {"Close": [100.0, 101.0]}, index=pd.date_range("2026-01-01", periods=2)
    )
    df.attrs.update({
        "is_proxy": True,
        "source_label": "^TNX 변동성 기반 추정치",
        "is_intraday": False,
    })

    store.put_frame("move", df)
    back = store.read_snapshot("move").payload

    assert back.attrs.get("is_proxy") is True
    assert back.attrs.get("source_label") == "^TNX 변동성 기반 추정치"
    assert back.attrs.get("is_intraday") is False


def test_nested_frame_attrs_survive_object_codec(db):
    from services import store

    df = pd.DataFrame({"v": [1.0]})
    df.attrs["is_proxy"] = True

    store.put_object("nested", {"MOVE": {"data": df}})
    back = store.read_snapshot("nested").payload["MOVE"]["data"]

    assert back.attrs.get("is_proxy") is True


def test_non_scalar_attrs_are_dropped_not_crashing(db):
    """직렬화 불가한 attrs 값이 있어도 저장이 실패하면 안 됩니다."""
    from services import store

    df = pd.DataFrame({"v": [1.0]})
    df.attrs.update({"ok": "yes", "bad": object(), "nested": {"a": 1}})

    store.put_frame("x", df)
    back = store.read_snapshot("x").payload

    assert back.attrs.get("ok") == "yes"
    assert "bad" not in back.attrs


# ==============================================================================
# 16. 변동성 지수: 긴 저장본에서 기간 슬라이싱
# ==============================================================================
def test_volatility_period_slicing_avoids_network(db, monkeypatch):
    """
    ^VIX/^MOVE는 5y로 한 번 저장하고 짧은 기간은 잘라 씁니다.
    store_only에서 네트워크를 타면 안 됩니다.
    """
    import numpy as np

    from services import datasets, store
    import services.macro_service as ms

    idx = pd.date_range(end="2026-09-12", periods=1300, freq="B")
    store.put_frame(
        datasets.snap_ticker_history("^VIX", datasets.VOLATILITY_STORE_PERIOD),
        pd.DataFrame({"Close": np.linspace(12, 34, len(idx))}, index=idx),
    )

    calls = []
    monkeypatch.setattr(
        ms, "collect_ticker_data",
        lambda s, p: (calls.append((s, p)), pd.DataFrame())[1],
    )

    lengths = {}
    for period in ("3mo", "1y", "5y"):
        ms.fetch_ticker_data.clear()
        lengths[period] = len(ms.fetch_ticker_data("^VIX", period=period))
    ms.fetch_ticker_data.clear()

    assert calls == [], "저장본이 있으면 네트워크를 타지 않아야 합니다"
    assert lengths["3mo"] < lengths["1y"] < lengths["5y"]


def test_non_volatility_ticker_is_not_store_backed(db, monkeypatch):
    """티커가 많아 전부 저장할 이유가 없으므로, 나머지는 직접 수집합니다."""
    import services.macro_service as ms

    calls = []
    monkeypatch.setattr(
        ms, "collect_ticker_data",
        lambda s, p: (calls.append((s, p)), pd.DataFrame({"Close": [1.0]}))[1],
    )
    ms.fetch_ticker_data.clear()
    ms.fetch_ticker_data("^GSPC", period="5d")
    ms.fetch_ticker_data.clear()

    assert calls == [("^GSPC", "5d")]


# ==============================================================================
# 17. 심화 매크로 지표
# ==============================================================================
def test_advanced_series_definitions_are_complete():
    from services.advanced_macro_service import (
        ADVANCED_SERIES,
        ADVANCED_SERIES_IDS,
    )

    assert set(ADVANCED_SERIES_IDS) == set(ADVANCED_SERIES)
    for sid, meta in ADVANCED_SERIES.items():
        for key in ("label", "unit", "digits", "group", "why", "source"):
            assert meta.get(key) is not None, f"{sid}: {key} 누락"


@pytest.mark.parametrize(
    "value,expected",
    [(0.8, "정상"), (0.2, "평탄"), (-0.35, "역전"), (-0.9, "깊은 역전")],
)
def test_t10y3m_interpretation(value, expected):
    from services.advanced_macro_service import interpret_t10y3m

    assert interpret_t10y3m(value)[0] == expected


@pytest.mark.parametrize(
    "value,expected",
    [(-0.5, "마이너스"), (0.6, "완화적"), (1.5, "중립"), (2.5, "긴축적")],
)
def test_real_rate_interpretation(value, expected):
    from services.advanced_macro_service import interpret_real_rate

    assert interpret_real_rate(value)[0] == expected


def test_advanced_indicators_read_from_store(db):
    """심화 지표는 FRED 저장본을 그대로 씁니다 (별도 네트워크 없음)."""
    import numpy as np

    from services import datasets, store
    from services.advanced_macro_service import (
        ADVANCED_SERIES_IDS,
        get_advanced_macro_indicators,
    )

    idx = pd.date_range(end="2026-09-12", periods=300, freq="B")
    for sid in ADVANCED_SERIES_IDS:
        store.put_frame(
            datasets.snap_fred_series(sid),
            pd.DataFrame({sid: np.linspace(0.5, 1.5, len(idx))}, index=idx),
        )

    get_advanced_macro_indicators.clear()
    result = get_advanced_macro_indicators()
    get_advanced_macro_indicators.clear()

    latest = result["latest"]
    assert set(latest) == set(ADVANCED_SERIES_IDS)
    for sid in ADVANCED_SERIES_IDS:
        assert latest[sid]["available"], f"{sid} 사용 불가"
        assert latest[sid]["value"] == pytest.approx(1.5, abs=0.01)


def test_advanced_summary_flags_missing_series():
    from services.advanced_macro_service import summarize_advanced_for_ai

    result = {
        "latest": {
            "T10Y3M": {"label": "10Y-3M", "available": False},
            "DFII10": {
                "label": "실질금리", "available": True, "value": 2.1,
                "digits": 3, "unit": "%", "delta": 0.01,
                "status": "긴축적", "percentile": 95.0,
            },
        },
        "derived": {},
    }
    text = summarize_advanced_for_ai(result)
    assert "수집 실패" in text
    assert "긴축적" in text


# ==============================================================================
# 18. 대시보드 스냅샷: 수급 레이더 / 참고 시세 / 심화 지표 포함
# ==============================================================================
def test_snapshot_includes_radar_and_scraper_and_advanced():
    """
    [회귀] "전체 대시보드 원본 데이터"에 국내 수급 레이더가 빠져 있어서
    AI 리포트가 국내 수급을 전혀 보지 못했습니다.
    """
    from services.dashboard_snapshot_service import format_dashboard_snapshot_text

    radar = pd.DataFrame([{
        "종목명": "삼성전자", "종목코드": "005930",
        "순매수대금(억)": 512.3, "등락률(%)": 1.8,
        "데이터_출처": "Daum 실시간",
    }])

    text = format_dashboard_snapshot_text({
        "collected_at": "2026-09-13 05:20:00 KST",
        "macro": ({}, None, None, None, None),
        "radar_foreign": radar,
        "radar_inst": radar,
        "scraper": {
            "updated_at": "05:08 KST",
            "items": [{
                "name": "미국채 10년물", "provider": "TradingView", "unit": "%",
                "status": "ok", "price": 4.969, "previous_close": 4.955,
                "change_pct": 0.28,
            }],
        },
        "advanced": {
            "latest": {"T10Y3M": {
                "label": "10Y-3M", "available": True, "value": -0.35,
                "digits": 3, "unit": "%p", "delta": -0.01,
                "status": "역전", "percentile": 5.0,
            }},
            "derived": {},
        },
    })

    assert "국내 수급 레이더" in text
    assert "삼성전자(005930)" in text
    assert "비공식 스크래핑 참고 시세" in text
    assert "심화 매크로 지표" in text
    assert "역전" in text


def test_snapshot_macro_section_accepts_list_payload():
    """저장 계층을 거치면 튜플이 리스트로 돌아옵니다."""
    from services.dashboard_snapshot_service import format_dashboard_snapshot_text

    payload = [
        {"통화": [{"name": "원/달러", "status": "ok", "price_str": "1,341.05",
                  "delta_str": "-0.29", "prev_str": "1,341.34"}]},
        4.969, 4.955, 4.630, 4.620,
    ]
    text = format_dashboard_snapshot_text({"macro": payload})

    assert "거시 지표 수집 실패" not in text
    assert "원/달러" in text
    assert "10Y-2Y 스프레드" in text


# ==============================================================================
# 19. 미국채 전일 종가 FRED 폴백
# ==============================================================================
def test_bond_previous_close_falls_back_to_fred(db):
    """
    [회귀] TradingView bonds scanner는 현재 수익률만 주고, Symbol Scanner·
    HTML 파서도 전일 종가를 못 주는 경우가 있어 미국채 카드가 계속
    "전일 종가 N/A"였습니다. FRED DGS는 미 재무부 공식 일별 확정치라
    직전 영업일 값이 곧 전일 종가입니다.
    """
    import numpy as np

    from services import datasets, store
    import services.macro_service as ms

    idx = pd.date_range(end="2026-09-11", periods=60, freq="B")
    for sid, last in (("DGS2", 4.615), ("DGS10", 4.951), ("DGS30", 5.340)):
        store.put_frame(
            datasets.snap_fred_series(sid),
            pd.DataFrame({sid: np.linspace(last - 0.3, last, len(idx))},
                         index=idx),
        )

    ms.fetch_fred_series.clear()
    assert ms.get_bond_previous_close_from_fred("us02y") == pytest.approx(4.615)
    assert ms.get_bond_previous_close_from_fred("us10y") == pytest.approx(4.951)
    assert ms.get_bond_previous_close_from_fred("us30y") == pytest.approx(5.340)
    assert ms.get_bond_previous_close_from_fred("없는키") is None
    ms.fetch_fred_series.clear()


def test_bond_override_uses_fred_when_scraper_lacks_previous(db, monkeypatch):
    import numpy as np

    from services import datasets, store
    import services.macro_service as ms
    import services.market_scraper_service as mss

    idx = pd.date_range(end="2026-09-11", periods=60, freq="B")
    store.put_frame(
        datasets.snap_fred_series("DGS10"),
        pd.DataFrame({"DGS10": np.linspace(4.6, 4.951, len(idx))}, index=idx),
    )

    monkeypatch.setattr(mss, "get_scraped_macro_markets", lambda: {"items": [{
        "key": "us10y", "status": "ok", "price": 4.969,
        "previous_close": None, "provider": "TradingView Scanner",
    }]})

    collected = {"국채": [{
        "name": "미국채 10년물 수익률(%) :gray[[TradingView 참고]]",
        "status": "ok",
    }]}

    ms.fetch_fred_series.clear()
    out, r10c, r10p, _, _ = ms._apply_bond_scanner_override(
        collected, None, None, None, None,
    )
    ms.fetch_fred_series.clear()

    item = out["국채"][0]
    assert item["prev_str"] != "N/A"
    assert item["prev_source"] == "FRED 공식 확정치"
    assert item["delta"] == pytest.approx(4.969 - 4.951, abs=1e-6)
    assert r10c == pytest.approx(4.969)
    assert r10p == pytest.approx(4.951)


def test_bond_override_keeps_na_when_no_source_has_previous(db, monkeypatch):
    """어느 출처도 전일값이 없으면 0.00%로 위장하지 않아야 합니다."""
    import services.macro_service as ms
    import services.market_scraper_service as mss

    monkeypatch.setattr(mss, "get_scraped_macro_markets", lambda: {"items": [{
        "key": "us02y", "status": "ok", "price": 4.630,
        "previous_close": None, "provider": "TradingView Scanner",
    }]})
    monkeypatch.setattr(ms, "get_bond_previous_close_from_fred", lambda k: None)

    collected = {"국채": [{
        "name": "미국채 2년물 수익률(%) :gray[[TradingView 참고]]",
        "status": "ok",
    }]}
    out, *_ = ms._apply_bond_scanner_override(collected, None, None, None, None)

    item = out["국채"][0]
    assert item["prev_str"] == "N/A"
    assert item["delta"] is None
    assert item["delta_str"] == "N/A"


# ==============================================================================
# 20. 심화 지표 카드 표시 순서
# ==============================================================================
def test_advanced_display_order_leads_with_recession_signal():
    """
    가나다순이면 "기대인플레이션"이 맨 앞에 옵니다. 이 화면의 머리기사는
    침체 신호(10Y-3M)이므로 순서를 명시적으로 고정합니다.
    """
    from services.advanced_macro_service import (
        ADVANCED_DISPLAY_ORDER,
        ADVANCED_SERIES_IDS,
    )

    assert ADVANCED_DISPLAY_ORDER[0] == "T10Y3M"
    assert set(ADVANCED_DISPLAY_ORDER) == set(ADVANCED_SERIES_IDS)
    # 실질금리와 기대인플레는 짝이므로 붙어 있어야 읽기 좋습니다.
    i_real = ADVANCED_DISPLAY_ORDER.index("DFII10")
    i_bei = ADVANCED_DISPLAY_ORDER.index("T10YIE")
    assert abs(i_real - i_bei) == 1


# ==============================================================================
# 21. dtype 보존 — 종목코드/cusip의 앞자리 0이 사라지면 안 된다
# ==============================================================================
def test_leading_zero_stock_codes_survive_store_roundtrip(db):
    """
    [회귀] to_json(orient="split")은 dtype을 저장하지 않아, 읽을 때 pandas가
    타입을 추론합니다. 숫자로만 이루어진 문자열 컬럼이 int로 바뀌면서
    "069500" → 69500, "005930" → 5930 으로 앞자리 0이 사라졌습니다.

    그 결과 Daum(A69500)·pykrx·yfinance(69500.KS) 조회가 모두 실패하고
    "가격/거래량 기반 추정치로 대체" 경로로 빠졌습니다.
    """
    from services import store

    df = pd.DataFrame([
        {"순위": 1, "종목코드": "069500", "종목명": "KODEX 200"},
        {"순위": 2, "종목코드": "005930", "종목명": "삼성전자"},
        {"순위": 3, "종목코드": "000660", "종목명": "SK하이닉스"},
    ])

    store.put_frame("radar", df)
    back = store.read_snapshot("radar").payload

    assert back["종목코드"].tolist() == ["069500", "005930", "000660"]
    assert back["종목코드"].dtype == object
    # 숫자 컬럼은 숫자로 남아야 합니다.
    assert pd.api.types.is_integer_dtype(back["순위"])


def test_leading_zero_cusip_survives_object_codec(db):
    """13F cusip도 9자리 숫자 문자열이라 같은 문제를 겪습니다."""
    from services import store

    cols = ["name", "cusip", "class", "value", "shares", "weight"]
    df = pd.DataFrame([
        ["APPLE INC", "037833100", "COM", 1.2e9, 5e6, 8.1],
        ["MICROSOFT", "594918104", "COM", 9.9e8, 3e6, 6.5],
    ], columns=cols)

    store.put_object("sec", ([(df, {"report_date": "2026-06-30"})], None))
    history, _ = store.read_snapshot("sec").payload

    assert history[0][0]["cusip"].tolist() == ["037833100", "594918104"]


def test_numeric_columns_keep_numeric_dtype(db):
    """dtype 보존이 숫자 컬럼을 문자열로 만들어서도 안 됩니다."""
    from services import store

    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    df = pd.DataFrame(
        {"Close": [1.5, 2.5, 3.5], "Volume": [100, 200, 300]}, index=idx,
    )
    store.put_frame("t", df)
    back = store.read_snapshot("t").payload

    assert pd.api.types.is_float_dtype(back["Close"])
    assert pd.api.types.is_integer_dtype(back["Volume"])
    assert isinstance(back.index, pd.DatetimeIndex)


def test_legacy_snapshot_without_dtypes_does_not_infer(db):
    """
    dtype 정보가 없는 예전 저장본은 추론을 꺼서, 최소한 문자열이
    숫자로 바뀌지는 않게 합니다.
    """
    import json

    from services import store

    payload = {
        "__frame__": json.dumps({
            "columns": ["종목코드"],
            "index": [0, 1],
            "data": [["069500"], ["005930"]],
        }),
        "index_is_datetime": False,
        # dtypes 키 없음 (예전 버전이 저장한 형태)
    }
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO snapshots (name, payload, kind, status, collected_at) "
            "VALUES ('legacy', ?, 'frame', 'ok', ?)",
            (json.dumps(payload), store._utc_now_iso()),
        )

    back = store.read_snapshot("legacy").payload
    assert back["종목코드"].tolist() == ["069500", "005930"]


# ==============================================================================
# 22. 미국채 보강이 bonds scanner 실패와 무관해야 한다
# ==============================================================================
def test_symbol_scanner_previous_close_used_when_bonds_scanner_fails(monkeypatch):
    """
    [회귀] 미국채 보강 로직 전체가 `if scanner_yields:` 안에 있어서,
    bonds scanner가 실패하면(현재 TradingView가 d=[]를 반환) Symbol Scanner로
    받아 둔 전일 종가까지 통째로 버려졌습니다.
    """
    import services.market_scraper_service as m

    monkeypatch.setattr(m, "_fetch_tradingview_us_treasury_yields", lambda: {})
    monkeypatch.setattr(
        m, "_fetch_tradingview_symbol_snapshot",
        lambda symbol: (4.969, 4.951, 0.018, 0.36),
    )
    monkeypatch.setattr(m, "_collect_one_market", lambda cfg: {
        "key": cfg["key"], "name": cfg["name"], "url": cfg["url"],
        "provider": cfg["provider"], "unit": cfg["unit"],
        "status": "fail", "price": None, "previous_close": None,
        "change": None, "change_pct": None, "error": "테스트",
    })

    result = m.get_scraped_macro_markets.__wrapped__()
    items = {i["key"]: i for i in result["items"]}

    for key in m.TREASURY_KEYS:
        assert items[key]["status"] == "ok", f"{key}: Symbol Scanner 값이 버려졌습니다"
        assert items[key]["previous_close"] == pytest.approx(4.951)
        assert items[key]["change_pct"] is not None
        assert items[key]["prev_source"] == "TradingView Symbol Scanner"


def test_treasury_keeps_na_when_all_sources_lack_previous(monkeypatch):
    """어느 출처도 전일값이 없으면 0.00%로 위장하지 않아야 합니다."""
    import services.market_scraper_service as m

    monkeypatch.setattr(m, "_fetch_tradingview_us_treasury_yields", lambda: {})
    monkeypatch.setattr(
        m, "_fetch_tradingview_symbol_snapshot",
        lambda symbol: (4.630, None, None, None),
    )
    monkeypatch.setattr(m, "_collect_one_market", lambda cfg: {
        "key": cfg["key"], "name": cfg["name"], "url": cfg["url"],
        "provider": cfg["provider"], "unit": cfg["unit"],
        "status": "fail", "price": None, "previous_close": None,
        "change": None, "change_pct": None, "error": None,
    })

    items = {i["key"]: i for i in m.get_scraped_macro_markets.__wrapped__()["items"]}

    assert items["us02y"]["price"] == pytest.approx(4.630)
    assert items["us02y"]["previous_close"] is None
    assert items["us02y"]["change_pct"] is None


# ==============================================================================
# 23. KRX 투자자 수급: 실데이터 우선, 폴백은 명확히 표시
# ==============================================================================
def test_krx_investor_prefers_real_daum_data(monkeypatch):
    """
    [회귀] 스냅샷이 get_krx_investor_derivatives_summary()를 곧바로 불렀는데,
    그 함수는 항상 고정 예시를 반환하는 최종 폴백입니다. Daum 실데이터가
    정상인데도 AI 리포트에 매번 가짜 수치가 들어가고 있었습니다.
    """
    import services.dashboard_snapshot_service as dss

    real = pd.DataFrame([{
        "투자 주체": "외국인", "당일 순매수": 100,
        "5일 누적": 200, "20일 누적": 300,
    }])
    monkeypatch.setattr(
        dss, "fetch_daum_futures_investor_trend", lambda d: real,
    )

    called = []
    monkeypatch.setattr(
        dss, "get_krx_investor_derivatives_summary",
        lambda: (called.append(1), pd.DataFrame())[1],
    )

    out = dss._collect_krx_investor_trend()
    assert not out["is_placeholder"].any()
    assert out["20일 누적"].iloc[0] == 300
    assert called == [], "실데이터가 있는데 폴백을 불렀습니다"


def test_krx_investor_falls_back_and_marks_placeholder(monkeypatch):
    import services.dashboard_snapshot_service as dss

    monkeypatch.setattr(
        dss, "fetch_daum_futures_investor_trend",
        lambda d: pd.DataFrame(),
    )
    out = dss._collect_krx_investor_trend()

    assert out is not None and not out.empty
    assert out["is_placeholder"].all()


def test_snapshot_labels_placeholder_vs_real_differently():
    from services.dashboard_snapshot_service import _append_krx_section

    real = pd.DataFrame([{
        "투자 주체": "외국인", "20일 누적": 38500, "is_placeholder": False,
    }])
    fake = real.assign(is_placeholder=True)

    real_lines: list[str] = []
    _append_krx_section(real_lines, None, real)
    fake_lines: list[str] = []
    _append_krx_section(fake_lines, None, fake)

    assert "Daum 금융" in "\n".join(real_lines)
    assert "placeholder" not in "\n".join(real_lines)
    assert "placeholder" in "\n".join(fake_lines)
    assert "판단 근거로 쓰지 마세요" in "\n".join(fake_lines)


# ==============================================================================
# 24. 지표 이름 정제 — 중첩 대괄호에서 "]"가 남으면 안 된다
# ==============================================================================
@pytest.mark.parametrize("raw,expected", [
    ("달러 인덱스 (DXY) :gray[[실시간]]", "달러 인덱스 (DXY)"),
    ("미국채 2년물 수익률(%) :gray[[TradingView 참고]]", "미국채 2년물 수익률(%)"),
    ("WTI 원유 ($) :gray[[15분 지연]]", "WTI 원유 ($)"),
    ("💵 통화 및 환율 :gray[(실시간)]", "💵 통화 및 환율"),
    ("태그 없는 이름", "태그 없는 이름"),
])
def test_clean_tag_ui_removes_nested_brackets(raw, expected):
    """
    [회귀] `:gray\\[.*?\\]`를 먼저 적용하면 non-greedy가 첫 번째 `]`에서
    멈춰 `:gray[[실시간]`까지만 지우고 닫는 `]` 하나가 남았습니다.
    화면 선택 목록에 "달러 인덱스 (DXY) ]" 처럼 표시됐습니다.
    """
    from services.macro_service import clean_tag_ui

    assert clean_tag_ui(raw) == expected


def test_no_bracket_residue_in_any_configured_indicator():
    from config import MACRO_CATEGORIES
    from services.macro_service import clean_tag_ui

    names = [n for items in MACRO_CATEGORIES.values() for n in items]
    assert names, "설정된 지표가 없습니다"

    for name in names:
        cleaned = clean_tag_ui(name)
        assert "[" not in cleaned and "]" not in cleaned, (
            f"대괄호 잔존: {name!r} -> {cleaned!r}"
        )
        assert cleaned.strip() == cleaned


def test_snapshot_tag_cleaner_delegates_to_single_implementation():
    """같은 로직이 두 곳에 복제돼 한쪽만 고쳐지는 일이 없어야 합니다."""
    from config import MACRO_CATEGORIES
    from services.dashboard_snapshot_service import clean_ui_tag
    from services.macro_service import clean_tag_ui

    names = [n for items in MACRO_CATEGORIES.values() for n in items]
    for name in names:
        assert clean_ui_tag(name) == clean_tag_ui(name)


# ==============================================================================
# 25. KRX 금액(억원) 기준 — 계약수를 1e8로 나눠 0이 되면 안 된다
# ==============================================================================
# 25. KRX 금액(억원) 기준 제거 — Daum이 제공하지 않는 모드였다
# ==============================================================================
def test_daum_futures_trend_has_no_measure_parameter():
    """
    [회귀] "수급 표시 기준: 금액(억원)"은 type=PRICE 파라미터가 먹힌다는
    가정 위에 있었습니다. 실제로는 계약수가 그대로 돌아왔고, 그 값을
    1억으로 나눠 화면의 금액이 전부 0.0이 됐습니다(사용자 신고).
    모드 자체를 제거했으므로 measure 인자가 되살아나면 안 됩니다.
    """
    import inspect
    from services import datasets
    from services.krx_service import (
        collect_daum_futures_investor_trend,
        fetch_daum_futures_investor_trend,
    )

    for fn in (collect_daum_futures_investor_trend,
               fetch_daum_futures_investor_trend):
        params = inspect.signature(fn).parameters
        assert "measure" not in params, f"{fn.__name__}에 measure가 남아 있습니다"

    assert "measure" not in inspect.signature(
        datasets.snap_daum_futures_trend
    ).parameters


def test_daum_futures_trend_returns_contract_counts(monkeypatch):
    """
    계약수는 1억으로 나누지 않고 그대로(정수) 나와야 합니다.
    예전 금액 모드에서 3450 계약 → 0.0이 되던 것이 버그의 실체였습니다.
    """
    import services.krx_service as krx

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [
                {"date": "2026-09-11", **{
                    field: 3450 for _, field in krx.DAUM_FUTURES_CATEGORY_MAP
                }},
            ]}

    monkeypatch.setattr(krx, "get_session", lambda: type(
        "S", (), {"get": staticmethod(lambda *a, **k: _Resp())},
    )())

    df = krx.collect_daum_futures_investor_trend(25)

    assert not df.empty
    assert df["data_measure"].iloc[0] == "CONTRACT"
    assert df["data_unit"].iloc[0] == "계약"
    assert "measure_fallback_reason" not in df.columns
    assert df["당일 순매수"].iloc[0] == 3450, "계약수가 0으로 뭉개졌습니다"


def test_krx_view_has_no_amount_radio():
    """화면에서도 금액(억원) 선택지가 사라져야 합니다."""
    source = pathlib.Path("views/krx_cot_view.py").read_text(encoding="utf-8")

    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert '"금액(억원)"' not in body
    assert "수급 표시 기준" not in body


# ==============================================================================
# 26. 분봉이 정체됐을 때 전일 종가를 일봉에서 보완한다
# ==============================================================================
def _daily_frame(values, start="2026-09-07"):
    idx = pd.date_range(start=start, periods=len(values), freq="D")
    return pd.DataFrame({"Close": values}, index=idx)


def test_previous_close_from_daily_skips_current_trading_day():
    """
    현재가가 속한 거래일은 건너뛰고 '그 앞' 거래일 종가를 골라야 합니다.
    """
    import services.macro_service as ms

    daily = _daily_frame([8.60, 8.65, 8.71])   # 09-07, 09-08, 09-09
    ms.fetch_ticker_data.clear()

    orig = ms.fetch_ticker_data
    try:
        ms.fetch_ticker_data = lambda symbol, period="1mo": daily
        # 현재가 시각이 09-09이면 09-08 종가가 전일 종가입니다.
        assert ms.get_previous_close_from_daily(
            "JPYKRW=X", pd.Timestamp("2026-09-09 06:28"),
        ) == pytest.approx(8.65)
        # 기준 시각을 안 주면 끝에서 두 번째 값
        assert ms.get_previous_close_from_daily("JPYKRW=X") == pytest.approx(8.65)
    finally:
        ms.fetch_ticker_data = orig


def test_previous_close_from_daily_returns_none_when_too_short():
    import services.macro_service as ms

    orig = ms.fetch_ticker_data
    try:
        ms.fetch_ticker_data = lambda symbol, period="1mo": _daily_frame([8.71])
        assert ms.get_previous_close_from_daily("JPYKRW=X") is None

        ms.fetch_ticker_data = lambda symbol, period="1mo": pd.DataFrame()
        assert ms.get_previous_close_from_daily("JPYKRW=X") is None
    finally:
        ms.fetch_ticker_data = orig


def test_jpy_krw_prev_close_recovered_from_daily_bars(monkeypatch):
    """
    [회귀] 엔/원 100엔당 카드가 '전일 종가: N/A'로 표시되던 문제.

    주말·비유동 시간대에는 1분봉 피드가 마지막 봉을 그대로 반복해서
    마지막 두 봉의 종가가 같아집니다. 그러면 전일 대비를 계산할 수 없는데,
    예전에는 이미 알고 있는 전일 종가까지 버려 N/A가 됐습니다.
    이제 일봉에서 직전 거래일 종가를 가져와 채웁니다.
    """
    import services.macro_service as ms

    # 마지막 두 봉의 종가가 완전히 같은 정체된 분봉
    stale = pd.DataFrame(
        {"Close": [8.71, 8.71]},
        index=pd.to_datetime(["2026-09-12 06:27", "2026-09-12 06:28"]),
    )
    stale.attrs["is_intraday"] = True

    daily = _daily_frame([8.60, 8.65, 8.71], start="2026-09-10")

    def _fake_fetch(symbol, period="1mo"):
        return stale if period == "5d" else daily

    monkeypatch.setattr(ms, "fetch_ticker_data", _fake_fetch)
    monkeypatch.setattr(
        ms, "MACRO_CATEGORIES",
        {"통화": {"엔/원 100엔당 (JPY/KRW) :gray[[실시간]]": "JPYKRW=X"}},
    )
    monkeypatch.setattr(ms, "_apply_bond_scanner_override", lambda *a, **k: a)

    collected = ms.collect_macro_data()[0]
    item = collected["통화"][0]

    assert item["status"] == "ok"
    # 8.71 < 50 이므로 100엔당으로 환산됩니다.
    assert item["price_str"] == "871.00"
    # 전일 종가가 N/A가 아니라 일봉의 직전 거래일(8.65 → 865.00)이어야 합니다.
    assert item["prev_str"] == "865.00", item["prev_str"]
    assert item["prev_source"] == "일봉 직전 거래일 종가"
    # 배율이 현재가에만 적용돼 100배 틀어지면 안 됩니다.
    assert item["delta"] == pytest.approx(6.0, abs=0.01)
    assert item["delta_str"].startswith("+6.00")


def test_single_bar_recovers_prev_close_from_daily(monkeypatch):
    """
    분봉이 딱 한 개만 오는 날에도 같은 원인으로 '전일 데이터 없음'이 됩니다.
    일봉에서 직전 거래일 종가를 찾으면 정상 카드로 승격돼야 합니다.
    """
    import services.macro_service as ms

    one_bar = pd.DataFrame(
        {"Close": [8.71]},
        index=pd.to_datetime(["2026-09-12 06:28"]),
    )
    one_bar.attrs["is_intraday"] = True
    daily = _daily_frame([8.60, 8.65, 8.71], start="2026-09-10")

    monkeypatch.setattr(
        ms, "fetch_ticker_data",
        lambda symbol, period="1mo": one_bar if period == "5d" else daily,
    )
    monkeypatch.setattr(
        ms, "MACRO_CATEGORIES",
        {"통화": {"엔/원 100엔당 (JPY/KRW) :gray[[실시간]]": "JPYKRW=X"}},
    )
    monkeypatch.setattr(ms, "_apply_bond_scanner_override", lambda *a, **k: a)

    item = ms.collect_macro_data()[0]["통화"][0]
    assert item["status"] == "ok"
    assert item["price_str"] == "871.00"
    assert item["prev_str"] == "865.00"
    assert item["prev_source"] == "일봉 직전 거래일 종가"


def test_single_bar_stays_na_without_daily_fallback(monkeypatch):
    """일봉에서도 못 찾으면 0.00%로 위장하지 않고 N/A로 남아야 합니다."""
    import services.macro_service as ms

    one_bar = pd.DataFrame(
        {"Close": [8.71]}, index=pd.to_datetime(["2026-09-12 06:28"]),
    )
    one_bar.attrs["is_intraday"] = True

    monkeypatch.setattr(
        ms, "fetch_ticker_data",
        lambda symbol, period="1mo": one_bar if period == "5d" else pd.DataFrame(),
    )
    monkeypatch.setattr(
        ms, "MACRO_CATEGORIES",
        {"통화": {"엔/원 100엔당 (JPY/KRW) :gray[[실시간]]": "JPYKRW=X"}},
    )
    monkeypatch.setattr(ms, "_apply_bond_scanner_override", lambda *a, **k: a)

    item = ms.collect_macro_data()[0]["통화"][0]
    assert item["status"] == "single"
    assert item["prev_str"] == "N/A"
    assert item["delta"] is None


def test_stale_intraday_stays_na_without_daily_fallback(monkeypatch):
    """정체된 분봉 + 일봉 폴백 실패 → 여전히 N/A (0.00% 위장 금지)."""
    import services.macro_service as ms

    stale = pd.DataFrame(
        {"Close": [8.71, 8.71]},
        index=pd.to_datetime(["2026-09-12 06:27", "2026-09-12 06:28"]),
    )
    stale.attrs["is_intraday"] = True

    monkeypatch.setattr(
        ms, "fetch_ticker_data",
        lambda symbol, period="1mo": stale if period == "5d" else pd.DataFrame(),
    )
    monkeypatch.setattr(
        ms, "MACRO_CATEGORIES",
        {"통화": {"엔/원 100엔당 (JPY/KRW) :gray[[실시간]]": "JPYKRW=X"}},
    )
    monkeypatch.setattr(ms, "_apply_bond_scanner_override", lambda *a, **k: a)

    item = ms.collect_macro_data()[0]["통화"][0]
    assert item["delta"] is None
    assert item["prev_str"] == "N/A"
    assert "prev_source" not in item
