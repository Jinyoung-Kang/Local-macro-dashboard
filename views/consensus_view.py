"""
views/consensus_view.py
🎯 기관 13F Money 교집합 분석 (Consensus Holdings)
선택한 특정 분기(시점)에서 주요 글로벌 기관들이 동시에 보유하거나 집중 매수/매도한 공통 종목 발굴
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from config import INSTITUTIONS
from services.consensus_service import (
    fetch_all_selected_histories,
    get_common_available_dates,
    calculate_consensus_by_date
)

def render_consensus_view():
    st.title("🎯 기관 13F Money 교집합 분석 (Consensus Holdings)")
    st.caption("선택한 특정 분기(시점)에서 주요 글로벌 기관들이 동시에 보유하거나 집중 매수/매도한 공통 종목을 발굴합니다.")

    # 1. 기관 다중 선택
    inst_names = list(INSTITUTIONS.keys())
    default_selected = [
        "🇺🇸 버크셔 해서웨이 (Berkshire Hathaway)",
        "🇺🇸 듀케인 패밀리 오피스 (Duquesne Family Office)",
    ]
    valid_defaults = [name for name in default_selected if name in inst_names]

    st.markdown("#### ⚙️ 교집합 분석 조건 설정")
    selected_insts = st.multiselect(
        "비교할 기관을 선택하세요 (최소 2개 이상)",
        options=inst_names,
        default=valid_defaults if valid_defaults else inst_names[:2]
    )

    if len(selected_insts) < 2:
        st.warning("⚠️ 공통 교집합을 분석하려면 최소 2개 이상의 기관을 선택해주세요.")
        return

    selected_dict = {name: INSTITUTIONS[name] for name in selected_insts}

    with st.spinner("선택된 기관들의 분기별 13F 공시 이력을 취합 중입니다..."):
        inst_histories = fetch_all_selected_histories(selected_dict, max_quarters=8)

    if not inst_histories:
        st.error("기관 데이터를 불러올 수 없습니다.")
        return

    available_dates = get_common_available_dates(inst_histories)
    if not available_dates:
        st.error("조회 가능한 공시 기준일이 없습니다.")
        return

    col_cfg1, col_cfg2 = st.columns([1, 1])
    with col_cfg1:
        selected_date = st.selectbox(
            "기준 공시일(분기) 선택",
            options=available_dates,
            index=0,
            key="consensus_date_select"
        )
    with col_cfg2:
        min_overlap = st.number_input(
            "최소 공통 보유 기관 수",
            min_value=2,
            max_value=max(2, len(selected_insts)),
            value=2,
            key="consensus_min_overlap"
        )

    # 2. 선택한 분기 기준 교집합 계산
    consensus_res = calculate_consensus_by_date(inst_histories, selected_date)

    if not consensus_res or consensus_res['summary'].empty:
        st.warning(f"선택한 기준일 `{selected_date}`에 공시 데이터가 없거나 공통 보유 종목이 존재하지 않습니다.")
        return

    summary_df = consensus_res['summary']
    filtered_df = summary_df[summary_df['holder_count'] >= min_overlap].copy()

    # 3. 요약 메트릭 카드 (4열 배치)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("해당 분기 공시 기관 수", f"{consensus_res['participating_count']} / {len(selected_insts)} 개")
    m2.metric(f"공통 보유 종목 수 (최소 {min_overlap}개 기관)", f"{len(filtered_df):,} 개")
    
    co_buy_count = len(filtered_df[filtered_df['buy_action_count'] >= min_overlap])
    m3.metric(
        "동시 순매수/비중확대 종목", 
        f"{co_buy_count:,} 개", 
        help=f"선택한 {min_overlap}개 이상 기관이 동시에 신규 매수하거나 비중을 확대한 종목"
    )

    co_sell_count = len(filtered_df[filtered_df['sell_action_count'] >= min_overlap])
    m4.metric(
        "동시 순매도/비중축소 종목", 
        f"{co_sell_count:,} 개", 
        help=f"선택한 {min_overlap}개 이상 기관이 동시에 전량 매도하거나 비중을 축소한 종목"
    )

    st.divider()

    # 4. 탭별 상세 시각화
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 공통 보유 상위 랭킹", 
        "🔥 동시 매수(신규/확대) 집중주", 
        "❄️ 동시 매도(전량/축소) 집중주",
        "📋 전체 공통 보유 종목 상세표"
    ])

    with tab1:
        top_n = st.selectbox("표시할 상위 종목 수", [10, 15, 20, 30, 50], index=0, key="consensus_top_n")
        plot_df = filtered_df.head(top_n).sort_values(by='total_value', ascending=True).copy()

        if not plot_df.empty:
            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=plot_df['total_value'] / 1e6,
                y=plot_df['name'],
                orientation='h',
                marker=dict(
                    color=plot_df['holder_count'],
                    colorscale='Viridis',
                    colorbar=dict(title="보유 기관 수")
                ),
                text=plot_df.apply(lambda r: f"{r['holder_count']}개 기관 (${r['total_value']/1e6:,.0f}M)", axis=1),
                textposition='outside',
                hovertemplate="<b>%{y}</b><br>합산 평가액: $%{x:,.1f}M<br>보유 기관수: %{marker.color}개<extra></extra>"
            ))
            fig.update_layout(
                height=max(420, top_n * 28 + 80),
                title=f"기관 13F Money 합산 평가액 상위 Top {top_n} (기준일: {selected_date})",
                xaxis_title="합산 평가액 ($M)",
                yaxis_title="",
                margin=dict(l=20, r=40, t=40, b=20)
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info(f"`{selected_date}` 기준 최소 {min_overlap}개 기관이 겹치는 공통 보유 종목이 없습니다.")

    with tab2:
        st.markdown(f"#### 🚀 직전 분기 대비 동시 매수/확대한 종목 (기준일: `{selected_date}`)")
        buy_filtered = filtered_df[filtered_df['buy_action_count'] >= 2].sort_values(
            by=['buy_action_count', 'total_value'], ascending=[False, False]
        )
        
        if not buy_filtered.empty:
            for _, row in buy_filtered.head(15).iterrows():
                with st.expander(f"⭐ **{row['name']}** — {row['buy_action_count']}개 기관 동시 매수 (합산 ${row['total_value']/1e6:,.1f}M)"):
                    st.write(f"**보유 기관:** {row['holders_str']}")
                    st.write(f"**평균 포트폴리오 비중:** `{row['avg_weight']:.2f}%` (최대 `{row['max_weight']:.2f}%`)")
        else:
            st.info(f"`{selected_date}` 분기에 2개 이상 기관이 동시에 신규 매수하거나 비중을 확대한 종목이 없습니다.")

    with tab3:
        st.markdown(f"#### ❄️ 직전 분기 대비 동시 매도/축소한 종목 (기준일: `{selected_date}`)")
        sell_filtered = filtered_df[filtered_df['sell_action_count'] >= 2].sort_values(
            by=['sell_action_count', 'total_value'], ascending=[False, False]
        )
        
        if not sell_filtered.empty:
            for _, row in sell_filtered.head(15).iterrows():
                with st.expander(f"🔻 **{row['name']}** — {row['sell_action_count']}개 기관 동시 매도/축소 (합산 ${row['total_value']/1e6:,.1f}M)"):
                    st.write(f"**보유 기관:** {row['holders_str']}")
                    st.write(f"**평균 포트폴리오 비중:** `{row['avg_weight']:.2f}%` (최대 `{row['max_weight']:.2f}%`)")
        else:
            st.info(f"`{selected_date}` 분기에 2개 이상 기관이 동시에 비중을 축소하거나 매도한 종목이 없습니다.")

    with tab4:
        st.subheader(f"📋 교집합 보유 목록 (기준일: {selected_date}, 총 {len(filtered_df)}개 종목)")
        display_tbl = filtered_df[['name', 'holder_count', 'holders_str', 'total_value', 'avg_weight', 'buy_action_count', 'sell_action_count']].copy()
        display_tbl.columns = ['종목명 (Issuer)', '보유 기관수', '보유 기관 목록', '합산 평가액($)', '평균 비중(%)', '동시 매수 기관수', '동시 매도 기관수']
        display_tbl['합산 평가액($)'] = display_tbl['합산 평가액($)'].map('${:,.0f}'.format)
        display_tbl['평균 비중(%)'] = display_tbl['평균 비중(%)'].map('{:.2f}%'.format)
        st.dataframe(display_tbl, use_container_width=True, hide_index=True)
