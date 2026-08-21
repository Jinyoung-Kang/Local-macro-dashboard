"""
views/cot_view.py
🇺🇸 글로벌 스마트머니 (CFTC COT) 분석 뷰
"""
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from services.cot_service import fetch_cftc_cot_legacy, CFTCTransientError
from config import ASSET_CODES


def render_cot_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    now_str = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🇺🇸 글로벌 스마트머니 (CFTC COT) 분석
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            미국 상품선물거래위원회(CFTC) 주간 공시 데이터를 바탕으로 비상업(투기적) 포지션과 상업(헤저) 포지션의 흐름을 분석합니다.
        </p>
    </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1.5, 2, 1])
    with col1:
        selected_asset = st.selectbox(
            "분석 대상 자산 선택",
            options=list(ASSET_CODES.keys()),
            index=0
        )
    with col2:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        st.caption(f"🕒 **시스템 현재 시각**: `{now_str}`")
    with col3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 최신 데이터 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    weeks_to_fetch = 156

    with st.spinner(f"{selected_asset}의 COT 주체별 데이터를 불러오는 중..."):
        try:
            df = fetch_cftc_cot_legacy(
                ASSET_CODES[selected_asset],
                limit=weeks_to_fetch,
            )
        except CFTCTransientError as e:
            st.error(f"CFTC 데이터 수집 실패: {e}")
            return
        except Exception as e:
            st.error(f"알 수 없는 오류: {e}")
            return

    if df is None or df.empty:
        st.warning("표시할 COT 데이터가 없습니다.")
        return

    df = df.sort_values(by="date")

    latest = df.iloc[-1]
    prev_1w = df.iloc[-2] if len(df) > 1 else latest
    prev_4w = df.iloc[-5] if len(df) >= 5 else latest
    
    cot_date = latest["date"].date() if hasattr(latest["date"], "date") else pd.to_datetime(latest["date"]).date()
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    age_days = (today - cot_date).days

    nc_net_val = latest["nc_net"]
    nc_net_1w_diff = nc_net_val - prev_1w["nc_net"]
    nc_net_4w_diff = nc_net_val - prev_4w["nc_net"]

    comm_net_val = latest["comm_net"]
    comm_net_1w_diff = comm_net_val - prev_1w["comm_net"]

    st.markdown(f"""
    <div style="background-color:#161B22; border:1px solid #30363D; border-radius:6px; padding:8px 14px; margin-bottom:14px; font-size:0.88rem; color:#8B949E; display:flex; justify-content:space-between; align-items:center;">
        <span>📅 <strong>최신 공시 기준일</strong>: <span style="color:#58A6FF;">{cot_date.strftime('%Y-%m-%d')} (수집 시점 기준 {age_days}일 전)</span></span>
        <span>🏷️ 대상 자산: <strong>{selected_asset}</strong></span>
    </div>
    """, unsafe_allow_html=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        label="비상업 (투기/스마트머니) 순포지션",
        value=f"{int(nc_net_val):,} 계약",
        delta=f"1주 전 대비: {int(nc_net_1w_diff):+,}",
        delta_color="normal"
    )
    m2.metric(
        label="비상업 4주 변동 (추세)",
        value=f"{int(nc_net_4w_diff):+,} 계약",
        delta="4주 누적 변화량",
        delta_color="off"
    )
    m3.metric(
        label="상업 (헤저/실수요) 순포지션",
        value=f"{int(comm_net_val):,} 계약",
        delta=f"1주 전 대비: {int(comm_net_1w_diff):+,}",
        delta_color="inverse"
    )
    
    nc_pctile = float(df["nc_net"].rank(pct=True).iloc[-1] * 100)
    m4.metric(
        label="투기적 순포지션 3년 백분위 (Percentile)",
        value=f"{nc_pctile:.1f} %",
        delta="100%=역대급 매수 / 0%=역대급 매도",
        delta_color="off"
    )

    st.divider()

    st.subheader("📈 스마트머니 포지셔닝 추세")
    st.caption("비상업(Non-Commercial) 순포지션 변화를 통해 글로벌 투기 자본의 매수/매도 압력을 파악합니다.")

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.6, 0.4],
        subplot_titles=(
            f"{selected_asset} 투기적(비상업) 순포지션 추이",
            "상업(헤저) vs 비상업 포지션 비교"
        )
    )

    fig.add_trace(
        go.Bar(
            x=df["date"],
            y=df["nc_net"],
            name="비상업 순포지션",
            marker_color=["#238636" if val >= 0 else "#DA3633" for val in df["nc_net"]]
        ),
        row=1, col=1
    )
    
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["nc_net"].rolling(window=4).mean(),
            name="4주 이동평균",
            line=dict(color="#58A6FF", width=2),
            mode="lines"
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["nc_net"],
            name="비상업",
            line=dict(color="#58A6FF", width=2),
            mode="lines"
        ),
        row=2, col=1
    )
    
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["comm_net"],
            name="상업(헤저)",
            line=dict(color="#E3B341", width=2, dash="dot"),
            mode="lines"
        ),
        row=2, col=1
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0D1117",
        plot_bgcolor="#161B22",
        height=650,
        margin=dict(l=30, r=30, t=50, b=30),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    
    fig.update_yaxes(title_text="계약 수 (순포지션)", row=1, col=1, gridcolor="#21262D")
    fig.update_yaxes(title_text="계약 수 비교", row=2, col=1, gridcolor="#21262D")

    st.plotly_chart(fig, use_container_width=True)

    with st.expander("📊 COT 데이터 상세 테이블 (최근 15주)", expanded=False):
        df_display = df.tail(15).sort_values(by="date", ascending=False).copy()
        df_display["date"] = df_display["date"].dt.strftime("%Y-%m-%d")
        
        for col in ["nc_long", "nc_short", "nc_net", "comm_long", "comm_short", "comm_net"]:
            df_display[col] = df_display[col].apply(lambda x: f"{int(x):,}")
            
        st.dataframe(
            df_display[["date", "nc_net", "nc_long", "nc_short", "comm_net", "comm_long", "comm_short"]],
            use_container_width=True,
            hide_index=True
        )
