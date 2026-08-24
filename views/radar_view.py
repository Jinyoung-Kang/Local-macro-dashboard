"""
views/radar_view.py
외국인/기관 수급 레이더 대시보드 뷰

장중 가집계 한계 고지, KIS 0440 공식 지원 투자주체 셀렉터,
Daum 집계 기간(당일/5거래일/20거래일) 선택 및 개발자용 검증 Expander 탑재.
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
    test_naver_scraping,
    test_daum_scraping,
    PYKRX_AVAILABLE
)

INTERVAL_LABELS = {
    "TODAY": "당일",
    "DAYS_5": "5거래일",
    "DAYS_20": "20거래일",
}


def render_radar_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_date = now_kst.date()

    st.markdown("""
        <div style="padding: 4px 0 12px 0;">
            <h2 style="margin:0; font-weight:700; color:#F0F6FC;">
                📡 외국인/기관 수급 레이더 (코스피 & 코스닥)
            </h2>
            <p style="margin:4px 0 0 0; color:#8B949E; font-size:0.92rem;">
                KIS 외국인·기관 장중 가집계 기반 상위 종목 참고 정보입니다.
                개인 및 세부 투자주체의 시장 전체 Top N은 제공하지 않습니다.
            </p>
        </div>
    """, unsafe_allow_html=True)

    # ==========================================================================
    # KIS / LS / PyKrx / Naver / Daum 연결 상태 진단 패널
    # ==========================================================================
    with st.expander("🛠️ KIS / LS / PyKrx / Naver / Daum 연결 상태 테스트", expanded=False):
        st.write("KIS, LS, PyKrx, Naver, Daum 5개 데이터 소스의 연결 상태를 각각 점검합니다.")
        st.caption(
            "LS API는 LS증권 계정이 없으면 실패가 정상입니다. "
            "KIS는 장중 가집계 전용이라 장 마감 후 실패는 정상입니다. "
            "Naver/Daum은 장 마감 후에도 확정 데이터를 제공해야 정상입니다."
        )

        if st.button("🔍 5개 소스 연결 테스트 실행", key="btn_test_broker_apis"):
            with st.spinner("KIS API 점검 중..."):
                k_ok, k_msg = test_kis_connection()
            with st.spinner("LS API 점검 중..."):
                l_ok, l_msg = test_ls_connection()
            with st.spinner("PyKrx(KRX) 점검 중..."):
                p_ok, p_msg = test_pykrx_connection()
            with st.spinner("Naver 스크래핑 점검 중..."):
                n_ok, n_msg = test_naver_scraping()
            with st.spinner("Daum 스크래핑 점검 중..."):
                d_ok, d_msg = test_daum_scraping()

            c1, c2, c3, c4, c5 = st.columns(5)
            with c1:
                if k_ok:
                    st.success(f"KIS API 정상\n\n{k_msg}")
                else:
                    st.warning(f"KIS API 실패\n\n{k_msg}")
            with c2:
                if l_ok:
                    st.success(f"LS API 정상\n\n{l_msg}")
                else:
                    st.warning(f"LS API 실패 (LS 미보유 시 정상)\n\n{l_msg}")
            with c3:
                if p_ok:
                    st.success(f"PyKrx(KRX) 정상\n\n{p_msg}")
                else:
                    st.error(f"PyKrx(KRX) 실패\n\n{p_msg}")
            with c4:
                if n_ok:
                    st.success(f"Naver 정상\n\n{n_msg}")
                else:
                    st.error(f"Naver 실패\n\n{n_msg}")
            with c5:
                if d_ok:
                    st.success(f"Daum 정상\n\n{d_msg}")
                else:
                    st.error(f"Daum 실패\n\n{d_msg}")

            if not n_ok and not d_ok:
                st.error("Naver와 Daum이 모두 실패했습니다. PyKrx로만 데이터가 제공되며, PyKrx도 실패하면 결과가 없습니다.")
            elif not n_ok:
                st.info("Naver는 실패했지만 Daum이 정상이므로 데이터는 계속 제공됩니다.")
            elif not d_ok:
                st.info("Daum은 실패했지만 Naver가 정상이므로 데이터는 계속 제공됩니다.")

    # ==========================================================================
    # 조회 조건 설정
    # ==========================================================================
    c1, c2, c3, c4, c5, c6 = st.columns([1.1, 1.1, 1.1, 1.3, 0.9, 1.1])
    with c1:
        market_sel = st.selectbox("시장", options=["KOSPI", "KOSDAQ"], index=0, key="radar_market")
    with c2:
        investor_sel = st.selectbox(
            "투자주체",
            options=["외국인", "기관", "투신", "은행", "보험", "종금", "기금", "기타기관", "기타법인"],
            index=0,
            key="radar_investor",
        )
    with c3:
        trade_type_sel = st.selectbox("매매방향", options=["순매수", "순매도"], index=0, key="radar_trade_type")
    with c4:
        target_date = st.date_input(
            "조회 기준일자", value=today_date, max_value=today_date, key="radar_date",
        )
    with c5:
        top_n = st.selectbox("표시 종목수", options=[10, 20, 30, 50], index=2, key="radar_top_n")
    with c6:
        interval_sel = st.selectbox(
            "집계 기간",
            options=["TODAY", "DAYS_5", "DAYS_20"],
            format_func=lambda x: INTERVAL_LABELS.get(x, x),
            index=0,
            key="radar_interval",
        )

    if interval_sel != "TODAY":
        st.info(
            "💡 5거래일/20거래일 집계는 Daum 소스에서만 지원됩니다. "
            "장중이거나 Daum이 실패할 경우 다른 소스(KIS/Naver/PyKrx)의 "
            "당일 데이터로 자동 대체됩니다."
        )

    col_cap, col_ref = st.columns([4, 1])
    with col_cap:
        st.caption(
            f"조회 조건: {market_sel} / {investor_sel} {trade_type_sel} / "
            f"{target_date} / Top {top_n} / {INTERVAL_LABELS.get(interval_sel, interval_sel)}"
        )
    with col_ref:
        if st.button("새로고침", use_container_width=True):
            get_market_radar_scanner.clear()
            st.rerun()

    # ==========================================================================
    # 데이터 조회
    # ==========================================================================
    with st.spinner(f"{target_date} {market_sel} - {investor_sel} {trade_type_sel} 데이터 수집 중..."):
        df_radar = get_market_radar_scanner(
            target_date_obj=target_date,
            market=market_sel,
            investor=investor_sel,
            trade_type=trade_type_sel,
            top_n=top_n,
            interval_type=interval_sel,
        )

    if df_radar is None or df_radar.empty:
        st.warning("해당 조건의 수급 데이터를 수집할 수 없거나 매매 기준을 만족하는 종목이 없습니다.")
        return

    data_source = df_radar.iloc[0]["데이터_출처"] if "데이터_출처" in df_radar.columns else "알 수 없음"
    captured_at = df_radar.iloc[0]["수집시각"] if "수집시각" in df_radar.columns else now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    st.info(
        f"데이터 출처: {data_source} | 수집 기준: {captured_at}\n\n"
        "KIS 장중 가집계는 실시간이며, KRX 확정 데이터와 소폭 차이가 있을 수 있습니다."
    )

    if investor_sel not in ["외국인", "기관"]:
        st.warning(f"{investor_sel}는 KIS API 장중 가집계 지원 범위 밖일 수 있습니다. 시장 전체 Top N은 제공되지 않습니다.")

    if now_kst.time() < time(15, 30):
        st.warning("현재 장중입니다. 14:30~14:40경 KIS 가집계가 일시 중단될 수 있으며, KRX 확정치와 다를 수 있습니다.")

    # ==========================================================================
    # 상단 요약 메트릭
    # ==========================================================================
    total_amount_eok = df_radar["순매수대금(억)"].sum()
    top_stock_name = df_radar.iloc[0]["종목명"] if not df_radar.empty else "-"
    top_stock_amt = df_radar.iloc[0]["순매수대금(억)"] if not df_radar.empty else 0.0

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("총 순매수대금(억)", f"{total_amount_eok:,.1f}", delta=None)
    with m2:
        st.metric(f"{trade_type_sel} 1위", top_stock_name, f"{top_stock_amt:,.1f}")
    with m3:
        st.metric("조회 종목 수", f"{len(df_radar)}")
    with m4:
        st.metric("데이터 소스", f"{data_source.split(' ')[0]}")

    # ==========================================================================
    # 트리맵
    # ==========================================================================
    st.markdown(f"#### {market_sel} {investor_sel} {trade_type_sel} 상위 종목 트리맵")

    df_plot = df_radar.copy()
    df_plot["시가총액_가중"] = df_plot["시가총액_가중"].abs()
    df_plot["시가총액_가중"] = df_plot["시가총액_가중"].apply(lambda x: max(x, 1.0))

    max_abs_pct = float(df_plot["등락률(%)"].abs().quantile(0.95)) if len(df_plot) > 0 else 8.0
    color_bound = max(max_abs_pct, 5.0)

    fig_treemap = px.treemap(
        df_plot,
        path=["종목명"],
        values="시가총액_가중",
        color="등락률(%)",
        color_continuous_scale=["#1F6FEB", "#0D1117", "#F85149"],
        color_continuous_midpoint=0.0,
        range_color=[-color_bound, color_bound],
        custom_data=["종목코드", "순매수대금(억)", "등락률(%)"],
        title=f"{target_date} {market_sel} {investor_sel} {trade_type_sel} Top {len(df_plot)}",
    )
    fig_treemap.update_traces(
        textposition="middle center",
        textfont=dict(size=14, color="white"),
        hovertemplate="<b>%{label}</b><br>종목코드: %{customdata[0]}<br>순매수대금: %{customdata[1]:,.1f}억<br>등락률: %{customdata[2]:.2f}%<extra></extra>",
    )
    fig_treemap.update_layout(
        template="plotly_dark",
        uniformtext=dict(minsize=10),
        margin=dict(t=30, l=10, r=10, b=10),
        height=450,
    )
    st.plotly_chart(fig_treemap, use_container_width=True)

    # ==========================================================================
    # 데이터 테이블
    # ==========================================================================
    st.markdown(f"#### {market_sel} {investor_sel} {trade_type_sel} 상세 데이터")

    disp_cols = ["순위", "종목코드", "종목명", "현재가", "등락률(%)", "순매수대금(억)", "데이터_출처"]
    existing_cols = [c for c in disp_cols if c in df_radar.columns]
    df_display = df_radar[existing_cols].copy()

    format_dict = {"현재가": "{:,.0f}", "등락률(%)": "{:.2f}", "순매수대금(억)": "{:,.1f}"}

    st.dataframe(
        df_display.style.format(format_dict).background_gradient(
            subset=["순매수대금(억)"],
            cmap="Reds" if trade_type_sel == "순매수" else "Blues",
        ),
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("🔍 원본 데이터 확인 (개발자용)", expanded=False):
        debug_cols = ["순위", "종목코드", "종목명", "현재가", "등락률(%)", "순매수대금(억)", "시가총액_가중", "데이터_출처"]
        st.dataframe(
            df_radar[[c for c in debug_cols if c in df_radar.columns]],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("---")

    # ==========================================================================
    # 종목별 기준일(0점) 누적 수급 차트
    # ==========================================================================
    st.markdown("#### 종목별 누적 수급 흐름 (기준일 대비 0점)")

    stock_options = [f"{r['종목명']} ({r['종목코드']})" for _, r in df_radar.iterrows()]

    col_sel1, col_sel2 = st.columns([2, 2])
    with col_sel1:
        selected_stock_str = st.selectbox("종목 선택", options=stock_options, index=0, key="cum_stock_select")
        selected_code = selected_stock_str.split("(")[-1].replace(")", "").strip()
        selected_name = selected_stock_str.split("(")[0].strip()
    with col_sel2:
        cum_start_date = st.date_input(
            "누적 기준일(0점)",
            value=today_date - timedelta(days=60),
            max_value=today_date - timedelta(days=2),
            key="cum_start_date",
        )

    if selected_code:
        with st.spinner(f"{selected_name}({selected_code}) 누적 수급 데이터 조회 중..."):
            df_cum = get_stock_cumulative_flow_from_base(
                stock_code=selected_code,
                start_date_obj=cum_start_date,
                end_date_obj=today_date,
            )

        if df_cum is not None and not df_cum.empty:
            is_estimated = bool(df_cum["is_estimated"].iloc[0]) if "is_estimated" in df_cum.columns else True
            cross_validated = bool(df_cum["cross_validated"].iloc[0]) if "cross_validated" in df_cum.columns else False
            source = str(df_cum["source"].iloc[0]) if "source" in df_cum.columns else ""

            if is_estimated:
                st.warning("pykrx/Daum 실제 데이터 수집에 실패해 가격·거래량 기반 통계적 추정치를 표시합니다. KRX 공시 수급과 다를 수 있습니다.")
            elif cross_validated:
                st.success(f"{source}")
            else:
                st.info(f"{source}. 교차검증이 확인되지 않았습니다.")

            suffix = " (추정)" if is_estimated else (" (교차검증됨)" if cross_validated else " (단일소스)")

            fig_cum = make_subplots(
                rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                row_heights=[0.55, 0.45],
                subplot_titles=(f"{selected_name} 주가", f"누적 수급{suffix}"),
            )

            fig_cum.add_trace(
                go.Scatter(x=df_cum["Date"], y=df_cum["Close"], name="Close", line=dict(color="#58A6FF", width=2)),
                row=1, col=1,
            )
            fig_cum.add_trace(
                go.Scatter(x=df_cum["Date"], y=df_cum["Foreigner_Cum"], name=f"외국인{suffix}", line=dict(color="#FF7B72", width=2)),
                row=2, col=1,
            )
            fig_cum.add_trace(
                go.Scatter(x=df_cum["Date"], y=df_cum["Institution_Cum"], name=f"기관{suffix}", line=dict(color="#FFA657", width=2)),
                row=2, col=1,
            )
            fig_cum.add_trace(
                go.Scatter(x=df_cum["Date"], y=df_cum["Retail_Cum"], name=f"개인{suffix}", line=dict(color="#7EE787", width=1.5, dash="dot")),
                row=2, col=1,
            )

            fig_cum.update_layout(
                template="plotly_dark",
                height=520,
                margin=dict(t=40, l=10, r=10, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                hovermode="x unified",
            )
            st.plotly_chart(fig_cum, use_container_width=True)

            st.caption(
                f"데이터 소스 우선순위: KIS API > Daum API > Naver API > PyKrx. "
                f"현재: {data_source.split(' ')[0]}"
            )
        else:
            st.warning(f"{selected_name}({selected_code})의 누적 수급 데이터를 가져오지 못했습니다.")
