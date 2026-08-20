"""
views/radar_view.py
외국인/기관 수급 레이더 대시보드 뷰
장중 가집계 한계 고지, 0440 공식 지원 투자주체 셀렉터 및 개발자용 검증 Expander 탑재
"""
from datetime import datetime, time, timedelta
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
    test_ls_connection,
    test_pykrx_connection,
    PYKRX_AVAILABLE
)


def render_radar_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_date = now_kst.date()

    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🎯 외국인/기관 장중 수급 레이더
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            KIS 외국인·기관 장중 가집계 기반 상위 종목 참고 정보입니다. 개인 및 세부 투자주체의 시장 전체 Top N은 제공하지 않습니다.
        </p>
    </div>
    """, unsafe_allow_html=True)

    # --------------------------------------------------------------------------
    # 0. 증권사 및 데이터 API 연결 상태 테스트 진단 구역
    # --------------------------------------------------------------------------
    with st.expander("🛠️ KIS / LS / PyKrx API 연결 상태 테스트", expanded=False):
        st.write("장중 실시간 수급을 제공하는 KIS(한국투자증권), LS증권 및 PyKrx의 작동 및 인증 상태를 점검합니다.")
        st.caption("ℹ️ LS증권 API는 참고용 연결 테스트만 제공하며, 시장 전체 순매수 랭킹 조회에는 사용되지 않습니다 (LS는 종목별 조회 전용 TR만 보유).")
        if st.button("🔌 KIS / LS / PyKrx API 테스트 실행", key="btn_test_broker_apis"):
            with st.spinner("KIS API 상태 점검 중..."):
                k_ok, k_msg = test_kis_connection()
            with st.spinner("LS API 상태 점검 중 (참고용)..."):
                l_ok, l_msg = test_ls_connection()
            with st.spinner("PyKrx(KRX 웹) 상태 점검 중..."):
                p_ok, p_msg = test_pykrx_connection()

            c1, c2, c3 = st.columns(3)
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
            with c3:
                if p_ok:
                    st.success(f"**PyKrx (KRX 웹 데이터)**: {p_msg}")
                else:
                    st.error(f"**PyKrx (KRX 웹 데이터)**: {p_msg}")

    # --------------------------------------------------------------------------
    # 1. 수급 스캐너 검색 컨트롤러 (0440 공식 지원 투자주체)
    # --------------------------------------------------------------------------
    c1, c2, c3, c4, c5 = st.columns([1.2, 1.2, 1.2, 1.4, 1.0])
    with c1:
        market_sel = st.selectbox("시장 선택", options=["KOSPI", "KOSDAQ"], index=0, key="radar_market")
    with c2:
        investor_sel = st.selectbox(
            "수급 주체",
            options=["외국인", "기관", "투신", "은행", "보험", "종금", "기금", "기타기관", "기타법인"],
            index=0,
            key="radar_investor"
        )
    with c3:
        trade_type_sel = st.selectbox("매매 구분", options=["순매수", "순매도"], index=0, key="radar_tradetype")
    with c4:
        target_date = st.date_input("조회 기준일", value=today_date, max_value=today_date, key="radar_date")
    with c5:
        top_n = st.selectbox("조회 종목 수", options=[10, 20, 30, 50], index=2, key="radar_topn")

    # --------------------------------------------------------------------------
    # 1-1. 조회 조건 상태 안내 및 캐시 강제 새로고침 바
    # --------------------------------------------------------------------------
    col_cap, col_ref = st.columns([4, 1])
    with col_cap:
        st.caption(f"🔎 현재 조회 조건: `{market_sel}` / `{investor_sel}` / `{trade_type_sel}` / `{target_date}` (Top {top_n})")
    with col_ref:
        if st.button("🔄 데이터 강제 새로고침", use_container_width=True):
            get_market_radar_scanner.clear()
            st.rerun()

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
        st.warning("⚠️ 해당 조건의 수급 데이터를 수집할 수 없거나 매매 기준을 만족하는 종목이 없습니다.")
        return

    data_source = df_radar["데이터_출처"].iloc[0] if "데이터_출처" in df_radar.columns else "수급 API"
    captured_at = df_radar["조회시각"].iloc[0] if "조회시각" in df_radar.columns else now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    # --------------------------------------------------------------------------
    # 2-1. 데이터 품질 안내 배너 및 투자주체별 한계 고지
    # --------------------------------------------------------------------------
    st.info(
        f"📌 **데이터 출처**: `{data_source}` | ⏱️ **앱 수집 시각**: `{captured_at}`  \n"
        "💡 *본 데이터는 KIS 장중 가집계 참고용입니다. KRX 장마감 확정 투자자별 거래실적과 차이가 날 수 있습니다.*"
    )

    if investor_sel not in ["외국인", "기관"]:
        st.warning(
            f"⚠️ **'{investor_sel}' 수급 안내**: KIS 장중 가집계 응답에 포함된 후보 종목 내에서 "
            "재정렬한 참고치입니다. 시장 전체 기준의 확정 Top N 랭킹을 보장하지 않습니다."
        )

    if now_kst.time() >= time(15, 30):
        st.warning(
            "🔔 **장 마감 안내**: 현재 시각은 정규장 종료 후입니다. 표기된 수치는 KIS의 마지막 장중 가집계 스냅샷(14:30~14:40 기준)일 수 있으며, "
            "종가 배분·시간외 거래 및 최종 확정 투자자별 수급은 반영되지 않을 수 있습니다."
        )

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

    fig_treemap.update_traces(
        textposition="middle center",
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

    disp_cols = ["순위", "종목코드", "종목명", "현재가", "등락률(%)", "순매수대금(억)", "금액_산출기준"]
    existing_cols = [c for c in disp_cols if c in df_radar.columns]

    df_display = df_radar[existing_cols].copy()

    format_dict = {
        "현재가": "{:,.0f} 원",
        "등락률(%)": "{:+.2f}%",
        "순매수대금(억)": "{:+,.1f} 억"
    }

    st.dataframe(
        df_display.style.format(format_dict).background_gradient(subset=["순매수대금(억)"], cmap="Reds" if trade_type_sel == "순매수" else "Blues"),
        use_container_width=True,
        hide_index=True
    )

    # --------------------------------------------------------------------------
    # 5-1. 개발자용 원본 수급 데이터 검증 Expander
    # --------------------------------------------------------------------------
    with st.expander("🔧 개발자용 원본 수급 데이터 검증", expanded=False):
        debug_cols = [
            "종목코드",
            "종목명",
            "원본_순매수거래대금",
            "원본_순매수수량",
            "순매수대금(억)",
            "금액_산출기준",
            "조회시각",
            "데이터_출처",
        ]
        st.dataframe(df_radar[[c for c in debug_cols if c in df_radar.columns]], use_container_width=True, hide_index=True)

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
