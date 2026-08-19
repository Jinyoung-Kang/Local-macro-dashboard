"""
views/liquidity_view.py
🏛️ 연준 순유동성 트래커 (Fed Net Liquidity Tracker) 뷰
연준 총자산, TGA, ON RRP 잔고 및 증시 지수 오버레이 상관관계 분석
"""
from datetime import datetime
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pytz
import streamlit as st
import yfinance as yf
from services.liquidity_service import get_fed_liquidity_data

@st.cache_data(ttl=60)
def fetch_overlay_index_data(ticker: str, start_date: str) -> pd.DataFrame:
    """비교할 자산 지수의 시계열 수집"""
    try:
        tk = yf.Ticker(ticker)
        df = tk.history(start=start_date)
        if df is not None and not df.empty:
            df = df.dropna(subset=['Close'])
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
            return df[['Close']]
    except Exception:
        pass
    return None


def render_liquidity_view():
    now = datetime.now(pytz.timezone('Asia/Seoul'))
    now_str = now.strftime("%Y-%m-%d %H:%M:%S KST")

    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🏛️ 연준 순유동성 트래커 (Fed Net Liquidity Tracker)
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            연준 총자산에서 재무부 일반계좌(TGA)와 역레포(ON RRP)를 차감한 실제 금융시장 가용 유동성을 모니터링합니다.
        </p>
    </div>
    """, unsafe_allow_html=True)

    with st.spinner("연준(FRED)으로부터 최신 순유동성 데이터를 수집 및 연산하고 있습니다..."):
        df_liq = get_fed_liquidity_data(period_years=10)

    if df_liq is None or df_liq.empty:
        st.error("유동성 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.")
        return

    # 최신 수치 및 변동폭 계산
    latest = df_liq.iloc[-1]
    prev_1w = df_liq.iloc[-2] if len(df_liq) >= 2 else latest
    prev_1m = df_liq.iloc[-5] if len(df_liq) >= 5 else prev_1w
    
    # YTD (연초 대비)
    curr_year = latest.name.year if hasattr(latest.name, 'year') else datetime.now().year
    df_curr_year = df_liq[df_liq.index.year == curr_year]
    ytd_base = df_curr_year.iloc[0] if not df_curr_year.empty else latest

    net_curr_t = latest['Net_Liquidity_T']
    net_diff_1w_b = (latest['Net_Liquidity_T'] - prev_1w['Net_Liquidity_T']) * 1000.0
    net_diff_1m_b = (latest['Net_Liquidity_T'] - prev_1m['Net_Liquidity_T']) * 1000.0
    net_diff_ytd_b = (latest['Net_Liquidity_T'] - ytd_base['Net_Liquidity_T']) * 1000.0

    walcl_curr_t = latest['WALCL_T']
    tga_curr_b = latest['WTREGEN_B']
    tga_curr_t = latest['WTREGEN_T']
    rrp_curr_b = latest['RRP_B']

    latest_date_str = latest.name.strftime('%Y-%m-%d') if hasattr(latest.name, 'strftime') else str(latest.name)[:10]

    # ==========================================================================
    # 1. 상단 핵심 메트릭 카드 (4열 배치)
    # ==========================================================================
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(
            label="현재 연준 순유동성",
            value=f"${net_curr_t:.3f} T",
            delta=f"{net_diff_1w_b:+.1f} B (1주)",
            help="연준 총자산(WALCL) - 재무부 TGA - 역레포(ON RRP)"
        )
        st.caption(f"1개월 변동: `{net_diff_1m_b:+.1f} B` | 연초비: `{net_diff_ytd_b:+.1f} B`")

    with m2:
        st.metric(
            label="연준 총자산 (WALCL)",
            value=f"${walcl_curr_t:.3f} T",
            help="연준의 전체 대차대조표 자산 규모"
        )
        st.caption(f"기준일: {latest_date_str}")

    with m3:
        st.metric(
            label="재무부 일반계좌 (TGA)",
            value=f"${tga_curr_b:.1f} B",
            help="미국 재무부의 연준 현금 계좌 (증가 시 시중 유동성 흡수)"
        )
        st.caption(f"약 ${tga_curr_t:.2f} T")

    with m4:
        st.metric(
            label="역레포 잔고 (ON RRP)",
            value=f"${rrp_curr_b:.1f} B",
            help="시중 유동성이 연준에 예치된 잔고 (감소 시 시중 유동성 방출)"
        )
        st.caption(f"약 ${rrp_curr_b/1000.0:.2f} T")

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # ==========================================================================
    # 2. 탭별 상세 시각화 & 상관관계 분석
    # ==========================================================================
    tab1, tab2, tab3 = st.tabs([
        "📈 순유동성 vs 증시 지수 오버레이",
        "📊 순유동성 3대 구성요소 추이",
        "📖 연준 순유동성 작동 원리 & 가이드"
    ])

    # --------------------------------------------------------------------------
    # TAB 1: 증시 지수 오버레이
    # --------------------------------------------------------------------------
    with tab1:
        st.markdown("#### ⚙️ 오버레이 차트 조건 설정")
        c1, c2 = st.columns([1.5, 1.5])
        with c1:
            INDEX_OPTIONS = {
                "S&P 500 (^GSPC)": "^GSPC",
                "나스닥 종합 (^IXIC)": "^IXIC",
                "비트코인 (BTC-USD)": "BTC-USD",
                "다우존스 산업평균 (^DJI)": "^DJI",
                "러셀 2000 (^RUT)": "^RUT",
                "KOSPI 200 (^KS200)": "^KS200"
            }
            selected_name = st.selectbox("비교할 주가지수 선택", options=list(INDEX_OPTIONS.keys()), index=0)
            selected_ticker = INDEX_OPTIONS[selected_name]

        with c2:
            PERIOD_MAP = {
                "1년": 365,
                "2년": 730,
                "3년": 1095,
                "5년": 1825,
                "10년": 3650
            }
            selected_period_label = st.selectbox("조회 기간 선택", options=list(PERIOD_MAP.keys()), index=1)
            days_back = PERIOD_MAP[selected_period_label]

        cutoff_date = pd.Timestamp.now() - pd.DateOffset(days=days_back)
        df_liq_sub = df_liq[df_liq.index >= cutoff_date].copy()
        
        start_str = cutoff_date.strftime('%Y-%m-%d')
        df_index = fetch_overlay_index_data(selected_ticker, start_str)

        if df_index is not None and not df_index.empty:
            merged_overlay = pd.merge(
                df_liq_sub[['Net_Liquidity_T']],
                df_index[['Close']],
                left_index=True,
                right_index=True,
                how='inner'
            ).dropna()

            if not merged_overlay.empty:
                corr_val = merged_overlay['Net_Liquidity_T'].corr(merged_overlay['Close'])
                corr_str = f"{corr_val:+.2f}" if not pd.isna(corr_val) else "N/A"
                
                st.info(f"💡 **선택 기간({selected_period_label}) 동안 연준 순유동성과 {selected_name} 간의 상관계수는 `{corr_str}` 입니다.** (1.0에 가까울수록 강한 양의 상관관계)")

                fig_overlay = make_subplots(specs=[[{"secondary_y": True}]])

                # 순유동성 (좌측 Y축)
                fig_overlay.add_trace(
                    go.Scatter(
                        x=merged_overlay.index,
                        y=merged_overlay['Net_Liquidity_T'],
                        name="연준 순유동성 ($T)",
                        line=dict(color="#00D2D3", width=2.5),
                        mode="lines"
                    ),
                    secondary_y=False
                )

                # 비교 지수 (우측 Y축)
                fig_overlay.add_trace(
                    go.Scatter(
                        x=merged_overlay.index,
                        y=merged_overlay['Close'],
                        name=selected_name,
                        line=dict(color="#FF9F43", width=2.0),
                        mode="lines"
                    ),
                    secondary_y=True
                )

                fig_overlay.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="#0D1117",
                    plot_bgcolor="#161B22",
                    height=520,
                    title=f"연준 순유동성 vs {selected_name} 동행 추이 ({selected_period_label})",
                    hovermode="x unified",
                    margin=dict(l=20, r=20, t=50, b=20),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
                )

                # [수정 지점]: titlefont 대신 최신 Plotly 호환 title=dict(...) 및 tickfont 적용
                fig_overlay.update_yaxes(
                    title=dict(text="순유동성 ($T)", font=dict(color="#00D2D3")),
                    secondary_y=False,
                    gridcolor="#21262D",
                    tickfont=dict(color="#00D2D3")
                )
                fig_overlay.update_yaxes(
                    title=dict(text=selected_name, font=dict(color="#FF9F43")),
                    secondary_y=True,
                    gridcolor="#21262D",
                    tickfont=dict(color="#FF9F43")
                )

                st.plotly_chart(fig_overlay, use_container_width=True)
            else:
                st.warning("비교 지수와 순유동성 시계열 일치 데이터가 부족합니다.")
        else:
            st.warning(f"{selected_name} 시세 데이터를 불러오지 못했습니다.")

    # --------------------------------------------------------------------------
    # TAB 2: 3대 구성요소 추이
    # --------------------------------------------------------------------------
    with tab2:
        st.markdown("#### 📊 순유동성 3대 구성요소 분해 추이")
        sub_period = st.selectbox("분해 차트 기간 선택", ["1년", "2년", "3년", "5년", "10년"], index=3, key="sub_period_sel")
        days_sub = PERIOD_MAP[sub_period]
        cutoff_sub = pd.Timestamp.now() - pd.DateOffset(days=days_sub)
        df_sub = df_liq[df_liq.index >= cutoff_sub]

        fig_comp = go.Figure()
        fig_comp.add_trace(go.Scatter(x=df_sub.index, y=df_sub['WALCL_T'], name="연준 총자산 (WALCL, $T)", line=dict(color="#3B82F6", width=2)))
        fig_comp.add_trace(go.Scatter(x=df_sub.index, y=df_sub['WTREGEN_T'], name="재무부 일반계정 (TGA, $T)", line=dict(color="#F59E0B", width=2)))
        fig_comp.add_trace(go.Scatter(x=df_sub.index, y=df_sub['RRP_B']/1000.0, name="역레포 잔고 (ON RRP, $T)", line=dict(color="#10B981", width=2)))
        fig_comp.add_trace(go.Scatter(x=df_sub.index, y=df_sub['Net_Liquidity_T'], name="연준 순유동성 ($T)", line=dict(color="#00D2D3", width=2.5, dash="solid")))

        fig_comp.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0D1117",
            plot_bgcolor="#161B22",
            height=480,
            title=f"연준 순유동성 3대 구성요소 과거 추이 ({sub_period})",
            xaxis_title="일자",
            yaxis_title="규모 ($T, 조 달러)",
            hovermode="x unified",
            margin=dict(l=20, r=20, t=50, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        st.plotly_chart(fig_comp, use_container_width=True)

    # --------------------------------------------------------------------------
    # TAB 3: 작동 원리 가이드
    # --------------------------------------------------------------------------
    with tab3:
        st.markdown("### 📖 연준 순유동성 (Fed Net Liquidity) 모델 가이드")
        st.markdown("""
        **연준 순유동성 공식**:
        $$\\text{Net Liquidity} = \\text{연준 총자산 (WALCL)} - \\text{재무부 일반계좌 (TGA)} - \\text{역레포 잔고 (ON RRP)}$$

        * **1. 연준 총자산 (WALCL)**: 연준의 대차대조표 자산 규모로 양적완화(QE) 시 증가하고 양적긴축(QT) 시 감소합니다.
        * **2. 재무부 일반계좌 (TGA)**: 미국 정부의 입출금 통장입니다. 국채 발행 및 세금 징수로 TGA가 증가하면 시중 유동성이 흡수(감소)되고, 정부 재정 지출로 TGA가 감소하면 시중에 유동성이 방출(증가)됩니다.
        * **3. 역레포 잔고 (ON RRP)**: 시중 MMF 및 금융기관이 단기 잉여 자금을 연준에 맡기는 창구입니다. 역레포 잔고가 감소하면 시중 자산시장(증시 및 가상자산)으로 유동성이 유입됩니다.
        """)
