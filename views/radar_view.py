"""
views/radar_view.py
📡 외국인/기관 정밀 수급 레이더 뷰
사용자 지정 날짜별 수급 조회 및 기준일(0점) 기반 누적 변화량 분석 탭 제공
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from services.radar_service import (
    get_market_radar_scanner,
    get_stock_cumulative_flow_from_base,
    test_kis_connection,
    test_ls_connection,
    PYKRX_AVAILABLE
)

def render_radar_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_date = now_kst.date()

    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            📡 외국인/기관 수급 레이더 (코스피 & 코스닥)
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            실시간/과거 특정 날짜별 순매수·순매도 스캐닝 및 사용자 지정 기준일(0점) 누적 변화량 분석
        </p>
    </div>
    """, unsafe_allow_html=True)

    # ==========================================================================
    # [신규] KIS / LS 증권사 API 연결 상태 자가 진단 패널
    # ==========================================================================
    with st.expander("🛠️ KIS / LS 증권사 API 연결 상태 테스트", expanded=False):
        st.write("장중 실시간 수급을 제공하는 KIS(한국투자증권) 및 LS증권 API의 작동 및 인증 상태를 점검합니다.")
        if st.button("🔌 KIS / LS API 테스트 실행"):
            with st.spinner("KIS API 상태 점검 중..."):
                k_ok, k_msg = test_kis_connection()
            with st.spinner("LS API 상태 점검 중..."):
                l_ok, l_msg = test_ls_connection()
            
            c1, c2 = st.columns(2)
            with c1:
                if k_ok:
                    st.success(f"**✅ KIS API:** {k_msg}")
                else:
                    st.error(f"**❌ KIS API:** {k_msg}")
            with c2:
                if l_ok:
                    st.success(f"**✅ LS API:** {l_msg}")
                else:
                    st.error(f"**❌ LS API:** {l_msg}")

    tab1, tab2 = st.tabs(["📊 날짜별 시장 수급 스캐너", "🔍 사용자 지정 기준일(0점) 누적 수급 변화"])

    # ==========================================================================
    # TAB 1: 날짜별 시장 수급 스캐너
    # ==========================================================================
    with tab1:
        c1, c2, c3, c4, c5 = st.columns([1.2, 1.2, 1.1, 1.1, 1])
        with c1:
            market_sel = st.selectbox("시장 구분", options=["KOSPI (코스피)", "KOSDAQ (코스닥)"], index=0, key="r_mkt")
        with c2:
            investor_sel = st.selectbox("투자 주체", options=["외국인", "기관", "연기금", "금융투자", "투신", "개인"], index=0, key="r_inv")
        with c3:
            trade_sel = st.selectbox("매매 방향", options=["순매수", "순매도"], index=0, key="r_trade")
        with c4:
            target_date = st.date_input("조회 기준일자", value=today_date, max_value=today_date, key="r_date")
        with c5:
            top_n = st.selectbox("표시 수", options=[15, 30, 50], index=1, key="r_top")

        market_key = "KOSPI" if "KOSPI" in market_sel else "KOSDAQ"
        
        # 6단계 무중단 파이프라인 스캔
        df_radar = get_market_radar_scanner(target_date_obj=target_date, market=market_key, investor=investor_sel, trade_type=trade_sel, top_n=top_n)

        if df_radar.empty:
            st.error("""
            ❌ **최근 7영업일 내 수급 데이터를 찾을 수 없습니다.**
            
            현재 KRX, KIS, LS, Daum, Naver API 서버 통신이 모두 지연되거나 접근이 차단된 상태입니다.
            잠시 후 데이터 새로고침을 진행해 주세요.
            """)
        else:
            data_source = df_radar["데이터_출처"].iloc[0] if "데이터_출처" in df_radar.columns else "공식 거래소 API"
            
            match = re.search(r'\((\d{8}|\d{4}-\d{2}-\d{2})\)', data_source)
            if match:
                raw_date = match.group(1).replace("-", "")
                actual_data_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
            else:
                actual_data_date = target_date.strftime('%Y-%m-%d')
            
            target_date_str_compact = target_date.strftime('%Y%m%d')
            if actual_data_date.replace("-", "") != target_date_str_compact:
                st.info(f"💡 **스마트 롤백 작동**: 선택하신 `{target_date.strftime('%Y-%m-%d')}`는 휴장일이거나 원장이 확정되지 않아, **가장 최근 거래일({actual_data_date})**의 데이터를 표출합니다.")

            top1 = df_radar.iloc[0]
            total_top_amt = df_radar["순매수대금(억)"].sum()
            
            st.caption(f"⚡ 파이프라인: `[KIS API ➔ LS API ➔ KRX OpenAPI ➔ Daum API ➔ Naver API ➔ PyKrx]` 중 **{data_source.split('(')[0].strip()}** 채널에서 수신 성공")
            
            sc1, sc2, sc3 = st.columns(3)
            with sc1:
                st.metric(label=f"🥇 1위 집중 종목 ({investor_sel} {trade_sel})", value=f"{top1['종목명']}", delta=f"{top1['순매수대금(억)']:+,.1f} 억")
            with sc2:
                st.metric(label=f"상위 {len(df_radar)}개사 합산 {trade_sel} 규모", value=f"{total_top_amt:+,.1f} 억원")
            with sc3:
                st.metric(label="📅 실제 데이터 기준일자 및 소스", value=actual_data_date, delta=data_source.split('(')[0].strip(), delta_color="off")

            st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

            st.markdown(f"#### 🗺️ {actual_data_date} | {investor_sel} {trade_sel} 상위 종목 맵")
            
            df_radar["Abs_Amt"] = df_radar["순매수대금(억)"].abs()
            fig_tree = px.treemap(
                df_radar,
                path=["종목명"],
                values="Abs_Amt",
                color="등락률(%)",
                color_continuous_scale=["#388BFD", "#161B22", "#F85149"],
                color_continuous_midpoint=0.0,
                hover_data={"종목코드": True, "현재가": ":,.0f", "순매수대금(억)": ":+,.1f", "등락률(%)": ":+.2f"},
                height=480
            )
            
            fig_tree.update_traces(
                textposition="middle center",
                textfont=dict(size=15, color="white", weight="bold"),
                hovertemplate="<b>%{label}</b><br>현재가: %{customdata[1]:,.0f}원<br>등락률: %{customdata[3]:+.2f}%<br>금액: %{customdata[2]:+,.1f}억원"
            )
            fig_tree.update_layout(
                paper_bgcolor="#0D1117",
                plot_bgcolor="#161B22",
                margin=dict(l=10, r=10, t=20, b=10)
            )
            st.plotly_chart(fig_tree, use_container_width=True)

            st.markdown(f"#### 📋 {investor_sel} {trade_sel} 상위 {len(df_radar)}개 종목 상세 랭킹")
            display_cols = ["순위", "종목코드", "종목명", "현재가", "등락률(%)", "순매수대금(억)"]
            st.dataframe(
                df_radar[display_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "현재가": st.column_config.NumberColumn(format="%d 원"),
                    "등락률(%)": st.column_config.NumberColumn(format="%+.2f%%"),
                    "순매수대금(억)": st.column_config.NumberColumn(format="%+,.1f 억")
                }
            )

    # ==========================================================================
    # TAB 2: 사용자 지정 기준일(0점) 누적 수급 변화 분석
    # ==========================================================================
    with tab2:
        st.markdown("#### 🎯 사용자 설정 기준일(0점) 기반 누적 수급 변화량 트래커")
        st.caption("특정 시작일(0점)을 지정하면, 해당 날짜부터 현재까지 주체별 순매수 대금이 어떻게 누적 변화했는지 추적합니다.")

        rc1, rc2, rc3 = st.columns([1.5, 1.5, 1])
        with rc1:
            stock_input = st.selectbox(
                "분석 대상 종목",
                options=[
                    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("005380", "현대차"),
                    ("035420", "NAVER"), ("068270", "셀트리온"), ("000270", "기아"),
                    ("105560", "KB금융"), ("051910", "LG화학")
                ],
                format_func=lambda x: f"{x[1]} ({x[0]})",
                key="cum_stock"
            )
        with rc2:
            default_start = today_date - timedelta(days=30)
            base_start_date = st.date_input("누적 기준 시작일 (0점 설정)", value=default_start, max_value=today_date, key="cum_start")
        with rc3:
            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            st.info(f"기준일: **{base_start_date.strftime('%Y-%m-%d')}** (0억 원)")

        sel_code = stock_input[0]
        sel_name = stock_input[1]

        df_base_cum = get_stock_cumulative_flow_from_base(stock_code=sel_code, start_date_obj=base_start_date, end_date_obj=today_date)

        if not df_base_cum.empty:
            st.markdown(f"#### 📈 {sel_name} ({sel_code}) | {base_start_date.strftime('%Y-%m-%d')} 이후 누적 수급 변화")
            
            fig_base = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.08,
                row_heights=[0.65, 0.35],
                subplot_titles=(
                    f"{sel_name} 주가 vs 기준일({base_start_date.strftime('%m-%d')}=0) 기준 누적 순매수",
                    "일별 순매수 변화 (억 원)"
                )
            )

            fig_base.add_trace(
                go.Scatter(
                    x=df_base_cum["Date"], y=df_base_cum["Close"],
                    name="주가 (종가)", line=dict(color="#C9D1D9", width=1.5, dash="dot"), yaxis="y2"
                ), row=1, col=1
            )
            fig_base.add_trace(
                go.Scatter(
                    x=df_base_cum["Date"], y=df_base_cum["Foreigner_Cum"],
                    name="외국인 누적(억)", line=dict(color="#F85149", width=2.5)
                ), row=1, col=1
            )
            fig_base.add_trace(
                go.Scatter(
                    x=df_base_cum["Date"], y=df_base_cum["Institution_Cum"],
                    name="기관 누적(억)", line=dict(color="#58A6FF", width=2)
                ), row=1, col=1
            )
            fig_base.add_trace(
                go.Scatter(
                    x=df_base_cum["Date"], y=df_base_cum["Retail_Cum"],
                    name="개인 누적(억)", line=dict(color="#E3B341", width=1.5)
                ), row=1, col=1
            )
            fig_base.add_trace(
                go.Bar(
                    x=df_base_cum["Date"], y=df_base_cum["Foreigner_Daily"],
                    name="외인 일별(억)", marker_color=["#F85149" if v >= 0 else "#388BFD" for v in df_base_cum["Foreigner_Daily"]]
                ), row=2, col=1
            )

            fig_base.update_layout(
                template="plotly_dark",
                paper_bgcolor="#0D1117",
                plot_bgcolor="#161B22",
                height=620,
                margin=dict(l=30, r=40, t=50, b=30),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                hovermode="x unified"
            )
            fig_base.update_yaxes(title_text="기준일 대비 누적 (억 원)", row=1, col=1, gridcolor="#21262D")
            fig_base.update_yaxes(title_text="일별 (억 원)", row=2, col=1, gridcolor="#21262D")

            st.plotly_chart(fig_base, use_container_width=True)

            with st.expander("📄 기준일 이후 일별 원장 데이터 확인", expanded=False):
                st.dataframe(
                    df_base_cum.sort_values("Date", ascending=False),
                    use_container_width=True,
                    hide_index=True
                )
        else:
            st.warning("⚠️ 선택하신 기준일부터의 수급 시계열 데이터를 계산할 수 없습니다.")
