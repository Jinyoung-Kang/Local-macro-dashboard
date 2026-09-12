"""
services/sec_service.py
SEC EDGAR 13F-HR 공시 데이터 수집 및 기관 포트폴리오 분석 엔진
(강력한 Session 기반 통신 방어, 콤마 수치 정제 및 무적 ElementTree XML 파서 탑재)
"""
import logging
import threading
import time
import xml.etree.ElementTree as ET
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services import datasets, store
from bs4 import BeautifulSoup
import streamlit as st
from config import INSTITUTIONS

logger = logging.getLogger(__name__)


# ==============================================================================
# 1. SEC 전용 강행 돌파 통신 세션 설정 (Timeout, Rate Limit 완벽 방어)
# ==============================================================================
# ==============================================================================
# SEC 레이트 리미터 (초당 10건 제한 준수 + 병렬 수집 허용)
# ==============================================================================
# SEC EDGAR는 초당 10요청을 넘기면 차단합니다. 기존 코드는 요청 사이에
# time.sleep(0.2)를 넣어 이를 지켰는데, 이 방식은 호출을 **직렬화**해서
# 기관 12곳 × 8분기 = 약 200요청이 한 줄로 늘어섭니다. 대기 시간만 40초가
# 넘고, 실제 왕복 지연까지 더하면 수 분이 걸립니다.
#
# 토큰 버킷으로 바꾸면 "전체 합계가 초당 N건을 넘지 않는" 조건을 지키면서
# 여러 스레드가 동시에 요청할 수 있습니다. 한도는 10이 아니라 8로 둡니다
# (버스트·시계 오차 여유분).
_SEC_MAX_RPS = 8.0

_sec_rate_lock = threading.Lock()
_sec_next_slot = [0.0]


def _sec_rate_limit() -> None:
    """전체 프로세스 합계가 _SEC_MAX_RPS를 넘지 않도록 대기합니다."""
    interval = 1.0 / _SEC_MAX_RPS
    with _sec_rate_lock:
        now = time.monotonic()
        slot = max(now, _sec_next_slot[0])
        _sec_next_slot[0] = slot + interval
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


@st.cache_resource(show_spinner=False)
def get_sec_session() -> requests.Session:
    """
    SEC EDGAR의 연결 끊김 및 Rate Limit을 방어하기 위한 전용 세션.

    [성능] @st.cache_resource로 한 번만 생성해 재사용합니다. 기존에는
    fetch_sec_13f_multi_quarters()가 호출될 때마다 새 Session을 만들어,
    13F 교집합 화면(기관 12곳)에서 세션과 커넥션 풀이 12벌씩 생겼습니다.

    [주의] SEC는 연락처가 포함된 User-Agent를 요구합니다(미준수 시 403).
    초당 요청 한도는 _sec_rate_limit() 토큰 버킷이 지킵니다.
    """
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[403, 408, 429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    session.mount(
        "https://",
        HTTPAdapter(max_retries=retries, pool_connections=4, pool_maxsize=10),
    )
    session.headers.update({
        "User-Agent": "MacroQuantResearchApp/3.0 (research_analytics@macrofintechhub.com)",
        "Accept-Encoding": "gzip, deflate",
    })
    # Host 헤더는 고정하지 않습니다. www.sec.gov 외의 호스트
    # (예: data.sec.gov)로 요청할 때 잘못된 Host가 붙어 실패하기 때문에,
    # requests가 URL에서 자동으로 채우게 둡니다.
    return session


# ==============================================================================
# 2. 통합 13F 분기 데이터 크롤러 (Type 컬럼 검색 및 콤마 제거 파싱)
# ==============================================================================
def collect_sec_13f_multi_quarters(cik: str, max_quarters: int = 4):
    """
    최대 max_quarters 분기만큼의 13F 공시를 수집하여
    [(df, meta_info), (df_prev, meta_info_prev), ...] 형태로 반환
    """
    session = get_sec_session()
    clean_cik = str(cik).lstrip("0")
    cik_padded = clean_cik.zfill(10)
    base_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_padded}&type=13F-HR&dateb=&owner=include&count=40"

    try:
        res = session.get(base_url, timeout=30)
        res.raise_for_status()
    except Exception as e:
        return None, f"SEC EDGAR 연결 실패 (서버 점검 또는 통신 지연): {str(e)}\n우측 상단의 [데이터 새로고침] 버튼을 눌러주세요."

    soup = BeautifulSoup(res.text, "html.parser")
    tables = soup.find_all("table", class_="tableFile2")
    if not tables:
        return None, f"해당 CIK({cik})의 13F-HR 검색 결과 테이블을 찾을 수 없습니다."

    history_links = []
    rows = tables[0].find_all("tr")[1:]
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 4:
            doc_type = cols[0].text.strip()
            if "13F-HR" in doc_type:
                a_tag = cols[1].find("a", href=True)
                filing_date = cols[3].text.strip()
                if a_tag:
                    doc_link = a_tag['href'] if a_tag['href'].startswith("http") else "https://www.sec.gov" + a_tag['href']
                    history_links.append((filing_date, doc_link))

        if len(history_links) >= max_quarters:
            break

    if not history_links:
        return None, "조회된 13F-HR 공시 문서가 없습니다."

    all_results = []
    for filing_date, doc_url in history_links:
        try:
            _sec_rate_limit()   # SEC 초당 요청 한도 준수 (병렬 허용)
            doc_res = session.get(doc_url, timeout=30)
            doc_res.raise_for_status()
        except Exception as e:
            logger.warning(f"13F 문서 목록 조회 실패 ({filing_date}): {e}")
            continue

        doc_soup = BeautifulSoup(doc_res.text, "html.parser")
        doc_table = doc_soup.find("table", class_="tableFile")
        xml_url = None

        # 1. Type 컬럼(c[3]) 및 파일명 검사로 실제 종목 테이블(information table) 타겟팅
        if doc_table:
            for r in doc_table.find_all("tr")[1:]:
                c = r.find_all("td")
                if len(c) >= 4:
                    fname = c[2].find("a", href=True)
                    if fname and fname.text.strip().lower().endswith(".xml"):
                        doc_type_col = c[3].text.strip().lower()
                        file_name_lower = fname.text.strip().lower()
                        href = fname['href']
                        full_href = href if href.startswith("http") else "https://www.sec.gov" + href

                        is_info_table = (
                            "information table" in doc_type_col
                            or "infotable" in doc_type_col
                            or "infotable" in file_name_lower
                            or "information" in file_name_lower
                        )
                        if is_info_table:
                            xml_url = full_href
                            break
                        if not xml_url and "primary_doc" not in file_name_lower:
                            xml_url = full_href

        # 2. 테이블 미존재 시 페이지 전체에서 primary_doc 제외하고 XML 링크 탐색
        if not xml_url:
            for a in doc_soup.find_all("a", href=True):
                href = a['href']
                href_lower = href.lower()
                if href_lower.endswith(".xml") and "primary_doc" not in href_lower:
                    xml_url = href if href.startswith("http") else "https://www.sec.gov" + href
                    if "infotable" in href_lower or "information" in href_lower:
                        break

        if not xml_url:
            continue

        try:
            _sec_rate_limit()
            xml_res = session.get(xml_url, timeout=30)
            xml_res.raise_for_status()
        except Exception as e:
            logger.warning(f"13F XML 다운로드 타임아웃 ({filing_date}): {e}")
            continue

        # XML 파싱 (네임스페이스 무시 및 콤마 완벽 제거)
        try:
            root = ET.fromstring(xml_res.content)
            data = []

            for info_table in root.iter():
                if info_table.tag.lower().endswith("infotable"):
                    name, title_class, cusip, val_text = "", "", "", "0"
                    shares = 0.0

                    for child in info_table.iter():
                        tag_name = child.tag.lower()
                        text = child.text.strip() if child.text else ""

                        if tag_name.endswith("nameofissuer"):
                            name = text
                        elif tag_name.endswith("titleofclass"):
                            title_class = text
                        elif tag_name.endswith("cusip"):
                            cusip = text
                        elif tag_name.endswith("value"):
                            val_text = text
                        elif tag_name.endswith("sshprnamt"):
                            try:
                                shares = float(text.replace(",", "").strip()) if text else 0.0
                            except (ValueError, TypeError):
                                shares = 0.0

                    try:
                        val = float(val_text.replace(",", "").strip()) if val_text else 0.0
                    except (ValueError, TypeError):
                        val = 0.0

                    if name and val > 0:
                        data.append({
                            'name': name.strip().upper(),
                            'class': title_class.strip() if title_class else "",
                            'cusip': cusip.strip() if cusip else "",
                            'value': val,
                            'shares': shares
                        })
        except Exception as e:
            logger.warning(f"XML 파싱 에러 ({filing_date}): {e}")
            continue

        if not data:
            continue

        df = pd.DataFrame(data)

        # 2023년 이전 공시 파일이거나 합산액이 $100M 미만인 경우 천 달러 단위로 간주하고 보정
        if df['value'].sum() > 0 and df['value'].sum() < 100_000_000:
            df['value'] = df['value'] * 1000.0

        # 종목명(CUSIP 기준)으로 그룹화하여 옵션/본주 분산 표기 합산
        df = df.groupby(['name', 'cusip', 'class'], as_index=False).agg({'value':'sum', 'shares':'sum'})
        df = df.sort_values(by='value', ascending=False).reset_index(drop=True)

        total_aum = df['value'].sum()
        df['weight'] = (df['value'] / total_aum) * 100.0 if total_aum > 0 else 0.0

        # Report Date 추정 (Filing date에서 가장 가까운 직전 분기말)
        fd_dt = pd.to_datetime(filing_date)
        year = fd_dt.year
        month = fd_dt.month
        if month <= 2:
            report_date = f"{year-1}-12-31"
        elif month <= 5:
            report_date = f"{year}-03-31"
        elif month <= 8:
            report_date = f"{year}-06-30"
        elif month <= 11:
            report_date = f"{year}-09-30"
        else:
            report_date = f"{year}-12-31"

        meta_info = {
            'filing_date': filing_date,
            'report_date': report_date,
            'total_value': total_aum,
        }
        all_results.append((df, meta_info))

    if not all_results:
        return None, "성공적으로 추출된 분기 데이터가 없습니다. (기관의 공시 문서가 비어있거나 파싱 가능한 13F XML이 없습니다.)"

    return all_results, None


# ==============================================================================
# 3. 직전 분기 대비 매수/매도 액션 분류 함수 (ImportError 해결 핵심)
# ==============================================================================
# ==============================================================================
# 저장본 우선 읽기 경로
# ==============================================================================
def _derive_from_longer_snapshot(cik: str, max_quarters: int):
    """
    요청한 분기 수보다 긴 저장본이 있으면 거기서 잘라 반환합니다.
    없으면 None (호출부가 평소 경로를 타도록).
    """
    if max_quarters >= _MAX_TRACKED_QUARTERS:
        return None

    for longer in range(max_quarters + 1, _MAX_TRACKED_QUARTERS + 1):
        snap = store.read_snapshot(datasets.snap_sec_13f(cik, longer))
        if snap is None or not snap.is_fresh(datasets.MAX_AGE_SLOW):
            continue
        if not _history_schema_ok(snap.payload):
            continue

        history, err = snap.payload
        if not isinstance(history, list) or len(history) < max_quarters:
            continue

        logger.debug(
            "13F q%s를 q%s 저장본에서 유도했습니다 (cik=%s)",
            max_quarters, longer, cik,
        )
        return history[:max_quarters], err

    return None


# 수집기가 저장하는 최대 분기 수 (collector._task_sec_13f의 QUARTERS와 일치)
_MAX_TRACKED_QUARTERS = 8


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sec_13f_multi_quarters(cik: str, max_quarters: int = 4):
    """
    화면용 진입점. 13F는 분기 공시(45일 지연)이므로 저장해 두기에 가장
    적합합니다. SEC는 초당 10건 제한이 있고 분기별로 문서를 따라 들어가야
    해서 수집이 특히 느립니다(기관 1곳 8분기에 수십 초).

    반환: (분기별 [(DataFrame, meta), ...], 오류 메시지 | None)
    """
    def _collect():
        return collect_sec_13f_multi_quarters(cik, max_quarters)

    snap_name = datasets.snap_sec_13f(cik, max_quarters)

    # q1 같은 짧은 요청은 더 긴 저장본(q8)의 앞부분과 동일합니다
    # (collect_*가 공시를 최신순으로 훑어 앞에서 자르기 때문).
    # 그러니 q1 저장본이 없어도 q8이 있으면 수집하지 않고 잘라 씁니다.
    derived = _derive_from_longer_snapshot(cik, max_quarters)
    if derived is not None:
        return derived

    # 저장본이 예전 버전의 컬럼 구성이면 화면이 KeyError로 죽습니다.
    # cached_or_live의 required_columns는 단일 DataFrame만 검사하므로,
    # 중첩 구조인 13F는 여기서 직접 검증하고 어긋나면 스냅샷을 버립니다.
    snap = store.read_snapshot(snap_name)
    if snap is not None and not _history_schema_ok(snap.payload):
        logger.warning(
            "13F 저장본 스키마 불일치(cik=%s q=%s). 다시 수집합니다.",
            cik, max_quarters,
        )
        payload = None
    else:
        payload = store.cached_or_live(
            snap_name,
            _collect,
            max_age_seconds=datasets.MAX_AGE_SLOW,
            as_object=True,
        )

    if payload is None:
        # 스키마 불일치 → 직접 수집해서 덮어씁니다.
        try:
            payload = _collect()
            store.put_object(snap_name, payload)
        except Exception as e:
            logger.warning("13F 재수집 실패 (cik=%s): %s", cik, e)
            return [], f"수집 실패: {e}"

    if not isinstance(payload, (list, tuple)) or len(payload) != 2:
        return [], "저장본이 없고 수집에도 실패했습니다."

    history, err = payload
    return (history if isinstance(history, list) else []), err


# 화면(views/sec_view.py)이 직접 인덱싱하는 13F 컬럼
_REQUIRED_13F_COLUMNS = ("name", "cusip", "class", "value", "shares", "weight")


def _history_schema_ok(payload) -> bool:
    """13F 저장본의 분기별 DataFrame이 필요한 컬럼을 갖고 있는지 확인합니다."""
    if not isinstance(payload, (list, tuple)) or len(payload) != 2:
        return False

    history = payload[0]
    if not isinstance(history, list) or not history:
        # 빈 이력은 스키마 문제가 아니라 '데이터 없음'이므로 통과시킵니다.
        return True

    for entry in history:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            return False
        df = entry[0]
        if not isinstance(df, pd.DataFrame):
            return False
        if df.empty:
            continue
        if any(c not in df.columns for c in _REQUIRED_13F_COLUMNS):
            return False
    return True


def classify_qoq_action(row):
    """직전 분기 대비 비중 증감폭을 기준으로 매수/매도/유지 액션 분류"""
    diff = row.get('weight_diff', 0.0) if isinstance(row, dict) else row['weight_diff']
    shares_curr = row.get('shares_curr', 0.0) if isinstance(row, dict) else row['shares_curr']
    shares_prev = row.get('shares_prev', 0.0) if isinstance(row, dict) else row['shares_prev']
    
    if shares_prev == 0 and shares_curr > 0:
        return "🆕 신규 매수 (New)"
    elif shares_curr == 0 and shares_prev > 0:
        return "❌ 전량 매도 (Closed)"
    elif diff > 0.05:
        return "📈 비중 확대 (Added)"
    elif diff < -0.05:
        return "📉 비중 축소 (Reduced)"
    else:
        return "⚪ 유지 (Unchanged)"


def format_currency(val):
    """달러 단위 포맷팅"""
    if val >= 1e9:
        return f"${val/1e9:,.2f}B"
    elif val >= 1e6:
        return f"${val/1e6:,.2f}M"
    else:
        return f"${val:,.0f}"


# ==============================================================================
# 4. 전체 기관 일괄 로딩 및 교집합 헬퍼 함수
# ==============================================================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_all_institutions_data():
    """config.py에 정의된 모든 기관의 최신 13F 데이터를 일괄 수집"""
    data = {}
    for inst_name, inst_info in INSTITUTIONS.items():
        results, err = fetch_sec_13f_multi_quarters(inst_info['cik'], max_quarters=1)
        if not err and results:
            df, meta = results[0]
            data[inst_name] = {
                'df': df,
                'meta': meta
            }
        _sec_rate_limit()
    return data


def calculate_consensus(inst_data):
    """모든 기관 데이터를 취합하여 공통 보유 종목(교집합)을 계산하는 함수"""
    if not inst_data:
        return pd.DataFrame()

    all_holdings = []
    for inst_name, data in inst_data.items():
        df = data['df'].copy()
        df['Institution'] = inst_name
        df['Name_Clean'] = df['name'].str.upper().str.replace(r'\b(INC|CORP|LLC|LTD|PLC|COMPANY|CO)\b', '', regex=True)
        df['Name_Clean'] = df['Name_Clean'].str.replace(r'[^\w\s]', '', regex=True).str.strip()
        df = df.head(100)

        for _, row in df.iterrows():
            all_holdings.append({
                'Name': row['name'],
                'Name_Clean': row['Name_Clean'],
                'Ticker': row['cusip'][:6] if 'cusip' in row else '',
                'Weight': row['weight'],
                'Institution': row['Institution']
            })

    holdings_df = pd.DataFrame(all_holdings)
    if holdings_df.empty:
        return pd.DataFrame()

    consensus = holdings_df.groupby('Name_Clean').agg(
        Name=('Name', 'first'),
        Ticker=('Ticker', 'first'),
        Institution_Count=('Institution', 'nunique'),
        Avg_Weight=('Weight', 'mean'),
        Holders=('Institution', list)
    ).reset_index()

    consensus = consensus[consensus['Institution_Count'] >= 2]
    consensus = consensus.sort_values(by=['Institution_Count', 'Avg_Weight'], ascending=[False, False]).reset_index(drop=True)
    consensus = consensus.drop(columns=['Name_Clean'])

    return consensus


def get_top_holdings_by_inst(inst_data, inst_name, top_n=20):
    """특정 기관의 상위 N개 종목을 포맷팅하여 반환"""
    if inst_name not in inst_data:
        return pd.DataFrame()

    df = inst_data[inst_name]['df'].head(top_n).copy()
    df_display = df[['name', 'cusip', 'weight', 'value', 'shares']].copy()
    df_display.columns = ['종목명 (Issuer)', 'CUSIP', '비중 (%)', '평가액 ($)', '보유 주식수']
    df_display['비중 (%)'] = df_display['비중 (%)'].map('{:.2f}%'.format)
    df_display['평가액 ($)'] = df_display['평가액 ($)'].map('${:,.0f}'.format)
    df_display['보유 주식수'] = df_display['보유 주식수'].map('{:,.0f}'.format)

    return df_display
