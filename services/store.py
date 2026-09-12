"""
services/store.py
SQLite 기반 로컬 저장 계층 (수집/표시 분리의 핵심).

[왜 필요한가]
기존 구조는 "사용자가 화면을 열면 그때 수집을 시작"했습니다.
st.cache_data(ttl=30)이 걸려 있어도 TTL이 만료된 순간 접속한 사람이
전체 수집 시간을 그대로 기다립니다. 자동 새로고침을 켜 두면 30초마다
수십 건의 외부 요청이 다시 나갑니다.

이 모듈은 수집 결과를 SQLite 파일 하나에 적재해, Streamlit은 **읽기만**
하도록 분리합니다.

    [collector.py · 주기 실행]  →  [data/dashboard.db]  →  [Streamlit · 읽기]
         느린 외부 수집                  로컬 파일              체감 ~수 ms

부수 효과가 더 큽니다. KRX/Daum/Naver처럼 "과거 날짜 조회를 지원하지
않는" 소스는 지금까지 앱을 끄면 데이터가 사라졌습니다. 이제 수집할 때마다
쌓이므로 **직접 시계열 이력을 축적**할 수 있습니다.

[동시성]
수집기(쓰기)와 Streamlit(읽기)이 같은 파일을 동시에 다루므로 WAL 모드를
사용합니다. WAL에서는 다수 읽기와 단일 쓰기가 서로를 막지 않습니다.
sqlite3 커넥션은 스레드 간 공유가 안전하지 않으므로, 커넥션을 캐시하지 않고
호출마다 열고 닫습니다(로컬 파일이라 비용이 무시할 수준입니다).

[저장 형태]
- snapshots  : "최신 상태" 1건 (매크로 카드, 스크래퍼 결과 등). JSON упsert.
- timeseries : (dataset, series_id, 날짜) → 값. 과거가 누적되는 수치 시계열.
- observations: (dataset, 날짜, 종목) → JSON. 날짜별 다중 컬럼 레코드
               (수급 레이더 랭킹 등)를 이력으로 축적.
- collector_runs: 수집 실행 로그. 화면에 "최근 수집 시각/상태" 표시용.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

logger = logging.getLogger(__name__)

# ==============================================================================
# 0. 경로 및 읽기 모드 설정
# ==============================================================================
_DEFAULT_DB_RELPATH = Path("data") / "dashboard.db"

# 읽기 모드
#   "auto"       : 저장본이 신선하면 사용, 아니면 직접 수집하고 저장 (기본값)
#   "store_only" : 저장본만 사용. 없으면 빈 결과. 화면이 절대 멈추지 않음
#   "live_only"  : 저장 계층 무시. 리팩토링 이전과 동일하게 매번 수집
READ_MODE_AUTO = "auto"
READ_MODE_STORE_ONLY = "store_only"
READ_MODE_LIVE_ONLY = "live_only"

_VALID_READ_MODES = {READ_MODE_AUTO, READ_MODE_STORE_ONLY, READ_MODE_LIVE_ONLY}

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def get_db_path() -> Path:
    """
    DB 파일 경로. DASHBOARD_DB 환경변수로 덮어쓸 수 있습니다(테스트용).

    기본값은 프로젝트 루트의 data/dashboard.db 입니다.
    """
    override = os.environ.get("DASHBOARD_DB", "").strip()
    if override:
        return Path(override).expanduser()

    project_root = Path(__file__).resolve().parent.parent
    return project_root / _DEFAULT_DB_RELPATH


def get_read_mode() -> str:
    """DASHBOARD_READ_MODE 환경변수로 결정되는 읽기 모드."""
    mode = os.environ.get("DASHBOARD_READ_MODE", READ_MODE_AUTO).strip().lower()
    if mode not in _VALID_READ_MODES:
        logger.warning(
            "알 수 없는 DASHBOARD_READ_MODE=%r, '%s'로 처리합니다. (가능: %s)",
            mode, READ_MODE_AUTO, ", ".join(sorted(_VALID_READ_MODES)),
        )
        return READ_MODE_AUTO
    return mode


# ==============================================================================
# 1. 스키마 및 커넥션
# ==============================================================================
_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    name          TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'json',
    status        TEXT NOT NULL DEFAULT 'ok',
    error         TEXT,
    collected_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeseries (
    dataset    TEXT NOT NULL,
    series_id  TEXT NOT NULL,
    obs_date   TEXT NOT NULL,
    value      REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset, series_id, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_timeseries_lookup
    ON timeseries (dataset, series_id, obs_date);

CREATE TABLE IF NOT EXISTS observations (
    dataset    TEXT NOT NULL,
    obs_date   TEXT NOT NULL,
    entity     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset, obs_date, entity)
);
CREATE INDEX IF NOT EXISTS idx_observations_lookup
    ON observations (dataset, obs_date);

CREATE TABLE IF NOT EXISTS collector_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    ok_count     INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def init_db(db_path: Path | None = None) -> Path:
    """
    스키마를 생성합니다(멱등). 프로세스당 경로별 1회만 실제로 실행합니다.
    """
    path = Path(db_path) if db_path else get_db_path()
    key = str(path)

    with _init_lock:
        if key in _initialized_paths:
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=15.0)
        try:
            # WAL: 읽기(Streamlit)와 쓰기(수집기)가 서로를 막지 않게 합니다.
            conn.execute("PRAGMA journal_mode=WAL")
            # 쓰기 잠금 충돌 시 즉시 실패하지 않고 대기합니다.
            conn.execute("PRAGMA busy_timeout=15000")
            # NORMAL: 로컬 대시보드에는 FULL fsync까지 필요하지 않습니다.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        _initialized_paths.add(key)
        logger.info("저장 계층 준비 완료: %s", path)
        return path


@contextmanager
def connect(db_path: Path | None = None, readonly: bool = False):
    """
    커넥션을 열고 닫습니다.

    커넥션을 캐시하지 않는 이유: sqlite3 커넥션은 기본적으로 생성 스레드에
    묶여 있고, Streamlit은 재실행마다 다른 스레드에서 동작할 수 있습니다.
    로컬 파일은 연결 비용이 사실상 0이므로 매번 여는 편이 안전합니다.
    """
    path = init_db(db_path)

    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


# ==============================================================================
# 2. 스냅샷 (최신 상태 1건)
# ==============================================================================
@dataclass(frozen=True)
class Snapshot:
    """저장된 스냅샷 1건과 그 신선도."""

    name: str
    payload: Any
    kind: str
    status: str
    error: str | None
    collected_at: datetime | None

    @property
    def age_seconds(self) -> float:
        if self.collected_at is None:
            return float("inf")
        delta = datetime.now(timezone.utc) - self.collected_at
        return max(0.0, delta.total_seconds())

    def is_fresh(self, max_age_seconds: float) -> bool:
        return self.age_seconds <= max_age_seconds

    def collected_at_kst_str(self) -> str:
        if self.collected_at is None:
            return "알 수 없음"
        from zoneinfo import ZoneInfo

        return (
            self.collected_at.astimezone(ZoneInfo("Asia/Seoul"))
            .strftime("%Y-%m-%d %H:%M:%S KST")
        )


# ==============================================================================
# 2-1. 중첩 구조 코덱 (DataFrame을 품은 dict/list를 타입 보존하며 저장)
# ==============================================================================
# 13F·COT 결과는 {"자산": {"data": DataFrame, "error": None}} 처럼 DataFrame을
# 중첩해서 담고 있습니다. 일반 JSON 직렬화는 DataFrame을 레코드 리스트로
# 납작하게 만들어 버려서, 읽을 때 화면이 기대하는 타입이 아닙니다.
#
# pickle을 쓰면 간단하지만 역직렬화 시 임의 코드 실행이 가능해집니다.
# DB 파일이 공유/이동될 수 있다는 점을 생각하면 쓰고 싶지 않습니다.
# 그래서 DataFrame만 명시적으로 태깅하는 안전한 코덱을 씁니다.
_DF_TAG = "__dataframe__"
_TUPLE_TAG = "__tuple__"


def _encode_obj(obj: Any) -> Any:
    """dict/list를 재귀적으로 돌며 DataFrame만 태깅해 JSON-safe로 만듭니다."""
    if isinstance(obj, pd.DataFrame):
        return {
            _DF_TAG: obj.to_json(orient="split", date_format="iso"),
            "index_is_datetime": isinstance(obj.index, pd.DatetimeIndex),
        }
    if isinstance(obj, pd.Series):
        return _encode_obj(obj.to_frame())
    if isinstance(obj, dict):
        return {str(k): _encode_obj(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return {_TUPLE_TAG: [_encode_obj(v) for v in obj]}
    if isinstance(obj, list):
        return [_encode_obj(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "item"):              # numpy 스칼라
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)


def _decode_obj(obj: Any) -> Any:
    """_encode_obj의 역변환."""
    from io import StringIO

    if isinstance(obj, dict):
        if _DF_TAG in obj:
            try:
                df = pd.read_json(StringIO(obj[_DF_TAG]), orient="split")
            except ValueError as e:
                logger.warning("중첩 DataFrame 역직렬화 실패: %s", e)
                return pd.DataFrame()
            if obj.get("index_is_datetime") and not isinstance(
                df.index, pd.DatetimeIndex
            ):
                df.index = pd.to_datetime(df.index, errors="coerce")
            return df
        if _TUPLE_TAG in obj:
            return tuple(_decode_obj(v) for v in obj[_TUPLE_TAG])
        return {k: _decode_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_obj(v) for v in obj]
    return obj


def put_object(
    name: str,
    payload: Any,
    *,
    status: str = "ok",
    error: str | None = None,
    db_path: Path | None = None,
) -> None:
    """DataFrame을 품은 중첩 구조를 타입 보존하며 저장합니다."""
    blob = json.dumps(_encode_obj(payload), ensure_ascii=False)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO snapshots (name, payload, kind, status, error, collected_at)
            VALUES (?, ?, 'object', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                payload      = excluded.payload,
                kind         = excluded.kind,
                status       = excluded.status,
                error        = excluded.error,
                collected_at = excluded.collected_at
            """,
            (name, blob, status, error, _utc_now_iso()),
        )


def put_snapshot(
    name: str,
    payload: Any,
    *,
    status: str = "ok",
    error: str | None = None,
    db_path: Path | None = None,
) -> None:
    """JSON 직렬화 가능한 스냅샷을 upsert 합니다."""
    blob = json.dumps(payload, ensure_ascii=False, default=_json_default)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO snapshots (name, payload, kind, status, error, collected_at)
            VALUES (?, ?, 'json', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                payload      = excluded.payload,
                kind         = excluded.kind,
                status       = excluded.status,
                error        = excluded.error,
                collected_at = excluded.collected_at
            """,
            (name, blob, status, error, _utc_now_iso()),
        )


def put_frame(
    name: str,
    df: pd.DataFrame,
    *,
    status: str = "ok",
    error: str | None = None,
    db_path: Path | None = None,
) -> None:
    """
    DataFrame을 스냅샷으로 저장합니다.

    orient="split" + date_format="iso"를 쓰면 인덱스(날짜)와 컬럼 순서가
    그대로 보존되고, NaN은 null로 직렬화됩니다.
    """
    if df is None:
        payload = None
    else:
        payload = {
            "__frame__": df.to_json(orient="split", date_format="iso"),
            "index_is_datetime": isinstance(df.index, pd.DatetimeIndex),
        }

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO snapshots (name, payload, kind, status, error, collected_at)
            VALUES (?, ?, 'frame', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                payload      = excluded.payload,
                kind         = excluded.kind,
                status       = excluded.status,
                error        = excluded.error,
                collected_at = excluded.collected_at
            """,
            (
                name,
                json.dumps(payload, ensure_ascii=False),
                status,
                error,
                _utc_now_iso(),
            ),
        )


def read_snapshot(name: str, db_path: Path | None = None) -> Snapshot | None:
    """저장된 스냅샷을 읽습니다. 없으면 None."""
    try:
        with connect(db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT name, payload, kind, status, error, collected_at "
                "FROM snapshots WHERE name = ?",
                (name,),
            ).fetchone()
    except sqlite3.Error as e:
        logger.warning("스냅샷 읽기 실패 (%s): %s", name, e)
        return None

    if row is None:
        return None

    try:
        raw = json.loads(row["payload"])
    except (TypeError, ValueError) as e:
        logger.warning("스냅샷 역직렬화 실패 (%s): %s", name, e)
        return None

    if row["kind"] == "frame":
        payload = _revive_frame(raw)
    elif row["kind"] == "object":
        payload = _decode_obj(raw)
    else:
        payload = raw

    return Snapshot(
        name=row["name"],
        payload=payload,
        kind=row["kind"],
        status=row["status"],
        error=row["error"],
        collected_at=_parse_iso(row["collected_at"]),
    )


def _revive_frame(raw: Any) -> pd.DataFrame | None:
    """put_frame이 저장한 payload를 DataFrame으로 되살립니다."""
    if not isinstance(raw, dict) or "__frame__" not in raw:
        return None

    from io import StringIO

    try:
        df = pd.read_json(StringIO(raw["__frame__"]), orient="split")
    except ValueError as e:
        logger.warning("DataFrame 역직렬화 실패: %s", e)
        return None

    if raw.get("index_is_datetime") and not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")

    return df


def _json_default(obj: Any):
    """json.dumps가 모르는 타입(numpy/pandas 스칼라, 날짜)을 변환합니다."""
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if hasattr(obj, "item"):          # numpy 스칼라
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict(orient="records")
    if isinstance(obj, pd.Series):
        return obj.to_dict()
    return str(obj)


# ==============================================================================
# 3. 시계열 누적 (과거 조회가 안 되는 소스의 이력을 직접 쌓는다)
# ==============================================================================
def put_timeseries(
    dataset: str,
    series_id: str,
    data: pd.Series | pd.DataFrame,
    *,
    value_col: str | None = None,
    db_path: Path | None = None,
) -> int:
    """
    날짜 인덱스를 가진 수치 시계열을 upsert 합니다. 반영된 행 수를 반환합니다.

    같은 (dataset, series_id, 날짜)는 최신 값으로 덮어씁니다. 따라서 매일
    수집하면 과거는 유지되고 최근 값만 갱신됩니다.
    """
    series = _coerce_series(data, value_col)
    if series is None or series.empty:
        return 0

    now = _utc_now_iso()
    rows = [
        (dataset, series_id, _date_key(idx), _to_float_or_none(val), now)
        for idx, val in series.items()
        if _date_key(idx) is not None
    ]
    if not rows:
        return 0

    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO timeseries (dataset, series_id, obs_date, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(dataset, series_id, obs_date) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at
            """,
            rows,
        )
    return len(rows)


def read_timeseries(
    dataset: str,
    series_id: str,
    *,
    start_date: str | None = None,
    value_name: str | None = None,
    db_path: Path | None = None,
) -> pd.DataFrame:
    """
    누적된 시계열을 DatetimeIndex DataFrame으로 반환합니다.
    값 컬럼명은 value_name(기본: series_id)입니다.
    """
    sql = (
        "SELECT obs_date, value FROM timeseries "
        "WHERE dataset = ? AND series_id = ?"
    )
    params: list[Any] = [dataset, series_id]
    if start_date:
        sql += " AND obs_date >= ?"
        params.append(start_date)
    sql += " ORDER BY obs_date"

    try:
        with connect(db_path, readonly=True) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        logger.warning("시계열 읽기 실패 (%s/%s): %s", dataset, series_id, e)
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    col = value_name or series_id
    df = pd.DataFrame(
        {col: [r["value"] for r in rows]},
        index=pd.to_datetime([r["obs_date"] for r in rows]),
    )
    df.index.name = "date"
    return df.dropna()


def put_frame_as_timeseries(
    dataset: str,
    df: pd.DataFrame,
    *,
    columns: Iterable[str] | None = None,
    db_path: Path | None = None,
) -> int:
    """와이드 DataFrame의 각 숫자 컬럼을 series_id로 삼아 누적합니다."""
    if df is None or df.empty:
        return 0

    targets = list(columns) if columns else [
        c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])
    ]
    total = 0
    for col in targets:
        if col in df.columns:
            total += put_timeseries(dataset, str(col), df[col], db_path=db_path)
    return total


# ==============================================================================
# 4. 날짜별 레코드 누적 (수급 랭킹처럼 행이 여러 컬럼인 데이터)
# ==============================================================================
def put_observations(
    dataset: str,
    obs_date: str,
    records: list[dict],
    *,
    entity_key: str,
    db_path: Path | None = None,
) -> int:
    """
    (dataset, 날짜, entity) 단위로 레코드를 upsert 합니다.

    Naver/Daum 수급 랭킹은 "현재 시점"만 제공하고 과거 조회가 불가능하므로,
    수집할 때마다 날짜별로 적재해 이력을 직접 만듭니다.
    """
    if not records:
        return 0

    now = _utc_now_iso()
    rows = []
    for rec in records:
        entity = rec.get(entity_key)
        if entity is None:
            continue
        rows.append((
            dataset,
            obs_date,
            str(entity),
            json.dumps(rec, ensure_ascii=False, default=_json_default),
            now,
        ))

    if not rows:
        return 0

    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO observations (dataset, obs_date, entity, payload, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(dataset, obs_date, entity) DO UPDATE SET
                payload    = excluded.payload,
                updated_at = excluded.updated_at
            """,
            rows,
        )
    return len(rows)


def read_observations(
    dataset: str,
    *,
    obs_date: str | None = None,
    start_date: str | None = None,
    db_path: Path | None = None,
) -> pd.DataFrame:
    """
    누적된 날짜별 레코드를 DataFrame으로 반환합니다.
    obs_date를 주면 그 날짜만, start_date를 주면 그 이후 전체를 반환합니다.
    """
    sql = "SELECT obs_date, payload FROM observations WHERE dataset = ?"
    params: list[Any] = [dataset]
    if obs_date:
        sql += " AND obs_date = ?"
        params.append(obs_date)
    if start_date:
        sql += " AND obs_date >= ?"
        params.append(start_date)
    sql += " ORDER BY obs_date, entity"

    try:
        with connect(db_path, readonly=True) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        logger.warning("레코드 읽기 실패 (%s): %s", dataset, e)
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    out = []
    for r in rows:
        try:
            rec = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        rec.setdefault("수집일자", r["obs_date"])
        out.append(rec)

    return pd.DataFrame(out)


def list_observation_dates(dataset: str, db_path: Path | None = None) -> list[str]:
    """해당 dataset에 이력이 쌓인 날짜 목록(오름차순)."""
    try:
        with connect(db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT obs_date FROM observations "
                "WHERE dataset = ? ORDER BY obs_date",
                (dataset,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [r["obs_date"] for r in rows]


# ==============================================================================
# 5. 수집 실행 로그
# ==============================================================================
def start_run(db_path: Path | None = None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO collector_runs (started_at, status) VALUES (?, 'running')",
            (_utc_now_iso(),),
        )
        return int(cur.lastrowid)


def finish_run(
    run_id: int,
    *,
    status: str,
    ok_count: int = 0,
    fail_count: int = 0,
    detail: str | None = None,
    db_path: Path | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE collector_runs SET finished_at = ?, status = ?, "
            "ok_count = ?, fail_count = ?, detail = ? WHERE id = ?",
            (_utc_now_iso(), status, ok_count, fail_count, detail, run_id),
        )


def read_last_run(db_path: Path | None = None) -> dict | None:
    try:
        with connect(db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def store_stats(db_path: Path | None = None) -> dict:
    """상태 화면용 요약 통계."""
    stats: dict[str, Any] = {
        "db_path": str(get_db_path() if db_path is None else db_path),
        "exists": False,
        "size_bytes": 0,
        "snapshots": [],
        "timeseries_rows": 0,
        "observation_rows": 0,
        "last_run": None,
    }

    path = Path(stats["db_path"])
    if not path.exists():
        return stats

    stats["exists"] = True
    stats["size_bytes"] = path.stat().st_size

    try:
        with connect(db_path, readonly=True) as conn:
            stats["snapshots"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT name, status, collected_at FROM snapshots "
                    "ORDER BY name"
                ).fetchall()
            ]
            stats["timeseries_rows"] = conn.execute(
                "SELECT COUNT(*) AS n FROM timeseries"
            ).fetchone()["n"]
            stats["observation_rows"] = conn.execute(
                "SELECT COUNT(*) AS n FROM observations"
            ).fetchone()["n"]
    except sqlite3.Error as e:
        logger.warning("통계 조회 실패: %s", e)

    stats["last_run"] = read_last_run(db_path)
    return stats


# ==============================================================================
# 6. 읽기 경로: 저장본 우선 + 필요 시 직접 수집
# ==============================================================================
def _schema_matches(
    payload: Any,
    required_columns: Iterable[str] | None,
    name: str,
) -> bool:
    """
    저장된 DataFrame이 화면이 기대하는 컬럼을 갖고 있는지 확인합니다.

    DataFrame이 아니거나 요구 컬럼이 없으면 검사를 통과시킵니다
    (스냅샷 종류마다 형태가 달라 일괄 검증이 불가능합니다).
    """
    if required_columns is None or not isinstance(payload, pd.DataFrame):
        return True

    missing = [c for c in required_columns if c not in payload.columns]
    if missing:
        logger.warning(
            "%s: 저장본 스키마 불일치(누락 컬럼 %s). 저장본을 버리고 다시 수집합니다.",
            name, missing,
        )
        return False
    return True


def cached_or_live(
    name: str,
    live_fn: Callable[[], Any],
    *,
    max_age_seconds: float,
    empty_value: Any = None,
    as_frame: bool = False,
    as_object: bool = False,
    required_columns: Iterable[str] | None = None,
) -> Any:
    """
    저장본이 신선하면 그것을 쓰고, 아니면 live_fn()으로 수집한 뒤 저장합니다.

    읽기 모드(DASHBOARD_READ_MODE)에 따라 동작이 달라집니다.
      - auto       : 신선하면 저장본, 아니면 수집 + 저장 (기본)
      - store_only : 저장본만. 없으면 empty_value. 화면이 절대 외부를 기다리지 않음
      - live_only  : 저장 계층 무시

    수집이 실패하면 "신선하지 않더라도" 남아 있는 저장본을 내려줍니다.
    외부 소스 장애 시 화면이 비는 것보다 오래된 값이라도 보여주는 편이
    낫고, 화면에는 수집 시각이 함께 표시되므로 오해 여지가 없습니다.

    required_columns를 주면 저장된 DataFrame이 그 컬럼을 모두 갖고 있는지
    확인합니다. 예전 버전의 코드가 저장해 둔 스냅샷은 컬럼 구성이 달라져
    있을 수 있고, 그대로 화면에 넘기면 KeyError로 페이지가 죽습니다.
    스키마가 맞지 않으면 저장본을 버리고 다시 수집합니다.
    """
    mode = get_read_mode()

    if mode == READ_MODE_LIVE_ONLY:
        return live_fn()

    snap = read_snapshot(name)

    snap_ok = (
        snap is not None
        and snap.payload is not None
        and _schema_matches(snap.payload, required_columns, name)
    )

    if mode == READ_MODE_STORE_ONLY and not snap_ok:
        # store_only에서 스키마가 깨진 저장본은 쓸 수 없으므로 빈 값을 줍니다.
        return empty_value

    if snap_ok and snap.is_fresh(max_age_seconds):
        return snap.payload

    try:
        value = live_fn()
    except Exception as e:
        logger.warning("직접 수집 실패 (%s): %s", name, e)
        if snap_ok:
            logger.info(
                "%s: 오래된 저장본으로 대체합니다 (수집 시각 %s)",
                name, snap.collected_at_kst_str(),
            )
            return snap.payload
        return empty_value

    try:
        if as_frame:
            put_frame(name, value)
        elif as_object:
            put_object(name, value)
        else:
            put_snapshot(name, value)
    except Exception as e:
        # 저장 실패가 화면을 막아서는 안 됩니다.
        logger.warning("스냅샷 저장 실패 (%s): %s", name, e)

    return value


def is_empty_result(value: Any) -> bool:
    """수집 결과가 '비어 있음'인지 판정합니다."""
    if value is None:
        return True
    if isinstance(value, pd.DataFrame):
        return value.empty
    if isinstance(value, (list, dict, tuple, str)):
        return len(value) == 0
    return False


def purge_older_than(days: int, db_path: Path | None = None) -> dict[str, int]:
    """
    오래된 누적 이력을 정리합니다. 기본 운용에서는 불필요하지만
    (하루 수집량이 작습니다) 장기 운영 시 관리용으로 둡니다.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    with connect(db_path) as conn:
        ts = conn.execute(
            "DELETE FROM timeseries WHERE obs_date < ?", (cutoff,)
        ).rowcount
        ob = conn.execute(
            "DELETE FROM observations WHERE obs_date < ?", (cutoff,)
        ).rowcount
        runs = conn.execute(
            "DELETE FROM collector_runs WHERE started_at < ? "
            "AND id NOT IN (SELECT id FROM collector_runs ORDER BY id DESC LIMIT 50)",
            (cutoff,),
        ).rowcount
    return {"timeseries": ts, "observations": ob, "collector_runs": runs}


# ==============================================================================
# 7. 내부 헬퍼
# ==============================================================================
def _coerce_series(
    data: pd.Series | pd.DataFrame,
    value_col: str | None,
) -> pd.Series | None:
    if data is None:
        return None
    if isinstance(data, pd.Series):
        return data.dropna()
    if isinstance(data, pd.DataFrame):
        if data.empty:
            return None
        if value_col and value_col in data.columns:
            return data[value_col].dropna()
        numeric = [c for c in data.columns if pd.api.types.is_numeric_dtype(data[c])]
        if not numeric:
            return None
        return data[numeric[0]].dropna()
    return None


def _date_key(value: Any) -> str | None:
    """인덱스 값을 'YYYY-MM-DD' 문자열로 정규화합니다."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return pd.to_datetime(value).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _to_float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out
