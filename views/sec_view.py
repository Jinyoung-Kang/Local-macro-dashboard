"""
views/sec_view.py
📑 주요 기관들의 포트폴리오 (13F Holdings & QoQ Analysis)
SEC EDGAR 공식 공시 데이터 기반 미국 주요 기관 투자자 포트폴리오 분석 & 기간별 비중 추적
"""
from collections import Counter
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from config import INSTITUTIONS
from services.ai_service import call_selected_ai_engine
from services.macro_service import fetch_ticker_data
from services.prompts import SEC_13F_CONSENSUS_PROMPT
from services.sec_service import classify_qoq_action, fetch_sec_13f_multi_quarters

def render_sec_view():
    st.title("📑 주요 기관들의 포트폴리오 (13F Holdings & QoQ Analysis)")
    st.caption("SEC EDGAR 공식 공시 데이터 기반 미국 주요 기관 투자자 포트폴리오 분석 & 기간별 비중 추적")

    # ==========================================
    # 🤖 [신규 기능] SEC 13F 기관 Money 교집합 기반 투자 테마 AI 요약
    # ==========================================
    with st.expander("🤖 [AI] SEC 13F 기관 Money 교집합 테마 분석 (Consensus Summary)", expanded=False):
        st.markdown("대형 기관 및 슈퍼 인베스터들이 최근 공통으로 보유·확대한 종목군을 바탕으로, **글로벌 스마트머니의 핵심 투자 내러티브와 공통 철학**을 AI가 구조화하여 분석합니다.")
        
        col_ai_sel, col_ai_btn = st.columns([2, 1])
        with col_ai_sel:
            ai_engine = st.selectbox(
                "분석에 사용할 AI 엔진을 선택하세요",
                [
                    "🛡️ 자동 탐색 (4단 Failover 무중단)",
                    "🥇 1순위: NVIDIA Nemotron-3 Super (120B)",
                    "🥈 2순위: Cloudflare AI (DeepSeek-R1-32B)",
                    "🥉 3순위: NVIDIA GPT-OSS-20B",
                    "🏅 4순위: Cerebras Cloud (Llama-3.3)"
                ],
                key="sec_ai_engine_select"
            )
        with col_ai_btn:
            st.write("")
            run_13f_ai = st.button("🚀 13F 스마트머니 테마 분석 실행", type="primary", use_container_width=True)

        if run_13f_ai:
            with st.spinner("전체 기관의 최신 13F 데이터를 취합하여 교집합을 추출 중입니다... (잠시만 기다려주세요)"):
                all_top_holdings = []
                
                # 등록된 전체 기관의 최신 분기 데이터 수집
                for inst_name, info in INSTITUTIONS.items():
                    hist, _ = fetch_sec_13f_multi_quarters(info['cik'], max_quarters=1)
                    if hist and len(hist) > 0:
                        top_df, _ = hist[0]
                        top_tickers = top_df.head(20)['name'].tolist()
                        all_top_holdings.extend(top_tickers)
                
                # 교집합 추출 (2개 이상 기관이 공통으로 보유한 종목)
                common_holdings = Counter(all_top_holdings).most_common(20)
                context_lines = [f"- {name}: {count}개 대형 기관 공통 보유 (포트폴리오 상위 편입)" for name, count in common_holdings if count >= 2]
                
                if not context_lines:
                    st.warning("공통 보유 종목 데이터를 충분히 추출하지 못했습니다.")
                else:
                    context_data = "\n".join(context_lines)
                    user_prompt = (
                        f"[최신 SEC 13F 대형 기관 공통 보유 상위 종목 현황]:\n"
                        f"{context_data}\n\n"
                        f"위 13F 공통 매수 데이터를 심층 분석하여 글로벌 기관들의 핵심 투자 내러티브와 공통 철학을 구조화된 형식으로 작성해줘."
                    )
                    
                    with st.spinner(f"'{ai_engine}' 엔진으로 13F 스마트머니 내러티브를 분석 중..."):
                        res = call_selected_ai_engine(ai_engine, user_prompt, SEC_13F_CONSENSUS_PROMPT)

                    if res.get("status"):
                        st.success(f"✅ 분석 완료 (엔진: {res['provider']} | 지연시간: {res['latency_ms']} ms)")
                        if "translation_info" in res:
                            st.caption(f"**번역 상태:** {res['translation_info']}")
                        st.markdown(f"<div style='padding:1rem; border-radius:0.5rem; background-color:rgba(0,100,255,0.1);'>{res['response']}</div>", unsafe_allow_html=True)
                    else:
                        st.error("🔴 분석 생성 실패")
                        st.caption(res.get("response", "응답 생성 실패"))

    st.divider()

    # ==========================================
    # 개별 기관 포트폴리오 분석 메인
    # ==========================================
    selected_inst_name = st.selectbox("분석할 기관을 선택하세요", options=list(INSTITUTIONS.keys()), index=0)
    inst_info = INSTITUTIONS[selected_inst_name]
    st.info(f"💡 **기관 소개:** {inst_info.get('desc', selected_inst_name)} (SEC CIK: `{inst_info['cik']}`)", icon="ℹ️")

    with st.spinner("SEC EDGAR에서 최근 분기별 13F 공시 데이터를 수집 및 분석 중입니다..."):
        all_history_results, err_msg = fetch_sec_13f_multi_quarters(inst_info['cik'], max_quarters=8)

    if err_msg or not all_history_results:
        st.error(f"⚠️ {err_msg if err_msg else '데이터를 불러올 수 없습니다.'}")
        return

    latest_df, latest_meta_info = all_history_results[0]
    latest_df = latest_df.sort_values(by='value', ascending=False).reset_index(drop=True)

    meta = {
        "report_date": latest_meta_info['report_date'],
        "filing_date": latest_meta_info['filing_date'],
        "total_aum": latest_df['value'].sum(),
        "total_count": len(latest_df),
        "top10_weight": latest_df.head(10)['weight'].sum()
    }

    # 원/달러 환율 종가 기준 원화 환산
    usdkrw_hist = fetch_ticker_data("KRW=X", period="5d")
    usdkrw_prev = 1416.85
    if usdkrw_hist is not None and len(usdkrw_hist) >= 2:
        usdkrw_prev = float(usdkrw_hist['Close'].iloc[-2])
    elif usdkrw_hist is not None and len(usdkrw_hist) == 1:
        usdkrw_prev = float(usdkrw_hist['Close'].iloc[-1])

    total_aum_krw = meta['total_aum'] * usdkrw_prev
    aum_krw_str = f"약 {total_aum_krw/1e12:,.1f}조 원" if total_aum_krw >= 1e12 else f"약 {total_aum_krw/1e8:,.0f}억 원"

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "총 운용자산 (AUM)", 
        f"${meta['total_aum']/1e9:,.2f} B", 
        delta=f"KRW {aum_krw_str}",
        delta_color="off",
        help=f"13F 공시 대상 미국 주식 총 평가액\n원/달러 전일 공식 종가({usdkrw_prev:,.2f}원/$) 기준 환산: {aum_krw_str}"
    )
    m2.metric("보유 종목 수", f"{meta['total_count']:,} 개")
    m3.metric("Top 10 집중도", f"{meta['top10_weight']:.1f} %", help="상위 10개 종목이 전체 포트폴리오에서 차지하는 비중")
    m4.metric("최신 보고서 기준일 (QoQ)", meta['report_date'], help=f"공시 제출일: {meta['filing_date']}")

    st.divider()

    st.subheader("📊 포트폴리오 시각화 및 기간별 비중 추이")
    tab_v1, tab_v2, tab_v3, tab_v4 = st.tabs([
        "🌳 포트폴리오 트리맵 (Treemap)", 
        "📈 기간별 비중 변화 추이 (QoQ History)",
        "🔄 직전 분기 대비 매수/매도 변동 (QoQ Changes)",
        "📊 상위 종목 비중 순위"
    ])

    # TAB 1: 트리맵
    with tab_v1:
        st.markdown("#### ⚙️ 트리맵 조회 조건 설정")
        col_t1, col_t2 = st.columns([1, 1])
        
        with col_t1:
            available_dates = [q_meta['report_date'] for _, q_meta in all_history_results]
            selected_report_date = st.selectbox(
                "기준 공시일(분기) 선택",
                options=available_dates,
                index=0,
                key="treemap_date_select"
            )
            
        selected_tree_df = None
        for df_q, q_meta in all_history_results:
            if q_meta['report_date'] == selected_report_date:
                selected_tree_df = df_q.sort_values(by='value', ascending=False).reset_index(drop=True)
                break
                
        if selected_tree_df is not None:
            with col_t2:
                tree_options = [10, 20, 30, 40, 50, 70, 100]
                valid_tree_options = [n for n in tree_options if n <= len(selected_tree_df)] or [min(10, len(selected_tree_df))]
                
                tree_n = st.selectbox(
                    "트리맵 표시 종목 수 선택",
                    options=valid_tree_options,
                    index=min(3, len(valid_tree_options)-1),
                    format_func=lambda x: f"상위 Top {x}개 종목",
                    key="treemap_n_select"
                )
                
            df_tree = selected_tree_df.head(tree_n).copy()
            df_tree['value_m'] = df_tree['value'] / 1e6
            
            df_tree['display_label'] = (
                "<b>" + df_tree['name'] + "</b><br>" + 
                df_tree['weight'].apply(lambda x: f"{x:.2f}%") + "<br>" + 
                df_tree['value_m'].apply(lambda x: f"${x:,.1f}M")
            )
            
            fig_tree = px.treemap(
                df_tree,
                path=['name'],
                values='value',
                title=f"{selected_inst_name} 주요 보유 종목 트리맵 (Top {tree_n}, 기준일: {selected_report_date})",
                color='weight',
                color_continuous_scale='Burg'
            )
            
            fig_tree.update_traces(
                text=df_tree['display_label'],
                textinfo="text",
                textposition="middle center",
                insidetextfont=dict(size=14, color="#161617", family="Arial, sans-serif"),
                marker=dict(line=dict(color="#0F172A", width=1.5)),
                hovertemplate="<b>%{label}</b><br>평가액: $%{value:,.0f}<br>포트폴리오 비중: %{color:.2f}%<extra></extra>"
            )
            
            dynamic_tree_height = max(480, int(360 + tree_n * 5.5))
            fig_tree.update_layout(
                height=dynamic_tree_height,
                margin=dict(l=10, r=10, t=40, b=10),
                coloraxis_colorbar=dict(title="비중 (%)")
            )
            st.plotly_chart(fig_tree, use_container_width=True)

    # TAB 2: 기간별 비중 추이
    with tab_v2:
        st.markdown("#### ⚙️ 기간별 비중 추이 조건 설정")
        col_ctl1, col_ctl2 = st.columns([1, 1])
        with col_ctl1:
            quarter_options = [2, 4, 6, 8]
            valid_q_options = [q for q in quarter_options if q <= len(all_history_results)] or [len(all_history_results)]
            selected_q_count = st.selectbox("조회 기간 선택 (분기)", options=valid_q_options, index=min(1, len(valid_q_options)-1), format_func=lambda x: f"최근 {x}개 분기 ({x*3}개월)")

        with col_ctl2:
            top_n_options = [10, 20, 30, 40, 50]
            valid_top_n = [n for n in top_n_options if n <= len(latest_df)] or [min(10, len(latest_df))]
            selected_top_n = st.selectbox("비교할 상위 종목 수 (Top N)", options=valid_top_n, index=0, format_func=lambda x: f"상위 Top {x}개 종목")

        active_history = all_history_results[:selected_q_count]
        
        appended_list = []
        for df_q, q_meta in active_history:
            temp_df = df_q.copy()
            temp_df['report_date'] = q_meta['report_date']
            appended_list.append(temp_df)
        combined_hist = pd.concat(appended_list, ignore_index=True)
        
        default_top_tickers = latest_df.head(selected_top_n)['name'].tolist()
        custom_tickers = st.multiselect("특정 종목만 직접 선택하여 비교 (선택 시 상위 N 대신 아래 선택 종목만 표기)", options=latest_df['name'].tolist(), default=[])
        target_tickers = custom_tickers if custom_tickers else default_top_tickers

        df_top_hist = combined_hist[combined_hist['name'].isin(target_tickers)].copy()
        df_top_hist['report_date'] = pd.to_datetime(df_top_hist['report_date'])
        df_top_hist = df_top_hist.sort_values(by='report_date', ascending=True)
        df_top_hist['report_date_str'] = df_top_hist['report_date'].dt.strftime('%Y-%m-%d')

        distinct_colors = (px.colors.qualitative.Plotly + px.colors.qualitative.Alphabet + px.colors.qualitative.Dark24 + px.colors.qualitative.Light24)
        fig_trend = go.Figure()
        max_y_val = df_top_hist['weight'].max() if not df_top_hist.empty else 10

        for idx, ticker in enumerate(target_tickers):
            sub_df = df_top_hist[df_top_hist['name'] == ticker]
            if not sub_df.empty:
                if len(target_tickers) <= 8:
                    fig_trend.add_trace(go.Scatter(
                        x=sub_df['report_date_str'], y=sub_df['weight'], mode='lines+markers+text', name=ticker,
                        text=sub_df['weight'].apply(lambda x: f"{x:.2f}%"), textposition='top center',
                        textfont=dict(size=10.5, color="#E5E7EB"), line=dict(width=2.5, color=distinct_colors[idx % len(distinct_colors)]),
                        marker=dict(size=7), cliponaxis=False, hovertemplate=f"<b>{ticker}</b><br>공시일: %{{x}}<br>비중: %{{y:.2f}}%<extra></extra>"
                    ))
                else:
                    fig_trend.add_trace(go.Scatter(
                        x=sub_df['report_date_str'], y=sub_df['weight'], mode='lines+markers', name=ticker,
                        line=dict(width=2, color=distinct_colors[idx % len(distinct_colors)]), marker=dict(size=6),
                        cliponaxis=False, hovertemplate=f"<b>{ticker}</b><br>공시일: %{{x}}<br>비중: %{{y:.2f}}%<extra></extra>"
                    ))

        fig_trend.update_layout(
            height=max(550, int(420 + len(target_tickers) * 12)),
            title=dict(text=f"{selected_inst_name} 상위 {len(target_tickers)}개 종목 분기별(QoQ) 비중 추이 (최근 {selected_q_count}분기)", font=dict(size=15), y=0.98, x=0.01, xanchor='left'),
            xaxis_title="공시 기준일 (분기)", yaxis=dict(title="포트폴리오 비중 (%)", range=[0, max(max_y_val * 1.18, 5.0)], automargin=True),
            hovermode="x unified", margin=dict(l=20, r=220, t=50, b=40),
            legend=dict(orientation="v", yanchor="top", y=1.0, xanchor="left", x=1.02, font=dict(size=10.5))
        )
        st.plotly_chart(fig_trend, use_container_width=True)

    # TAB 3: 직전 분기 대비 매수/매도 변동 내역
    with tab_v3:
        if len(all_history_results) >= 2:
            prev_df, prev_meta = all_history_results[1]
            merged_qoq = pd.merge(
                latest_df[['name', 'weight', 'value', 'shares']],
                prev_df[['name', 'weight', 'value', 'shares']],
                on='name', how='outer', suffixes=('_curr', '_prev')
            ).fillna(0)
            merged_qoq['weight_diff'] = merged_qoq['weight_curr'] - merged_qoq['weight_prev']
            merged_qoq['shares_diff'] = merged_qoq['shares_curr'] - merged_qoq['shares_prev']
            merged_qoq['value_diff'] = merged_qoq['value_curr'] - merged_qoq['value_prev']
            merged_qoq['action'] = merged_qoq.apply(classify_qoq_action, axis=1)

            st.markdown(f"**기준:** 최신 분기 `{latest_meta_info['report_date']}` vs 직전 분기 `{prev_meta['report_date']}`")
            top_increased = merged_qoq[merged_qoq['weight_diff'] > 0].sort_values(by='weight_diff', ascending=False).head(5)
            top_decreased = merged_qoq[merged_qoq['weight_diff'] < 0].sort_values(by='weight_diff', ascending=True).head(5)
            qoq_bar_df = pd.concat([top_decreased, top_increased])

            if not qoq_bar_df.empty:
                fig_qoq = go.Figure(go.Bar(
                    x=qoq_bar_df['weight_diff'], y=qoq_bar_df['name'], orientation='h',
                    marker=dict(color=['#EF4444' if x < 0 else '#10B981' for x in qoq_bar_df['weight_diff']]),
                    text=qoq_bar_df['weight_diff'].apply(lambda x: f"{x:+.2f}%p"), textposition='outside',
                    hovertemplate="<b>%{y}</b><br>비중 증감: %{x:+.2f}%p<extra></extra>"
                ))
                fig_qoq.update_layout(height=max(380, len(qoq_bar_df) * 34 + 90), title="직전 분기 대비 비중 변동 상위 종목 (%p)", xaxis_title="비중 증감폭 (%p)", yaxis_title="", margin=dict(l=20, r=40, t=40, b=20))
                st.plotly_chart(fig_qoq, use_container_width=True)

            qoq_display = merged_qoq[merged_qoq['action'] != "⚪ 유지 (Unchanged)"].sort_values(by='weight_diff', key=abs, ascending=False).head(30)
            qoq_table = qoq_display[['name', 'action', 'weight_curr', 'weight_diff', 'value_curr', 'shares_diff']].copy()
            qoq_table.columns = ['종목명 (Issuer)', '투자 활동', '현재 비중', '비중 증감(%p)', '현재 평가액($)', '주식수 증감']
            qoq_table['현재 비중'] = qoq_table['현재 비중'].map('{:.2f}%'.format)
            qoq_table['비중 증감(%p)'] = qoq_table['비중 증감(%p)'].map('{:+.2f}%p'.format)
            qoq_table['현재 평가액($)'] = qoq_table['현재 평가액($)'].map('${:,.0f}'.format)
            qoq_table['주식수 증감'] = qoq_table['주식수 증감'].map('{:+,.0f}'.format)
            st.dataframe(qoq_table, use_container_width=True, hide_index=True)
        else:
            st.info("비교 가능한 직전 분기 공시 데이터가 없습니다.")

    # TAB 4: 상위 종목 비중 순위
    with tab_v4:
        st.markdown("#### ⚙️ 상위 종목 비중 순위 조회 조건 설정")
        col_b1, col_b2 = st.columns([1, 1])
        
        with col_b1:
            available_dates_bar = [q_meta['report_date'] for _, q_meta in all_history_results]
            selected_bar_date = st.selectbox(
                "기준 공시일(분기) 선택",
                options=available_dates_bar,
                index=0,
                key="barchart_date_select"
            )
            
        selected_bar_df = None
        for df_q, q_meta in all_history_results:
            if q_meta['report_date'] == selected_bar_date:
                selected_bar_df = df_q.sort_values(by='weight', ascending=False).reset_index(drop=True)
                break
                
        if selected_bar_df is not None:
            with col_b2:
                bar_options = [10, 20, 30, 40, 50, 70, 100]
                valid_bar_options = [n for n in bar_options if n <= len(selected_bar_df)] or [min(10, len(selected_bar_df))]
                
                bar_n = st.selectbox(
                    "바 차트 표시 종목 수 선택",
                    options=valid_bar_options,
                    index=min(0, len(valid_bar_options)-1),
                    format_func=lambda x: f"상위 Top {x}개 종목",
                    key="barchart_n_select"
                )
                
            df_bar_top = selected_bar_df.head(bar_n).sort_values(by='weight', ascending=True).copy()
            
            fig_bar = go.Figure(go.Bar(
                x=df_bar_top['weight'], y=df_bar_top['name'], orientation='h',
                marker=dict(color='#0066FF', opacity=0.85), text=df_bar_top['weight'].apply(lambda x: f"{x:.2f}%"),
                textposition='outside', hovertemplate="<b>%{y}</b><br>포트폴리오 비중: %{x:.2f}%<extra></extra>"
            ))
            fig_bar.update_layout(
                height=max(380, bar_n * 26 + 100),
                title=f"{selected_inst_name} 상위 Top {bar_n} 보유 비중 (기준일: {selected_bar_date})",
                xaxis_title="포트폴리오 비중 (%)", yaxis_title="",
                margin=dict(l=20, r=40, t=40, b=20)
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    st.divider()

    # 전체 보유 지분 상세 목록
    st.subheader(f"📋 전체 보유 지분 상세 목록 (기준일: {meta['report_date']})")
    df_display = latest_df[['name', 'weight', 'value', 'shares', 'class', 'cusip']].copy()
    df_display.columns = ['종목명 (Issuer)', '비중 (%)', '평가액 ($)', '보유 주식수', '주식 종류', 'CUSIP']
    df_display['비중 (%)'] = df_display['비중 (%)'].map('{:.2f}%'.format)
    df_display['평가액 ($)'] = df_display['평가액 ($)'].map('${:,.0f}'.format)
    df_display['보유 주식수'] = df_display['보유 주식수'].map('{:,.0f}'.format)
    st.dataframe(df_display, use_container_width=True, hide_index=True)
