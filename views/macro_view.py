"""
views/macro_view.py
거시경제 매크로 지표 대시보드 뷰
"""
from datetime import datetime
import pandas as pd
import plotly.graph_objects as go
import pytz
import streamlit as st
import yfinance as yf
from config import MACRO_CATEGORIES, RISK_MODEL_TABLE, SPREAD_TABLE_DATA
from services.macro_service import (
    clean_tag_ui,
    fetch_fred_cp_spread,
    fetch_fred_series,
    fetch_ticker_data,
    generate_briefing_text,
    get_collected_macro_data,
)

@st.cache_data(ttl=60)
def get_us_market_status() -> str:
    try:
        state = yf.Ticker("^GSPC").info.get("marketState", "CLOSED").upper()
        if state in ["REGULAR", "PRE", "POST"]:
            return "개장"
        return "마감"
    except:
        return "마감"


def inject_market_status(name: str) -> str:
    now = datetime.now(pytz.timezone('Asia/Seoul'))
    wd = now.weekday()
    hm = now.hour * 100 + now.minute
    is_weekend = wd >= 5 
    
    status = "마감"
    
    if "비트코인" in name or "이더리움" in name or "암호화폐" in name:
        status = "개장" 
    elif "코스피" in name or "코스닥" in name:
        if not is_weekend and 900 <= hm < 1530: status = "개장"
    elif "닛케이" in name or "일본" in name:
        if not is_weekend and 900 <= hm < 1500: status = "개장"
    elif "상하이" in name or "중국" in name:
        if not is_weekend and 1030 <= hm < 1600: status = "개장"
    elif "항셍" in name or "홍콩" in name:
        if not is_weekend and 1030 <= hm < 1700: status = "개장"
    elif any(k in name for k in ["S&P", "NASDAQ", "나스닥", "다우", "러셀"]):
        status = get_us_market_status()
    else:
        if wd == 5 and hm >= 600: status = "마감"
        elif wd == 6: status = "마감"
        elif wd == 0 and hm < 600: status = "마감"
        else: status = "개장"

    if "]]" in name:
        return name.replace("]]", f" / {status}]]")
    elif "]" in name:
        return name.replace("]", f" / {status}]")
    else:
        return f"{name} :gray[({status})]"


def render_macro_view(now_str_kst: str, refresh_interval: int):
    try:
        collected_data, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev = get_collected_macro_data()
    except Exception as e:
        st.error(f"데이터 수집 중 오류가 발생했습니다: {e}")
        return

    vix_hist = fetch_ticker_data("^VIX", period="1mo")
    move_hist = fetch_ticker_data("^MOVE", period="1mo")
    hy_df = fetch_fred_series("BAMLH0A0HYM2")
    stlfsi_df = fetch_fred_series("STLFSI4")
    cp_spread_df = fetch_fred_cp_spread()

    report_text = generate_briefing_text(
        collected_data, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev,
        vix_hist, move_hist, hy_df, cp_spread_df, stlfsi_df, now_str_kst
    )

    header_left, header_right = st.columns([2.7, 1.3])
    with header_left:
        st.title("📊 Global Macro Dashboard")
        st.caption(f"최근 데이터 갱신 시각: {now_str_kst} (KST) | 갱신 주기: {refresh_interval}초")

    # 우측 상단 브리핑 팝오버 버튼 2개
    with header_right:
        st.write("")
        with st.popover("📋 텍스트 브리핑 보기 / 복사", use_container_width=True):
            st.markdown("**현재 시세 텍스트 종합 브리핑**")
            st.caption("우측 상단 복사 아이콘(📋)을 눌러 즉시 복사하세요.")
            st.code(report_text, language="text")
            
        st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)
            
        with st.popover("🤖 AI로 텍스트 브리핑 보기 / 복사", use_container_width=True):
            st.markdown("**🤖 AI 기반 통합 데이터 브리핑**")
            st.caption(f"대시보드 전역의 최신 데이터를 취합하여 즉시 분석합니다.\n(기준 시각: {now_str_kst})")
            
            engine_options = [
                "자동 탐색 (Failover 무중단)", "NVIDIA NIM (Nemotron-3-Super)", 
                "Cloudflare (DeepSeek-R1 번역)", "NVIDIA NIM (GPT-OSS-20B)", "Cerebras Cloud (Llama-3.3)"
            ]
            macro_ai_engine = st.selectbox("AI 분석 엔진 선택", options=engine_options, index=0, key="popover_ai_engine")
            
            if st.button("🧠 AI 브리핑 생성", key="btn_macro_ai_popover", use_container_width=True):
                with st.spinner(f"[{macro_ai_engine}] 5대 핵심 지표 데이터를 수집 및 분석 중입니다..."):
                    try:
                        from views.ai_report_view import build_comprehensive_context
                        from services.ai_service import call_selected_ai_engine
                        from services.prompts import COMPREHENSIVE_REPORT_PROMPT
                        
                        context_data = build_comprehensive_context()
                        res = call_selected_ai_engine(macro_ai_engine, prompt=context_data, system_prompt=COMPREHENSIVE_REPORT_PROMPT)
                        
                        ai_text = f"**[📅 데이터 수집 및 분석 기준 시각: {now_str_kst}]**\n\n" + res.get("response", "데이터 처리에 실패했습니다.")
                        st.markdown(ai_text)
                        
                        st.divider()
                        st.markdown("**📋 전체 복사용 텍스트**")
                        st.code(ai_text, language="markdown")
                    except Exception as e:
                        st.error(f"AI 브리핑 생성 실패: {e}")

    st.divider()

    # 1. 메인 시세 요약 카드
    st.subheader("실시간/최근 시세 요약")
    st.info("💡 **변동 수치(+/-) 기준:** 각 지표 하단의 수치는 '직전 거래일 공식 종가(Previous Close) 대비 등락폭과 등락률(%)'입니다.", icon="ℹ️")

    for cat_name, items in collected_data.items():
        st.markdown(f"#### {cat_name}")
        cols = st.columns(len(items))
        for idx, item in enumerate(items):
            display_name = inject_market_status(item["name"])
            
            if item["status"] == "ok":
                cols[idx].metric(
                    label=display_name,
                    value=item["price_str"],
                    delta=item["delta_str"],
                    help=f"직전 거래일 종가: {item['prev_str']}"
                )
                cols[idx].caption(f"전일 종가: `{item['prev_str']}`")
            elif item["status"] == "single":
                cols[idx].metric(label=display_name, value=item["price_str"])
                cols[idx].caption("전일 데이터 없음")
            else:
                cols[idx].metric(label=display_name, value="로드 실패")

    st.divider()

    # 2. 10Y-2Y 장단기 금리차
    st.subheader("📊 10Y-2Y 장단기 금리차의 핵심 해석 모델")
    st.markdown("미국채 10년물(장기 금리)에서 2년물(단기 금리)을 뺀 값은 채권 시장에서 가장 주목하는 **경기 선행 지표**입니다.")
    st.code("스프레드(Spread) = 장기 금리(미래 경기 전망) - 단기 금리(현재 통화 정책)", language="text")

    if rate_10y_curr is not None and rate_2y_curr is not None:
        curr_spread = rate_10y_curr - rate_2y_curr
        prev_spread = rate_10y_prev - rate_2y_prev
        spread_delta = curr_spread - prev_spread
        
        if curr_spread < 0:
            status_title = "🚨 역전 (Inversion)"
            status_color = "red"
            status_desc = "현재 인플레이션을 잡기 위해 금리를 급격히 올렸으나, 미래 경기는 침체될 것으로 시장이 확신하고 있습니다. **(역사적으로 1~2년 내 경기 침체 Recession 도래)**"
        elif 0 <= curr_spread <= 0.2:
            status_title = "⚠️ 평탄화 (Flattening)"
            status_color = "orange"
            status_desc = "미래 경기 성장이 둔화될 것이라는 우려가 커지기 시작했습니다. **(경기 정점 통과 및 둔화 신호)**"
        else:
            status_title = "✅ 정상 (Normal)"
            status_color = "green"
            status_desc = "장기 미래의 불확실성(프리미엄)으로 인해 장기 금리가 더 높은 정상 상태입니다. **(경제의 점진적인 성장 및 확장)**"

        sc1, sc2 = st.columns([1, 2])
        with sc1:
            st.metric(
                label="현재 10Y - 2Y 스프레드 :gray[[15분 지연]]",
                value=f"{curr_spread:+.2f} %p",
                delta=f"{spread_delta:+.2f} %p (전일비)"
            )
            st.caption(f"10Y: `{rate_10y_curr:.2f}%` | 2Y: `{rate_2y_curr:.2f}%` (전일: `{prev_spread:+.2f}%p`)")
        with sc2:
            st.markdown(f"**현재 시장 진단:** :{status_color}[{status_title}]")
            st.write(status_desc)

    st.dataframe(pd.DataFrame(SPREAD_TABLE_DATA), use_container_width=True, hide_index=True)

    spread_period = st.selectbox("금리차 추이 기간 선택", ["6mo", "1y", "2y", "5y", "max"], index=2, key="spread_period_select")
    df_10y = fetch_ticker_data("^TNX", period=spread_period)
    df_2y = fetch_ticker_data("2YY=F", period=spread_period)

    if df_10y is not None and df_2y is not None and not df_10y.empty and not df_2y.empty:
        s_10y = df_10y['Close'].copy()
        s_2y = df_2y['Close'].copy()
        if s_10y.index.tz is not None:
            s_10y.index = s_10y.index.tz_localize(None)
        if s_2y.index.tz is not None:
            s_2y.index = s_2y.index.tz_localize(None)
        s_10y.index = s_10y.index.normalize()
        s_2y.index = s_2y.index.normalize()

        df_spread = pd.DataFrame({'10Y': s_10y, '2Y': s_2y}).ffill().dropna()
        df_spread['Spread'] = df_spread['10Y'] - df_spread['2Y']

        if not df_spread.empty:
            fig_spread = go.Figure()
            fig_spread.add_trace(go.Scatter(
                x=df_spread.index, y=df_spread['Spread'], mode='lines',
                name='10Y-2Y 스프레드 (%p)', line=dict(color='#E02424', width=2),
                fill='tozeroy', fillcolor='rgba(224, 36, 36, 0.15)'
            ))
            fig_spread.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.8, annotation_text="기준선 (0%p 역전 경계)")
            fig_spread.update_layout(
                title=f"미국채 10Y - 2Y 스프레드 과거 추이 ({spread_period})",
                xaxis_title="일자", yaxis_title="스프레드 (%p)", hovermode="x unified",
                margin=dict(l=20, r=20, t=40, b=20)
            )
            st.plotly_chart(fig_spread, use_container_width=True)

    st.divider()

    # 3. 신용 리스크, 은행권 및 시장 변동성
    st.subheader("⚡ 신용 리스크, 은행권 및 시장 변동성 (Credit & Liquidity Risk)")
    st.caption("주식·채권 가격 변동성, 기업 부도 위험(HY OAS), 글로벌 은행권 단기 자금경색(3M CP) 및 종합 금융스트레스(STLFSI4)를 모니터링합니다.")

    col_v, col_m, col_h = st.columns(3)
    with col_v:
        if vix_hist is not None and len(vix_hist) >= 2:
            v_curr = vix_hist['Close'].iloc[-1]
            v_prev = vix_hist['Close'].iloc[-2]
            v_delta = v_curr - v_prev
            v_pct = (v_delta / v_prev) * 100
            v_status, v_color = ("안도", "green") if v_curr < 15 else ("정상", "blue") if v_curr <= 20 else ("경계", "orange") if v_curr <= 30 else ("공포", "red")
            st.metric("CBOE VIX (주식 변동성) :gray[[15분 지연]]", f"{v_curr:.2f}", f"{v_delta:+.2f} ({v_pct:+.2f}%)")
            st.markdown(f"상태: :{v_color}[**{v_status}**] (전일: `{v_prev:.2f}`)")
        else:
            st.metric("CBOE VIX", "로드 실패")

    with col_m:
        if move_hist is not None and len(move_hist) >= 2:
            m_curr = move_hist['Close'].iloc[-1]
            m_prev = move_hist['Close'].iloc[-2]
            m_delta = m_curr - m_prev
            m_pct = (m_delta / m_prev) * 100 if m_prev != 0 else 0.0
            m_status, m_color = ("안정", "green") if m_curr < 80 else ("정상", "blue") if m_curr <= 120 else ("경계", "orange") if m_curr <= 140 else ("위기", "red")
            st.metric("ICE BofA MOVE (채권 변동성) :gray[[지연/마감]]", f"{m_curr:.2f}", f"{m_delta:+.2f} ({m_pct:+.2f}%)")
            st.markdown(f"상태: :{m_color}[**{m_status}**] (전일: `{m_prev:.2f}`)")
        else:
            st.metric("ICE BofA MOVE", "로드 실패")

    with col_h:
        if hy_df is not None and len(hy_df) >= 2:
            h_curr = hy_df['BAMLH0A0HYM2'].iloc[-1]
            h_prev = hy_df['BAMLH0A0HYM2'].iloc[-2]
            h_date = hy_df.index[-1].strftime('%m-%d')
            h_delta = h_curr - h_prev
            h_status, h_color = ("완화", "green") if h_curr < 3.5 else ("정상", "blue") if h_curr <= 5.0 else ("경계", "orange") if h_curr <= 7.0 else ("위기", "red")
            st.metric(f"하이일드 스프레드 (HY OAS) :gray[[1일 지연 {h_date} EOD]]", f"{h_curr:.2f} %p", f"{h_delta:+.2f} %p")
            st.markdown(f"상태: :{h_color}[**{h_status}**] (직전: `{h_prev:.2f}%p`)")
        else:
            st.metric("하이일드 스프레드", "로드 실패")

    col_cp, col_fsi = st.columns(2)
    with col_cp:
        if cp_spread_df is not None and len(cp_spread_df) >= 2:
            cp_curr = cp_spread_df['CP_SPREAD'].iloc[-1]
            cp_prev = cp_spread_df['CP_SPREAD'].iloc[-2]
            cp_date = cp_spread_df.index[-1].strftime('%m-%d')
            cp_delta = cp_curr - cp_prev
            cp_status, cp_color = ("안정", "green") if cp_curr < 0.20 else ("정상", "blue") if cp_curr <= 0.50 else ("경계", "orange") if cp_curr <= 0.80 else ("자금경색 / 위기", "red")
            st.metric(f"3M 금융 CP 스프레드 (은행권 자금위험) :gray[[1일 지연 {cp_date} EOD]]", f"{cp_curr:.2f} %p", f"{cp_delta:+.2f} %p")
            st.markdown(f"상태: :{cp_color}[**{cp_status}**] (직전: `{cp_prev:.2f}%p`)")
        else:
            st.metric("3M 금융 CP 스프레드", "로드 실패")

    with col_fsi:
        if stlfsi_df is not None and len(stlfsi_df) >= 2:
            fsi_curr = stlfsi_df['STLFSI4'].iloc[-1]
            fsi_prev = stlfsi_df['STLFSI4'].iloc[-2]
            fsi_date = stlfsi_df.index[-1].strftime('%m-%d')
            fsi_delta = fsi_curr - fsi_prev
            fsi_status, fsi_color = ("안정", "green") if fsi_curr < 0.0 else ("정상", "blue") if fsi_curr <= 0.5 else ("경계", "orange") if fsi_curr <= 1.0 else ("시스템 위기", "red")
            st.metric(f"세인트루이스 연준 금융스트레스 (STLFSI4) :gray[[주간 {fsi_date}]]", f"{fsi_curr:+.2f} pt", f"{fsi_delta:+.2f} pt")
            st.markdown(f"상태: :{fsi_color}[**{fsi_status}**] (직전: `{fsi_prev:+.2f} pt`)")
        else:
            st.metric("STLFSI4 금융스트레스지수", "로드 실패")

    st.markdown("#### 📖 신용, 은행권 및 변동성 핵심 해석 기준표")
    st.dataframe(pd.DataFrame(RISK_MODEL_TABLE), use_container_width=True, hide_index=True)

    st.markdown("#### 📈 위험 지표 상세 과거 추이")
    risk_tab1, risk_tab2, risk_tab3, risk_tab4 = st.tabs([
        "📊 VIX & MOVE 변동성 지수", "📉 하이일드 채권 스프레드", "🏦 3M 금융 CP 스프레드", "⚠️ STLFSI4 금융스트레스지수"
    ])

    with risk_tab1:
        vix_period = st.selectbox("변동성 지수 기간 선택", ["6mo", "1y", "2y", "5y", "max"], index=1, key="vix_period_sel")
        v_chart = fetch_ticker_data("^VIX", period=vix_period)
        m_chart = fetch_ticker_data("^MOVE", period=vix_period)
        if v_chart is not None and not v_chart.empty:
            fig_vol = go.Figure()
            fig_vol.add_trace(go.Scatter(x=v_chart.index, y=v_chart['Close'], mode='lines', name='VIX (주식 변동성)', line=dict(color='#FF5722', width=2)))
            if m_chart is not None and not m_chart.empty:
                fig_vol.add_trace(go.Scatter(x=m_chart.index, y=m_chart['Close'], mode='lines', name='MOVE (채권 변동성)', line=dict(color='#3F51B5', width=2), yaxis="y2"))
            fig_vol.update_layout(
                title=f"VIX 및 MOVE 지수 비교 추이 ({vix_period})", xaxis_title="일자",
                yaxis=dict(title=dict(text="VIX (pt)", font=dict(color="#FF5722")), tickfont=dict(color="#FF5722")),
                yaxis2=dict(title=dict(text="MOVE (pt)", font=dict(color="#3F51B5")), tickfont=dict(color="#3F51B5"), overlaying="y", side="right"),
                hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
            )
            st.plotly_chart(fig_vol, use_container_width=True)

    with risk_tab2:
        if hy_df is not None and not hy_df.empty:
            hy_period_years = st.selectbox("하이일드 기간 선택", [1, 2, 5, 10], index=2, format_func=lambda x: f"최근 {x}년", key="hy_period_sel")
            cutoff_date = pd.Timestamp.now() - pd.DateOffset(years=hy_period_years)
            filtered_hy = hy_df[hy_df.index >= cutoff_date]
            fig_hy = go.Figure()
            fig_hy.add_trace(go.Scatter(x=filtered_hy.index, y=filtered_hy['BAMLH0A0HYM2'], mode='lines', name='US High Yield OAS (%p)', line=dict(color='#D32F2F', width=2), fill='tozeroy', fillcolor='rgba(211, 47, 47, 0.1)'))
            fig_hy.add_hline(y=5.0, line_dash="dot", line_color="orange", annotation_text="경계선 (5.0%p)")
            fig_hy.add_hline(y=7.0, line_dash="dash", line_color="red", annotation_text="위기/침체선 (7.0%p)")
            fig_hy.update_layout(title=f"미국 하이일드 채권 스프레드 (HY OAS) 추이 (최근 {hy_period_years}년)", xaxis_title="일자", yaxis_title="스프레드 (%p)", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_hy, use_container_width=True)

    with risk_tab3:
        if cp_spread_df is not None and not cp_spread_df.empty:
            cp_period_years = st.selectbox("3M 금융 CP 기간 선택", [1, 2, 5, 10], index=2, format_func=lambda x: f"최근 {x}년", key="cp_period_sel")
            cutoff_date_cp = pd.Timestamp.now() - pd.DateOffset(years=cp_period_years)
            filtered_cp = cp_spread_df[cp_spread_df.index >= cutoff_date_cp]
            fig_cp = go.Figure()
            fig_cp.add_trace(go.Scatter(x=filtered_cp.index, y=filtered_cp['CP_SPREAD'], mode='lines', name='3M Financial CP Spread (%p)', line=dict(color='#0284C7', width=2), fill='tozeroy', fillcolor='rgba(2, 132, 199, 0.1)'))
            fig_cp.add_hline(y=0.50, line_dash="dot", line_color="orange", annotation_text="주의선 (0.50%p)")
            fig_cp.add_hline(y=0.80, line_dash="dash", line_color="red", annotation_text="위기 경계선 (0.80%p)")
            fig_cp.update_layout(title=f"3개월 금융 CP 스프레드 추이 (현대판 TED 스프레드, 최근 {cp_period_years}년)", xaxis_title="일자", yaxis_title="스프레드 (%p)", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_cp, use_container_width=True)

    with risk_tab4:
        if stlfsi_df is not None and not stlfsi_df.empty:
            fsi_period_years = st.selectbox("STLFSI4 기간 선택", [1, 2, 5, 10], index=2, format_func=lambda x: f"최근 {x}년", key="fsi_period_sel")
            cutoff_date_fsi = pd.Timestamp.now() - pd.DateOffset(years=fsi_period_years)
            filtered_fsi = stlfsi_df[stlfsi_df.index >= cutoff_date_fsi]
            fig_fsi = go.Figure()
            fig_fsi.add_trace(go.Scatter(x=filtered_fsi.index, y=filtered_fsi['STLFSI4'], mode='lines', name='St. Louis Fed Financial Stress Index', line=dict(color='#8B5CF6', width=2), fill='tozeroy', fillcolor='rgba(139, 92, 246, 0.1)'))
            fig_fsi.add_hline(y=0.0, line_dash="dash", line_color="white", opacity=0.8, annotation_text="평균 기준선 (0.0 pt)")
            fig_fsi.add_hline(y=1.0, line_dash="dash", line_color="red", annotation_text="시스템 위기 경보선 (+1.0 pt)")
            fig_fsi.update_layout(title=f"세인트루이스 연준 금융스트레스지수 (STLFSI4) 추이 (최근 {fsi_period_years}년)", xaxis_title="일자", yaxis_title="스트레스 지수 (pt)", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_fsi, use_container_width=True)

    st.divider()

    # 4. 개별 지표 상세 차트
    st.subheader("지표별 기간별 단독 차트")
    ALL_TICKERS = {}
    for cat in MACRO_CATEGORIES.values():
        ALL_TICKERS.update(cat)

    c1, c2 = st.columns([2, 1])
    with c1:
        selected_name = st.selectbox("조회할 단일 지표 선택", list(ALL_TICKERS.keys()), format_func=clean_tag_ui)
    with c2:
        period = st.selectbox("조회 기간", ["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"], index=3, key="single_period")

    selected_symbol = ALL_TICKERS[selected_name]
    df = fetch_ticker_data(selected_symbol, period=period)
    if df is not None and not df.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df.index, y=df['Close'], mode='lines', name=clean_tag_ui(selected_name), line=dict(color='#0066FF', width=2)))
        fig.update_layout(title=f"{clean_tag_ui(selected_name)} ({selected_symbol}) 상세 차트", xaxis_title="일자", yaxis_title="수치/가격", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # 5. 다중 지표 오버레이 비교 차트
    st.subheader("🔀 다중 지표 오버레이 비교 차트")
    st.caption("서로 다른 지표들을 한 차트 위에 겹쳐서 추세 및 상관관계를 비교합니다.")

    col_comp1, col_comp2, col_comp3 = st.columns([2, 1, 1])
    with col_comp1:
        multi_selected = st.multiselect("비교할 지표 선택 (다중 선택 가능)", options=list(ALL_TICKERS.keys()), default=["원/달러 (USD/KRW) :gray[[실시간]]", "달러 인덱스 (DXY) :gray[[실시간]]"], format_func=clean_tag_ui)
    with col_comp2:
        multi_period = st.selectbox("비교 기간", options=["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"], index=3, key="multi_period")
    with col_comp3:
        norm_mode = st.radio("비교 방식", options=["수익률/변동률(%) 기준", "실제 수치(절대값) 기준"], index=0)

    if multi_selected:
        fig_multi = go.Figure()
        for name in multi_selected:
            sym = ALL_TICKERS[name]
            m_df = fetch_ticker_data(sym, period=multi_period)
            if m_df is not None and not m_df.empty:
                y_data = m_df['Close']
                if "JPY/KRW" in name and y_data.iloc[-1] < 50:
                    y_data = y_data * 100
                if norm_mode == "수익률/변동률(%) 기준":
                    base_val = y_data.iloc[0]
                    y_data = ((y_data - base_val) / base_val) * 100 if base_val != 0 else y_data
                    y_title = "기준일 대비 누적 변동률 (%)"
                else:
                    y_title = "실제 수치 / 가격"

                fig_multi.add_trace(go.Scatter(x=m_df.index, y=y_data, mode='lines', name=clean_tag_ui(name), line=dict(width=2)))

        fig_multi.update_layout(title=f"다중 지표 비교 추이 ({multi_period} 기준)", xaxis_title="일자", yaxis_title=y_title, hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        if norm_mode == "수익률/변동률(%) 기준":
            fig_multi.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.7)
        st.plotly_chart(fig_multi, use_container_width=True)
