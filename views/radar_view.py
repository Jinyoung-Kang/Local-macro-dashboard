"""
views/radar_view.py
외국인/기관 수급 레이더 대시보드 뷰
실시간 순매수/순매도 스캐닝, 트리맵 시각화(Plotly 호환성 패치) 및 종목별 기준일 누적 수급 차트
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from services.radar_service import (
    get_market_radar_scanner,
    get_stock_cumulative_flow_from_base,
    test_kis_connection,
    test_ls_connection
)


def render_radar_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_date = now_kst.date()

    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🎯 외국인/기관 실시간 수급 레이더
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            장중 실시간 및 일자별 메이저 주체(외국인·기관)의 순매수/순매도 자금 흐름을 스캐닝합니다.
        </p>
    </div>
    """, unsafe_allow_html=True)

    # --------------------------------------------------------------------------
    # 0. 증권사 API 연결 상태 테스트 진단 구역
    # --------------------------------------------------------------------------
    with st.expander("🛠️ KIS / LS 증권사 API 연결 상태 테스트", expanded=False):
        st.write("장중 실시간 수급을 제공하는 KIS(한국투자증권) API의 작동 및 인증 상태를 점검합니다.")
        st.caption("ℹ️ LS증권 API는 참고용 연결 테스트만 제공하며, 시장 전체 순매수 랭킹 조회에는 사용되지 않습니다 (LS는 종목별 조회 전용 TR만 보유).")
        if st.button("🔌 KIS / LS API 테스트 실행", key="btn_test_broker_apis"):
            with st.spinner("KIS API 상태 점검 중..."):
                k_ok, k_msg = test_kis_connection()
            with st.spinner("LS API 상태 점검 중 (참고용)..."):
                l_ok, l_msg = test_ls_connection()

            c1, c2 = st.columns(2)
            with c1:
                if k_ok:
                    st.success(f"**KIS API (한국투자증권)**: {k_msg}")
                else:
                    st.error(f"**KIS API (한국투자증권)**: {k_msg}")
            with c2:
                if l_ok:
                    st.success(f"**LS API (LS증권 - 참고용)**: {l_msg}")
                else:
                    st.warning(f"**LS API (LS증권 - 참고용)**: {l_msg}")

    # --------------------------------------------------------------------------
    # 1. 수급 스캐너 검색 컨트롤러
    # --------------------------------------------------------------------------
    c1, c2, c3, c4, c5 = st.columns([1.2, 1.2, 1.2, 1.4, 1.0])
    with c1:
        market_sel = st.selectbox("시장 선택", options=["KOSPI", "KOSDAQ"], index=0, key="radar_market")
    with c2:
        investor_sel = st.selectbox("수급 주체", options=["외국인", "기관", "연기금", "금융투자", "투신", "개인"], index=0, key="radar_investor")
    with c3:
        trade_type_sel = st.selectbox("매매 구분", options=["순매수", "순매도"], index=0, key="radar_tradetype")
    with c4:
        target_date = st.date_input("조회 기준일", value=today_date, max_value=today_date, key="radar_date")
    with c5:
        top_n = st.selectbox("조회 종목 수", options=[10, 20, 30, 50], index=2, key="radar_topn")

    # --------------------------------------------------------------------------
    # 2. 데이터 수집
    # --------------------------------------------------------------------------
    with st.spinner(f"🔍 {target_date} [{market_sel} - {investor_sel} {trade_type_sel}] 수급 스캐닝 중..."):
        df_radar = get_market_radar_scanner(
            target_date_obj=target_date,
            market=market_sel,
            investor=investor_sel,
            trade_type=trade_type_sel,
            top_n=top_n
        )

    if df_radar is None or df_radar.empty:
        st.warning("⚠️ 해당 일자의 수급 데이터를 수집할 수 없거나 장 시작 전/휴장일입니다.")
        return

    data_source = df_radar["데이터_출처"].iloc[0] if "데이터_출처" in df_radar.columns else "수급 API"

    # --------------------------------------------------------------------------
    # 3. 요약 지표 카드
    # --------------------------------------------------------------------------
    total_amount_eok = df_radar["순매수대금(억)"].sum()
    top_stock_name = df_radar["종목명"].iloc[0] if not df_radar.empty else "-"
    top_stock_amt = df_radar["순매수대금(억)"].iloc[0] if not df_radar.empty else 0.0

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("상위 종목 합계 대금", f"{total_amount_eok:,.1f} 억원", delta=None)
    with m2:
        st.metric(f"{trade_type_sel} 1위 종목", f"{top_stock_name}", f"{top_stock_amt:,.1f} 억")
    with m3:
        st.metric("스캐닝 종목 수", f"{len(df_radar)} 개")
    with m4:
        st.metric("데이터 수신 채널", f"{data_source.split('(')[0].strip()}")

    # --------------------------------------------------------------------------
    # 4. 트리맵 및 수급 차트 시각화
    # --------------------------------------------------------------------------
    st.markdown(f"#### 📊 {market_sel} {investor_sel} {trade_type_sel} 상위 비중 시각화")

    df_plot = df_radar.copy()
    df_plot["절대대금"] = df_plot["순매수대금(억)"].abs()
    df_plot["절대대금_표시용"] = df_plot["절대대금"].apply(lambda x: max(x, 1.0))

    max_abs_pct = float(df_plot["등락률(%)"].abs().quantile(0.95)) if len(df_plot) > 0 else 8.0
    color_bound = max(max_abs_pct, 5.0)

    fig_treemap = px.treemap(
        df_plot,
        path=["종목명"],
        values="절대대금_표시용",
        color="등락률(%)",
        color_continuous_scale=["#1F6FEB", "#0D1117", "#F85149"],
        color_continuous_midpoint=0.0,
        range_color=[-color_bound, color_bound],
        custom_data=["현재가", "순매수대금(억)", "등락률(%)"],
        title=f"{target_date} {market_sel} {investor_sel} {trade_type_sel} Top {len(df_plot)}"
    )

    # Plotly Treemap 전용 유효 속성 적용
    fig_treemap.update_traces(
        textfont=dict(size=14, color="white"),
        hovertemplate="<b>%{label}</b><br>현재가: %{customdata[0]:,.0f}원<br>순매수대금: %{customdata[1]:+,.1f}억원<br>등락률: %{customdata[2]:+.2f}%<extra></extra>"
    )

    fig_treemap.update_layout(
        template="plotly_dark",
        uniformtext=dict(minsize=10, mode="hide"),
        margin=dict(t=30, l=10, r=10, b=10),
        height=450
    )
    st.plotly_chart(fig_treemap, use_container_width=True)

    # --------------------------------------------------------------------------
    # 5. 수급 상위 종목 상세 테이블
    # --------------------------------------------------------------------------
    st.markdown(f"#### 📋 {market_sel} {investor_sel} {trade_type_sel} 상위 상세 리스트")

    disp_cols = ["순위", "종목코드", "종목명", "현재가", "등락률(%)", "순매수대금(억)"]
    existing_cols = [c for c in disp_cols if c in df_radar.columns]

    df_display = df_radar[existing_cols].copy()

    st.dataframe(
        df_display.style.format({
            "현재가": "{:,.0f} 원",
            "등락률(%)": "{:+.2f}%",
            "순매수대금(억)": "{:+,.1f} 억"
        }).background_gradient(subset=["순매수대금(억)"], cmap="Reds" if trade_type_sel == "순매수" else "Blues"),
        use_container_width=True,
        hide_index=True
    )

    # --------------------------------------------------------------------------
    # 6. 종목별 기준일(0점) 누적 수급 심층 분석 차트
    # --------------------------------------------------------------------------
    st.markdown("---")
    st.markdown("#### 📈 종목별 주가 vs 기준일(0점) 누적 수급 흐름 분석")

    stock_options = [f"{r['종목명']} ({r['종목코드']})" for _, r in df_radar.iterrows()]

    col_sel1, col_sel2 = st.columns([2, 2])
    with col_sel1:
        selected_stock_str = st.selectbox("분석 대상 종목 선택", options=stock_options, index=0, key="cum_stock_select")
        selected_code = selected_stock_str.split("(")[-1].replace(")", "").strip()
        selected_name = selected_stock_str.split("(")[0].strip()
    with col_sel2:
        cum_start_date = st.date_input(
            "누적 시작 기준일 (0점)",
            value=today_date - timedelta(days=60),
            max_value=today_date - timedelta(days=2),
            key="cum_start_date"
        )

    if selected_code:
        with st.spinner(f"{selected_name} ({selected_code}) 누적 시계열 산출 중..."):
            df_cum = get_stock_cumulative_flow_from_base(
                stock_code=selected_code,
                start_date_obj=cum_start_date,
                end_date_obj=today_date
            )

        if df_cum is not None and not df_cum.empty:
            fig_cum = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.08,
                row_heights=[0.55, 0.45],
                subplot_titles=[f"주가 추이 ({selected_name})", "기준일 누적 순매수 흐름 (억원)"]
            )

            # Row 1: 주가
            fig_cum.add_trace(
                go.Scatter(
                    x=df_cum["Date"], y=df_cum["Close"],
                    name="주가 (Close)",
                    line=dict(color="#58A6FF", width=2)
                ),
                row=1, col=1
            )

            # Row 2: 외인 / 기관 누적 수급
            fig_cum.add_trace(
                go.Scatter(
                    x=df_cum["Date"], y=df_cum["Foreigner_Cum"],
                    name="외국인 누적",
                    line=dict(color="#FF7B72", width=2)
                ),
                row=2, col=1
            )
            fig_cum.add_trace(
                go.Scatter(
                    x=df_cum["Date"], y=df_cum["Institution_Cum"],
                    name="기관 누적",
                    line=dict(color="#FFA657", width=2)
                ),
                row=2, col=1
            )
            fig_cum.add_trace(
                go.Scatter(
                    x=df_cum["Date"], y=df_cum["Retail_Cum"],
                    name="개인 누적",
                    line=dict(color="#7EE787", width=1.5, dash="dot")
                ),
                row=2, col=1
            )

            fig_cum.update_layout(
                template="plotly_dark",
                height=520,
                margin=dict(t=40, l=10, r=10, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                hovermode="x unified"
            )
            st.plotly_chart(fig_cum, use_container_width=True)

    # --------------------------------------------------------------------------
    # 7. 하단 파이프라인 수신 정보
    # --------------------------------------------------------------------------
    st.caption(f"⚡ 파이프라인: `[KIS API ➔ KRX OpenAPI ➔ Daum API ➔ Naver API ➔ PyKrx]` 중 **{data_source.split('(')[0].strip()}** 채널에서 수신 성공")
