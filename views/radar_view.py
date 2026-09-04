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
    PYKRX_AVAILABLE,
)


INTERVAL_LABELS = {
    "TODAY": "당일",
    "DAYS_5": "5거래일",
    "DAYS_20": "20거래일",
}


def render_radar_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_date = now_kst.date()

    st.markdown(
        """
        <div style="padding: 4px 0 12px 0;">
            <h2 style="margin: 0; font-weight: 700; color: #F0F6FC;">외국인/기관 수급 레이더</h2>
            <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
                KIS 외국인·기관 장중 가집계 기반 상위 종목 참고 정보입니다.
                개인 및 세부 투자주체의 시장 전체 Top N은 제공하지 않습니다.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("연결 상태 테스트", expanded=False):
        st.write("KIS, LS, PyKrx, Naver, Daum 5개 데이터 소스의 연결 상태를 확인합니다.")
        st.caption(
            "LS API는 현재 LS증권 계좌가 연결된 경우에만 사용됩니다. "
            "KIS는 장중 가집계, Naver/Daum은 장 마감 후 시장 전체 순위 확인에 사용됩니다."
        )

        if st.button("5개 데이터 소스 연결 상태 테스트", key="btn_test_broker_apis"):
            with st.spinner("KIS API 연결 상태를 확인하는 중..."):
                kis_ok, kis_msg = test_kis_connection()
            with st.spinner("LS API 연결 상태를 확인하는 중..."):
                ls_ok, ls_msg = test_ls_connection()
            with st.spinner("PyKrx/KRX 연결 상태를 확인하는 중..."):
                pykrx_ok, pykrx_msg = test_pykrx_connection()
            with st.spinner("Naver 스크래핑 연결 상태를 확인하는 중..."):
                naver_ok, naver_msg = test_naver_scraping()
            with st.spinner("Daum 스크래핑 연결 상태를 확인하는 중..."):
                daum_ok, daum_msg = test_daum_scraping()

            test_col1, test_col2, test_col3, test_col4, test_col5 = st.columns(5)
            with test_col1:
                if kis_ok:
                    st.success(f"KIS API\n\n{kis_msg}")
                else:
                    st.warning(f"KIS API\n\n{kis_msg}")
            with test_col2:
                if ls_ok:
                    st.success(f"LS API\n\n{ls_msg}")
                else:
                    st.warning(f"LS API\n\nLS 계좌 미연결 또는 미사용\n\n{ls_msg}")
            with test_col3:
                if pykrx_ok:
                    st.success(f"PyKrx/KRX\n\n{pykrx_msg}")
                else:
                    st.error(f"PyKrx/KRX\n\n{pykrx_msg}")
            with test_col4:
                if naver_ok:
                    st.success(f"Naver\n\n{naver_msg}")
                else:
                    st.error(f"Naver\n\n{naver_msg}")
            with test_col5:
                if daum_ok:
                    st.success(f"Daum\n\n{daum_msg}")
                else:
                    st.error(f"Daum\n\n{daum_msg}")

            if not naver_ok and not daum_ok:
                st.error(
                    "Naver와 Daum 모두 연결에 실패했습니다. "
                    "장 마감 후 시장 전체 순위는 PyKrx 데이터에 의존하며, "
                    "PyKrx도 실패하면 수급 레이더를 표시할 수 없습니다."
                )
            elif not naver_ok:
                st.info("Naver 연결에 실패했지만 Daum API가 정상입니다.")
            elif not daum_ok:
                st.info("Daum 연결에 실패했지만 Naver 스크래핑이 정상입니다.")

    st.markdown("---")

    cfg_col1, cfg_col2, cfg_col3, cfg_col4, cfg_col5, cfg_col6 = st.columns(
        [1.1, 1.1, 1.1, 1.3, 0.9, 1.1]
    )

    with cfg_col1:
        market_sel = st.selectbox(
            "시장",
            options=["KOSPI", "KOSDAQ"],
            index=0,
            key="radar_market",
        )

    with cfg_col2:
        investor_sel = st.selectbox(
            "투자주체",
            options=["외국인", "기관"],
            index=0,
            key="radar_investor",
            help="시장 전체 Top N은 외국인과 기관만 제공합니다.",
        )

    with cfg_col3:
        trade_type_sel = st.selectbox(
            "매매방향",
            options=["순매수", "순매도"],
            index=0,
            key="radar_trade_type",
        )

    with cfg_col4:
        target_date = st.date_input(
            "기준일",
            value=today_date,
            max_value=today_date,
            key="radar_date",
        )

    with cfg_col5:
        top_n = st.selectbox(
            "표시 종목 수",
            options=[10, 20, 30, 50],
            index=2,
            key="radar_top_n",
        )

    if investor_sel != "외국인" and st.session_state.get("radar_interval") != "TODAY":
        st.session_state.radar_interval = "TODAY"

    interval_options = ["TODAY", "DAYS_5", "DAYS_20"] if investor_sel == "외국인" else ["TODAY"]

    with cfg_col6:
        interval_sel = st.selectbox(
            "집계 기간",
            options=interval_options,
            format_func=lambda value: INTERVAL_LABELS.get(value, value),
            index=0,
            key="radar_interval",
            help="5거래일·20거래일은 Daum API 외국인 데이터에서만 확인됩니다.",
        )

    st.caption(
        "기관은 현재 Daum API에서 확인된 당일 시장 전체 Top N만 제공합니다. "
        "외국인은 당일·5거래일·20거래일 집계를 선택할 수 있습니다."
    )

    if interval_sel != "TODAY":
        st.info(
            "5거래일·20거래일 수급은 Daum API의 기간 집계 데이터입니다. "
            "당일 장중 가집계와 달리 장 마감 후 최신 거래일 기준으로 제공됩니다."
        )

    cap_col, refresh_col = st.columns([4, 1])
    with cap_col:
        st.caption(
            f"{market_sel} · {investor_sel} · {trade_type_sel} · "
            f"{target_date} · Top {top_n} · "
            f"{INTERVAL_LABELS.get(interval_sel, interval_sel)}"
        )
    with refresh_col:
        if st.button("새로고침", use_container_width=True):
            get_market_radar_scanner.clear()
            st.rerun()

    st.markdown("---")

    with st.spinner(
        f"{target_date} {market_sel} {investor_sel} {trade_type_sel} 상위 종목을 조회하는 중..."
    ):
        df_radar = get_market_radar_scanner(
            target_date_obj=target_date,
            market=market_sel,
            investor=investor_sel,
            trade_type=trade_type_sel,
            top_n=top_n,
            interval_type=interval_sel,
        )

    if df_radar is None or df_radar.empty:
        if investor_sel == "기관":
            st.warning(
                "기관합계 수급 데이터를 수집하지 못했습니다. 장 마감 후 최신 거래일은 "
                "Daum/Naver/PyKrx 데이터를 사용하며, 현재 해당 소스의 응답 또는 "
                "파싱에 실패했을 수 있습니다. 상단의 연결 상태 테스트와 터미널 로그에서 "
                "Daum·Naver·PyKrx 상태를 확인해 주세요."
            )
        else:
            st.warning(
                "외국인 수급 데이터를 수집하지 못했습니다. 장 마감 후 최신 거래일은 "
                "Daum/Naver/PyKrx 데이터를 사용하며, 현재 해당 소스의 응답 또는 "
                "파싱에 실패했을 수 있습니다. 상단의 연결 상태 테스트와 터미널 로그에서 "
                "Daum·Naver·PyKrx 상태를 확인해 주세요."
            )
        return

    data_source = (
        str(df_radar.iloc[0]["데이터_출처"])
        if "데이터_출처" in df_radar.columns
        else "알 수 없음"
    )
    capture_date = (
        str(df_radar.iloc[0]["수집시각"])
        if "수집시각" in df_radar.columns
        else now_kst.strftime("%Y-%m-%d %H:%M:%S KST")
    )

    st.info(
        f"데이터 출처: {data_source} · 수집 시각: {capture_date}\n\n"
        "KIS 장중 가집계는 장중 참고용이며, 장 마감 후에는 Daum/Naver/PyKrx의 "
        "최신 거래일 데이터를 우선 사용합니다."
    )

    if now_kst.time() < time(15, 30):
        st.warning(
            "장중에는 14:30~14:40경 KIS 가집계 데이터가 표시될 수 있으며, "
            "최종 확정 수급은 장 마감 후 KRX 기준 데이터와 차이가 날 수 있습니다."
        )

    total_amount_eok = float(df_radar["순매수대금(억)"].sum())
    top_stock_name = df_radar.iloc[0]["종목명"] if not df_radar.empty else "-"
    top_stock_amt = float(df_radar.iloc[0]["순매수대금(억)"]) if not df_radar.empty else 0.0

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    with metric_col1:
        st.metric("총 순매수대금(억)", f"{total_amount_eok:,.1f}", delta=None)
    with metric_col2:
        st.metric(f"{trade_type_sel} 1위", top_stock_name, f"{top_stock_amt:,.1f}")
    with metric_col3:
        st.metric("조회 종목 수", f"{len(df_radar)}")
    with metric_col4:
        st.metric("데이터 소스", f"{data_source.split(' ')[0]}")

    # ==========================================================================
    # 트리맵
    # ==========================================================================
    st.markdown(f"#### {market_sel} {investor_sel} {trade_type_sel} 상위 종목 트리맵")

    df_plot = df_radar.copy()
    df_plot["절대순매수대금"] = pd.to_numeric(
        df_plot["순매수대금(억)"], errors="coerce"
    ).abs().fillna(0.0)

    df_plot["절대순매수대금"] = df_plot["절대순매수대금"].clip(lower=0.1)

    df_plot = df_plot.sort_values(
        "절대순매수대금", ascending=False
    ).reset_index(drop=True)

    max_abs_pct = (
        float(df_plot["등락률(%)"].abs().quantile(0.95))
        if len(df_plot) > 0
        else 8.0
    )
    color_bound = max(max_abs_pct, 5.0)

    fig_treemap = px.treemap(
        df_plot,
        path=["종목명"],
        values="절대순매수대금",
        color="등락률(%)",
        color_continuous_scale=["#1F6FEB", "#0D1117", "#F85149"],
        color_continuous_midpoint=0.0,
        range_color=[-color_bound, color_bound],
        custom_data=["종목코드", "순매수대금(억)", "등락률(%)"],
        title=(
            f"{target_date} {market_sel} {investor_sel} "
            f"{trade_type_sel} Top {len(df_plot)}"
        ),
    )

    fig_treemap.update_traces(
        textposition="middle center",
        textfont=dict(size=14, color="white"),
        hovertemplate=(
            "<b>%{label}</b><br>"
            "종목코드: %{customdata[0]}<br>"
            "순매수대금: %{customdata[1]:,.1f}억<br>"
            "등락률: %{customdata[2]:.2f}%<extra></extra>"
        ),
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

    disp_cols = [
        "순위",
        "종목코드",
        "종목명",
        "현재가",
        "등락률(%)",
        "순매수대금(억)",
        "데이터_출처",
    ]
    existing_cols = [col for col in disp_cols if col in df_radar.columns]
    df_display = df_radar[existing_cols].copy()

    format_dict = {
        "현재가": "{:,.0f}",
        "등락률(%)": "{:.2f}",
        "순매수대금(억)": "{:,.1f}",
    }

    st.dataframe(
        df_display.style.format(format_dict).background_gradient(
            subset=["순매수대금(억)"],
            cmap="Reds" if trade_type_sel == "순매수" else "Blues",
        ),
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("원본 데이터 및 디버그 정보", expanded=False):
        debug_cols = [
            "순위",
            "종목코드",
            "종목명",
            "현재가",
            "등락률(%)",
            "순매수대금(억)",
            "시가총액_가중",
            "데이터_출처",
            "수집시각",
        ]
        st.dataframe(
            df_radar[[col for col in debug_cols if col in df_radar.columns]],
            use_container_width=True,
            hide_index=True,
        )

        export_csv = df_radar.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "원본 데이터 CSV 다운로드",
            data=export_csv,
            file_name=(
                f"{target_date}_{market_sel}_{investor_sel}_"
                f"{trade_type_sel}_{interval_sel}_export.csv"
            ),
            mime="text/csv",
        )

    st.markdown("---")

    # ==========================================================================
    # 개별 종목 누적 수급
    # ==========================================================================
    st.markdown("#### 개별 종목 누적 수급")
    st.caption(
        "수급 레이더 상위 종목 중 하나를 선택하여 외국인·기관·개인 누적 수급과 "
        "종가 추이를 확인할 수 있습니다."
    )

    stock_options = [
        f"{row['종목명']} - {row['종목코드']}"
        for _, row in df_radar.iterrows()
    ]

    select_col1, select_col2 = st.columns([2, 2])
    with select_col1:
        selected_stock_str = st.selectbox(
            "종목 선택",
            options=stock_options,
            index=0,
            key="cum_stock_select",
        )
        selected_code = selected_stock_str.split("-")[-1].replace(" ", "").strip()
        selected_name = selected_stock_str.split("-")[0].strip()

    with select_col2:
        cum_start_date = st.date_input(
            "누적 수급 시작일",
            value=today_date - timedelta(days=60),
            max_value=today_date - timedelta(days=2),
            key="cum_start_date",
        )

    if selected_code:
        with st.spinner(f"{selected_name}({selected_code}) 누적 수급 데이터를 조회하는 중..."):
            df_cum = get_stock_cumulative_flow_from_base(
                stock_code=selected_code,
                start_date_obj=cum_start_date,
                end_date_obj=today_date,
            )

        if df_cum is not None and not df_cum.empty:
            is_estimated = bool(df_cum["is_estimated"].iloc[0]) if "is_estimated" in df_cum.columns else True
            cross_validated = bool(df_cum["cross_validated"].iloc[0]) if "cross_validated" in df_cum.columns else False
            source = str(df_cum["source"].iloc[0]) if "source" in df_cum.columns else ""

            # [수정] 소스별로 안내 메시지를 구분합니다.
            # Daum 종목별 실데이터는 개인(리테일)을 제공하지 않으므로 별도 안내를 표시합니다.
            has_retail = (
                "Retail_Cum" in df_cum.columns
                and df_cum["Retail_Cum"].notna().any()
            )

            if is_estimated:
                st.warning(
                    "PyKrx/Daum 원본 데이터가 아닌 추정 누적 수급입니다. "
                    "정확한 확정 수급은 KRX 기준 데이터와 차이가 날 수 있습니다."
                )
            elif not has_retail:
                st.info(
                    f"{source}. 외국인·기관은 실제 데이터이며, "
                    "개인(리테일) 순매수는 Daum이 직접 제공하지 않아 이 화면에서는 표시하지 않습니다."
                )
            elif cross_validated:
                st.success(f"{source} 교차 검증이 완료된 확정 수급 데이터입니다.")
            else:
                st.info(f"{source} 데이터입니다. 소스 특성상 KRX 확정치와 차이가 날 수 있습니다.")

            suffix = "(추정)" if is_estimated else "(확정)" if cross_validated else ""

            fig_cum = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.08,
                row_heights=[0.55, 0.45],
                subplot_titles=(
                    f"{selected_name} 종가",
                    f"외국인·기관·개인 누적 수급 {suffix}",
                ),
            )

            fig_cum.add_trace(
                go.Scatter(
                    x=df_cum["Date"],
                    y=df_cum["Close"],
                    name="Close",
                    line=dict(color="#58A6FF", width=2),
                ),
                row=1,
                col=1,
            )
            fig_cum.add_trace(
                go.Scatter(
                    x=df_cum["Date"],
                    y=df_cum["Foreigner_Cum"],
                    name=f"외국인 {suffix}",
                    line=dict(color="#FF7B72", width=2),
                ),
                row=2,
                col=1,
            )
            fig_cum.add_trace(
                go.Scatter(
                    x=df_cum["Date"],
                    y=df_cum["Institution_Cum"],
                    name=f"기관 {suffix}",
                    line=dict(color="#FFA657", width=2),
                ),
                row=2,
                col=1,
            )

            # [수정] 개인(리테일) 데이터가 있을 때만 라인을 추가합니다.
            # Daum 종목별 실데이터는 개인을 제공하지 않으므로 자동으로 생략됩니다.
            if has_retail:
                fig_cum.add_trace(
                    go.Scatter(
                        x=df_cum["Date"],
                        y=df_cum["Retail_Cum"],
                        name=f"개인 {suffix}",
                        line=dict(color="#7EE787", width=1.5, dash="dot"),
                    ),
                    row=2,
                    col=1,
                )

            fig_cum.update_layout(
                template="plotly_dark",
                height=520,
                margin=dict(t=40, l=10, r=10, b=10),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="right",
                    x=1,
                ),
                hovermode="x unified",
            )
            st.plotly_chart(fig_cum, use_container_width=True)
            st.caption(
                "KIS API, Daum API, Naver API, PyKrx 데이터의 제공 시점·집계 방식 차이로 "
                "인해 수급 값은 거래소 최종 확정치와 다를 수 있습니다."
            )
        else:
            st.warning(
                f"{selected_name}({selected_code})의 누적 수급 데이터를 가져오지 못했습니다."
            )
