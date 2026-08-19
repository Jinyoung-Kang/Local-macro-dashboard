# config.py
import os
import streamlit as st

# ==============================================================================
# 0. Secret & 환경 변수 로드 헬퍼 및 API 설정
# ==============================================================================
def get_secret(key_path: str, default: str = "") -> str:
    """Streamlit Secrets (중첩 섹션 및 단일 키 지원) 및 환경변수 안전 로드"""
    try:
        if hasattr(st, "secrets") and st.secrets:
            # 1. 'section.key' 형태 탐색
            keys = key_path.split(".")
            val = st.secrets
            found = True
            for k in keys:
                if hasattr(val, "get") and val.get(k) is not None:
                    val = val.get(k)
                elif hasattr(val, "__getitem__") and k in val:
                    val = val[k]
                else:
                    found = False
                    break
            if found and val is not None:
                return str(val).strip()

            # 2. 단일 키 탐색
            leaf = keys[-1]
            if hasattr(st.secrets, "get") and st.secrets.get(leaf) is not None:
                return str(st.secrets.get(leaf)).strip()
            if hasattr(st.secrets, "get") and st.secrets.get(leaf.upper()) is not None:
                return str(st.secrets.get(leaf.upper())).strip()
    except Exception:
        pass
    return os.environ.get(key_path, os.environ.get(key_path.replace(".", "_").upper(), default))


def get_krx_key() -> str:
    """
    Streamlit Secrets의 [krx] api_key 또는 auth_key / KRX_AUTH_KEY 등 다양한 포맷 자동 탐색
    """
    key = get_secret("krx.api_key", "")
    if not key:
        key = get_secret("krx.auth_key", "")
    if not key:
        key = get_secret("KRX_AUTH_KEY", "")
    if not key:
        key = get_secret("krx_api_key", "")
    return key

# 앱 비밀번호
APP_PASSWORD = get_secret("auth.password", get_secret("APP_PASSWORD", "admin1234@"))

# KRX Open API 설정
KRX_AUTH_KEY = get_krx_key()
KRX_BASE_URL = "http://data-dbg.krx.co.kr/svc/apis"

# ==============================================================================
# 1. 거시경제 매크로 지표 카테고리 매핑
# ==============================================================================
MACRO_CATEGORIES = {
    "💵 통화 및 환율 :gray[(실시간)]": {
        "달러 인덱스 (DXY) :gray[[실시간]]": "DX-Y.NYB",
        "원/달러 (USD/KRW) :gray[[실시간]]": "KRW=X",
        "달러/엔 (USD/JPY) :gray[[실시간]]": "JPY=X",
        "엔/원 100엔당 (JPY/KRW) :gray[[실시간]]": "JPYKRW=X"
    },
    "🏛️ 미국 국채 금리 :gray[(15분 지연)]": {
        "미국채 2년물 금리(%) :gray[[15분 지연]]": "2YY=F",
        "미국채 10년물 금리(%) :gray[[15분 지연]]": "^TNX",
        "미국채 30년물 금리(%) :gray[[15분 지연]]": "^TYX"
    },
    "🛢️ 원자재 :gray[(15분 지연)]": {
        "WTI 원유 ($) :gray[[15분 지연]]": "CL=F",
        "브렌트유 ($) :gray[[15분 지연]]": "BZ=F",
        "금 선물 ($) :gray[[15분 지연]]": "GC=F"
    },
    "🇺🇸 미국 주가지수 및 선물 :gray[(15분 지연)]": {
        "S&P 500 :gray[[15분 지연]]": "^GSPC",
        "S&P 500 선물 (ES) :gray[[15분 지연]]": "ES=F",
        "나스닥 100 :gray[[15분 지연]]": "^NDX",
        "나스닥 선물 (NQ) :gray[[15분 지연]]": "NQ=F"
    },
    "🌏 아시아 주요 주가지수 :gray[(15분 지연)]": {
        "코스피 (KOSPI) :gray[[15분 지연]]": "^KS11",
        "닛케이 225 (Nikkei) :gray[[15분 지연]]": "^N225",
        "상하이 종합 (SSE) :gray[[15분 지연]]": "000001.SS",
        "항셍 지수 (HSI) :gray[[15분 지연]]": "^HSI"
    }
}

# ==============================================================================
# 2. SEC 13F 주요 기관 매핑
# ==============================================================================
INSTITUTIONS = {
    "🇰🇷 국민연금 (National Pension Service)": {"cik": "0001608046", "desc": "글로벌 자산배분 및 미국 대형 우량주 중심 장기 투자 "},
    "🇳🇴 노르웨이 국부펀드 (Norges Bank / GPFG)": {"cik": "0001374170", "desc": "세계 최대 규모의 글로벌 국부펀드, 미 증시 우상향의 과실을 통째로 흡수하는 가장 정석적인 인덱스형 거인 "},
    "🇨🇦 캐나다 연금투자위원회 (CPPIB)": {"cik": "0001283718", "desc": "캐나다 연금을 운용하는 세계적인 대형 연기금, 글로벌 자산배분 중심 "},
    "🇳🇱 네덜란드 연금자산운용 (APG Asset Management)": {"cik": "0001434819", "desc": "네덜란드 최대 연기금 자산운용사, 안정적인 글로벌 분산투자 포트폴리오 "},    
    "🇸🇦 사우디 국부펀드 (Public Investment Fund - PIF)": {"cik": "0001767640", "desc": "대규모 글로벌 전략적 투자, 성장성이 보이면 돈을 쏟아붓는 공격적인 벤처캐피털 "},
    "🇺🇸 블랙록 (BlackRock)": {"cik": "0002012383", "desc": "세계 최대 자산운용사, 광범위한 글로벌 자산군 "},
    "🇺🇸 뱅가드 (Vanguard Group)": {"cik": "0000102909", "desc": "글로벌 인덱스 펀드의 거두, 시장 전체를 아우르는 포트폴리오 "},
    "🇺🇸 버크셔 해서웨이 (Berkshire Hathaway)": {"cik": "0001067983", "desc": "가치투자 포트폴리오, 핵심 우량주 집중 "},    
    "🇺🇸 듀케인 패밀리 오피스 (Duquesne Family Office)": {"cik": "0001536411", "desc": "조지 소로스의 오른팔, 30년간 연평균 30% 수익률을 낸 전설. 테크 트렌드를 포착 "},
    "🇺🇸 피셔 자산운용 (Fisher Asset Management)": {"cik": "0000850529", "desc": "켄 피셔의 글로벌 성장주 및 빅테크 중심 탑다운 롱온리 전략(군중 심리를 역이용) "},
    "🇺🇸 브리지워터 어소시에이츠 (Bridgewater)": {"cik": "0001350694", "desc": "레이 달리오 설립, 올웨더 및 글로벌 매크로 헤지펀드 "},
    "🇺🇸 사이언 자산운용 (Scion Asset Management)": {"cik": "0001649339", "desc": "마이클 버리의 역발상 딥밸류 및 숏(풋옵션)/롱 전략 "}
}

# ==============================================================================
# 3. 11대 S&P 500 섹터 ETF 매핑
# ==============================================================================
SECTOR_ETFS = {
    "XLK": {"name": "정보기술 (Technology)", "type": "공격 / 성장"},
    "XLC": {"name": "통신서비스 (Communication)", "type": "공격 / 성장"},
    "XLY": {"name": "임의소비재 (Consumer Discretionary)", "type": "경기민감 / 성장"},
    "XLI": {"name": "산업재 (Industrials)", "type": "경기민감 / 가치"},
    "XLF": {"name": "금융 (Financials)", "type": "경기민감 / 가치"},
    "XLB": {"name": "소재 (Materials)", "type": "경기민감 / 원자재"},
    "XLE": {"name": "에너지 (Energy)", "type": "경기민감 / 원자재"},
    "XLV": {"name": "헬스케어 (Health Care)", "type": "방어주"},
    "XLP": {"name": "필수소비재 (Consumer Staples)", "type": "방어주"},
    "XLU": {"name": "유틸리티 (Utilities)", "type": "방어주 / 배당"},
    "XLRE": {"name": "부동산 (Real Estate)", "type": "방어주 / 금리민감"}
}

# ==============================================================================
# 4. 글로벌 주요 자산군 ETF 매핑
# ==============================================================================
ASSET_CLASS_ETFS = {
    "SPY": {"name": "미국 대형주 (S&P 500)", "category": "주식"},
    "QQQ": {"name": "미국 기술주 (Nasdaq 100)", "category": "주식"},
    "IWM": {"name": "미국 중소형주 (Russell 2000)", "category": "주식"},
    "EEM": {"name": "신흥국 주식 (Emerging Markets)", "category": "주식"},
    "TLT": {"name": "미국 20년+ 장기국채", "category": "채권"},
    "IEF": {"name": "미국 7-10년 중기국채", "category": "채권"},
    "SHY": {"name": "미국 1-3년 단기국채", "category": "채권"},
    "GLD": {"name": "금 (Gold)", "category": "원자재"},
    "USO": {"name": "원유 (WTI Crude Oil)", "category": "원자재"},
    "DBA": {"name": "농산물 (Agriculture)", "category": "원자재"},
    "UUP": {"name": "미국 달러 인덱스 ETF", "category": "통화"}
}

# ==============================================================================
# 5. 장단기 금리차 및 리스크 모델 해석 테이블
# ==============================================================================
SPREAD_TABLE_DATA = {
    "시장 상태": ["정상 (Normal)", "평탄화 (Flattening)", "역전 (Inversion) ⚠️"],
    "스프레드 수치": ["양수 (+)", "0에 수렴", "음수 (-)"],
    "시장의 심리 및 해석": [
        "장기 미래의 불확실성(프리미엄)으로 인해 장기 금리가 더 높음.",
        "미래 경기 성장이 둔화될 것이라는 우려가 커지기 시작함.",
        "현재 인플레이션을 잡기 위해 금리를 급격히 올렸으나, 미래 경기는 침체될 것으로 확신함."
    ],
    "경제적 귀결": [
        "경제의 점진적인 성장 및 확장",
        "경기 정점 통과 및 둔화 신호",
        "역사적으로 1~2년 내 경기 침체(Recession) 도래"
    ]
}

RISK_MODEL_TABLE = {
    "지표명 (지연 수준)": [
        "CBOE VIX [15분 지연]", 
        "ICE BofA MOVE [지연/마감]", 
        "하이일드 스프레드 [1일 지연 EOD]",
        "3M 금융 CP 스프레드 [1일 지연 EOD]",
        "STLFSI4 금융스트레스지수 [주간]"
    ],
    "정상 / 안정 범위": [
        "15 ~ 20 (15 미만: 과도한 낙관)", 
        "80 ~ 120 (80 미만: 금리 초안정)", 
        "3.5% ~ 5.0% (3.5% 미만: 유동성 풍부)",
        "0.20%p ~ 0.50%p (0.20%p 미만: 풍부한 단기 유동성)",
        "0.0 이하 (0.0은 역사적 장기 평균치)"
    ],
    "위험 / 발작 임계치": [
        "30 이상 (패닉 / 급락 / 투매)", 
        "140 이상 (채권 발작 / 긴축 충격)", 
        "7.0% 이상 (본격 신용경색 / 경기침체)",
        "0.80%p ~ 1.00%p 이상 (단기 자금시장 유동성 경색)",
        "+1.0 이상 (금융 시스템 충격 및 위기 경보)"
    ],
    "지표의 성격 및 핵심 해석": [
        "주식 시장의 단기 공포 측정기. 급등 시 주가 급락 및 투매 발생 신호.",
        "채권 시장의 공포 지수. 연준 통화정책 불확실성과 유동성 경색에 민감하게 반응.",
        "한계 기업의 부도 리스크 프리미엄. 경기 침체 진입 시 가장 먼저 급등하는 신용 선행 지표.",
        "은행·금융기관의 3개월 단기 자금조달 가산금리(현대판 TED 스프레드). 은행권 유동성 위기 발생 시 급등.",
        "18개 금융시장 지표(자금, 채권, 주식 등)를 종합한 복합 척도. 0보다 크면 스트레스 고조, 1.0 초과 시 시스템 위기."
    ]
}

# ==============================================================================
# 6. 실시간 거래소 시계 & 휴장 판별 HTML
# ==============================================================================
LIVE_CLOCK_HTML = """
<!DOCTYPE html>
<html>
<head>
<style>
    body {
        margin: 0;
        padding: 0;
        background-color: transparent;
        color: #E2E8F0;
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif;
        font-size: 14px;
        display: flex;
        align-items: center;
        height: 100vh;
    }
    .clock-container {
        display: flex;
        gap: 24px;
        background: rgba(30, 41, 59, 0.6);
        padding: 8px 18px;
        border-radius: 8px;
        border: 1px solid rgba(255,255,255,0.08);
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    .market-badge {
        margin-left: 10px;
        padding: 3px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.5px;
        display: inline-block;
    }
    .status-trading { background: rgba(16, 185, 129, 0.15); color: #10B981; border: 1px solid rgba(16, 185, 129, 0.4); }
    .status-pre     { background: rgba(245, 158, 11, 0.15); color: #F59E0B; border: 1px solid rgba(245, 158, 11, 0.4); }
    .status-post    { background: rgba(139, 92, 246, 0.15); color: #8B5CF6; border: 1px solid rgba(139, 92, 246, 0.4); }
    .status-closed  { background: rgba(107, 114, 128, 0.15); color: #9CA3AF; border: 1px solid rgba(107, 114, 128, 0.4); }
</style>
</head>
<body>
    <div class="clock-container">
        <div id="kst-clock">🇰🇷 한국 (KOSPI) <span id="kst-time" style="margin-left:6px; font-variant-numeric: tabular-nums;"></span> <span id="kst-status" class="market-badge"></span></div>
        <div id="est-clock" style="margin-left: 10px;">🗽 뉴욕 (NASDAQ) <span id="est-time" style="margin-left:6px; font-variant-numeric: tabular-nums;"></span> <span id="est-status" class="market-badge"></span></div>
    </div>

    <script>
        let holidaysKR = new Set();
        let holidaysUS = new Set();
        let loadedYear = null;

        async function fetchHolidays(year) {
            if (loadedYear === year) return;
            try {
                const [resKR, resUS] = await Promise.all([
                    fetch(`https://date.nager.at/api/v3/PublicHolidays/${year}/KR`),
                    fetch(`https://date.nager.at/api/v3/PublicHolidays/${year}/US`)
                ]);
                
                if (resKR.ok) {
                    const dataKR = await resKR.json();
                    holidaysKR = new Set(dataKR.map(h => h.date));
                    holidaysKR.add(`${year}-05-01`);
                    holidaysKR.add(`${year}-12-31`);
                }
                
                if (resUS.ok) {
                    const dataUS = await resUS.json();
                    holidaysUS = new Set(dataUS.map(h => h.date));
                    const goodFriday = calculateGoodFriday(year);
                    if (goodFriday) holidaysUS.add(goodFriday);
                }
                loadedYear = year;
            } catch (err) {
                console.warn("Holiday API load fallback:", err);
            }
        }

        function calculateGoodFriday(year) {
            const a = year % 19;
            const b = Math.floor(year / 100);
            const c = year % 100;
            const d = Math.floor(b / 4);
            const e = b % 4;
            const f = Math.floor((b + 8) / 25);
            const g = Math.floor((b - f + 1) / 3);
            const h = (19 * a + b - d - g + 15) % 30;
            const i = Math.floor(c / 4);
            const k = c % 4;
            const l = (32 + 2 * e + 2 * i - h - k) % 7;
            const m = Math.floor((a + 11 * h + 22 * l) / 451);
            const month = Math.floor((h + l - 7 * m + 114) / 31);
            const day = ((h + l - 7 * m + 114) % 31) + 1;
            const easter = new Date(Date.UTC(year, month - 1, day));
            easter.setUTCDate(easter.getUTCDate() - 2);
            return easter.toISOString().split('T')[0];
        }

        function getMarketStatus(timeZone, type) {
            const now = new Date();
            const tzString = now.toLocaleString("en-US", { timeZone: timeZone });
            const tzDate = new Date(tzString);
            
            const year = tzDate.getFullYear();
            const month = String(tzDate.getMonth() + 1).padStart(2, '0');
            const date = String(tzDate.getDate()).padStart(2, '0');
            const yyyymmdd = `${year}-${month}-${date}`;
            
            const day = tzDate.getDay();
            const hour = tzDate.getHours();
            const minute = tzDate.getMinutes();
            const timeNum = hour * 100 + minute;

            if (day === 0 || day === 6) {
                return { text: "휴장 (주말)", className: "status-closed" };
            }

            if (type === 'KOSPI') {
                if (holidaysKR.has(yyyymmdd)) {
                    return { text: "휴장 (공휴일)", className: "status-closed" };
                }
                if (timeNum >= 900 && timeNum < 1530) return { text: "거래 중", className: "status-trading" };
                if (timeNum >= 830 && timeNum < 900)  return { text: "프리마켓", className: "status-pre" };
                if (timeNum >= 1530 && timeNum <= 1800) return { text: "애프터마켓", className: "status-post" };
                return { text: "장 마감", className: "status-closed" };
            } else {
                if (holidaysUS.has(yyyymmdd)) {
                    return { text: "휴장 (공휴일)", className: "status-closed" };
                }
                if (timeNum >= 930 && timeNum < 1600) return { text: "거래 중", className: "status-trading" };
                if (timeNum >= 400 && timeNum < 930)  return { text: "프리마켓", className: "status-pre" };
                if (timeNum >= 1600 && timeNum <= 2000) return { text: "애프터마켓", className: "status-post" };
                return { text: "장 마감", className: "status-closed" };
            }
        }

        function updateTime() {
            const now = new Date();
            const currentYear = now.getFullYear();
            if (loadedYear !== currentYear) {
                fetchHolidays(currentYear);
            }
            
            const kstOptions = { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };
            const estOptions = { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };
            
            document.getElementById('kst-time').innerHTML = `<b>${new Intl.DateTimeFormat('ko-KR', kstOptions).format(now)}</b>`;
            document.getElementById('est-time').innerHTML = `<b>${new Intl.DateTimeFormat('ko-KR', estOptions).format(now)}</b>`;

            const kstStatus = getMarketStatus('Asia/Seoul', 'KOSPI');
            const kstBadge = document.getElementById('kst-status');
            kstBadge.innerText = kstStatus.text;
            kstBadge.className = "market-badge " + kstStatus.className;

            const estStatus = getMarketStatus('America/New_York', 'NASDAQ');
            const estBadge = document.getElementById('est-status');
            estBadge.innerText = estStatus.text;
            estBadge.className = "market-badge " + estStatus.className;
        }

        setInterval(updateTime, 1000);
        updateTime();
    </script>
</body>
</html>
"""
