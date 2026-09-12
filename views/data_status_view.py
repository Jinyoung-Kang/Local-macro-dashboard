"""
views/data_status_view.py
데이터 저장소(SQLite) 상태 화면 및 사이드바 신선도 표시.

수집/표시를 분리하면 "지금 보고 있는 숫자가 언제 수집된 것인가"가
반드시 화면에 드러나야 합니다. 오래된 값을 최신처럼 보여주는 것이
수집 분리의 가장 큰 위험이기 때문입니다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from services import datasets, store


# ==============================================================================
# 사이드바: 한 줄 신선도 표시
# ==============================================================================
def render_data_freshness_sidebar() -> None:
    """
    사이드바에 "데이터가 얼마나 신선한지"를 한 줄로 표시합니다.

    수집기가 돌고 있지 않으면 그 사실을 알려 줍니다. 그래야 사용자가
    "왜 느린지"(= 화면이 직접 수집하고 있다) 이해할 수 있습니다.
    """
    mode = store.get_read_mode()

    try:
        snap = store.read_snapshot(datasets.SNAP_MACRO_COLLECTED)
        last_run = store.read_last_run()
    except Exception as e:
        st.sidebar.caption(f"저장소 상태 조회 실패: `{e}`")
        return

    if snap is None and last_run is None:
        st.sidebar.warning(
            "수집기가 아직 실행된 적이 없습니다.\n\n"
            "화면이 직접 수집하므로 느릴 수 있습니다. "
            "터미널에서 `python collector.py --loop`을 실행하면 "
            "로딩이 크게 빨라집니다.",
            icon="⚠️",
        )
        return

    if snap is not None and snap.collected_at is not None:
        age_min = snap.age_seconds / 60
        if age_min < 10:
            icon, tone = "🟢", "최신"
        elif age_min < 60:
            icon, tone = "🟡", "다소 지연"
        else:
            icon, tone = "🔴", "오래됨"

        st.sidebar.caption(
            f"{icon} 저장 데이터 {tone} · {age_min:,.0f}분 전\n\n"
            f"수집 시각: `{snap.collected_at_kst_str()}`"
        )

    if last_run and last_run.get("status") not in (None, "ok"):
        st.sidebar.caption(
            f"⚠️ 최근 수집 실패 {last_run.get('fail_count', 0)}건 "
            "— '🗄️ 데이터 저장소 상태' 메뉴에서 확인"
        )

    if mode != store.READ_MODE_AUTO:
        st.sidebar.caption(f"읽기 모드: `{mode}`")


# ==============================================================================
# 본 화면
# ==============================================================================
def render_data_status_view() -> None:
    st.title("🗄️ 데이터 저장소 상태")
    st.caption(
        "수집기(collector.py)가 SQLite에 적재한 데이터의 현황입니다. "
        "화면은 이 저장본을 읽기만 하므로, 여기서 신선도와 수집 실패를 "
        "확인할 수 있습니다."
    )

    try:
        stats = store.store_stats()
    except Exception as e:
        st.error(f"저장소 상태를 읽지 못했습니다: {e}")
        return

    # ------------------------------------------------------------------ 요약
    if not stats["exists"]:
        st.warning(
            "아직 저장소가 만들어지지 않았습니다. "
            "터미널에서 아래를 실행하세요.",
            icon="⚠️",
        )
        st.code("python collector.py            # 1회 수집\n"
                "python collector.py --loop     # 상주 수집 (권장)", language="bash")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("DB 크기", f"{stats['size_bytes'] / 1024:,.0f} KB")
    c2.metric("스냅샷", f"{len(stats['snapshots']):,}개")
    c3.metric("누적 시계열", f"{stats['timeseries_rows']:,}행")
    c4.metric("누적 수급 이력", f"{stats['observation_rows']:,}행")

    st.caption(f"DB 경로: `{stats['db_path']}`")

    st.divider()

    # ------------------------------------------------------------- 최근 수집
    st.subheader("최근 수집 실행")
    last_run = stats.get("last_run")
    if not last_run:
        st.info("수집 실행 기록이 없습니다.")
    else:
        status = last_run.get("status", "?")
        badge = {
            "ok": ("✅ 정상", "green"),
            "partial": ("⚠️ 일부 실패", "orange"),
            "fail": ("❌ 전체 실패", "red"),
            "running": ("⏳ 진행 중", "blue"),
        }.get(status, (f"? {status}", "gray"))

        r1, r2, r3 = st.columns([1.2, 1, 1])
        r1.markdown(f"상태: :{badge[1]}[**{badge[0]}**]")
        r2.metric("성공", last_run.get("ok_count", 0))
        r3.metric("실패", last_run.get("fail_count", 0))

        st.caption(
            f"시작 `{_to_kst(last_run.get('started_at'))}` → "
            f"종료 `{_to_kst(last_run.get('finished_at')) or '진행 중'}`"
        )

        if last_run.get("detail"):
            with st.expander("실패 상세", expanded=status == "fail"):
                for line in str(last_run["detail"]).split("; "):
                    st.markdown(f"- {line}")

    st.divider()

    # --------------------------------------------------------- 스냅샷 신선도
    st.subheader("스냅샷 신선도")
    st.caption(
        "`최대 허용 수집 경과`를 넘으면 화면이 저장본 대신 직접 수집을 "
        "시도합니다 (읽기 모드 auto 기준)."
    )

    rows = []
    now = datetime.now(timezone.utc)
    for snap in stats["snapshots"]:
        collected = _parse(snap.get("collected_at"))
        age_min = (now - collected).total_seconds() / 60 if collected else None
        max_age_min = _expected_max_age_minutes(snap["name"])
        rows.append({
            "데이터셋": snap["name"],
            "상태": snap.get("status", "?"),
            "수집 시각(KST)": _to_kst(snap.get("collected_at")) or "알 수 없음",
            "경과(분)": round(age_min, 1) if age_min is not None else None,
            "최대 허용(분)": max_age_min,
            "신선": (
                None if age_min is None or max_age_min is None
                else age_min <= max_age_min
            ),
        })

    if rows:
        st.dataframe(
            pd.DataFrame(rows),
            width="stretch",
            hide_index=True,
            column_config={
                "경과(분)": st.column_config.NumberColumn(format="%.1f"),
                "신선": st.column_config.CheckboxColumn(
                    "신선", help="체크 해제면 화면이 직접 수집을 시도합니다",
                ),
            },
        )
    else:
        st.info("저장된 스냅샷이 없습니다.")

    st.divider()

    # ------------------------------------------------- 누적 수급 이력 (핵심)
    st.subheader("📈 누적 수급 이력")
    st.caption(
        "Naver·Daum은 **과거 날짜 조회를 지원하지 않습니다.** 수집기가 도는 "
        "동안 날짜별로 쌓인 이 데이터는 외부에서 다시 받을 수 없는, "
        "이 대시보드만의 자산입니다."
    )

    try:
        from services.radar_service import list_radar_history_dates, read_radar_history

        dates = list_radar_history_dates()
    except Exception as e:
        st.warning(f"수급 이력 조회 실패: {e}")
        dates = []

    if not dates:
        st.info(
            "아직 누적된 수급 이력이 없습니다. "
            "수집기를 거래일에 돌리면 하루씩 쌓입니다."
        )
    else:
        h1, h2 = st.columns([1, 2])
        h1.metric("누적 거래일", f"{len(dates)}일")
        h2.caption(f"기간: `{dates[0]}` ~ `{dates[-1]}`")

        picked = st.selectbox(
            "조회할 거래일", options=list(reversed(dates)), index=0,
        )
        hist = read_radar_history(start_date=picked)
        hist = hist[hist["수집일자"] == picked] if "수집일자" in hist.columns else hist

        if hist.empty:
            st.info("해당 거래일에 데이터가 없습니다.")
        else:
            show_cols = [
                c for c in [
                    "순위", "종목코드", "종목명", "현재가", "등락률(%)",
                    "순매수대금(억)", "시장", "투자주체", "매매구분", "데이터_출처",
                ]
                if c in hist.columns
            ]
            st.dataframe(
                hist[show_cols] if show_cols else hist,
                width="stretch",
                hide_index=True,
            )

    st.divider()

    # --------------------------------------------------------------- 사용법
    with st.expander("⚙️ 수집기 실행 방법", expanded=False):
        st.markdown(
            "**1회 수집**\n"
            "```bash\n"
            "python collector.py              # 전체\n"
            "python collector.py --only fast  # 시세·수급만\n"
            "python collector.py --only slow  # FRED·KRX 마감만\n"
            "```\n"
            "**상주 수집 (권장)**\n"
            "```bash\n"
            "python collector.py --loop       # fast 5분 / slow 1시간\n"
            "```\n"
            "**macOS 자동 시작**\n"
            "```bash\n"
            "python collector.py --install-launchd   # plist 예시 출력\n"
            "```\n"
            "**상태 확인 / 정리**\n"
            "```bash\n"
            "python collector.py --status\n"
            "python collector.py --purge-days 400    # 오래된 이력 정리\n"
            "```"
        )

        st.markdown(
            "**읽기 모드** (`DASHBOARD_READ_MODE` 환경변수)\n\n"
            "| 값 | 동작 |\n"
            "|---|---|\n"
            "| `auto` (기본) | 저장본이 신선하면 사용, 아니면 직접 수집 후 저장 |\n"
            "| `store_only` | 저장본만 사용. 화면이 외부를 절대 기다리지 않음 |\n"
            "| `live_only` | 저장 계층 무시 (분리 이전 동작) |\n"
        )


# ==============================================================================
# 헬퍼
# ==============================================================================
def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _to_kst(text: str | None) -> str | None:
    dt = _parse(text)
    if dt is None:
        return None
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")


def _expected_max_age_minutes(name: str) -> float | None:
    """데이터셋 이름으로 기대 신선도 기준을 추정합니다."""
    if name in (datasets.SNAP_MACRO_COLLECTED, datasets.SNAP_SCRAPER_MARKETS):
        return datasets.MAX_AGE_REALTIME / 60
    if name.startswith("radar.scanner."):
        return datasets.MAX_AGE_REALTIME / 60
    if name.startswith("fred.series.") or name in (
        datasets.SNAP_FED_LIQUIDITY,
        datasets.SNAP_KRX_FUTURES,
        datasets.SNAP_SECTOR_HISTORY,
    ):
        return datasets.MAX_AGE_DAILY / 60
    return None
