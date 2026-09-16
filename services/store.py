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
import time
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

-- 태스크별 실행 결과. 집계만 있으면 "무엇이 왜 실패했는지" 알 수 없습니다.
CREATE TABLE IF NOT EXISTS collector_task_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    task         TEXT NOT NULL,
    speed        TEXT,
    status       TEXT NOT NULL,      -- ok | empty | error
    started_at   TEXT NOT NULL,
    duration_ms  INTEGER,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task
    ON collector_task_runs (task, id DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_run
    ON collector_task_runs (run_id);
"""

# collector_runs에 나중에 추가된 컬럼들.
# 기존 사용자의 DB에는 없으므로 ALTER TABLE로 보강합니다
# (CREATE TABLE IF NOT EXISTS는 이미 있는 테이블의 컬럼을 추가해 주지 않습니다).
_MIGRATIONS = [
    ("collector_runs", "pid", "INTEGER"),
    ("collector_runs", "host", "TEXT"),
    ("collector_runs", "heartbeat_at", "TEXT"),
    ("collector_runs", "group_name", "TEXT"),
]


def _apply_migrations(conn) -> None:
    """없는 컬럼만 추가합니다 (멱등)."""
    for table, column, coltype in _MIGRATIONS:
        existing = {
            r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            logger.info("스키마 마이그레이션: %s.%s 추가", table, column)


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
            _apply_migrations(conn)
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
            "attrs": _json_safe_attrs(obj.attrs),
            "dtypes": {str(c): str(t) for c, t in obj.dtypes.items()},
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
                df = pd.read_json(
                    StringIO(obj[_DF_TAG]),
                    orient="split",
                    dtype=_restore_dtypes(obj.get("dtypes")),
                )
            except ValueError as e:
                logger.warning("중첩 DataFrame 역직렬화 실패: %s", e)
                return pd.DataFrame()
            if obj.get("index_is_datetime") and not isinstance(
                df.index, pd.DatetimeIndex
            ):
                df.index = pd.to_datetime(df.index, errors="coerce")
            nested_attrs = obj.get("attrs")
            if isinstance(nested_attrs, dict):
                df.attrs.update(nested_attrs)
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


def _restore_dtypes(stored):
    """
    put_frame이 기록한 dtype 맵을 read_json의 dtype 인자로 변환합니다.

    반환값 의미:
      - dict : 컬럼별 dtype을 그대로 적용
      - False: dtype 정보가 없는 예전 저장본 → 추론을 끕니다.
               추론을 켜 두면 "069500" 같은 문자열이 int로 바뀌어
               앞자리 0이 사라지므로, 모르면 추론하지 않는 편이 안전합니다.

    datetime 계열은 read_json이 직접 처리하므로 제외합니다
    (dtype으로 넘기면 파싱 단계에서 충돌합니다).
    """
    if not isinstance(stored, dict) or not stored:
        return False

    out = {}
    for col, dtype in stored.items():
        text = str(dtype)
        if "datetime" in text or "period" in text or "interval" in text:
            continue
        out[col] = text
    return out or False


def _json_safe_attrs(attrs) -> dict:
    """
    df.attrs에서 JSON으로 안전하게 저장 가능한 스칼라만 추립니다.
    (플래그·라벨 용도이므로 스칼라면 충분합니다.)
    """
    if not isinstance(attrs, dict):
        return {}
    return {
        str(k): v
        for k, v in attrs.items()
        if isinstance(v, (str, int, float, bool, type(None)))
    }


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
            # df.attrs는 to_json이 보존하지 않습니다. 이 프로젝트는 attrs에
            # is_proxy / is_intraday / source_label 같은 "이 값은 실제
            # 지표가 아니다" 표시를 담고 있어서, 잃어버리면 추정치가
            # 공식 데이터처럼 화면과 AI 리포트에 나갑니다. 따로 싣습니다.
            "attrs": _json_safe_attrs(df.attrs),
            # [중요] to_json(orient="split")은 dtype을 저장하지 않습니다.
            # 읽을 때 pandas가 타입을 추론하는데, 숫자로만 이루어진 문자열
            # 컬럼("069500", "005930", cusip "037833100")을 int로 바꿔버려
            # **앞자리 0이 영구히 사라집니다**. 종목코드가 69500이 되면
            # Daum/pykrx/yfinance 조회가 전부 실패합니다.
            # dtype을 함께 저장해 복원 시 그대로 되돌립니다.
            "dtypes": {str(c): str(t) for c, t in df.dtypes.items()},
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
        df = pd.read_json(
            StringIO(raw["__frame__"]),
            orient="split",
            # dtype 추론을 끄고 저장된 dtype을 그대로 적용합니다.
            # (추론에 맡기면 "069500" 같은 문자열이 int가 됩니다.)
            dtype=_restore_dtypes(raw.get("dtypes")),
        )
    except ValueError as e:
        logger.warning("DataFrame 역직렬화 실패: %s", e)
        return None

    if raw.get("index_is_datetime") and not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")

    attrs = raw.get("attrs")
    if isinstance(attrs, dict):
        df.attrs.update(attrs)

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
# 이 시간 동안 heartbeat가 갱신되지 않은 'running' 레코드는 죽은 것으로 봅니다.
# 수집기는 태스크마다 heartbeat를 찍으므로, 가장 느린 태스크(13F)보다
# 넉넉하게 잡습니다.
STALE_RUN_SECONDS = 30 * 60


def start_run(
    db_path: Path | None = None,
    group_name: str | None = None,
) -> int:
    """
    수집 실행을 기록하고 run_id를 반환합니다.

    PID와 heartbeat를 함께 남기는 이유: 수집기가 Ctrl+C나 절전·강제종료로
    죽으면 status가 'running'에 영구히 남아, --status가 "진행 중"이라고
    거짓 보고합니다. PID 생존 여부와 heartbeat로 실제 진행 중인지 판정합니다.
    """
    import socket

    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO collector_runs "
            "(started_at, status, pid, host, heartbeat_at, group_name) "
            "VALUES (?, 'running', ?, ?, ?, ?)",
            (
                _utc_now_iso(), os.getpid(), socket.gethostname(),
                _utc_now_iso(), group_name,
            ),
        )
        return int(cur.lastrowid)


def heartbeat_run(run_id: int, db_path: Path | None = None) -> None:
    """진행 중임을 알리는 타임스탬프를 갱신합니다."""
    try:
        with connect(db_path) as conn:
            conn.execute(
                "UPDATE collector_runs SET heartbeat_at = ? WHERE id = ?",
                (_utc_now_iso(), run_id),
            )
    except sqlite3.Error as e:
        logger.debug("heartbeat 갱신 실패: %s", e)


def record_task_run(
    run_id: int | None,
    task: str,
    *,
    speed: str | None,
    status: str,
    started_at: datetime,
    duration_ms: int,
    detail: str | None = None,
    db_path: Path | None = None,
) -> None:
    """
    태스크 1건의 결과를 기록합니다.

    status: "ok" | "empty"(수집됐지만 데이터 없음) | "error"
    """
    try:
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO collector_task_runs "
                "(run_id, task, speed, status, started_at, duration_ms, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, task, speed, status,
                    started_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    duration_ms, (detail or "")[:1000] or None,
                ),
            )
    except sqlite3.Error as e:
        logger.warning("태스크 로그 기록 실패 (%s): %s", task, e)


def _pid_is_alive(pid: int | None) -> bool:
    """해당 PID가 살아 있는지 확인합니다 (같은 머신일 때만 의미 있음)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)      # 시그널 0 = 존재 확인만
    except ProcessLookupError:
        return False
    except PermissionError:
        return True               # 존재하지만 권한이 없음
    except (TypeError, ValueError, OSError):
        return False
    return True


def resolve_run_status(run: dict | None) -> str:
    """
    기록된 status를 실제 상태로 보정합니다.

    'running'으로 남아 있지만 프로세스가 죽었거나 heartbeat가 끊긴 경우
    'interrupted'로 보고합니다. 그래야 --status가 거짓말을 하지 않습니다.
    """
    if not run:
        return "none"

    status = run.get("status") or "?"
    if status != "running":
        return status

    import socket

    same_host = (run.get("host") or socket.gethostname()) == socket.gethostname()
    if same_host and not _pid_is_alive(run.get("pid")):
        return "interrupted"

    beat = _parse_iso(run.get("heartbeat_at") or run.get("started_at"))
    if beat is not None:
        age = (datetime.now(timezone.utc) - beat).total_seconds()
        if age > STALE_RUN_SECONDS:
            return "interrupted"

    return "running"


def mark_stale_runs_interrupted(db_path: Path | None = None) -> int:
    """
    죽은 'running' 레코드를 정리합니다. 수집기 시작 시 호출합니다.
    정리한 건수를 반환합니다.
    """
    try:
        with connect(db_path, readonly=True) as conn:
            rows = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM collector_runs WHERE status = 'running'"
                ).fetchall()
            ]
    except sqlite3.Error:
        return 0

    stale = [
        r["id"] for r in rows
        if resolve_run_status(r) == "interrupted"
    ]
    if not stale:
        return 0

    with connect(db_path) as conn:
        conn.executemany(
            "UPDATE collector_runs SET status = 'interrupted', "
            "detail = COALESCE(detail, '') || ' [비정상 종료로 판정]' "
            "WHERE id = ?",
            [(i,) for i in stale],
        )
    logger.info("비정상 종료된 수집 기록 %d건을 정리했습니다.", len(stale))
    return len(stale)


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
            "ok_count = ?, fail_count = ?, detail = ?, heartbeat_at = ? "
            "WHERE id = ?",
            (
                _utc_now_iso(), status, ok_count, fail_count, detail,
                _utc_now_iso(), run_id,
            ),
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
    stats["last_run_status"] = resolve_run_status(stats["last_run"])
    stats["task_summary"] = read_task_summary(db_path)
    return stats


def read_task_summary(db_path: Path | None = None) -> list[dict]:
    """
    태스크별 '가장 최근 실행 결과'를 반환합니다.

    집계(성공 N·실패 M)만으로는 어떤 태스크가 왜 실패했는지 알 수 없어서,
    태스크 단위로 최신 1건씩 추립니다.
    """
    try:
        with connect(db_path, readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT t.task, t.speed, t.status, t.started_at,
                       t.duration_ms, t.detail
                FROM collector_task_runs t
                JOIN (
                    SELECT task, MAX(id) AS max_id
                    FROM collector_task_runs GROUP BY task
                ) m ON m.task = t.task AND m.max_id = t.id
                ORDER BY t.speed, t.task
                """
            ).fetchall()
    except sqlite3.Error as e:
        logger.warning("태스크 요약 조회 실패: %s", e)
        return []
    return [dict(r) for r in rows]


def read_task_history(
    task: str | None = None,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[dict]:
    """태스크 실행 이력(최신순). task를 주면 그 태스크만."""
    sql = (
        "SELECT run_id, task, speed, status, started_at, duration_ms, detail "
        "FROM collector_task_runs"
    )
    params: list[Any] = []
    if task:
        sql += " WHERE task = ?"
        params.append(task)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))

    try:
        with connect(db_path, readonly=True) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error as e:
        logger.warning("태스크 이력 조회 실패: %s", e)
        return []


def missing_datasets(db_path: Path | None = None) -> list[dict]:
    """
    "있어야 하는데 없는" 데이터셋을 찾습니다.

    기존 store_stats()는 존재하는 스냅샷만 나열해서, 수집이 아예 안 된
    데이터셋은 목록에서 조용히 빠져 있었습니다. 그래서 무엇이 누락됐는지
    알아챌 수 없었습니다.
    """
    from services import datasets as ds

    try:
        with connect(db_path, readonly=True) as conn:
            present = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM snapshots"
                ).fetchall()
            }
    except sqlite3.Error:
        return []

    expected: list[tuple[str, str]] = [
        (ds.SNAP_MACRO_COLLECTED, "매크로 카드"),
        (ds.SNAP_SCRAPER_MARKETS, "TradingView/Yahoo 참고 시세"),
        (ds.SNAP_FED_LIQUIDITY, "연준 순유동성"),
        (ds.SNAP_KRX_FUTURES, "KRX 선물 시계열"),
        (ds.SNAP_SECTOR_HISTORY, "섹터·자산군 ETF 종가"),
        (ds.SNAP_COT_HISTORY, "CFTC COT 통합"),
    ]
    fred_ids = ["DGS2", "DGS10", "DGS30", "DGS3MO",
                "BAMLH0A0HYM2", "STLFSI4", "CPF3M"]
    try:
        from services.advanced_macro_service import ADVANCED_SERIES_IDS

        fred_ids.extend(ADVANCED_SERIES_IDS)
    except Exception:
        pass

    for sid in fred_ids:
        expected.append((ds.snap_fred_series(sid), f"FRED {sid}"))

    for symbol in ("^VIX", "^MOVE"):
        expected.append((
            ds.snap_ticker_history(symbol, ds.VOLATILITY_STORE_PERIOD),
            f"변동성 {symbol}",
        ))

    expected.append((
        ds.snap_daum_futures_trend(25),
        "Daum 선물 수급 (계약수)",
    ))

    try:
        from services.cot_service import COT_ASSETS

        weeks = int(3 * 52 + 10)
        for asset, info in COT_ASSETS.items():
            expected.append((
                ds.snap_cot_contract(info["code"], weeks), f"COT {asset}",
            ))
    except Exception:
        pass

    for market, investor, trade in (
        ("KOSPI", "외국인", "순매수"),
        ("KOSPI", "기관", "순매수"),
        ("KOSPI", "외국인", "순매도"),
    ):
        expected.append((
            ds.snap_radar_scanner(market, investor, trade, "TODAY"),
            f"수급 레이더 {investor}/{trade}",
        ))

    try:
        from config import INSTITUTIONS

        for name, info in INSTITUTIONS.items():
            cik = info.get("cik")
            if not cik:
                continue
            short = name.split("(")[0].strip()
            for q in (1, 8):
                expected.append((
                    ds.snap_sec_13f(cik, q), f"13F {short} q{q}",
                ))
    except Exception:
        pass

    return [
        {"name": name, "label": label}
        for name, label in expected
        if name not in present
    ]


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


# ==============================================================================
# 수동 새로고침 (화면의 "새로고침" 버튼)
# ==============================================================================
# [버그 수정] 화면의 새로고침 버튼은 st.cache_data.clear()만 했습니다. 그건
# Streamlit의 메모리 캐시만 비울 뿐이고, 그 다음 조회는 다시 cached_or_live로
# 들어와 **아직 신선한 SQLite 저장본**을 그대로 돌려줬습니다. 결과적으로
# 버튼을 눌러도 화면의 숫자가 하나도 바뀌지 않았습니다(사용자 신고).
#
# 저장본을 지우는 방식은 쓰지 않습니다. 수집이 실패하면 보여 줄 값이 아예
# 없어지기 때문입니다. 대신 "이 시각 이전에 수집된 저장본은 낡은 것으로
# 본다"는 기준 시각을 하나 들고, 그보다 오래된 저장본만 다시 수집합니다.
_refresh_token: float = 0.0

# 한 번의 새로고침에서 이미 수집을 시도한 스냅샷. 수집이 실패하면 저장본의
# 수집 시각이 그대로 남아, 이후 모든 rerun마다 실패하는 외부 호출을 반복하게
# 됩니다(화면이 계속 느려짐). 새로고침 1회당 1번만 시도합니다.
_refresh_attempted: dict[str, float] = {}


def request_refresh() -> None:
    """
    다음 조회에서 저장본의 신선도를 무시하고 다시 수집하게 합니다.

    화면의 새로고침 버튼이 st.cache_data.clear()와 함께 호출합니다.
    store_only 모드에서는 외부를 호출하지 않는다는 약속이 우선이므로
    아무 효과가 없습니다(저장본 재조회만 일어납니다).
    """
    global _refresh_token
    _refresh_token = time.time()
    _refresh_attempted.clear()


def refresh_requested_at() -> float:
    """마지막 수동 새로고침 요청 시각(epoch). 요청이 없었으면 0.0."""
    return _refresh_token


def _superseded_by_refresh(name: str, snap: "Snapshot | None") -> bool:
    """이 저장본이 수동 새로고침 요청보다 오래됐는지."""
    if not _refresh_token:
        return False
    if _refresh_attempted.get(name) == _refresh_token:
        # 이번 새로고침에서 이미 시도했습니다. 다시 조르지 않습니다.
        return False
    if snap is None or snap.collected_at is None:
        return True
    return snap.collected_at.timestamp() < _refresh_token


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
      - store_only : 저장본만. 오래됐어도 그대로 주고, 없으면 empty_value.
                     화면이 절대 외부를 기다리지 않습니다.
      - live_only  : 저장 계층 무시

    request_refresh()가 호출된 뒤에는(화면의 새로고침 버튼) 그보다 먼저
    수집된 저장본을 신선하지 않은 것으로 보고 다시 수집합니다. store_only
    모드에서는 외부를 부르지 않는다는 약속이 우선입니다.

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

    if mode == READ_MODE_STORE_ONLY:
        # [버그 수정] 예전에는 저장본이 '오래됐을 때' 여기를 그냥 통과해
        # live_fn()을 불렀습니다. store_only의 약속은 "화면이 절대 외부를
        # 기다리지 않는다"이므로, 오래됐더라도 있는 저장본을 그대로 주고
        # 없으면 빈 값을 줍니다. 갱신은 수집기의 몫입니다.
        return snap.payload if snap_ok else empty_value

    forced = _superseded_by_refresh(name, snap)

    if snap_ok and snap.is_fresh(max_age_seconds) and not forced:
        return snap.payload

    if _refresh_token:
        # 수집 성공/실패와 무관하게 "이번 새로고침에서 시도했음"을 남깁니다.
        # 실패한 소스를 매 rerun마다 다시 호출하면 화면이 계속 느려집니다.
        _refresh_attempted[name] = _refresh_token

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
