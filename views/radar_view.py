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

from services import store
from services.radar_service import (
    get_market_radar_scanner,
    get_stock_cumulative_flow_from_base,
    calculate_stock_flow_confirmation,
    test_kis_connection,
    test_ls_connection,
    test_pykrx_connection,
    test_naver_scraping,
    test_daum_scraping,
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
        if st.button("새로고침", width="stretch"):
            # [버그 수정] st.cache_data.clear()만으로는 아직 신선한 SQLite
            # 저장본이 그대로 반환돼 화면이 전혀 바뀌지 않았습니다.
            # request_refresh()가 저장본을 낡은 것으로 만들어 실제로 다시
            # 수집하게 합니다.
            store.request_refresh()
            st.cache_data.clear()
            if store.get_read_mode() == store.READ_MODE_STORE_ONLY:
                st.toast(
                    "store_only 모드입니다. 저장본만 다시 읽었습니다.",
                    icon="ℹ️",
                )
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
    st.plotly_chart(fig_treemap, width="stretch")

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
        width="stretch",
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
            width="stretch",
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
            # 선택 종목이 현재 레이더 목록에서 몇 위인지 찾습니다.
            selected_row = df_radar[
                df_radar["종목코드"].astype(str).str.replace(
                    "A",
                    "",
                    regex=False,
                ) == selected_code.replace("A", "")
            ]
    
            if selected_row.empty:
                selected_rank = 0
            else:
                selected_rank = int(selected_row.iloc[0]["순위"])
    
            # Daum 종목별 실제 수급 데이터일 때만 확증 점수를 계산합니다.
            confirmation = calculate_stock_flow_confirmation(
                flow_df=df_cum,
                rank=selected_rank,
                top_n=top_n,
                trade_type=trade_type_sel,
            )
            
            is_estimated = bool(df_cum["is_estimated"].iloc[0]) if "is_estimated" in df_cum.columns else True
            cross_validated = bool(df_cum["cross_validated"].iloc[0]) if "cross_validated" in df_cum.columns else False
            source = str(df_cum["source"].iloc[0]) if "source" in df_cum.columns else ""

            # 소스별로 안내 메시지를 구분합니다.
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

            # ==============================================================
            # 종목 수급 확증 점수
            # ==============================================================
            st.markdown("#### 🎯 종목 수급 확증 점수")
    
            if not confirmation.get("available"):
                st.info(
                    "수급 확증 점수는 Daum 종목별 외국인·기관 실데이터가 "
                    "확보된 경우에만 계산합니다. "
                    f"현재 상태: {confirmation.get('reason', '데이터 미확인')}"
                )
            else:
                total_score = confirmation["total_score"]
                grade = confirmation["grade"]
                grade_color = confirmation["grade_color"]
    
                grade_palette = {
                    "green": "#3FB950",
                    "blue": "#58A6FF",
                    "gray": "#8B949E",
                    "orange": "#D29922",
                    "red": "#F85149",
                }
                accent_color = grade_palette.get(
                    grade_color,
                    "#8B949E",
                )
    
                score_col1, score_col2 = st.columns([1, 2])
    
                with score_col1:
                    st.markdown(
                        f"""
                        <div style="
                            background-color:#161B22;
                            border:1px solid #30363D;
                            border-top:4px solid {accent_color};
                            border-radius:8px;
                            padding:16px;
                            min-height:132px;
                        ">
                            <div style="
                                color:#8B949E;
                                font-size:0.82rem;
                                margin-bottom:6px;
                            ">
                                {selected_name} · {trade_type_sel} 관점
                            </div>
                            <div style="
                                color:#F0F6FC;
                                font-size:2.0rem;
                                font-weight:700;
                                line-height:1.1;
                            ">
                                {total_score}<span style="
                                    color:#8B949E;
                                    font-size:0.95rem;
                                "> / 100</span>
                            </div>
                            <div style="
                                color:{accent_color};
                                font-size:0.92rem;
                                font-weight:600;
                                margin-top:8px;
                            ">
                                {grade}
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
    
                with score_col2:
                    score_breakdown = pd.DataFrame({
                        "평가 요소": [
                            "시장 전체 수급 순위",
                            "외국인 최근 5거래일",
                            "기관 최근 5거래일",
                            "외국인 보유율 변화",
                            "당일 거래량 대비 수급 강도",
                        ],
                        "점수": [
                            f"{confirmation['rank_score']} / 20",
                            f"{confirmation['foreign_score']} / 25",
                            f"{confirmation['institution_score']} / 25",
                            f"{confirmation['ownership_score']} / 15",
                            f"{confirmation['intensity_score']} / 15",
                        ],
                        "세부 정보": [
                            f"현재 레이더 {selected_rank}위 / Top {top_n}",
                            (
                                f"{confirmation['foreign_5d_sum']:+,.0f}주 · "
                                f"{confirmation['foreign_aligned_days']}/"
                                f"{confirmation['sample_days']}일 방향 일치"
                            ),
                            (
                                f"{confirmation['institution_5d_sum']:+,.0f}주 · "
                                f"{confirmation['institution_aligned_days']}/"
                                f"{confirmation['sample_days']}일 방향 일치"
                            ),
                            (
                                f"{confirmation['ownership_change_bp']:+.1f}bp"
                            ),
                            (
                                f"{confirmation['flow_intensity_pct']:+.2f}%"
                            ),
                        ],
                    })
    
                    st.dataframe(
                        score_breakdown,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "평가 요소": st.column_config.TextColumn(
                                width="medium",
                            ),
                            "점수": st.column_config.TextColumn(
                                width="small",
                            ),
                            "세부 정보": st.column_config.TextColumn(
                                width="large",
                            ),
                        },
                    )
    
                                # --------------------------------------------------------------
                # 확증 요인 / 주의 요인
                # 기존 st.success(), st.warning() 반복 표시 대신 한 개의
                # HTML 카드로 묶어 세로 길이를 줄이고 정보 밀도를 높입니다.
                # --------------------------------------------------------------
                reason_col1, reason_col2 = st.columns(2)

                with reason_col1:
                    st.markdown("##### ✅ 확증 요인")

                    if confirmation["positive_reasons"]:
                        positive_items = "".join(
                            f"""
                            <div style="
                                padding:8px 10px;
                                border-bottom:1px solid rgba(63,185,80,0.18);
                                color:#7EE787;
                                font-size:0.90rem;
                            ">
                                ✓ {reason}
                            </div>
                            """
                            for reason in confirmation["positive_reasons"]
                        )

                        st.markdown(
                            f"""
                            <div style="
                                background-color:rgba(35,134,54,0.18);
                                border:1px solid rgba(63,185,80,0.35);
                                border-radius:8px;
                                overflow:hidden;
                            ">
                                {positive_items}
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.caption(
                            "강한 동일 방향 수급 확증 요인이 아직 확인되지 않았습니다."
                        )

                with reason_col2:
                    st.markdown("##### ⚠️ 주의 요인")

                    if confirmation["warning_reasons"]:
                        warning_items = "".join(
                            f"""
                            <div style="
                                padding:8px 10px;
                                border-bottom:1px solid rgba(210,153,34,0.18);
                                color:#D29922;
                                font-size:0.90rem;
                            ">
                                ! {reason}
                            </div>
                            """
                            for reason in confirmation["warning_reasons"]
                        )

                        st.markdown(
                            f"""
                            <div style="
                                background-color:rgba(210,153,34,0.12);
                                border:1px solid rgba(210,153,34,0.35);
                                border-radius:8px;
                                overflow:hidden;
                            ">
                                {warning_items}
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            """
                            <div style="
                                background-color:#161B22;
                                border:1px solid #30363D;
                                border-radius:8px;
                                padding:12px;
                                color:#8B949E;
                                font-size:0.90rem;
                            ">
                                현재 확인 가능한 주요 수급 충돌 요인이 없습니다.
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                # --------------------------------------------------------------
                # 확증 점수 데이터 품질 / 기준일 안내
                # Daum API가 제공하는 실제 외국인·기관 일자별 수급만 사용했음을
                # 명확히 밝히고, 개인 수급 미제공도 함께 표시합니다.
                # --------------------------------------------------------------
                score_data_date = (
                    pd.to_datetime(df_cum["Date"].max()).strftime("%Y-%m-%d")
                    if "Date" in df_cum.columns and not df_cum.empty
                    else "알 수 없음"
                )
    
                st.markdown(
                    f"""
                    <div style="
                        margin-top:14px;
                        background-color:#161B22;
                        border:1px solid #30363D;
                        border-radius:6px;
                        padding:10px 14px;
                        color:#8B949E;
                        font-size:0.82rem;
                        line-height:1.65;
                    ">
                        <div style="color:#58A6FF; font-weight:600;">
                            📡 수급 확증 점수 데이터 기준
                        </div>
                        <div>
                            출처: <strong style="color:#C9D1D9;">
                            Daum 종목별 외국인/기관 일자별 수급 데이터</strong>
                            · 기준일: <strong style="color:#58A6FF;">
                            {score_data_date} 장 마감 후 집계</strong>
                        </div>
                        <div>
                            외국인·기관 수급, 외국인 보유율 변화, 당일 거래량,
                            시장 전체 레이더 순위를 결합해 계산합니다.
                            개인(리테일) 순매수는 Daum이 직접 제공하지 않아 점수에 포함하지 않습니다.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
    
                with st.expander(
                    "📖 종목 수급 확증 점수 해석 가이드",
                    expanded=False,
                ):
                    st.caption(
                        "수급 확증 점수는 당일 레이더 수급 방향과 최근 외국인·기관 수급의 "
                        "정합성을 0~100점으로 요약한 참고 지표입니다. "
                        "실제 Daum 종목별 수급 데이터가 있을 때만 계산됩니다."
                    )

                    guide_col1, guide_col2 = st.columns([1.05, 1.95])

                    with guide_col1:
                        st.markdown("##### 점수 등급")

                        st.markdown(
                            """
                            <div style="
                                border:1px solid #30363D;
                                border-radius:8px;
                                overflow:hidden;
                                background-color:#161B22;
                                font-size:0.87rem;
                            ">
                                <div style="
                                    padding:9px 12px;
                                    border-left:4px solid #3FB950;
                                    border-bottom:1px solid #30363D;
                                    color:#C9D1D9;
                                ">
                                    <strong style="color:#3FB950;">80~100점</strong>
                                    · 강한 수급 확증
                                </div>
                                <div style="
                                    padding:9px 12px;
                                    border-left:4px solid #58A6FF;
                                    border-bottom:1px solid #30363D;
                                    color:#C9D1D9;
                                ">
                                    <strong style="color:#58A6FF;">60~79점</strong>
                                    · 수급 확증 우위
                                </div>
                                <div style="
                                    padding:9px 12px;
                                    border-left:4px solid #8B949E;
                                    border-bottom:1px solid #30363D;
                                    color:#C9D1D9;
                                ">
                                    <strong style="color:#8B949E;">40~59점</strong>
                                    · 수급 혼조
                                </div>
                                <div style="
                                    padding:9px 12px;
                                    border-left:4px solid #D29922;
                                    border-bottom:1px solid #30363D;
                                    color:#C9D1D9;
                                ">
                                    <strong style="color:#D29922;">20~39점</strong>
                                    · 약한 수급 확증
                                </div>
                                <div style="
                                    padding:9px 12px;
                                    border-left:4px solid #F85149;
                                    color:#C9D1D9;
                                ">
                                    <strong style="color:#F85149;">0~19점</strong>
                                    · 반대 수급 우세
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                        with guide_col2:
                            st.markdown("##### 평가 요소와 배점")
    
                            score_guide_df = pd.DataFrame({
                                "평가 요소": [
                                    "시장 전체 수급 순위",
                                    "외국인 최근 5거래일 수급",
                                    "기관 최근 5거래일 수급",
                                    "외국인 보유율 변화",
                                    "당일 거래량 대비 수급 강도",
                                ],
                                "최대 점수": [
                                    "20점",
                                    "25점",
                                    "25점",
                                    "15점",
                                    "15점",
                                ],
                                "확증 판단 기준": [
                                    "현재 시장 전체 Top N에서 상위권일수록 높은 점수",
                                    "최근 5거래일 누적 방향 및 일별 방향 일치 여부",
                                    "최근 5거래일 누적 방향 및 일별 방향 일치 여부",
                                    "외국인 보유율이 레이더 수급 방향과 같은 방향으로 변화",
                                    "외국인+기관 당일 순매수량이 거래량에서 차지하는 비중",
                                ],
                            })
    
                            st.dataframe(
                                score_guide_df,
                                width="stretch",
                                hide_index=True,
                                column_config={
                                    "평가 요소": st.column_config.TextColumn(
                                        width="medium",
                                    ),
                                    "최대 점수": st.column_config.TextColumn(
                                        width="small",
                                    ),
                                    "확증 판단 기준": st.column_config.TextColumn(
                                        width="large",
                                    ),
                                },
                            )
    
                        st.markdown(
                            """
                            <div style="
                                margin-top:12px;
                                background-color:rgba(88,166,255,0.08);
                                border:1px solid rgba(88,166,255,0.25);
                                border-left:4px solid #58A6FF;
                                border-radius:6px;
                                padding:11px 14px;
                                color:#C9D1D9;
                                font-size:0.84rem;
                                line-height:1.65;
                            ">
                                <strong style="color:#58A6FF;">점수 해석 예시:</strong><br>
                                순매수 레이더에서 외국인·기관의 최근 5거래일 누적 수급이 모두 순매수이고,
                                외국인 보유율까지 상승하며 시장 전체 순위가 높다면 높은 점수가 부여됩니다.
                                반대로 당일 순매수 상위 종목이더라도 최근 5거래일 외국인 또는 기관 수급이
                                반대 방향이면 확증 점수가 낮아집니다.
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
    
                        st.markdown(
                            """
                            <div style="
                                margin-top:8px;
                                background-color:rgba(210,153,34,0.10);
                                border:1px solid rgba(210,153,34,0.28);
                                border-left:4px solid #D29922;
                                border-radius:6px;
                                padding:11px 14px;
                                color:#C9D1D9;
                                font-size:0.84rem;
                                line-height:1.65;
                            ">
                                <strong style="color:#D29922;">⚠️ 해석 유의사항:</strong><br>
                                이 점수는 미래 가격 상승·하락을 예측하거나 매수·매도를 추천하는 신호가 아닙니다.
                                단기 수급은 ETF 설정·환매, 프로그램 매매, 차익거래, 대차·결제 시차,
                                블록딜 등으로 왜곡될 수 있습니다. 실적·밸류에이션·시장 환경·변동성·뉴스와
                                함께 사용하세요.
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

            
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

            # 개인(리테일) 데이터가 있을 때만 라인을 추가합니다.
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
            st.plotly_chart(fig_cum, width="stretch")
            st.caption(
                "KIS API, Daum API, Naver API, PyKrx 데이터의 제공 시점·집계 방식 차이로 "
                "인해 수급 값은 거래소 최종 확정치와 다를 수 있습니다."
            )
        else:
            st.warning(
                f"{selected_name}({selected_code})의 누적 수급 데이터를 가져오지 못했습니다."
            )
