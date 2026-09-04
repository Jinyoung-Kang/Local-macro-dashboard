"""
views/cot_view.py
🇺🇸 글로벌 스마트머니 (CFTC COT) 분석 뷰

[수정 사항]
- CFTC COT 리포트는 매주 화요일 포지션을 그 주 금요일 15:30 ET에 발표하는
  구조적 3영업일 지연이 있습니다. 이 발표 주기를 사용자가 "매주 화요일 갱신"으로
  오해하지 않도록, 최신 공시일이 이번 주 화요일보다 오래된 경우
  "다음 CFTC 발표 예정 시각" 안내를 화면에 함께 표시합니다.
"""
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.cot_service import fetch_cftc_cot_legacy, CFTCTransientError, COT_ASSETS


def _get_next_cftc_release_notice(cot_date, now_kst: datetime) -> str:
    et = ZoneInfo("America/New_York")
    now_et = now_kst.astimezone(et)

    # 오늘 이전(또는 오늘)에 가장 최근으로 지난 화요일을 구합니다.
    # weekday(): 월=0, 화=1, ..., 일=6
    days_since_tuesday = (now_et.weekday() - 1) % 7
    latest_tuesday = now_et.date() - timedelta(days=days_since_tuesday)

    latest_friday_release = latest_tuesday + timedelta(days=3)
    latest_friday_release_et = datetime.combine(
        latest_friday_release,
        datetime.min.time(),
        tzinfo=et,
    ).replace(hour=15, minute=30)

    latest_friday_release_kst = latest_friday_release_et.astimezone(
        ZoneInfo("Asia/Seoul")
    )

    # 아직 이번 주기 금요일 발표 시각이 되지 않았다면,
    # 최신 데이터가 지난 화요일 기준이라도 정상입니다.
    if now_et < latest_friday_release_et:
        return ""

    if cot_date < latest_tuesday:
        return (
            f"다음 CFTC COT 발표 예정: {latest_friday_release_et.strftime('%Y-%m-%d')} "
            f"15:30 ET (한국시간 {latest_friday_release_kst.strftime('%Y-%m-%d %H:%M')} KST) · "
            f"{latest_tuesday.strftime('%Y-%m-%d')}(화) 기준 데이터가 아직 공시되지 않았습니다. "
            "CFTC COT는 매주 화요일 포지션을 3영업일 뒤인 금요일 오후에 발표하는 "
            "구조적 지연이 있어 정상적인 상태입니다."
        )
    return ""

def render_cot_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    now_str = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    st.markdown(
        """
        <div style="padding: 4px 0 12px 0;">
            <h2 style="margin:0; font-weight:700; color:#F0F6FC;">
                🇺🇸 글로벌 스마트머니 (CFTC COT) 분석
            </h2>
            <p style="margin:4px 0 0 0; color:#8B949E; font-size:0.92rem;">
                미국 상품선물거래위원회(CFTC) 주간 공시 데이터를 바탕으로
                비상업(투기적) 포지션과 상업(헤저) 포지션의 흐름을 분석합니다.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1.5, 2, 1])
    with col1:
        selected_asset = st.selectbox(
            "분석 대상 자산 선택",
            options=list(COT_ASSETS.keys()),
            index=0,
        )
    with col2:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        st.caption(f"⏰ 시스템 현재 시각: {now_str}")
    with col3:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if st.button("🔄 최신 데이터 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    weeks_to_fetch = 156

    with st.spinner(f"{selected_asset} CFTC COT 데이터를 수집하는 중..."):
        try:
            df = fetch_cftc_cot_legacy(
                COT_ASSETS[selected_asset]["code"],
                limit=weeks_to_fetch,
            )
        except CFTCTransientError as e:
            st.error(f"CFTC 서버 일시 오류: {e}")
            return
        except Exception as e:
            st.error(f"데이터 수집 중 오류가 발생했습니다: {e}")
            return

    if df is None or df.empty:
        st.warning("COT 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
        return

    df = df.sort_values(by="date")

    latest = df.iloc[-1]
    prev_1w = df.iloc[-2] if len(df) > 1 else latest
    prev_4w = df.iloc[-5] if len(df) > 5 else latest

    cot_date = (
        latest["date"].date()
        if hasattr(latest["date"], "date")
        else pd.to_datetime(latest["date"]).date()
    )
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    age_days = (today - cot_date).days

    nc_net_val = latest["nc_net"]
    nc_net_1w_diff = nc_net_val - prev_1w["nc_net"]
    nc_net_4w_diff = nc_net_val - prev_4w["nc_net"]
    comm_net_val = latest["comm_net"]
    comm_net_1w_diff = comm_net_val - prev_1w["comm_net"]

    st.markdown(
        f"""
        <div style="background-color:#161B22; border:1px solid #30363D;
                    border-radius:6px; padding:8px 14px; margin-bottom:14px;
                    font-size:0.88rem; color:#8B949E; display:flex;
                    justify-content:space-between; align-items:center;">
            <span>📅 최신 공시 기준일:
                <strong style="color:#58A6FF;">{cot_date.strftime('%Y-%m-%d')}</strong>
                (수집 시점 기준 {age_days}일 전)
            </span>
            <span>🏷️ 대상 자산: <strong>{selected_asset}</strong></span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # [신규] 다음 CFTC 발표 예정 시각 안내
    # 최신 공시일이 이번 주 화요일보다 오래됐다면, 아직 이번 주 금요일 발표가
    # 이루어지지 않은 것이므로 발표 지연이 아니라 정상적인 공시 주기임을 안내합니다.
    release_notice = _get_next_cftc_release_notice(cot_date, now_kst)
    if release_notice:
        st.info(f"ℹ️ {release_notice}")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        label="비상업 (투기/스마트머니) 순포지션",
        value=f"{int(nc_net_val):,} 계약",
        delta=f"1주 전 대비: {int(nc_net_1w_diff):+,}",
        delta_color="normal",
    )
    m2.metric(
        label="비상업 4주 변동 (추세)",
        value=f"{int(nc_net_4w_diff):+,} 계약",
        delta="4주 누적 변화량",
        delta_color="off",
    )
    m3.metric(
        label="상업 (헤저/실수요) 순포지션",
        value=f"{int(comm_net_val):,} 계약",
        delta=f"1주 전 대비: {int(comm_net_1w_diff):+,}",
        delta_color="inverse",
    )
    nc_pctile = float(df["nc_net"].rank(pct=True).iloc[-1] * 100)
    m4.metric(
        label="투기적 순포지션 3년 백분위 (Percentile)",
        value=f"{nc_pctile:.1f} %",
        delta="↑ 100%=역대급 매수 / 0%=역대급 매도",
        delta_color="off",
    )

    st.divider()

    st.subheader(f"{selected_asset} 포지셔닝 추이")
    st.caption("Non-Commercial(비상업/투기) 순포지션과 4주 이동평균, Commercial(상업/헤저) 순포지션을 함께 표시합니다.")

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.6, 0.4],
        subplot_titles=(
            f"{selected_asset} 비상업 순포지션",
            "비상업 vs 상업 순포지션 비교",
        ),
    )

    fig.add_trace(
        go.Bar(
            x=df["date"],
            y=df["nc_net"],
            name="비상업 순포지션",
            marker_color=["#238636" if val >= 0 else "#DA3633" for val in df["nc_net"]],
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["nc_net"].rolling(window=4).mean(),
            name="4주 이동평균",
            line=dict(color="#58A6FF", width=2),
            mode="lines",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["nc_net"],
            name="비상업(투기)",
            line=dict(color="#58A6FF", width=2),
            mode="lines",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["comm_net"],
            name="상업(헤저)",
            line=dict(color="#E3B341", width=2, dash="dot"),
            mode="lines",
        ),
        row=2,
        col=1,
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0D1117",
        plot_bgcolor="#161B22",
        height=650,
        margin=dict(l=30, r=30, t=50, b=30),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_yaxes(title_text="순포지션 (계약)", row=1, col=1, gridcolor="#21262D")
    fig.update_yaxes(title_text="순포지션 (계약)", row=2, col=1, gridcolor="#21262D")

    st.plotly_chart(fig, use_container_width=True)

    with st.expander("📄 COT 원본 데이터 (최근 15주)", expanded=False):
        df_display = df.tail(15).sort_values(by="date", ascending=False).copy()
        df_display["date"] = df_display["date"].dt.strftime("%Y-%m-%d")
        for col in ["nc_long", "nc_short", "nc_net", "comm_long", "comm_short", "comm_net"]:
            df_display[col] = df_display[col].apply(lambda x: f"{int(x):,}")
        st.dataframe(
            df_display[["date", "nc_net", "nc_long", "nc_short", "comm_net", "comm_long", "comm_short"]],
            use_container_width=True,
            hide_index=True,
        )
