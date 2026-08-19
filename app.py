# app.py
import streamlit as st
import streamlit.components.v1 as components
import urllib3
from datetime import datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

from config import LIVE_CLOCK_HTML, APP_PASSWORD
from views.macro_view import render_macro_view
from views.liquidity_view import render_liquidity_view
from views.sector_view import render_sector_view
from views.sec_view import render_sec_view
from views.consensus_view import render_consensus_view
from views.cot_view import render_cot_view
from views.krx_cot_view import render_krx_cot_view
from views.radar_view import render_radar_view
from views.ai_test_view import render_ai_test_view
from views.ai_report_view import render_ai_report_view

# SSL 경고 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 페이지 기본 설정
st.set_page_config(page_title="Global Macro & 13F Dashboard", layout="wide", initial_sidebar_state="expanded")

# ==========================================
# 0. 커스텀 CSS 주입 (모던 다크 핀테크 UI & 사이드바 메뉴 디자인)
# ==========================================
st.markdown("""
<style>
    /* 전체 배경 및 폰트 */
    .stApp {
        background-color: #0D1117;
        color: #C9D1D9;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    
    /* 사이드바 배경 */
    section[data-testid="stSidebar"] {
        background-color: #161B22;
        border-right: 1px solid #30363D;
    }
    
    /* 사이드바 라디오 메뉴 디자인 모던화 (카드형 UI) */
    section[data-testid="stSidebar"] div.stRadio > div[role="radiogroup"] > label {
        padding: 10px 14px !important;
        margin-bottom: 6px !important;
        border-radius: 8px !important;
        background-color: rgba(255, 255, 255, 0.03) !important;
        border: 1px solid rgba(255, 255, 255, 0.06) !important;
        transition: all 0.2s ease-in-out !important;
        cursor: pointer;
    }
    
    /* 마우스 호버 시 입체감 애니메이션 */
    section[data-testid="stSidebar"] div.stRadio > div[role="radiogroup"] > label:hover {
        background-color: rgba(88, 166, 255, 0.1) !important;
        border-color: rgba(88, 166, 255, 0.3) !important;
        transform: translateX(4px);
    }

    /* 메트릭 카드 컨테이너 */
    div[data-testid="stMetric"] {
        background-color: #161B22;
        border: 1px solid #30363D;
        border-radius: 8px;
        padding: 14px 18px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.12);
    }
    
    div[data-testid="stMetricLabel"] {
        color: #8B949E;
        font-size: 0.85rem;
        font-weight: 500;
    }
    
    div[data-testid="stMetricValue"] {
        color: #F0F6FC;
        font-size: 1.4rem;
        font-weight: 700;
    }

    /* 버튼 스타일 */
    .stButton>button {
        background-color: #21262D;
        color: #C9D1D9;
        border: 1px solid #30363D;
        border-radius: 6px;
        font-weight: 600;
        transition: all 0.2s ease;
    }
    
    .stButton>button:hover {
        background-color: #30363D;
        border-color: #8B949E;
        color: #F0F6FC;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 1. 간이 인증 (비밀번호 잠금) 시스템
# ==========================================
def check_password():
    correct_password = st.secrets.get("auth", {}).get("password", APP_PASSWORD)

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        col1, col2, col3 = st.columns([1, 1.5, 1])
        with col2:
            st.markdown("<div style='height: 60px;'></div>", unsafe_allow_html=True)
            st.markdown("### 🔒 Global Macro & 13F Dashboard")
            st.caption("인가된 사용자만 접근할 수 있는 시스템입니다.")
            pwd_input = st.text_input("접속 비밀번호를 입력하세요", type="password", placeholder="Enter Password...")
            
            if st.button("로그인", type="primary", use_container_width=True):
                if pwd_input == correct_password:
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("비밀번호가 올바르지 않습니다.")
        return False
    return True

if not check_password():
    st.stop()

# ==========================================
# 2. 사이드바 네비게이션 & 갱신 설정
# ==========================================
st.sidebar.markdown("## 대시보드 메뉴")
st.sidebar.caption("글로벌 매크로 및 시장 수급 정밀 분석 시스템")

menu_selection = st.sidebar.radio(
    "이동할 메뉴를 선택하세요",
    [
        "📊 거시경제 매크로 지표", 
        "🏢 연준 순유동성 트래커",
        "🔄 섹터 & 자산군 로테이션",
        "📑 기관 13F 포트폴리오 분석", 
        "🎯 기관 13F Money 교집합",
        "🏛️ 글로벌 투기세력 (COT)",
        "🇰🇷 국내 파생 & 투기세력 (KRX)",
        "📡 외국인/기관 수급 레이더 (코스피)",
        "🤖 AI 종합 데이터 분석 & 결론 리포트",
        "🤖 AI API 연결 테스트"
    ],
    index=0,
    label_visibility="collapsed"
)

st.sidebar.divider()
st.sidebar.markdown("#### 🔄 데이터 갱신 설정")

if st.sidebar.button("데이터 수동 새로고침 🚀", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

auto_refresh_enabled = st.sidebar.checkbox("실시간 자동 새로고침 활성화", value=False)
refresh_interval = st.sidebar.selectbox(
    "새로고침 주기",
    options=[10, 30, 60, 120, 300],
    index=2,
    format_func=lambda x: f"{x}초 간격"
)

if auto_refresh_enabled:
    st_autorefresh(interval=refresh_interval * 1000, key="datarefresh")

st.sidebar.divider()
if st.sidebar.button("로그아웃", use_container_width=True):
    st.session_state.authenticated = False
    st.rerun()

st.sidebar.caption("© 2026 Macro Web Dashboard v2.3")

# ==========================================
# 3. 공통 헤더 시계 및 메뉴 렌더링
# ==========================================
kst_tz = ZoneInfo("Asia/Seoul")
now_kst = datetime.now(kst_tz)
now_str_kst = now_kst.strftime('%Y-%m-%d %H:%M:%S')

components.html(LIVE_CLOCK_HTML, height=45)

if menu_selection == "📊 거시경제 매크로 지표":
    render_macro_view(now_str_kst, refresh_interval)
elif menu_selection == "🏢 연준 순유동성 트래커":
    render_liquidity_view()
elif menu_selection == "🔄 섹터 & 자산군 로테이션":
    render_sector_view()
elif menu_selection == "📑 기관 13F 포트폴리오 분석":
    render_sec_view()
elif menu_selection == "🎯 기관 13F Money 교집합":
    render_consensus_view()
elif menu_selection == "🏛️ 글로벌 투기세력 (COT)":  
    render_cot_view()
elif menu_selection == "🇰🇷 국내 파생 & 투기세력 (KRX)":
    render_krx_cot_view()
elif menu_selection == "📡 외국인/기관 수급 레이더 (코스피)":
    render_radar_view()
elif menu_selection == "🤖 AI 종합 데이터 분석 & 결론 리포트":
    render_ai_report_view()
elif menu_selection == "🤖 AI API 연결 테스트":
    render_ai_test_view()
