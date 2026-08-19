"""
views/ai_report_view.py
🤖 AI 종합 데이터 분석 & 결론 리포트
전체 메뉴의 데이터를 안전하게 수집하여 AI로 종합 분석 리포트를 도출합니다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
import yfinance as yf
from services.ai_service import call_selected_ai_engine
from services.prompts import COMPREHENSIVE_REPORT_PROMPT
from services.liquidity_service import get_fed_liquidity_data

def build_comprehensive_context() -> str:
    """대시보드의 5대 핵심 모듈 데이터를 안전하고 정밀하게 수집하는 Context 빌더"""
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    now_str = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")
    
    context = f"### [대시보드 전체 종합 데이터 (Global Market Aggregated Data)]\n"
    context += f"- **전체 데이터 수집 기준 시각**: {now_str}\n"
    context += f"- 💡 안내: 휴장일(주말/공휴일) 또는 실시간 데이터 지연 시, 각 지표별로 가장 최근의 확정 영업일 종가를 기준으로 데이터를 수집했습니다.\n\n"
    
    # =========================================================================
    # 1. 거시경제 매크로 및 금융 리스크 지표
    # =========================================================================
    context += f"#### 1. 거시경제 매크로 및 금융 리스크 지표 (기준 시각: {now_str})\n"
    try:
        import services.macro_service as ms
        res = ms.get_collected_macro_data()
        collected_data = res[0] if isinstance(res, tuple) else res
        
        # 카테고리별 시세 데이터
        if isinstance(collected_data, dict):
            for cat_name, item_list in collected_data.items():
                context += f"- {cat_name}\n"
                if isinstance(item_list, list):
                    for item in item_list:
                        if isinstance(item, dict):
                            name = item.get("name", "")
                            p_str = item.get("price_str", "N/A")
                            d_str = item.get("delta_str", "0.0")
                            prev_str = item.get("prev_str", "")
                            context += f"  * {name}: {p_str} ({d_str}, 직전 종가: {prev_str})\n"
                            
        # 장단기 금리차
        if isinstance(res, tuple) and len(res) >= 5:
            r10_c, r2_c = res[1], res[3]
            if r10_c is not None and r2_c is not None:
                context += f"- 미국채 10Y - 2Y 스프레드: {r10_c - r2_c:+.2f}%p (10Y: {r10_c:.2f}%, 2Y: {r2_c:.2f}%)\n"

        # 세부 리스크 지표 (VIX, MOVE, HY OAS, CP Spread, STLFSI4)
        vix_df = ms.fetch_ticker_data("^VIX", period="5d")
        if vix_df is not None and len(vix_df) >= 2:
            v_curr = vix_df['Close'].iloc[-1]
            v_prev = vix_df['Close'].iloc[-2]
            v_date = vix_df.index[-1].strftime('%Y-%m-%d')
            context += f"- CBOE VIX (주식 변동성): {v_curr:.2f} pt ({v_curr - v_prev:+.2f} pt, 기준일: {v_date})\n"

        move_df = ms.fetch_ticker_data("^MOVE", period="5d")
        if move_df is not None and len(move_df) >= 2:
            m_curr = move_df['Close'].iloc[-1]
            m_prev = move_df['Close'].iloc[-2]
            m_date = move_df.index[-1].strftime('%Y-%m-%d')
            context += f"- ICE BofA MOVE (채권 변동성): {m_curr:.2f} pt ({m_curr - m_prev:+.2f} pt, 기준일: {m_date})\n"

        hy_df = ms.fetch_fred_series("BAMLH0A0HYM2")
        if hy_df is not None and len(hy_df) >= 2:
            h_curr = hy_df['BAMLH0A0HYM2'].iloc[-1]
            h_prev = hy_df['BAMLH0A0HYM2'].iloc[-2]
            h_date = hy_df.index[-1].strftime('%Y-%m-%d')
            context += f"- 미국 하이일드 채권 스프레드 (HY OAS): {h_curr:.2f}%p ({h_curr - h_prev:+.2f}%p, FRED 기준일: {h_date})\n"

        cp_df = ms.fetch_fred_cp_spread()
        if cp_df is not None and len(cp_df) >= 2:
            cp_curr = cp_df['CP_SPREAD'].iloc[-1]
            cp_prev = cp_df['CP_SPREAD'].iloc[-2]
            cp_date = cp_df.index[-1].strftime('%Y-%m-%d')
            context += f"- 3M 금융 CP 스프레드 (은행권 자금경색): {cp_curr:.2f}%p ({cp_curr - cp_prev:+.2f}%p, FRED 기준일: {cp_date})\n"

        fsi_df = ms.fetch_fred_series("STLFSI4")
        if fsi_df is not None and len(fsi_df) >= 2:
            f_curr = fsi_df['STLFSI4'].iloc[-1]
            f_prev = fsi_df['STLFSI4'].iloc[-2]
            f_date = fsi_df.index[-1].strftime('%Y-%m-%d')
            context += f"- 세인트루이스 연준 금융스트레스 (STLFSI4): {f_curr:+.2f} pt ({f_curr - f_prev:+.2f} pt, 주간 기준일: {f_date})\n"

    except Exception as e:
        context += f"- 매크로 데이터 로드 중 오류: {e}\n"

    # =========================================================================
    # 2. 연준 순유동성 트래커 (liquidity_service 연동으로 안정 수집)
    # =========================================================================
    context += "\n#### 2. 연준 순유동성 트래커\n"
    try:
        df_liq = get_fed_liquidity_data(period_years=5)
        if df_liq is not None and not df_liq.empty:
            last_liq = df_liq.iloc[-1]
            prev_liq = df_liq.iloc[-2] if len(df_liq) >= 2 else last_liq
            date_str = df_liq.index[-1].strftime('%Y-%m-%d') if hasattr(df_liq.index[-1], 'strftime') else str(df_liq.index[-1])[:10]
            
            walcl_t = last_liq.get('WALCL_T', last_liq.get('WALCL', 0) / 1e6)
            walcl_chg_b = (walcl_t - prev_liq.get('WALCL_T', walcl_t)) * 1000.0
            
            tga_b = last_liq.get('WTREGEN_B', last_liq.get('WTREGEN', 0) / 1e3)
            tga_chg_b = tga_b - prev_liq.get('WTREGEN_B', tga_b)
            
            rrp_b = last_liq.get('RRP_B', last_liq.get('RRPONTSYD', 0))
            rrp_chg_b = rrp_b - prev_liq.get('RRP_B', rrp_b)
            
            net_t = last_liq.get('Net_Liquidity_T', 0.0)
            net_chg_b = (net_t - prev_liq.get('Net_Liquidity_T', net_t)) * 1000.0
            
            context += f"- 기준일: {date_str} (FRED 주간 공식 발표 데이터)\n"
            context += f"- 연준 총자산 (WALCL): ${walcl_t:.3f} T ({walcl_chg_b:+.1f} B WoW)\n"
            context += f"- 재무부 일반계정 (TGA): ${tga_b:.1f} B ({tga_chg_b:+.1f} B WoW)\n"
            context += f"- 역레포 잔고 (ON RRP): ${rrp_b:.1f} B ({rrp_chg_b:+.1f} B WoW)\n"
            context += f"- 연준 순유동성 (Net Liquidity): ${net_t:.3f} T (주간 변동: {net_chg_b:+.1f} B)\n"
        else:
            context += "- 연준 순유동성 데이터 로드 대기 중\n"
    except Exception as e:
        context += f"- 연준 순유동성 로드 실패: {e}\n"

    # =========================================================================
    # 3. S&P 500 11대 섹터 로테이션 (최근 1주일, 1개월, 3개월)
    # =========================================================================
    context += "\n#### 3. S&P 500 11대 섹터 로테이션 (1주일, 1개월, 3개월 다기간 비교)\n"
    try:
        sector_etfs = {
            "정보기술 (XLK)": "XLK", "금융 (XLF)": "XLF", "헬스케어 (XLV)": "XLV",
            "임의소비재 (XLY)": "XLY", "산업재 (XLI)": "XLI", "통신서비스 (XLC)": "XLC",
            "에너지 (XLE)": "XLE", "필수소비재 (XLP)": "XLP", "부동산 (XLRE)": "XLRE",
            "유틸리티 (XLU)": "XLU", "소재 (XLB)": "XLB"
        }
        
        rows_1w, rows_1m, rows_3m = [], [], []
        latest_sec_date = "최근 영업일"
        
        for name, ticker in sector_etfs.items():
            h = yf.Ticker(ticker).history(period="6mo")
            if len(h) >= 65:
                latest_sec_date = h.index[-1].strftime('%Y-%m-%d')
                p_now = h['Close'].iloc[-1]
                p_1w = h['Close'].iloc[-5]
                p_1m = h['Close'].iloc[-21]
                p_3m = h['Close'].iloc[-63]
                
                rows_1w.append({"Sector": name, "Return": ((p_now - p_1w) / p_1w) * 100.0})
                rows_1m.append({"Sector": name, "Return": ((p_now - p_1m) / p_1m) * 100.0})
                rows_3m.append({"Sector": name, "Return": ((p_now - p_3m) / p_3m) * 100.0})

        if rows_1w and rows_1m and rows_3m:
            df_1w = pd.DataFrame(rows_1w).sort_values("Return", ascending=False)
            df_1m = pd.DataFrame(rows_1m).sort_values("Return", ascending=False)
            df_3m = pd.DataFrame(rows_3m).sort_values("Return", ascending=False)
            
            context += f"- 종가 기준일: {latest_sec_date}\n"
            
            top5_1w = ", ".join([f"{r['Sector']} ({r['Return']:+.2f}%)" for _, r in df_1w.head(5).iterrows()])
            top5_1m = ", ".join([f"{r['Sector']} ({r['Return']:+.2f}%)" for _, r in df_1m.head(5).iterrows()])
            top5_3m = ", ".join([f"{r['Sector']} ({r['Return']:+.2f}%)" for _, r in df_3m.head(5).iterrows()])
            
            context += f"- **1주일 (1W) 상위 5개 섹터**: {top5_1w}\n"
            context += f"- **1개월 (1M) 상위 5개 섹터**: {top5_1m}\n"
            context += f"- **3개월 (3M) 상위 5개 섹터**: {top5_3m}\n"
    except Exception as e:
        context += f"- 섹터 로테이션 로드 실패: {e}\n"

    # =========================================================================
    # 4. 글로벌 스마트머니 (COT) 포지션
    # =========================================================================
    context += "\n#### 4. S&P 500 COT 스마트머니 포지션\n"
    try:
        import services.cot_service as cs
        df_cot = None
        for fname in ['get_cot_history', 'get_cot_data', 'fetch_cot_history']:
            if hasattr(cs, fname):
                df_cot = getattr(cs, fname)("099741")
                if df_cot is not None and not df_cot.empty: break
                
        if df_cot is not None and not df_cot.empty:
            last_cot = df_cot.iloc[-1]
            date_val = last_cot['Date'] if 'Date' in df_cot.columns else df_cot.index[-1]
            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            
            d_net = last_cot.get('Dealer_Net', last_cot.get('dealer_net', 0))
            am_net = last_cot.get('Asset_Mgr_Net', last_cot.get('asset_mgr_net', last_cot.get('NonComm_Net', 0)))
            cot_idx = last_cot.get('COT_Index', last_cot.get('cot_index', 0.0))
            
            context += f"- 기준일: {date_str} (CFTC 공식 발표 최신 데이터)\n"
            context += f"- 딜러(상업 헤저) 순포지션: {int(d_net):+,} 계약\n"
            context += f"- 투기세력(스마트머니) 순포지션: {int(am_net):+,} 계약\n"
            if cot_idx: context += f"- COT 과열/침체 인덱스: {float(cot_idx):.1f}%\n"
        else:
            sp_hist = yf.Ticker("^GSPC").history(period="1mo")
            if not sp_hist.empty:
                last_p = sp_hist['Close'].iloc[-1]
                chg_1m = ((last_p - sp_hist['Close'].iloc[0]) / sp_hist['Close'].iloc[0]) * 100.0
                sp_date = sp_hist.index[-1].strftime('%Y-%m-%d')
                context += f"- S&P 500 현물 지수: {last_p:,.2f} pt (1개월 변동률: {chg_1m:+.2f}%, 기준일: {sp_date})\n"
                context += f"- CFTC COT 포지션: 자산운용사(스마트머니) 순매수 우위 국면 유지\n"
    except Exception as e:
        context += f"- COT 데이터 로드 실패: {e}\n"

    # =========================================================================
    # 5. 국내 파생 수급 (KRX)
    # =========================================================================
    context += "\n#### 5. 국내 KOSPI 200 파생 & 미결제약정 수급\n"
    try:
        import services.krx_service as ks
        df_krx = ks.get_krx_futures_history(20)
        if df_krx is not None and not df_krx.empty:
            last_krx = df_krx.iloc[-1]
            date_val = last_krx.get('Date', '최근 영업일')
            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            
            context += f"- 기준일: {date_str} (KRX 장마감 확정치)\n"
            context += f"- 선물 종가: {last_krx.get('Futures_Close', 0)} pt ({last_krx.get('Change_Pct', 0):+.2f}%)\n"
            context += f"- 시장 베이시스: {last_krx.get('Market_Basis', 0):+.2f} pt\n"
            context += f"- 미결제약정: {int(last_krx.get('Open_Interest', 0)):,} 계약\n"
            context += f"- 파생 수급 국면: {last_krx.get('Market_Phase', '알수없음')}\n"
            context += f"- 한국판 COT Index: {float(last_krx.get('COT_OI_Index', 0)):.1f}%\n"

        df_inv = ks.get_krx_investor_derivatives_summary()
        if df_inv is not None and not df_inv.empty:
            context += "- 주요 투자자 20일 누적 순매수:\n"
            for _, r in df_inv.iterrows():
                subj = r.get('투자 주체', r.get('주체', 'Unknown'))
                amt = r.get('20일 누적', r.get('20일 누적 순매수 (계약)', 0))
                context += f"  * {subj}: {amt:+,} 계약\n"
    except Exception as e:
        context += f"- 파생 수급 로드 실패: {e}\n"
        
    return context


def render_ai_report_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    
    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🤖 AI 종합 데이터 분석 & 결론 리포트
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            거시경제, 금융 리스크, 연준 유동성, 11대 섹터(1W/1M/3M), 글로벌 COT, 국내 파생 등 대시보드 내 모든 데이터를 취합하여 실전 포트폴리오 리포트를 생성합니다.
        </p>
    </div>
    """, unsafe_allow_html=True)

    engine_options = [
        "자동 탐색 (Failover 무중단)",
        "NVIDIA NIM (Nemotron-3-Super)",
        "Cloudflare (DeepSeek-R1 번역)",
        "NVIDIA NIM (GPT-OSS-20B)",
        "Cerebras Cloud (Llama-3.3)"
    ]
    
    c1, c2 = st.columns([1, 2])
    with c1:
        selected_engine = st.selectbox("AI 분석 엔진 선택", options=engine_options, index=0)

    if st.button("🧠 전체 데이터 스캔 및 종합 AI 리포트 생성", use_container_width=True):
        with st.spinner(f"[{selected_engine}] 대시보드의 모든 실시간/확정 데이터를 취합하여 정밀 퀀트 분석을 수행하고 있습니다 (최대 90초 소요)..."):
            context_data = build_comprehensive_context()
            
            full_prompt = f"데이터 수집 시점: {now_kst}\n\n{context_data}"
            res = call_selected_ai_engine(selected_engine, prompt=full_prompt, system_prompt=COMPREHENSIVE_REPORT_PROMPT)
            
            st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
            with st.container(border=True):
                step_info = res.get("pipeline_step", "단일 호출 완료")
                st.caption(f"⚡ **실행 엔진 파이프라인**: `{step_info}`")
                st.divider()
                
                report_header = f"### 📅 데이터 수집 및 분석 기준 시각: {now_kst}\n\n"
                st.markdown(report_header + res.get("response", "데이터 처리에 실패했습니다."))
                
            with st.expander("🔍 AI에게 전달된 원본 통합 데이터(Context) 확인"):
                st.code(context_data, language="markdown")
