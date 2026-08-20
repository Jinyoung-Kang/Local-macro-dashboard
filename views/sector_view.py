"""
views/sector_view.py
섹터 & 자산군 로테이션 맵 (Sector Momentum & Rotation)
기존 고유 UI(HTML 테이블, 모멘텀 순위) 완벽 보존 및 시계열 비교 차트(Tab 2) 고도화 적용
"""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime
from config import SECTOR_ETFS, ASSET_CLASS_ETFS
from services.sector_service import calculate_returns_matrix


def render_styled_table(df: pd.DataFrame, return_cols: list):
    """
    Streamlit DataGrid의 CSS 색상 무시 문제를 해결하기 위해
    HTML/CSS 기반으로 양수(+% 초록), 음수(-% 빨강)를 렌더링하는 테이블 함수
    """
    html = """
    <div style="overflow-x: auto; margin-top: 10px; margin-bottom: 25px;">
        <table style="
            width: 100%; 
            border-collapse: collapse; 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
            font-size: 13.5px; 
            background: rgba(255, 255, 255, 0.02); 
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 8px;
        ">
            <thead>
                <tr style="background: rgba(255, 255, 255, 0.06); border-bottom: 1px solid rgba(255, 255, 255, 0.18);">
    """
    for col in df.columns:
        align = "right" if col in return_cols else "left"
        html += f"<th style='padding: 10px 14px; font-weight: 600; color: #E2E8F0; text-align: {align}; white-space: nowrap;'>{col}</th>"
    html += "</tr></thead><tbody>"

    for _, row in df.iterrows():
        html += "<tr style='border-bottom: 1px solid rgba(255, 255, 255, 0.05);'>"
        for col in df.columns:
            val = row[col]
            if col in return_cols:
                try:
                    num = float(val)
                    if num > 0:
                        color = "#10B981"  # 초록색
                        weight = "bold"
                        val_str = f"+{num:.2f}%"
                    elif num < 0:
                        color = "#EF4444"  # 빨간색
                        weight = "bold"
                        val_str = f"{num:.2f}%"
                    else:
                        color = "#94A3B8"  # 회색
                        weight = "normal"
                        val_str = f"{num:.2f}%"
                    html += f"<td style='padding: 9px 14px; text-align: right; color: {color}; font-weight: {weight}; font-family: monospace; font-size: 13.5px; white-space: nowrap;'>{val_str}</td>"
                except Exception:
                    html += f"<td style='padding: 9px 14px; text-align: right; color: #E2E8F0;'>{val}</td>"
            else:
                bold = "font-weight: 600;" if col in ['티커', '섹터명', '자산군 명칭'] else "color: #94A3B8;"
                html += f"<td style='padding: 9px 14px; text-align: left; color: #E2E8F0; {bold} white-space: nowrap;'>{val}</td>"
        html += "</tr>"
    html += "</tbody></table></div>"

    st.markdown(html, unsafe_allow_html=True)


def normalize_cumulative_return(series: pd.Series, lookback_days: int | None = None) -> pd.Series:
    """시계열을 공통 시작점 0% 기준 누적 수익률로 변환"""
    clean = series.dropna().copy()
    if lookback_days is not None:
        clean = clean.iloc[-lookback_days:]
    if clean.empty:
        return pd.Series(dtype=float)
    base = clean.iloc[0]
    if base == 0:
        return pd.Series(dtype=float)
    return ((clean / base) - 1) * 100


def render_sector_view():
    st.title("🔄 섹터 & 자산군 로테이션 맵 (Sector Momentum & Rotation)")
    st.caption("S&P 500 11대 섹터 및 글로벌 핵심 자산군의 단기/중기 자금 이동과 주도 섹터(공격 vs 방어)를 모니터링합니다.")

    with st.spinner("S&P 500 섹터 및 자산군 시세 데이터를 분석 중입니다..."):
        sector_df, sector_hist = calculate_returns_matrix(SECTOR_ETFS, benchmark_ticker="SPY")
        asset_df, asset_hist = calculate_returns_matrix(ASSET_CLASS_ETFS, benchmark_ticker="SPY")

    if sector_df is None or sector_df.empty:
        st.error("섹터 데이터를 불러오는 데 실패했습니다. 잠시 후 다시 시도해주세요.")
        return

    # S&P 500(SPY) 초과 성과(Alpha) 컬럼 동적 보완 (Tab 1 안전 장치)
    for col in ["1W", "1M", "3M", "6M", "1Y", "YTD"]:
        alpha_col = f"{col}_alpha"
        if alpha_col not in sector_df.columns:
            spy_row = sector_df[sector_df['ticker'] == 'SPY']
            if not spy_row.empty:
                spy_val = spy_row.iloc[0][col]
                sector_df[alpha_col] = sector_df[col] - spy_val
            else:
                sector_df[alpha_col] = sector_df[col]

    # 기준 거래일 추출 (어떤 티커에서든 최근 날짜를 정확히 포착)
    latest_date_str = ""
    for hist_dict in [sector_hist, asset_hist]:
        if hist_dict:
            for s in hist_dict.values():
                if s is not None and not s.empty:
                    latest_date_str = s.index[-1].strftime('%Y-%m-%d')
                    break
        if latest_date_str:
            break
    if not latest_date_str:
        latest_date_str = datetime.now().strftime('%Y-%m-%d')

    # 1. 메인 핵심 요약 메트릭
    best_1m = sector_df.sort_values(by="1M", ascending=False).iloc[0]
    worst_1m = sector_df.sort_values(by="1M", ascending=True).iloc[0]
    best_ytd = sector_df.sort_values(by="YTD", ascending=False).iloc[0]

    m1, m2, m3 = st.columns(3)
    m1.metric("최근 1개월 1등 주도 섹터", f"{best_1m['name'].split()[0]} ({best_1m['ticker']})", f"{best_1m['1M']:+.2f}%")
    m2.metric("최근 1개월 최하위 섹터", f"{worst_1m['name'].split()[0]} ({worst_1m['ticker']})", f"{worst_1m['1M']:+.2f}%")
    m3.metric("올해(YTD) 1등 주도 섹터", f"{best_ytd['name'].split()[0]} ({best_ytd['ticker']})", f"{best_ytd['YTD']:+.2f}%")

    st.divider()

    # 2. 탭별 상세 시각화
    tab1, tab2, tab3 = st.tabs([
        "📊 11대 섹터 모멘텀 순위",
        "📈 섹터별 누적 수익률 추이",
        "🌐 글로벌 자산군(Asset Class) 로테이션"
    ])

    return_cols = ['1주(%)', '1개월(%)', '3개월(%)', '6개월(%)', '1년(%)', 'YTD(%)']

    # ==============================================================================
    # TAB 1: 11대 섹터 모멘텀 순위 (원본 유지)
    # ==============================================================================
    with tab1:
        st.markdown("#### ⚙️ 기간별 섹터 성과 순위")
        col_c1, col_c2 = st.columns([1, 1])
        with col_c1:
            period_sel = st.selectbox("조회 기간 선택", ["1W", "1M", "3M", "6M", "1Y", "YTD"], index=1, key="sector_period_sel")
        with col_c2:
            mode_sel = st.radio("표시 기준", ["단순 수익률 (%)", "S&P 500(SPY) 대비 초과성과 (Alpha %p)"], index=0, horizontal=True)

        target_col = period_sel if mode_sel == "단순 수익률 (%)" else f"{period_sel}_alpha"
        sorted_df = sector_df.sort_values(by=target_col, ascending=True).copy()

        fig_bar = go.Figure()
        fig_bar.add_trace(go.Bar(
            x=sorted_df[target_col],
            y=sorted_df['ticker'] + " (" + sorted_df['name'] + ")",
            orientation='h',
            marker=dict(
                color=['#EF4444' if v < 0 else '#10B981' for v in sorted_df[target_col]]
            ),
            text=sorted_df[target_col].apply(lambda x: f"{x:+.2f}%" if mode_sel == "단순 수익률 (%)" else f"{x:+.2f}%p"),
            textposition='outside',
            hovertemplate="<b>%{y}</b><br>성과: %{x:+.2f}%<extra></extra>"
        ))
        fig_bar.add_vline(x=0, line_dash="dash", line_color="white", opacity=0.6)
        fig_bar.update_layout(
            height=460,
            title=f"11대 섹터 {period_sel} {mode_sel} 순위 (기준일: {latest_date_str})",
            xaxis_title="수익률 (%)" if mode_sel == "단순 수익률 (%)" else "초과성과 (%p)",
            yaxis_title="",
            margin=dict(l=20, r=50, t=40, b=20)
        )
        st.plotly_chart(fig_bar, use_container_width=True)

        # 제목 및 기준일 (회색 글씨 적용)
        st.markdown(
            f"#### 📋 11대 섹터 기간별 수익률 종합 매트릭스 <span style='color: #94A3B8; font-size: 14.5px; font-weight: normal;'>(기준일: {latest_date_str})</span>",
            unsafe_allow_html=True
        )

        disp_df = sector_df[['ticker', 'name', 'type', '1W', '1M', '3M', '6M', '1Y', 'YTD']].copy()
        disp_df.columns = ['티커', '섹터명', '성격', '1주(%)', '1개월(%)', '3개월(%)', '6개월(%)', '1년(%)', 'YTD(%)']
        render_styled_table(disp_df, return_cols)

    # ==============================================================================
    # TAB 2: 섹터별 상대 수익률 시계열 비교 (고도화 반영)
    # ==============================================================================
    with tab2:
        st.markdown("#### 📈 섹터별 상대 수익률 시계열 비교")

        PERIOD_CONFIG = {
            "1개월": 22,
            "3개월": 66,
            "6개월": 132,
            "1년": 264,
            "2년": None,
        }

        SECTOR_COLORS = {
            "XLK": "#60A5FA",   # 정보기술: 파랑
            "XLF": "#34D399",   # 금융: 초록
            "XLV": "#F472B6",   # 헬스케어: 분홍
            "XLY": "#FBBF24",   # 임의소비재: 노랑
            "XLI": "#A78BFA",   # 산업재: 보라
            "XLC": "#22D3EE",   # 통신: 청록
            "XLE": "#FB923C",   # 에너지: 주황
            "XLP": "#4ADE80",   # 필수소비재: 연녹색
            "XLRE": "#F87171",  # 부동산: 빨강
            "XLU": "#94A3B8",   # 유틸리티: 회색
            "XLB": "#D97706",   # 소재: 갈색
        }

        col_t1, col_t2 = st.columns([2, 1])

        with col_t1:
            selected_tickers = st.multiselect(
                "비교할 섹터 선택",
                options=list(SECTOR_ETFS.keys()),
                default=["XLK", "XLF", "XLV", "XLE"],
                format_func=lambda ticker: f"{ticker} — {SECTOR_ETFS[ticker]['name']}",
                key="sector_relative_tickers",
            )

        with col_t2:
            chart_period = st.selectbox(
                "비교 기간",
                options=list(PERIOD_CONFIG.keys()),
                index=2,
                key="sector_relative_period",
            )

        MAX_SECTORS = 6
        if len(selected_tickers) > MAX_SECTORS:
            st.warning(f"가독성을 위해 최대 {MAX_SECTORS}개 섹터만 비교할 수 있습니다.")
            selected_tickers = selected_tickers[:MAX_SECTORS]

        lookback_days = PERIOD_CONFIG[chart_period]

        if not selected_tickers:
            st.info("비교할 섹터를 하나 이상 선택하세요.")

        elif "SPY" not in sector_hist or sector_hist["SPY"].empty:
            st.warning("SPY 벤치마크 데이터를 불러오지 못했습니다.")

        else:
            fig_trend = go.Figure()

            spy_close = sector_hist["SPY"]
            spy_return = normalize_cumulative_return(spy_close, lookback_days=lookback_days)

            if spy_return.empty:
                st.warning("SPY 수익률 데이터를 계산할 수 없습니다.")
            else:
                # 0% 기준선
                fig_trend.add_hline(
                    y=0,
                    line_width=1,
                    line_dash="dot",
                    line_color="rgba(148, 163, 184, 0.55)",
                    annotation_text="0% 기준",
                    annotation_position="bottom right",
                )

                # SPY 벤치마크
                fig_trend.add_trace(
                    go.Scatter(
                        x=spy_return.index,
                        y=spy_return.values,
                        mode="lines",
                        name=f"SPY ({spy_return.iloc[-1]:+.1f}%)",
                        line=dict(
                            color="#E5E7EB",
                            width=3,
                            dash="dash",
                        ),
                        hovertemplate=(
                            "<b>S&P 500 (SPY)</b><br>"
                            "날짜: %{x|%Y-%m-%d}<br>"
                            "누적 수익률: %{y:+.2f}%"
                            "<extra></extra>"
                        ),
                    )
                )

                # 섹터별 수익률
                for ticker in selected_tickers:
                    if ticker not in sector_hist:
                        continue

                    sector_close = sector_hist[ticker]

                    if sector_close is None or sector_close.empty:
                        continue

                    sector_return = normalize_cumulative_return(sector_close, lookback_days=lookback_days)

                    if sector_return.empty:
                        continue

                    aligned_sector, aligned_spy = sector_return.align(spy_return, join="inner")

                    if aligned_sector.empty:
                        continue

                    alpha = aligned_sector - aligned_spy
                    latest_return = float(aligned_sector.iloc[-1])
                    latest_alpha = float(alpha.iloc[-1])

                    sector_name = SECTOR_ETFS[ticker].get("name", ticker)

                    fig_trend.add_trace(
                        go.Scatter(
                            x=aligned_sector.index,
                            y=aligned_sector.values,
                            mode="lines",
                            name=f"{ticker} {sector_name} ({latest_return:+.1f}%, α {latest_alpha:+.1f}%p)",
                            line=dict(
                                color=SECTOR_COLORS.get(ticker, "#38BDF8"),
                                width=2.5,
                            ),
                            hovertemplate=(
                                f"<b>{ticker} — {sector_name}</b><br>"
                                "날짜: %{x|%Y-%m-%d}<br>"
                                "누적 수익률: %{y:+.2f}%<br>"
                                "SPY 대비 초과 수익률: %{customdata:+.2f}%p"
                                "<extra></extra>"
                            ),
                            customdata=alpha.values,
                        )
                    )

                fig_trend.update_layout(
                    template="plotly_dark",
                    height=520,
                    margin=dict(l=20, r=20, t=55, b=25),
                    title=dict(
                        text=f"{chart_period} 섹터 누적 수익률 및 S&P 500 대비 초과 성과",
                        x=0.02,
                        xanchor="left",
                    ),
                    legend=dict(
                        orientation="h",
                        yanchor="bottom",
                        y=1.02,
                        xanchor="left",
                        x=0,
                        bgcolor="rgba(0,0,0,0)",
                    ),
                    hovermode="x unified",
                    xaxis=dict(
                        title="",
                        showgrid=False,
                        rangeslider=dict(visible=False),
                    ),
                    yaxis=dict(
                        title="누적 수익률 (%)",
                        ticksuffix="%",
                        gridcolor="rgba(148, 163, 184, 0.15)",
                        zeroline=False,
                    ),
                    paper_bgcolor="#0E1117",
                    plot_bgcolor="#0E1117",
                )

                st.plotly_chart(fig_trend, use_container_width=True, key="sector_relative_return_chart")

                # 하단 기간 성과 요약 표
                summary_rows = []
                for ticker in selected_tickers:
                    if ticker not in sector_hist:
                        continue

                    sector_return = normalize_cumulative_return(sector_hist[ticker], lookback_days=lookback_days)
                    aligned_sector, aligned_spy = sector_return.align(spy_return, join="inner")

                    if aligned_sector.empty:
                        continue

                    summary_rows.append({
                        "티커": ticker,
                        "섹터": SECTOR_ETFS[ticker].get("name", ticker),
                        "누적 수익률": float(aligned_sector.iloc[-1]),
                        "SPY 대비 초과 성과": float(aligned_sector.iloc[-1] - aligned_spy.iloc[-1]),
                    })

                if summary_rows:
                    summary_df = (
                        pd.DataFrame(summary_rows)
                        .sort_values("SPY 대비 초과 성과", ascending=False)
                        .reset_index(drop=True)
                    )

                    st.markdown("##### 📊 기간 성과 요약")
                    st.dataframe(
                        summary_df.style.format({
                            "누적 수익률": "{:+.2f}%",
                            "SPY 대비 초과 성과": "{:+.2f}%p",
                        }).background_gradient(
                            subset=["SPY 대비 초과 성과"],
                            cmap="RdYlGn",
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

    # ==============================================================================
    # TAB 3: 글로벌 자산군 로테이션 (원본 유지)
    # ==============================================================================
    with tab3:
        if asset_df is not None and not asset_df.empty:
            st.markdown("#### 🌐 주식 · 채권 · 원자재 · 달러 자산군 모멘텀 순위")
            asset_period = st.selectbox("자산군 순위 기준 기간", ["1W", "1M", "3M", "6M", "1Y", "YTD"], index=1, key="asset_period_sel")
            sorted_asset = asset_df.sort_values(by=asset_period, ascending=True).copy()

            fig_asset = go.Figure(go.Bar(
                x=sorted_asset[asset_period],
                y=sorted_asset['name'] + " (" + sorted_asset['ticker'] + ")",
                orientation='h',
                marker=dict(
                    color=['#EF4444' if v < 0 else '#3B82F6' for v in sorted_asset[asset_period]]
                ),
                text=sorted_asset[asset_period].apply(lambda x: f"{x:+.2f}%"),
                textposition='outside',
                hovertemplate="<b>%{y}</b><br>수익률: %{x:+.2f}%<extra></extra>"
            ))
            fig_asset.add_vline(x=0, line_dash="dash", line_color="white", opacity=0.6)
            fig_asset.update_layout(
                height=460,
                title=f"글로벌 주요 자산군 {asset_period} 수익률 순위 (기준일: {latest_date_str})",
                xaxis_title="수익률 (%)", yaxis_title="",
                margin=dict(l=20, r=50, t=40, b=20)
            )
            st.plotly_chart(fig_asset, use_container_width=True)

            # 제목 및 기준일 (회색 글씨 적용)
            st.markdown(
                f"#### 📋 자산군별 기간별 수익률표 <span style='color: #94A3B8; font-size: 14.5px; font-weight: normal;'>(기준일: {latest_date_str})</span>",
                unsafe_allow_html=True
            )

            disp_asset = asset_df[['ticker', 'name', 'type', '1W', '1M', '3M', '6M', '1Y', 'YTD']].copy()
            disp_asset.columns = ['티커', '자산군 명칭', '카테고리', '1주(%)', '1개월(%)', '3개월(%)', '6개월(%)', '1년(%)', 'YTD(%)']
            render_styled_table(disp_asset, return_cols)
