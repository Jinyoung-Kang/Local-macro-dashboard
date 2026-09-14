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
    저장소 파일 경로를 돌려줍니다.

    파라미터:
        없음.

    반환값:
        DB 파일 경로(Path). 기본값은 프로젝트 루트의 data/dashboard.db.

    주의사항:
        DASHBOARD_DB 환경변수로 덮어쓸 수 있습니다(테스트용). WAL 모드라
        실제로는 .db / .db-wal / .db-shm 세 파일이 생기므로, 백업·이동
        시 셋을 함께 옮기세요.
    """
    override = os.environ.get("DASHBOARD_DB", "").strip()
    if override:
        return Path(override).expanduser()

    project_root = Path(__file__).resolve().parent.parent
    return project_root / _DEFAULT_DB_RELPATH


def get_read_mode() -> str:
    """
    현재 읽기 모드를 돌려줍니다.

    파라미터:
        없음.

    반환값:
        "auto" | "store_only" | "live_only" 중 하나.
          auto       : 신선하면 저장본, 아니면 수집 후 저장 (기본)
          store_only : 저장본만. 화면이 절대 외부를 기다리지 않음
          live_only  : 저장 계층 무시. collector.py가 이 모드로 돕니다

    주의사항:
        알 수 없는 값이 오면 경고만 남기고 "auto"로 처리합니다. 오타 때문에
        앱이 뜨지 않는 것보다 낫지만, 의도한 모드가 아닐 수 있으니 로그를
        확인하세요.
    """
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
    """
    스키마에 나중에 추가된 컬럼을 보강합니다.

    파라미터:
        conn : 열려 있는 sqlite3 커넥션.

    반환값:
        없음.

    주의사항:
        CREATE TABLE IF NOT EXISTS는 **이미 있는 테이블에 컬럼을 더해
        주지 않습니다.** 그래서 기존 사용자의 DB를 위해 ALTER TABLE로
        따로 보강합니다. 여러 번 실행해도 안전합니다.
    """
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
    DB 파일과 스키마를 준비합니다 (여러 번 불러도 안전).

    파라미터:
        db_path : DB 경로. None이면 get_db_path()가 정하는 기본 경로
                  (프로젝트 루트의 data/dashboard.db).

    반환값:
        실제로 사용한 DB 경로(Path).

    주의사항:
        - **프로세스당 경로별로 딱 한 번만** 실제 작업을 합니다. 두 번째
          부터는 캐시된 경로 집합을 보고 바로 돌아옵니다. connect()가
          호출마다 이 함수를 부르기 때문에 이 최적화가 필요합니다.
        - 부모 디렉터리가 없으면 만듭니다.
        - WAL 모드로 바꿔 수집기(쓰기)와 Streamlit(읽기)이 서로를 막지
          않게 합니다. WAL은 파일이 3개(.db/.db-wal/.db-shm)가 되므로,
          DB를 복사·이동할 때 세 개를 함께 옮기세요.
        - 나중에 추가된 컬럼은 _MIGRATIONS가 ALTER TABLE로 보강합니다.
          CREATE TABLE IF NOT EXISTS는 이미 있는 테이블에 컬럼을 더해
          주지 않기 때문입니다.
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
    SQLite 커넥션을 열고, 블록이 끝나면 커밋·정리합니다 (with 전용).

    파라미터:
        db_path  : DB 경로 오버라이드. None이면 기본 경로.
        readonly : True면 PRAGMA query_only로 쓰기를 막고 커밋하지
                   않습니다. 읽기 전용 조회에는 반드시 True를 주세요.

    반환값:
        sqlite3.Connection을 내주는 컨텍스트 매니저.
        row_factory가 sqlite3.Row라서 결과를 r["컬럼명"]으로 읽습니다.

    주의사항:
        - **커넥션을 캐시하지 않습니다.** sqlite3 커넥션은 생성 스레드에
          묶여 있고 Streamlit은 rerun마다 다른 스레드에서 돌 수 있습니다.
          로컬 파일이라 연결 비용이 사실상 0이므로 매번 여는 편이
          안전합니다. 이 커넥션을 밖으로 들고 나가 다른 스레드에서
          쓰지 마세요.
        - 예외가 나면 롤백하고 그대로 다시 올립니다(삼키지 않습니다).
        - 쓰기가 많은 반복문에서는 이 컨텍스트를 루프 안에 두지 마세요.
          호출마다 트랜잭션이 따로 생겨 느려집니다. 행을 먼저 모아
          executemany로 한 번에 쓰세요
          (put_frame_as_timeseries가 그렇게 합니다).
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
        """
        이 저장본이 수집된 지 몇 초 지났는지.

        파라미터:
            없음.

        반환값:
            경과 초(float). 수집 시각을 모르면 **무한대**입니다.

        주의사항:
            무한대를 돌려주는 것은 의도적입니다. 어떤 신선도 기준과
            비교해도 "오래됨"으로 판정되어, 시각을 모르는 저장본을
            신선한 것처럼 쓰는 사고를 막습니다.
        """
        if self.collected_at is None:
            return float("inf")
        delta = datetime.now(timezone.utc) - self.collected_at
        return max(0.0, delta.total_seconds())

    def is_fresh(self, max_age_seconds: float) -> bool:
        """
        주어진 기준 안에 수집된 저장본인지.

        파라미터:
            max_age_seconds : 신선하다고 볼 최대 나이(초).
                              services/datasets.py의 MAX_AGE_* 를 쓰세요.

        반환값:
            bool.

        주의사항:
            수집 시각을 모르면 항상 False입니다(age_seconds 참고).
        """
        return self.age_seconds <= max_age_seconds

    def collected_at_kst_str(self) -> str:
        """
        수집 시각을 화면에 쓸 KST 문자열로.

        파라미터:
            없음.

        반환값:
            "YYYY-MM-DD HH:MM:SS KST" 문자열. 시각을 모르면 "알 수 없음".

        주의사항:
            화면은 이 값을 **반드시** 함께 보여 줘야 합니다. 오래된 값을
            최신처럼 보여주는 것이 수집/표시 분리 구조의 가장 큰 위험입니다.
        """
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
    """
    중첩 구조를 JSON으로 쓸 수 있는 형태로 바꿉니다 (DataFrame은 태깅).

    파라미터:
        obj : dict/list/DataFrame/스칼라가 섞인 임의의 구조.

    반환값:
        JSON 직렬화 가능한 구조. DataFrame과 tuple은 되살릴 수 있도록
        특별한 키로 감쌉니다.

    주의사항:
        - **pickle을 쓰지 않는 이유**는 역직렬화 시 임의 코드 실행이
          가능해지기 때문입니다. DB 파일이 공유·이동될 수 있으므로
          받아들일 수 없는 위험입니다.
        - 코덱이 모르는 타입은 str()로 떨어집니다. 되읽을 때 문자열이
          되어 있다면 여기를 의심하세요.
    """
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
    """
    _encode_obj가 만든 구조를 원래 타입으로 되살립니다.

    파라미터:
        obj : _encode_obj의 결과를 JSON에서 읽은 값.

    반환값:
        DataFrame·tuple이 복원된 구조.

    주의사항:
        DataFrame 복원에 실패하면 경고만 남기고 **빈 DataFrame**을
        돌려줍니다. 예외로 화면을 죽이지 않기 위해서지만, 조용히 빈
        표가 보이면 로그를 확인하세요.
    """
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
    """
    DataFrame을 품은 중첩 dict/list를 타입을 보존하며 저장합니다.

    파라미터:
        name    : 스냅샷 이름.
        payload : {"자산": {"data": DataFrame, "error": None}} 처럼
                  DataFrame이 안쪽에 들어 있는 구조.
        status  : 품질 표시.
        error   : 수집 중 오류 메시지(있으면).
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        없음.

    주의사항:
        - 13F·COT 결과가 정확히 이 모양입니다. put_snapshot()으로 저장하면
          DataFrame이 레코드 리스트로 납작해져 화면이 깨집니다.
        - pickle을 쓰지 않는 이유는 역직렬화 시 임의 코드 실행이
          가능해지기 때문입니다. DB 파일이 공유·이동될 수 있다는 점을
          생각하면 받아들일 수 없는 위험입니다. 그래서 DataFrame만
          명시적으로 태깅하는 코덱을 씁니다.
        - 코덱이 모르는 타입은 str()로 떨어집니다. 되읽을 때 문자열이
          되어 있다면 여기를 의심하세요.
    """
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
    """
    JSON으로 표현 가능한 값을 스냅샷으로 upsert 합니다.

    파라미터:
        name    : 스냅샷 이름.
        payload : dict/list/스칼라 등 JSON으로 바꿀 수 있는 값.
        status  : 품질 표시("ok" | "estimated" 등).
        error   : 수집 중 오류 메시지(있으면).
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        없음.

    주의사항:
        **DataFrame이 섞여 있으면 이 함수를 쓰지 마세요.** JSON으로
        납작해져 레코드 리스트가 되고, 읽을 때 화면이 기대하는 타입이
        아닙니다. DataFrame 하나면 put_frame(), DataFrame을 품은 중첩
        구조면 put_object()를 쓰세요.
    """
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
    DataFrame을 스냅샷으로 저장합니다 (같은 이름이면 덮어씁니다).

    파라미터:
        name    : 스냅샷 이름. services/datasets.py의 규칙을 따르세요.
        df      : 저장할 DataFrame. None이면 payload가 null로 저장됩니다.
        status  : "ok" | "estimated" 등 화면에 보여 줄 품질 표시.
                  추정치를 저장할 때는 반드시 "estimated"를 주세요.
        error   : 수집 중 발생한 오류 메시지(있으면).
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        없음. 실패하면 예외가 올라갑니다.

    주의사항:
        - orient="split" + date_format="iso"라서 인덱스(날짜)와 컬럼 순서가
          보존되고 NaN은 null이 됩니다.
        - **df.attrs를 따로 싣습니다.** to_json은 attrs를 버리는데, 이
          프로젝트는 attrs에 is_proxy / is_intraday / source_label 같은
          "이 값은 실제 지표가 아니다" 표시를 담습니다. 잃어버리면 추정치가
          공식 데이터처럼 화면과 AI 리포트에 나갑니다.
        - **dtype도 따로 싣습니다.** orient="split"은 dtype을 저장하지
          않아서, 읽을 때 pandas가 숫자로만 된 문자열 컬럼("069500",
          cusip "037833100")을 int로 바꿔 **앞자리 0을 영구히 날립니다.**
          종목코드가 69500이 되면 Daum/pykrx/yfinance 조회가 전부 실패합니다.
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
    """
    저장된 스냅샷 1건을 읽습니다.

    파라미터:
        name    : 스냅샷 이름.
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        Snapshot 객체. 없거나 읽기에 실패하면 None.
        payload는 저장할 때 쓴 함수에 따라 타입이 복원됩니다
        (put_frame → DataFrame, put_object → 중첩 구조, put_snapshot → 원본).

    주의사항:
        - **None은 "없음"과 "읽기 실패"를 구분하지 않습니다.** DB 오류와
          역직렬화 실패는 경고 로그만 남기고 None이 됩니다. 화면이
          조용히 비는 것을 막으려면 호출부에서 로그를 확인하세요.
        - 반환된 Snapshot의 신선도는 .age_seconds / .is_fresh()로 봅니다.
          collected_at이 없으면 age_seconds는 무한대입니다.
    """
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
    """
    put_frame이 저장한 payload를 DataFrame으로 되살립니다.

    파라미터:
        raw : 스냅샷에서 읽은 dict.

    반환값:
        복원된 DataFrame. 형식이 맞지 않으면 None.

    주의사항:
        저장 당시의 dtype과 attrs(is_proxy 등)를 함께 복원합니다. 이
        복원이 빠지면 종목코드의 앞자리 0이 사라지고, 추정치 표시가
        없어져 화면이 추정치를 공식 지표처럼 보여 줍니다.
    """
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
_TIMESERIES_UPSERT = """
    INSERT INTO timeseries (dataset, series_id, obs_date, value, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(dataset, series_id, obs_date) DO UPDATE SET
        value      = excluded.value,
        updated_at = excluded.updated_at
"""


def _timeseries_rows(
    dataset: str,
    series_id: str,
    data: pd.Series | pd.DataFrame,
    value_col: str | None,
    now: str,
) -> list[tuple]:
    """
    시계열 하나를 INSERT용 행 튜플 리스트로 바꿉니다.

    파라미터:
        dataset   : 데이터셋 이름 (예: "fred").
        series_id : 시리즈 식별자 (예: "DGS10").
        data      : 날짜 인덱스를 가진 Series 또는 DataFrame.
        value_col : DataFrame일 때 쓸 값 컬럼명. None이면 첫 숫자 컬럼.
        now       : updated_at에 넣을 UTC ISO 문자열. 한 번의 저장에서
                    모든 행이 같은 값을 갖도록 호출부가 만들어 넘깁니다.

    반환값:
        (dataset, series_id, obs_date, value, updated_at) 튜플의 리스트.
        쓸 수 있는 행이 없으면 빈 리스트.

    주의사항:
        날짜로 해석되지 않는 인덱스는 조용히 버립니다. 인덱스가 통째로
        날짜가 아니면 빈 리스트가 나오므로, 호출부에서 "0행 저장"이
        보이면 인덱스부터 확인하세요.
    """
    series = _coerce_series(data, value_col)
    if series is None or series.empty:
        return []

    return [
        (dataset, series_id, _date_key(idx), _to_float_or_none(val), now)
        for idx, val in series.items()
        if _date_key(idx) is not None
    ]


def put_timeseries(
    dataset: str,
    series_id: str,
    data: pd.Series | pd.DataFrame,
    *,
    value_col: str | None = None,
    db_path: Path | None = None,
) -> int:
    """
    날짜 인덱스를 가진 수치 시계열을 upsert 합니다.

    파라미터:
        dataset   : 데이터셋 이름. 같은 이름끼리 한 묶음으로 조회됩니다.
        series_id : 시리즈 식별자.
        data      : 날짜 인덱스를 가진 Series 또는 DataFrame.
        value_col : DataFrame일 때 쓸 값 컬럼명. None이면 첫 숫자 컬럼.
        db_path   : DB 경로 오버라이드(테스트용). None이면 기본 경로.

    반환값:
        실제로 반영한 행 수(int). 쓸 행이 없으면 0.

    주의사항:
        같은 (dataset, series_id, 날짜)는 **최신 값으로 덮어씁니다.**
        그래서 매일 수집하면 과거는 그대로 유지되고 최근 값만 갱신됩니다.
        추정치를 여기에 넣으면 확정치를 덮어쓸 수 있으니, 호출부에서
        is_estimated 같은 표시를 먼저 확인하세요
        (collector.py의 _task_fed_liquidity가 그렇게 합니다).
    """
    rows = _timeseries_rows(dataset, series_id, data, value_col, _utc_now_iso())
    if not rows:
        return 0

    with connect(db_path) as conn:
        conn.executemany(_TIMESERIES_UPSERT, rows)
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
    누적된 시계열을 DataFrame으로 읽습니다.

    파라미터:
        dataset    : 데이터셋 이름.
        series_id  : 시리즈 식별자.
        start_date : "YYYY-MM-DD". 주면 그 날짜 이후만.
        value_name : 값 컬럼의 이름. None이면 series_id를 씁니다.
        db_path    : DB 경로 오버라이드(테스트용).

    반환값:
        DatetimeIndex를 가진 DataFrame. 데이터가 없거나 조회에 실패하면
        **빈 DataFrame**입니다(None이 아닙니다).

    주의사항:
        - 값이 NULL인 행은 버립니다(dropna). 행 수가 기대와 다르면
          이것을 먼저 의심하세요.
        - 조회 실패도 빈 DataFrame이라 "데이터 없음"과 구분되지 않습니다.
          구분이 필요하면 로그를 보세요.
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
    """
    와이드 DataFrame의 각 컬럼을 series_id로 삼아 한꺼번에 누적합니다.

    파라미터:
        dataset : 데이터셋 이름.
        df      : 날짜 인덱스 + 컬럼마다 하나의 시계열을 담은 DataFrame.
        columns : 저장할 컬럼 이름들. None이면 숫자형 컬럼을 모두 씁니다.
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        모든 컬럼에 대해 반영한 행 수의 합(int).

    주의사항:
        - [성능] 예전에는 컬럼마다 put_timeseries()를 불러서, 컬럼 수만큼
          커넥션을 새로 열고 트랜잭션을 따로 커밋했습니다. 지금은 커넥션
          하나에서 한 트랜잭션으로 씁니다. 덕분에 **전부 저장되거나 전부
          저장되지 않거나** 둘 중 하나가 됩니다(예전에는 중간에 실패하면
          앞쪽 컬럼만 저장된 어중간한 상태가 남았습니다).
        - df에 없는 컬럼 이름을 columns로 주면 조용히 건너뜁니다.
    """
    if df is None or df.empty:
        return 0

    targets = list(columns) if columns else [
        c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])
    ]

    now = _utc_now_iso()
    rows: list[tuple] = []
    for col in targets:
        if col in df.columns:
            rows.extend(
                _timeseries_rows(dataset, str(col), df[col], None, now)
            )

    if not rows:
        return 0

    with connect(db_path) as conn:
        conn.executemany(_TIMESERIES_UPSERT, rows)
    return len(rows)


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
    날짜별 레코드를 (dataset, 날짜, entity) 단위로 upsert 합니다.

    파라미터:
        dataset    : 데이터셋 이름.
        obs_date   : "YYYY-MM-DD" 관측 날짜.
        records    : 레코드 dict의 리스트.
        entity_key : 각 레코드에서 종목 식별자로 쓸 키 이름.
        db_path    : DB 경로 오버라이드(테스트용).

    반환값:
        반영한 행 수(int). entity_key가 없는 레코드는 건너뛰므로
        len(records)보다 작을 수 있습니다.

    주의사항:
        **이 테이블이 곧 백업입니다.** Naver·Daum 수급 랭킹은 "현재
        시점"만 제공하고 과거 조회가 불가능합니다. 수집기가 도는 동안
        여기에 쌓이는 것이 외부에서 다시 받을 수 없는 유일한 이력이므로,
        data/dashboard.db는 백업할 가치가 있습니다.
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
    누적된 날짜별 레코드를 DataFrame으로 읽습니다.

    파라미터:
        dataset    : 데이터셋 이름.
        obs_date   : 특정 날짜만 읽을 때.
        start_date : 그 날짜 이후 전체를 읽을 때.
        db_path    : DB 경로 오버라이드(테스트용).

    반환값:
        레코드를 행으로 펼친 DataFrame. 없으면 빈 DataFrame.
        각 행에 "수집일자" 컬럼이 채워집니다.

    주의사항:
        JSON으로 되읽으므로 저장 당시의 dtype이 보존되지 않습니다.
        종목코드처럼 앞자리 0이 중요한 값은 문자열인지 확인하세요.
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
    """
    이력이 쌓인 날짜 목록을 돌려줍니다.

    파라미터:
        dataset : 데이터셋 이름.
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        "YYYY-MM-DD" 문자열의 오름차순 리스트. 없으면 빈 리스트.

    주의사항:
        거래일만 들어 있지 않습니다. 수집기가 돈 날만 쌓이므로, 수집기를
        꺼 둔 날은 빠져 있습니다.
    """
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
    """
    실행이 살아 있음을 기록합니다.

    파라미터:
        run_id  : start_run이 돌려준 id.
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        없음.

    주의사항:
        13F처럼 한 태스크가 10분을 넘기는 경우가 있어 태스크마다 찍어야
        합니다. 이게 없으면 죽은 수집기를 "진행 중"으로 오인합니다.
    """
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
    """
    해당 PID의 프로세스가 살아 있는지 확인합니다.

    파라미터:
        pid : 확인할 프로세스 id. None도 받습니다.

    반환값:
        bool. 살아 있으면 True.

    주의사항:
        **같은 머신에서만 의미가 있습니다.** 다른 호스트에서 기록된 PID는
        이 머신의 무관한 프로세스와 우연히 겹칠 수 있습니다.
    """
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
    기록된 status를 실제 상태로 해석합니다.

    파라미터:
        run : read_last_run()이 돌려준 실행 기록 dict. None도 받습니다.

    반환값:
        "ok" | "partial" | "fail" | "running" | "interrupted" | "none".

    주의사항:
        DB에 'running'으로 남아 있어도 프로세스가 죽었거나 heartbeat가
        끊겼으면 **'interrupted'로 판정합니다.** 화면·CLI에 상태를 보여
        줄 때는 DB 값이 아니라 반드시 이 함수를 거치세요. 그래야
        --status가 거짓말을 하지 않습니다.
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
    비정상 종료로 'running'에 남은 기록을 정리합니다.

    파라미터:
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        정리한 행 수(int).

    주의사항:
        수집기가 시작할 때 부릅니다. 빠뜨리면 절전·강제 종료로 남은
        기록이 영원히 "진행 중"으로 보입니다.
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
    """
    수집 실행 하나를 마감 처리합니다.

    파라미터:
        run_id     : start_run()이 돌려준 실행 id.
        status     : "ok" | "partial" | "fail".
        ok_count   : 성공한 태스크 수.
        fail_count : 실패한 태스크 수.
        detail     : 실패 요약 문자열(있으면).
        db_path    : DB 경로 오버라이드(테스트용).

    반환값:
        없음.

    주의사항:
        이 함수가 불리지 않으면 그 실행은 영원히 'running'으로 남습니다.
        비정상 종료가 그런 기록을 남기므로, 수집기는 시작할 때
        mark_stale_runs_interrupted()로 먼저 정리합니다.
    """
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
    """
    가장 최근 수집 실행 기록 1건을 읽습니다.

    파라미터:
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        실행 기록 dict. 기록이 없으면 None.

    주의사항:
        여기 담긴 status는 **DB에 적힌 값 그대로**입니다. 비정상 종료한
        실행은 'running'으로 남아 있으므로, 화면에 보여 줄 때는
        resolve_run_status()를 거쳐 'interrupted'를 가려내세요.
    """
    try:
        with connect(db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def store_stats(db_path: Path | None = None) -> dict:
    """
    저장소 상태 화면용 요약 통계를 모읍니다.

    파라미터:
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        dict. exists / size_bytes / snapshots / timeseries_rows /
        observation_rows / last_run / last_run_status / task_summary.
        DB 파일이 없으면 exists=False이고 나머지는 기본값입니다.

    주의사항:
        - 행 수를 COUNT(*)로 셉니다. 누적이 수백만 행으로 커지면 이
          화면이 느려집니다. 그때는 purge_older_than()으로 정리하세요.
        - last_run의 status는 DB에 적힌 값 그대로입니다. 비정상 종료를
          가려내려면 last_run_status(resolve_run_status 결과)를 보세요.
    """
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
    태스크별 가장 최근 실행 결과를 1건씩 추립니다.

    파라미터:
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        태스크별 최신 기록 dict의 리스트. 실패하면 빈 리스트.

    주의사항:
        집계(성공 N·실패 M)만으로는 **어떤** 태스크가 **왜** 실패했는지
        알 수 없어서 태스크 단위로 추립니다.
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
    """
    태스크 실행 이력을 최신순으로 읽습니다.

    파라미터:
        task    : 태스크 이름. None이면 전체.
        limit   : 최대 행 수.
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        실행 기록 dict의 리스트(최신순). 없거나 실패하면 빈 리스트.

    주의사항:
        `python collector.py --history <태스크>` 가 이 함수를 씁니다.
        터미널을 닫으면 사라지는 로그와 달리 여기에는 남으므로, 간헐적
        실패를 추적할 때 먼저 보세요.
    """
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

    파라미터:
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        {"name", "label"} dict의 리스트. 누락이 없으면 빈 리스트.

    주의사항:
        기대 목록이 **이 함수 안에 하드코딩**돼 있습니다. 수집 대상을
        바꾸면 여기도 함께 고쳐야 합니다. 안 그러면 멀쩡한 상태가
        "누락"으로 보이거나, 진짜 누락이 안 보입니다.
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
    저장본이 화면이 기대하는 컬럼을 갖고 있는지 확인합니다.

    파라미터:
        payload          : 저장본에서 읽은 값.
        required_columns : 반드시 있어야 할 컬럼들. None이면 검사 생략.
        name             : 경고 로그에 쓸 스냅샷 이름.

    반환값:
        bool. False면 저장본을 버리고 다시 수집해야 합니다.

    주의사항:
        DataFrame이 아니면 **통과시킵니다.** 스냅샷 종류마다 형태가 달라
        일괄 검증이 불가능하기 때문입니다. 이 검사는 예전 버전이 저장해 둔
        컬럼 구성이 다른 DataFrame이 화면에 넘어가 KeyError로 페이지를
        죽이는 것을 막는 용도입니다.
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
# 수동 새로고침
# ==============================================================================
# 저장본을 **지우지 않고** 기준 시각 하나를 듭니다. "이 시각 이전에 수집된
# 저장본은 낡은 것으로 본다"는 뜻입니다. 지워 버리면 재수집이 실패했을 때
# 보여 줄 값이 아예 없어집니다.
_refresh_token: float = 0.0

# 한 번의 새로고침에서 이미 시도한 스냅샷. 실패한 소스를 rerun마다 다시
# 부르면 화면이 계속 느려지므로, 새로고침 1회당 1번만 시도합니다.
_refresh_attempted: dict[str, float] = {}


def request_refresh() -> None:
    """
    다음 조회에서 저장본을 낡은 것으로 보고 다시 수집하게 합니다.

    파라미터:
        없음.

    반환값:
        없음. 모듈 전역의 기준 시각만 바꿉니다.

    주의사항:
        - 화면의 새로고침 버튼은 이것과 st.cache_data.clear()를 **함께**
          불러야 합니다. 캐시만 비우면 아직 신선한 저장본이 그대로
          반환돼 화면이 바뀌지 않습니다.
        - store_only 모드에서는 효과가 없습니다. "화면이 절대 외부를
          기다리지 않는다"는 약속이 우선이라 저장본 재조회만 일어납니다.
    """
    global _refresh_token
    _refresh_token = time.time()
    _refresh_attempted.clear()


def refresh_requested_at() -> float:
    """
    마지막 새로고침 요청 시각.

    파라미터:
        없음.

    반환값:
        epoch 초(float). 요청이 없었으면 0.0.

    주의사항:
        프로세스 전역 상태입니다. 앱을 재시작하면 0.0으로 돌아갑니다.
    """
    return _refresh_token


def _superseded_by_refresh(name: str, snap: "Snapshot | None") -> bool:
    """
    이 저장본이 새로고침 요청보다 먼저 수집된 것인지 판정합니다.

    파라미터:
        name : 스냅샷 이름.
        snap : 읽어 둔 Snapshot 또는 None.

    반환값:
        bool. True면 다시 수집해야 합니다.

    주의사항:
        같은 이름을 **새로고침 1회당 한 번만** True로 봅니다. 수집이
        실패하면 저장본의 시각이 그대로 남아, 이후 모든 rerun에서
        실패하는 외부 호출을 반복하게 되기 때문입니다.
    """
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

    **이 프로젝트에서 가장 중요한 함수입니다.** 화면의 거의 모든 데이터
    조회가 여기를 지나갑니다. "왜 화면이 느린가" / "왜 새로고침해도
    안 바뀌는가" / "왜 옛날 값이 보이는가"의 답이 전부 이 함수 안에
    있습니다.

    파라미터:
        name             : 스냅샷 이름. services/datasets.py가 만드는
                           규칙적인 문자열을 쓰세요. 이름이 겹치면 서로
                           덮어씁니다.
        live_fn          : 인자 없이 호출해 실제 수집을 수행하는 함수.
                           **네트워크를 타는 무거운 함수**입니다.
        max_age_seconds  : 저장본을 "신선하다"고 볼 최대 나이(초).
                           datasets.MAX_AGE_* 상수를 쓰세요.
        empty_value      : 줄 값이 아무것도 없을 때 돌려줄 기본값.
                           DataFrame을 기대하는 호출부라면 pd.DataFrame()을
                           넘기세요. 기본값 None을 그대로 두면 호출부에서
                           AttributeError가 나기 쉽습니다.
        as_frame         : 결과를 DataFrame 전용 형식으로 저장할지 여부.
        as_object        : 결과를 "DataFrame을 품은 중첩 dict/list" 형식으로
                           저장할지 여부. as_frame과 동시에 켜지 마세요.
        required_columns : 저장본 DataFrame이 반드시 가져야 할 컬럼들.

    반환값:
        수집 결과 또는 저장본. 둘 다 없으면 empty_value.
        **반환 타입은 live_fn이 돌려주는 것과 같습니다.**

    주의사항:
        - 읽기 모드(DASHBOARD_READ_MODE)에 따라 동작이 크게 달라집니다.
            auto       : 신선하면 저장본, 아니면 수집 + 저장 (기본)
            store_only : 저장본만. 오래됐어도 그대로 주고, 없으면
                         empty_value. 화면이 절대 외부를 기다리지 않습니다.
            live_only  : 저장 계층 무시. 항상 live_fn을 부릅니다.
                         collector.py가 이 모드로 돕니다.
        - **수집이 실패하면 오래된 저장본을 그대로 내려줍니다.** 외부 소스
          장애 시 화면이 비는 것보다 낫다는 판단입니다. 즉 반환값이
          "지금 수집한 값"이라는 보장이 없습니다. 화면은 반드시 수집
          시각을 함께 표시해야 합니다(Snapshot.collected_at_kst_str()).
        - 저장 실패는 삼킵니다. 저장이 안 됐다고 화면을 막지는 않습니다.
        - required_columns를 주면 저장본의 컬럼 구성을 검사합니다. 예전
          버전이 저장해 둔 스냅샷은 컬럼이 달라져 있을 수 있고, 그대로
          화면에 넘기면 KeyError로 페이지가 죽습니다. 맞지 않으면 저장본을
          버리고 다시 수집합니다.
        - request_refresh() 이후에는 그보다 먼저 수집된 저장본을 낡은
          것으로 봅니다. 단, **새로고침 1회당 이름 하나를 한 번만**
          다시 시도합니다. 실패하는 소스를 rerun마다 재호출하면 화면이
          계속 느려지기 때문입니다.
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
        # store_only의 약속은 "화면이 절대 외부를 기다리지 않는다"입니다.
        # 오래됐더라도 있는 저장본을 그대로 주고, 없으면 빈 값을 줍니다.
        return snap.payload if snap_ok else empty_value

    forced = _superseded_by_refresh(name, snap)

    if snap_ok and snap.is_fresh(max_age_seconds) and not forced:
        return snap.payload

    if _refresh_token:
        # 성공·실패와 무관하게 "이번 새로고침에서 시도했음"을 남깁니다.
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
    """
    수집 결과가 "비어 있음"인지 판정합니다.

    파라미터:
        value : 수집 함수가 돌려준 값.

    반환값:
        bool. None·빈 DataFrame·빈 컨테이너면 True.

    주의사항:
        숫자 0과 False는 **비어 있지 않은 것**으로 봅니다. 0도 유효한
        관측값이기 때문입니다.
    """
    if value is None:
        return True
    if isinstance(value, pd.DataFrame):
        return value.empty
    if isinstance(value, (list, dict, tuple, str)):
        return len(value) == 0
    return False


def purge_older_than(days: int, db_path: Path | None = None) -> dict[str, int]:
    """
    지정 일수보다 오래된 누적 이력을 삭제합니다.

    파라미터:
        days    : 보관할 일수. 이보다 오래된 행을 지웁니다.
        db_path : DB 경로 오버라이드(테스트용).

    반환값:
        {"timeseries": 삭제행수, "observations": 삭제행수} dict.

    주의사항:
        - **되돌릴 수 없습니다.** observations의 수급 이력은 외부에서
          다시 받을 수 없습니다(Naver·Daum은 과거 조회 미지원).
          지우기 전에 data/dashboard.db를 백업하세요.
        - 기본 운용에서는 쓸 일이 없습니다. 하루 수집량이 작습니다.
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
