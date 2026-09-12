"""
tests/test_store.py
저장 계층(services/store.py)과 수집/표시 분리 동작에 대한 테스트.

네트워크를 쓰지 않습니다. DB는 tmp_path에 만들어 테스트 간 격리합니다.
"""
import os
import sys

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
        datasets.snap_daum_futures_trend(25, "CONTRACT"),
    ]
    assert len(generated) == len(set(generated))
    assert not set(fixed) & set(generated)
