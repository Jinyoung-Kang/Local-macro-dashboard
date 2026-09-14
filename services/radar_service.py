"""
services/radar_service.py
무중단(Fail-safe) 파이프라인 기반 날짜별/누적 수급 스캐닝 엔진
[장중: KIS -> Daum -> Naver] [장마감 후/과거: Naver -> Daum -> PyKrx]
공식 지원 투자주체(외국인/기관/투신/은행/보험/종금/기금/기타기관/기타법인) 매핑 탑재

"""
import logging
from importlib import metadata
import re
from datetime import datetime, time, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# 공용 커넥션 풀 세션을 사용해 요청마다 TCP/TLS 핸드셰이크를
# 반복하지 않습니다 (services/http_client.py).
from services.http_client import get_session
import streamlit as st
import yfinance as yf
from bs4 import BeautifulSoup
import numpy as np 

from services.ls_service import call_ls_api
from services.kis_service import call_kis_api
from services import datasets, store
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from pykrx import stock
    PYKRX_AVAILABLE = True
except ImportError:
    PYKRX_AVAILABLE = False

logger = logging.getLogger(__name__)

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# 렌더링 수집은 services/browser_pool.py의 공용 Chromium을 재사용합니다.
# (기존에는 호출마다 Chromium을 새로 띄워 매번 콜드 스타트 비용을 냈습니다.)
from services.browser_pool import fetch_rendered_html as _fetch_rendered_html

# ==============================================================================
# KIS FHPTJ04400000 투자자별 실제 필드 매핑
# ==============================================================================
KIS_INVESTOR_FIELDS = {
    "외국인": {
        "quantity": "frgn_ntby_qty",
        "amount": "frgn_ntby_tr_pbmn",
    },
    "기관": {
        "quantity": "orgn_ntby_qty",
        "amount": "orgn_ntby_tr_pbmn",
    },
    "투신": {
        "quantity": "ivtr_ntby_qty",
        "amount": "ivtr_ntby_tr_pbmn",
    },
    "은행": {
        "quantity": "bank_ntby_qty",
        "amount": "bank_ntby_tr_pbmn",
    },
    "보험": {
        "quantity": "insu_ntby_qty",
        "amount": "insu_ntby_tr_pbmn",
    },
    "종금": {
        "quantity": "mrbn_ntby_qty",
        "amount": "mrbn_ntby_tr_pbmn",
    },
    "기금": {
        "quantity": "fund_ntby_qty",
        "amount": "fund_ntby_tr_pbmn",
    },
    "기타기관": {
        "quantity": "etcorgt_ntby_vol",
        "amount": "etcorgt_ntby_tr_pbmn",
    },
    "기타법인": {
        "quantity": "etccorp_ntby_vol",
        "amount": "etccorp_ntby_tr_pbmn",
    },
}

# Naver/Daum이 지원하는 투자주체만 여기서 매핑합니다.
NAVER_INVESTOR_MAP = {
    "외국인": "9000",
    "기관": "7000",
    "개인": "8000",
    "연기금": "6000",
    "금융투자": "2000",
    "투신": "3000",
}

DAUM_INVESTOR_MAP = {
    "외국인": "FOREIGN",
    "기관": "INSTITUTION",
    "연기금": "PENSION",
    "금융투자": "FINANCIAL",
    "투신": "TRUST",
    "개인": "INDIVIDUAL",
}


def _to_float(value, default=0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


# ==============================================================================
# 1. KIS / LS / PyKrx / Naver / Daum 연결 상태 진단 함수
# ==============================================================================
def test_kis_connection():
    """
    KIS 국내기관·외국인 매매종목 가집계 API 연결 점검.
    FHPTJ04400000은 장중 가집계 전용 TR입니다.
    """
    base_params = {
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_RANK_SORT_CLS_CODE": "0",
        "FID_ETC_CLS_CODE": "0",
    }

    for market_division in ["V", "J"]:
        params = {
            **base_params,
            "FID_COND_MRKT_DIV_CODE": market_division,
        }

        try:
            res = call_kis_api(
                tr_id="FHPTJ04400000",
                endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total",
                params=params,
            )

            if res and res.get("rt_cd") == "0":
                output = res.get("output", [])
                if isinstance(output, list) and output:
                    return (
                        True,
                        f"정상 통신 성공 ({market_division} 구분, "
                        f"조회 종목 수: {len(output)}개)",
                    )

            if res:
                logger.warning(
                    "KIS 연결 점검 실패 (%s): %s",
                    market_division,
                    res.get("msg1", str(res)),
                )

        except Exception as e:
            logger.warning(
                "KIS 연결 점검 예외 (%s): %s",
                market_division,
                e,
            )

    return False, (
        "KIS FHPTJ04400000 가집계 데이터가 비어있거나 API 호출에 실패했습니다. "
        "장 마감 후에는 이 TR이 원래 빈 데이터를 반환하는 것이 정상입니다."
    )


# LS 조회에 쓰는 TR 정의 (진단과 수집이 같은 목록을 봅니다).
#
# t1664 `/stock/investor`는 정상 응답합니다.
# t1452 `/stock/market-sum`은 현재 **HTTP 404** — 그 경로가 존재하지 않습니다.
# 404가 확인됐지만 LS가 되살릴 수도 있으므로 지우지 않고 뒤로 미룹니다.
LS_PROBE_TRS = (
    ("t1664", "/stock/investor", {
        "t1664InBlock": {
            "gubun1": "1", "gubun2": "1", "gubun3": "1", "cnt": 30,
        }
    }),
    ("t1452", "/stock/market-sum", {
        "t1452InBlock": {
            "gubun": "1", "jnilgubun": "1", "paygubun": "2",
            "ordergubun": "1", "cnt": 30,
        }
    }),
)


def test_ls_connection():
    """
    LS증권 OPEN API 연결 점검 — **단계를 나눠** 보고합니다.

    [버그 수정] 예전에는 어떤 이유로 실패하든 화면이
    "LS 계좌 미연결 또는 미사용"이라고 적었습니다. 그런데 사용자가 실제로
    받은 응답은 `해당자료가 없습니다`였습니다. 이건 LS 서버가 TR에 **정상
    응답한 내용**입니다. 즉 앱키/시크릿은 유효했고 토큰도 발급됐는데,
    화면은 "키가 등록 안 됐다"는 뜻으로 읽히는 문구를 보여 줬습니다.

    무엇이 실제로 막혔는지 알 수 있도록 이렇게 나눕니다.
        1. 키가 secrets.toml에 있는가
        2. OAuth 토큰이 발급되는가  ← 앱키/시크릿 유효성의 진짜 판정
        3. TR이 데이터를 주는가      ← 장 시간·상품 권한의 문제

    2단계까지 통과하면 **연결은 성공**입니다. 3단계에서 데이터가 비는 것은
    주말·장 마감 시간대에는 정상입니다.
    """
    from services import ls_service as ls_mod
    from services.ls_service import get_secret as ls_secret, request_ls_token

    # --- 1단계: 키 존재 ---
    app_key = ls_secret("ls.app_key", ls_secret("LS_APP_KEY", ""))
    app_secret = ls_secret("ls.app_secret", ls_secret("LS_APP_SECRET", ""))
    if not app_key or not app_secret:
        return False, (
            "secrets.toml에 `[ls] app_key` / `app_secret`이 없습니다. "
            "LS를 쓰지 않는다면 무시해도 됩니다."
        )

    # --- 2단계: 토큰 발급 ---
    # 실패해도 원인이 전혀 다릅니다. 망에서 서버에 닿지 못한 것과 키가
    # 거절된 것을 섞으면, 멀쩡한 키를 계속 의심하게 됩니다(사용자 신고).
    token, token_error, fail_kind = request_ls_token()

    if not token and fail_kind == ls_mod.FAIL_NETWORK:
        return False, (
            "**LS 서버에 접속하지 못했습니다. 키 문제가 아닙니다.**\n\n"
            f"{token_error}\n\n"
            "LS OPEN API는 8080 포트를 쓰는데, 회사·학교 망이나 VPN에서 "
            "8080 아웃바운드가 막혀 있으면 이렇게 됩니다. 443으로도 시도했지만 "
            "역시 닿지 않았습니다.\n\n"
            "터미널에서 확인해 보세요:\n"
            "`curl -v --max-time 10 https://openapi.ls-sec.co.kr:8080/oauth2/token`\n\n"
            "VPN을 끄거나 다른 네트워크(휴대폰 핫스팟)에서 다시 시도해 보시고, "
            "포트가 바뀐 것이 확인되면 secrets.toml에 "
            "`[ls] base_url = \"https://...\"` 로 지정할 수 있습니다."
        )

    if not token:
        return False, (
            f"OAuth 토큰 발급 거절 — 앱키/시크릿이 유효하지 않거나 "
            f"OPEN API 사용등록이 안 된 상태입니다.\n\n{token_error}"
        )

    # --- 3단계: TR 조회 ---
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    is_regular_session = (
        now_kst.weekday() < 5
        and dtime(9, 0) <= now_kst.time() < dtime(15, 30)
    )

    # 동작이 확인된 TR을 먼저 시도합니다. t1452(/stock/market-sum)는 현재
    # HTTP 404입니다 — 그 경로는 LS에 존재하지 않습니다(사용자 진단으로 확인).
    messages = []
    broken_endpoints = []

    for tr_cd, tr_url, body in LS_PROBE_TRS:
        try:
            res = call_ls_api(tr_cd=tr_cd, tr_url=tr_url, body_params=body)
        except Exception as e:                               # noqa: BLE001
            messages.append(f"{tr_cd}: 예외 {type(e).__name__}")
            continue

        if not res:
            messages.append(f"{tr_cd}: 응답 없음")
            continue

        block = res.get(f"{tr_cd}OutBlock1")
        if isinstance(block, list) and block:
            return True, (
                f"정상 통신 성공 (토큰 발급 OK · {tr_cd} 조회 종목 수: "
                f"{len(block)}개)"
            )

        reason = str(res.get("rsp_msg", "데이터 없음"))

        # HTTP 4xx/5xx는 "데이터가 없다"와 전혀 다릅니다. 경로·권한 문제이며
        # 장 시간과 무관합니다. 이걸 "장 시간이 아니라 정상"이라고 뭉뚱그리면
        # 진짜 고쳐야 할 것을 놓칩니다.
        if reason.startswith("HTTP "):
            broken_endpoints.append(f"{tr_cd} {tr_url} → {reason}")

        messages.append(f"{tr_cd}: {reason}")

    detail = " / ".join(messages) if messages else "데이터 없음"

    # 토큰이 나왔다는 것은 인증이 된다는 뜻입니다. 이것을 "미연결"이라고
    # 말하면 사용자가 키를 계속 의심하게 됩니다.
    if is_regular_session:
        return False, (
            f"인증은 성공했습니다(토큰 발급 OK). 다만 TR이 데이터를 주지 "
            f"않습니다 — {detail}\n\n"
            f"지금은 정규장인데도 비어 있으므로, 해당 TR의 사용 권한이나 "
            f"입력값을 확인해야 합니다."
        )

    msg = (
        f"인증 성공 (토큰 발급 OK). 조회 데이터는 비어 있습니다 — {detail}\n\n"
        f"현재 장 시간이 아니라 시세 TR이 빈 값을 주는 것은 정상입니다. "
        f"정규장(평일 09:00~15:30)에 다시 확인하세요."
    )
    if broken_endpoints:
        msg += (
            "\n\n⚠️ 다만 아래는 장 시간과 무관한 **경로/권한 문제**입니다:\n"
            + "\n".join(f"- {b}" for b in broken_endpoints)
        )
    return True, msg


def test_pykrx_connection():
    """
    PyKrx 연결 점검.

    [개선] 예전에는 실패했을 때 "최근 7일 내 유효한 KOSPI 종목 리스트를
    가져오지 못했습니다"만 말해서, 무엇을 해야 할지 알 수 없었습니다.
    PyKrx는 KRX 웹 엔드포인트를 **비공식으로** 긁는 라이브러리라
    KRX가 응답 형식을 바꾸면 예외 없이 빈 리스트만 돌려주기 시작합니다.
    그 경우와 진짜 통신 오류를 구분해서 알려 줍니다.

    PyKrx는 수급 레이더의 **마지막 폴백**이며, 여기서 실패해도 KIS/Daum/
    Naver가 살아 있으면 당일 조회는 정상입니다. 다만 Naver·Daum은 과거
    날짜 조회를 지원하지 않아, **기준일을 과거로 바꾸면 PyKrx만 남습니다.**
    """
    if not PYKRX_AVAILABLE:
        return False, "pykrx 패키지가 설치되지 않았습니다 (requirements.txt 확인 필요)."

    try:
        version = metadata.version("pykrx")
    except Exception:
        version = "알 수 없음"

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    check_date = now_kst
    empty_days = []
    last_error = None

    for _ in range(7):
        date_str = check_date.strftime("%Y%m%d")
        try:
            tickers = stock.get_market_ticker_list(date_str, market="KOSPI")
        except Exception as e:                       # noqa: BLE001
            last_error = f"{type(e).__name__}: {str(e)[:120]}"
            tickers = None
        else:
            if tickers:
                return True, (
                    f"정상 통신 성공 (기준일 {date_str}, "
                    f"KOSPI 종목 수: {len(tickers)}개, pykrx {version})"
                )
            empty_days.append(date_str)
        check_date -= timedelta(days=1)

    if last_error:
        return False, (
            f"KRX 서버 통신에 실패했습니다 (pykrx {version}). "
            f"마지막 오류: {last_error} — 네트워크·방화벽을 먼저 확인하세요."
        )

    return False, (
        f"KRX가 JSON이 아닌 응답(차단 페이지 등)을 돌려주고 있습니다 "
        f"(pykrx {version}). 로그에 'Expecting value: line 1 column 1'이 "
        f"보이면 같은 증상입니다. PyKrx는 KRX 웹을 비공식으로 긁는 "
        f"라이브러리라 KRX가 응답 형식·차단 정책을 바꾸면 이렇게 조용히 "
        f"빈 값만 돌려줍니다. **버전 업그레이드로는 해결되지 않습니다** "
        f"(1.2.8이 최신). 라이브러리가 KRX 변경을 따라잡을 때까지 기다려야 "
        f"합니다.\n\n"
        f"영향 범위: 당일 조회는 KIS/Daum/Naver로 정상 동작합니다. "
        f"과거 날짜 조회만 영향을 받으며, 수집기가 쌓아 온 누적 이력이 "
        f"있으면 그것으로 대체됩니다 "
        f"(🗄️ 데이터 저장소 상태 → 누적 수급 이력)."
    )


def test_naver_scraping():
    """
    Naver 금융 수급 순위 iframe 페이지를 헤드리스 브라우저로 렌더링하여
    실제 표가 정상적으로 생성되는지 점검합니다.

    주의: 이 페이지에는 class="type_1"인 표가 여러 개(레이아웃용 포함)
    존재할 수 있습니다. 첫 번째 표가 아니라, 실제로 종목 링크(a 태그)를
    포함한 표를 찾아야 합니다.
    """
    url = (
        "https://finance.naver.com/sise/sise_deal_rank_iframe.naver"
        "?sosok=01&investor_gubun=9000&type=buy"
    )

    try:
        html = _fetch_rendered_html(url, wait_selector="table")
        soup = BeautifulSoup(html, "html.parser")

        candidate_tables = soup.find_all("table", {"class": "type_1"})
        if not candidate_tables:
            candidate_tables = soup.find_all("table")

        if not candidate_tables:
            return False, "렌더링 후에도 표(table)를 전혀 찾지 못했습니다."

        best_table = None
        best_parsed = 0

        for table in candidate_tables:
            rows = table.find_all("tr")
            parsed = sum(
                1 for row in rows
                if len(row.find_all("td")) >= 4 and row.find("a")
            )
            if parsed > best_parsed:
                best_parsed = parsed
                best_table = table

        if best_table is None or best_parsed == 0:
            return False, (
                f"표는 {len(candidate_tables)}개 렌더링됐지만, "
                "종목 링크가 포함된 유효한 표를 찾지 못했습니다 (휴장일 가능)."
            )

        return True, f"정상 통신 성공 (렌더링 방식, 파싱된 종목 수: {best_parsed}개)"

    except Exception as e:
        return False, f"헤드리스 브라우저 렌더링 실패: {e}"


def test_daum_scraping():
    """
    Daum 금융 투자자별 매매종목 연결 점검.

    [버그 수정] 예전에는 finance.daum.net/domestic/influential_investors
    **페이지를 헤드리스 브라우저로 렌더링**해 표를 찾았습니다. 그런데 화면이
    실제로 쓰는 Daum 경로는 그 페이지가 아니라 내부 JSON API
    (finance.daum.net/api/trend/investor_purchase/)입니다.

    서로 다른 것을 재고 있었기 때문에, API가 멀쩡히 30종목을 돌려주는
    상황에서도 카드가 "Daum 실패"로 빨갛게 떴습니다(사용자 신고). 진단은
    화면이 실제로 쓰는 경로를 그대로 따라가야 의미가 있습니다. 그렇지 않으면
    ⓐ 멀쩡한데 실패라고 하거나 ⓑ 망가졌는데 정상이라고 하게 됩니다.

    그래서 화면과 **똑같은 함수**(fetch_daum_deal_ranking)를 호출합니다.
    """
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    last_error = None

    # Daum API는 최근 거래일 데이터를 줍니다. 주말·휴장일을 감안해 거슬러 봅니다.
    for back in range(7):
        date_str = (now_kst - timedelta(days=back)).strftime("%Y%m%d")
        try:
            df = fetch_daum_deal_ranking(
                date_str, "KOSPI", "외국인", "순매수", 30,
            )
        except Exception as e:                               # noqa: BLE001
            last_error = f"{type(e).__name__}: {str(e)[:120]}"
            continue

        if df is not None and not df.empty:
            data_date = ""
            if "데이터_출처" in df.columns:
                data_date = str(df.iloc[0]["데이터_출처"])
            return True, (
                f"정상 통신 성공 (investor_purchase API, 종목 수: {len(df)}개)"
                + (f"\n\n{data_date}" if data_date else "")
            )

    if last_error:
        return False, (
            f"investor_purchase API 호출에 실패했습니다. 마지막 오류: "
            f"{last_error}"
        )

    return False, (
        "investor_purchase API가 최근 7일 내내 빈 응답을 돌려줬습니다. "
        "Daum이 API 경로나 파라미터를 바꿨을 수 있습니다 "
        "(services/radar_service.py의 fetch_daum_deal_ranking 확인)."
    )


# ==============================================================================
# 2. KIS 증권사 API (순매수/순매도 전용 TR, 장중 전용)
# ==============================================================================
def fetch_kis_deal_ranking(
    target_date: str,
    market: str,
    investor: str,
    trade_type: str,
    top_n: int,
) -> pd.DataFrame:
    """
    KIS FHPTJ04400000 장중 외국인·기관 가집계 Top N 수집.

    주의:
    - 장중 가집계이며 KRX 장마감 확정치와 다를 수 있습니다.
    - 이 TR은 당일 데이터만 조회합니다.
    - 장 마감 후 호출하면 정상적으로 빈 데이터를 반환합니다 (오류 아님).
    """
    field_map = KIS_INVESTOR_FIELDS.get(investor)

    if field_map is None:
        logger.warning("KIS 가집계 미지원 투자주체: %s", investor)
        return pd.DataFrame()

    is_kospi = (
        "KOSPI" in market.upper()
        or "코스피" in market
    )
    issue_code = "0000" if is_kospi else "1001"

    if investor == "외국인":
        rank_sort_code = "0" if trade_type == "순매수" else "1"
    else:
        rank_sort_code = "2" if trade_type == "순매수" else "3"

    base_params = {
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": issue_code,
        "FID_DIV_CLS_CODE": "0",
        "FID_RANK_SORT_CLS_CODE": rank_sort_code,
        "FID_ETC_CLS_CODE": "0",
    }

    output = []

    for market_division in ["V", "J"]:
        params = {
            **base_params,
            "FID_COND_MRKT_DIV_CODE": market_division,
        }

        try:
            res = call_kis_api(
                tr_id="FHPTJ04400000",
                endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total",
                params=params,
            )

            if res and res.get("rt_cd") == "0":
                candidate = res.get("output", [])

                if isinstance(candidate, list) and candidate:
                    output = candidate
                    break

            if res:
                logger.warning(
                    "KIS 가집계 API 실패 (%s): %s",
                    market_division,
                    res.get("msg1", str(res)),
                )

        except Exception as e:
            logger.warning(
                "KIS 가집계 API 예외 (%s): %s",
                market_division,
                e,
            )

    if not output:
        return pd.DataFrame()

    records = []

    for row in output:
        stock_code = row.get(
            "stck_shrn_iscd",
            row.get("mksc_shrn_iscd", ""),
        )
        stock_name = row.get("hts_kor_isnm", "")

        price = _to_float(row.get("stck_prpr"))
        change_pct = _to_float(row.get("prdy_ctrt"))

        net_amount_raw = _to_float(row.get(field_map["amount"]))
        net_quantity = _to_float(row.get(field_map["quantity"]))

        if net_amount_raw != 0:
            net_amount_eok = net_amount_raw / 100.0
            amount_basis = "KIS 원본 순매수 거래대금"
        elif net_quantity != 0 and price > 0:
            net_amount_eok = (net_quantity * price) / 100_000_000.0
            amount_basis = "KIS 원본 순매수 수량×현재가 환산"
        else:
            continue

        if trade_type == "순매수" and net_amount_eok <= 0:
            continue

        if trade_type == "순매도" and net_amount_eok >= 0:
            continue

        if not stock_code or not stock_name:
            continue

        records.append({
            "종목코드": str(stock_code).zfill(6),
            "종목명": stock_name,
            "현재가": price,
            "등락률(%)": change_pct,
            "순매수대금(억)": round(net_amount_eok, 1),
            "원본_순매수거래대금": net_amount_raw,
            "원본_순매수수량": net_quantity,
            "금액_산출기준": amount_basis,
            "수집시각": datetime.now(
                ZoneInfo("Asia/Seoul")
            ).strftime("%Y-%m-%d %H:%M:%S KST"),
            "데이터_출처": f"KIS 장중 가집계 / FHPTJ04400000 ({target_date})",
            "시가총액_가중": max(price * 1000, 500),
        })

    if not records:
        logger.warning(
            "KIS 가집계 파싱 결과 없음: 시장=%s, 투자주체=%s, 방향=%s",
            market,
            investor,
            trade_type,
        )
        return pd.DataFrame()

    result = pd.DataFrame(records)

    result = result.sort_values(
        "순매수대금(억)",
        ascending=(trade_type == "순매도"),
    ).head(top_n).reset_index(drop=True)

    result["순위"] = result.index + 1

    return result


# ==============================================================================
# 3. LS 증권사 API (t1452 및 t1664)
# ==============================================================================
def fetch_ls_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    """
    LS증권 OPEN API로 투자주체별 매매상위 종목을 조회합니다.

    파라미터:
        target_date : "YYYYMMDD" 문자열. 다만 LS는 당일 기준으로만
                      응답하므로 실질적으로 무시됩니다.
        market      : "KOSPI" | "KOSDAQ".
        investor    : "외국인" | "기관" 등.
        trade_type  : "순매수" | "순매도".
        top_n       : 가져올 상위 종목 수.

    반환값:
        수급 랭킹 DataFrame. 실패하거나 결과가 없으면 빈 DataFrame.

    주의사항:
        - **폴백 체인에서 네 번째**입니다. KIS·Daum·Naver 중 하나라도
          성공하면 호출되지 않습니다.
        - 이 함수는 오랫동안 정의만 되어 있고 **어디에서도 호출되지
          않았습니다.** 그래서 LS 키를 정확히 넣어도 화면 데이터는 전혀
          달라지지 않았습니다. 지금은 체인에 연결돼 있습니다.
        - LS 토큰은 443 포트로 발급받습니다. 문서에 적힌 8080은 서버가
          더 이상 열어 두지 않습니다(즉시 connection refused).
    """
    mkt_code = "1" if "KOSPI" in market.upper() or "코스피" in market else "2"
    order_code = "1" if trade_type == "순매수" else "2"

    # [순서 변경] 예전에는 t1452를 먼저 불렀는데, 그 경로
    # (/stock/market-sum)는 현재 HTTP 404입니다. 매번 실패하는 호출을
    # 먼저 내보내면 응답만 느려지므로, 정상 응답하는 t1664를 앞에 둡니다.
    inv_map = {"외국인": "1", "기관": "2", "개인": "3", "투신": "4", "연기금": "7", "금융투자": "5"}
    gubun2 = inv_map.get(investor, "1")
    body_params_1664 = {
        "t1664InBlock": {
            "gubun1": mkt_code,
            "gubun2": gubun2,
            "gubun3": order_code,
            "cnt": top_n,
        }
    }

    try:
        res = call_ls_api(tr_cd="t1664", tr_url="/stock/investor", body_params=body_params_1664)
        if res and "t1664OutBlock1" in res:
            data_list = res["t1664OutBlock1"]
            if data_list:
                records = []
                for idx, row in enumerate(data_list[:top_n], start=1):
                    code = str(row.get("shcode", "")).strip().zfill(6)
                    name = str(row.get("hname", "")).strip()
                    price = float(row.get("price", 0))
                    change_pct = float(row.get("diff", 0))

                    svalue = float(row.get("svalue", 0))
                    svolume = float(row.get("svolume", row.get("volume", 0)))

                    if svalue == 0 and svolume == 0:
                        continue

                    if svalue != 0:
                        net_amt_eok = round(svalue / 100.0, 1)
                    else:
                        net_amt_eok = round((svolume * price) / 100000000.0, 1)

                    if trade_type == "순매도" and net_amt_eok > 0:
                        net_amt_eok = -net_amt_eok

                    if name and code:
                        records.append({
                            "순위": idx,
                            "종목코드": code,
                            "종목명": name,
                            "현재가": price,
                            "등락률(%)": change_pct,
                            "순매수대금(억)": net_amt_eok,
                            "시가총액_가중": max(price * 1000, 500),
                            "수집시각": datetime.now(
                                ZoneInfo("Asia/Seoul")
                            ).strftime("%Y-%m-%d %H:%M:%S KST"),
                            "데이터_출처": f"LS 증권사 API ({target_date})",
                        })
                if records:
                    return pd.DataFrame(records)
    except Exception as e:
        logger.warning("LS t1664 호출 실패: %s", e)

    # t1664가 비면 t1452로 한 번 더 시도합니다(현재 404이지만 LS가 되살릴
    # 수 있으므로 남겨 둡니다).
    body_params_1452 = {
        "t1452InBlock": {
            "gubun": mkt_code,
            "jnilgubun": "1",
            "paygubun": "2",
            "ordergubun": order_code,
            "cnt": top_n,
        }
    }

    try:
        res = call_ls_api(tr_cd="t1452", tr_url="/stock/market-sum", body_params=body_params_1452)
        if res and "t1452OutBlock1" in res:
            data_list = res["t1452OutBlock1"]
            if data_list:
                records = []
                for idx, row in enumerate(data_list[:top_n], start=1):
                    # [버그 수정] 앞자리 0이 있는 종목코드(069500 등)를 그대로
                    # 두면 이후 단계에서 정수로 해석돼 69500으로 깨집니다.
                    # KIS 경로는 zfill(6)을 쓰는데 LS 경로만 빠져 있었습니다.
                    code = str(row.get("shcode", "")).strip().zfill(6)
                    name = str(row.get("hname", "")).strip()
                    price = float(row.get("price", 0))
                    change_pct = float(row.get("diff", 0))

                    val_key = "forval" if investor == "외국인" else "orgval"
                    svalue = float(row.get(val_key, row.get("svalue", 0)))

                    if svalue == 0:
                        continue

                    net_amt_eok = round(svalue / 100.0, 1) if abs(svalue) > 1000 else round(svalue, 1)

                    if trade_type == "순매도" and net_amt_eok > 0:
                        net_amt_eok = -net_amt_eok

                    if name and code:
                        records.append({
                            "순위": idx,
                            "종목코드": code,
                            "종목명": name,
                            "현재가": price,
                            "등락률(%)": change_pct,
                            "순매수대금(억)": net_amt_eok,
                            "시가총액_가중": max(price * 1000, 500),
                            "수집시각": datetime.now(
                                ZoneInfo("Asia/Seoul")
                            ).strftime("%Y-%m-%d %H:%M:%S KST"),
                            "데이터_출처": f"LS 증권사 API ({target_date})",
                        })
                if records:
                    return pd.DataFrame(records)
    except Exception as e:                                   # noqa: BLE001
        # 조용히 넘기면 LS가 왜 안 되는지 영영 알 수 없습니다.
        logger.warning("LS t1452 호출 실패: %s", e)

    return pd.DataFrame()


INTERVAL_LABELS = {
    "TODAY": "당일",
    "DAYS_5": "5거래일",
    "DAYS_20": "20거래일",
}

# ==============================================================================
# 4. Daum 실시간 API (시장 전체 랭킹)
# ==============================================================================
def fetch_daum_deal_ranking(
    target_date: str,
    market: str,
    investor: str,
    trade_type: str,
    top_n: int,
    interval_type: str = "TODAY",
) -> pd.DataFrame:
    """
    Daum 금융 외국인/기관 매매종목 JSON API에서 시장 전체 Top N을 조회합니다.

    실제 확인된 엔드포인트:
        https://finance.daum.net/api/trend/investor_purchase/

    실제 확인된 투자주체 파라미터:
        - 외국인: investorType=FOREIGN
        - 기관합계: investorType=INSTITUTION

    실제 응답 구조:
        {
            "data": {
                "BUY": [...],
                "SELL": [...]
            },
            "fromDate": "YYYY-MM-DD",
            "toDate": "YYYY-MM-DD"
        }

    interval_type:
        - TODAY: 당일
        - DAYS_5: 5거래일
        - DAYS_20: 20거래일

    주의:
        - 기관합계는 현재 당일(TODAY) 조회를 우선 지원합니다.
        - 세부 기관 주체(투신, 은행, 보험, 종금, 기금 등)는 이 API의
          시장 전체 Top N 지원 여부가 검증되지 않았으므로 빈 DataFrame을 반환합니다.
        - Daum API는 공식 API가 아닌 웹사이트 내부 JSON 요청이므로,
          응답 구조나 파라미터가 변경될 수 있습니다.
    """
    investor_type_map = {
        "외국인": "FOREIGN",
        "기관": "INSTITUTION",
    }

    investor_type = investor_type_map.get(investor)
    if investor_type is None:
        logger.warning(
            "Daum investor_purchase API 미지원 투자주체: %s "
            "(지원: 외국인, 기관)",
            investor,
        )
        return pd.DataFrame()

    valid_intervals = {"TODAY", "DAYS_5", "DAYS_20"}
    if interval_type not in valid_intervals:
        logger.warning(
            "Daum 지원하지 않는 interval_type=%s. TODAY로 변경합니다.",
            interval_type,
        )
        interval_type = "TODAY"

    # 현재 기관합계의 기간별 누적(5일/20일) API 응답은 별도 검증하지 않았으므로,
    # 잘못된 기간별 값을 보여주지 않도록 기관은 당일만 허용합니다.
    if investor == "기관" and interval_type != "TODAY":
        logger.warning(
            "Daum 기관합계는 현재 당일(TODAY) 시장 전체 Top N만 지원합니다. "
            "요청 interval=%s를 TODAY로 변경합니다.",
            interval_type,
        )
        interval_type = "TODAY"

    market_param = (
        "KOSPI"
        if "KOSPI" in market.upper() or "코스피" in market
        else "KOSDAQ"
    )

    url = "https://finance.daum.net/api/trend/investor_purchase/"

    # DevTools에서 실제 확인된 기본 요청 파라미터와 일치시킵니다.
    params = {
        "buyFieldName": "straightPurchasePrice",
        "buyOrder": "desc",
        "sellFieldName": "straightPurchasePrice",
        "sellOrder": "asc",
        "limit": top_n,
        "market": market_param,
        "investorType": investor_type,
    }

    # 캡처된 당일 요청에는 intervalType이 없었습니다.
    # 기간별 조회일 때만 intervalType을 추가합니다.
    if interval_type != "TODAY":
        params["intervalType"] = interval_type

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": (
            "https://finance.daum.net/domestic/"
            f"influential_investors?market={market_param}"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        response = get_session().get(
            url,
            headers=headers,
            params=params,
            timeout=10,
        )

        if response.status_code != 200:
            logger.warning(
                "Daum investor_purchase API HTTP 실패: status=%s, "
                "market=%s, investor=%s, interval=%s",
                response.status_code,
                market_param,
                investor,
                interval_type,
            )
            return pd.DataFrame()

        payload = response.json()
        data = payload.get("data", {})

        list_key = "BUY" if trade_type == "순매수" else "SELL"
        target_list = data.get(list_key, [])

        if not isinstance(target_list, list) or not target_list:
            logger.warning(
                "Daum investor_purchase API 빈 결과: "
                "market=%s, investor=%s(%s), direction=%s, interval=%s, "
                "payload_keys=%s, data_keys=%s",
                market_param,
                investor,
                investor_type,
                trade_type,
                interval_type,
                list(payload.keys()) if isinstance(payload, dict) else [],
                list(data.keys()) if isinstance(data, dict) else [],
            )
            return pd.DataFrame()

        from_date = payload.get("fromDate", "")
        to_date = payload.get("toDate", target_date)

        if interval_type == "TODAY":
            period_label = to_date or target_date
        elif from_date and to_date:
            period_label = f"{from_date}~{to_date}"
        else:
            period_label = target_date

        records = []

        for row in target_list[:top_n]:
            if not isinstance(row, dict):
                continue

            raw_code = str(row.get("symbolCode", "")).strip()
            stock_code = (
                raw_code[1:]
                if raw_code.startswith("A")
                else raw_code
            )

            stock_name = str(row.get("name", "")).strip()

            try:
                price = float(row.get("tradePrice", 0) or 0)
            except (TypeError, ValueError):
                price = 0.0

            try:
                change_rate = float(row.get("changeRate", 0) or 0)
            except (TypeError, ValueError):
                change_rate = 0.0

            # Daum changeRate는 0.0238 = 2.38% 형태의 소수 비율입니다.
            change_pct = round(change_rate * 100.0, 2)

            try:
                purchase_price = float(
                    row.get("straightPurchasePrice", 0) or 0
                )
            except (TypeError, ValueError):
                purchase_price = 0.0

            # straightPurchasePrice는 원 단위.
            # BUY는 양수, SELL은 음수 형태를 그대로 유지합니다.
            net_amount_eok = round(
                purchase_price / 100_000_000.0,
                1,
            )

            # API 응답에 매도 값의 부호가 비정상적으로 양수로 올 경우를 방어합니다.
            if trade_type == "순매도" and net_amount_eok > 0:
                net_amount_eok = -abs(net_amount_eok)

            # API 응답에 매수 값의 부호가 비정상적으로 음수로 올 경우를 방어합니다.
            if trade_type == "순매수" and net_amount_eok < 0:
                net_amount_eok = abs(net_amount_eok)

            try:
                rank = int(row.get("rank", 0) or 0)
            except (TypeError, ValueError):
                rank = 0

            if not stock_name or not stock_code:
                continue

            records.append({
                "순위": rank if rank > 0 else len(records) + 1,
                "종목코드": stock_code,
                "종목명": stock_name,
                "현재가": price,
                "등락률(%)": change_pct,
                "순매수대금(억)": net_amount_eok,
                "시가총액_가중": max(price * 1000, 500),
                "데이터_출처": (
                    f"Daum API ({investor}, "
                    f"{INTERVAL_LABELS.get(interval_type, interval_type)}, "
                    f"{period_label})"
                ),
                "수집시각": datetime.now(
                    ZoneInfo("Asia/Seoul")
                ).strftime("%Y-%m-%d %H:%M:%S KST"),
            })

        if not records:
            logger.warning(
                "Daum API 응답은 있으나 유효 레코드가 없습니다: "
                "market=%s, investor=%s, trade_type=%s",
                market_param,
                investor,
                trade_type,
            )
            return pd.DataFrame()

        result_df = pd.DataFrame(records)

        # 순위 필드가 비정상적이거나 중복된 경우 표시 순서를 금액 기준으로 재정렬합니다.
        result_df = result_df.sort_values(
            "순매수대금(억)",
            ascending=(trade_type == "순매도"),
        ).head(top_n).reset_index(drop=True)

        result_df["순위"] = range(1, len(result_df) + 1)

        logger.info(
            "Daum investor_purchase API 성공: market=%s, investor=%s(%s), "
            "trade_type=%s, interval=%s, rows=%s",
            market_param,
            investor,
            investor_type,
            trade_type,
            interval_type,
            len(result_df),
        )

        return result_df

    except ValueError as e:
        logger.warning(
            "Daum investor_purchase API JSON 파싱 실패: "
            "market=%s, investor=%s, error=%s",
            market_param,
            investor,
            e,
        )
    except requests.RequestException as e:
        logger.warning(
            "Daum investor_purchase API 통신 실패: "
            "market=%s, investor=%s, error=%s",
            market_param,
            investor,
            e,
        )
    except Exception as e:
        logger.exception(
            "Daum investor_purchase API 예외: "
            "market=%s, investor=%s, error=%s",
            market_param,
            investor,
            e,
        )

    return pd.DataFrame()


def debug_daum_investor_purchase_response(interval_type: str = "TODAY") -> dict:
    """
    [진단 전용] investor_purchase API의 실제 JSON 응답 키 구조를
    확인합니다. fetch_daum_deal_ranking()의 필드 매핑이 정확한지
    검증하기 위한 일회성 도구입니다.
    """
    url = "https://finance.daum.net/api/trend/investor_purchase/"
    params = {
        "buyFieldName": "straightPurchasePrice",
        "buyOrder": "desc",
        "sellFieldName": "straightPurchasePrice",
        "sellOrder": "asc",
        "limit": 5,
        "market": "KOSPI",
        "investorType": "FOREIGN",
        "intervalType": interval_type,
    }
    headers = {
        **COMMON_HEADERS,
        "Referer": "https://finance.daum.net/domestic/influential_investors",
        "Accept": "application/json, text/plain, */*",
    }

    try:
        resp = get_session().get(url, headers=headers, params=params, timeout=8)
        return {
            "status_code": resp.status_code,
            "body": resp.json() if resp.status_code == 200 else resp.text[:500],
        }
    except Exception as e:
        return {"error": str(e)}


# ==============================================================================
# 5. Naver 실시간 API (시장 전체 랭킹)
# ==============================================================================
def fetch_naver_html_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    """
    ⚠️ 주의: 이 함수도 target_date를 실제로 조회 조건에 사용하지 않습니다.
    Naver iframe 페이지는 "현재 시점"의 최신 순위만 제공합니다.
    반드시 get_market_radar_scanner()에서 "오늘" 조회일 때만 호출하세요.
    """
    sosok = "01" if "KOSPI" in market.upper() or "코스피" in market else "02"
    inv_code = NAVER_INVESTOR_MAP.get(investor)

    if inv_code is None:
        logger.warning("Naver 미지원 투자주체: %s (Daum/PyKrx로 대체됩니다)", investor)
        return pd.DataFrame()

    buy_sell = "buy" if trade_type == "순매수" else "sell"

    url = (
        "https://finance.naver.com/sise/sise_deal_rank_iframe.naver"
        f"?sosok={sosok}&investor_gubun={inv_code}&type={buy_sell}"
    )

    try:
        html = _fetch_rendered_html(url, wait_selector="table")
        soup = BeautifulSoup(html, "html.parser")

        candidate_tables = soup.find_all("table", {"class": "type_1"})
        if not candidate_tables:
            candidate_tables = soup.find_all("table")

        if not candidate_tables:
            return pd.DataFrame()

        best_table = None
        best_row_count = 0

        for table in candidate_tables:
            row_count = sum(
                1 for row in table.find_all("tr")
                if len(row.find_all("td")) >= 4 and row.find("a")
            )
            if row_count > best_row_count:
                best_row_count = row_count
                best_table = table

        if best_table is None:
            return pd.DataFrame()

        records = []
        rank = 1
        for row in best_table.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) >= 4:
                name_tag = cols[1].find("a")
                if name_tag:
                    href = name_tag.get("href", "")
                    code_match = re.search(r'code=(\d+)', href)
                    if not code_match:
                        continue
                    code = code_match.group(1)
                    name = name_tag.text.strip()

                    def clean(x):
                        try:
                            return float(x.text.replace(",", "").replace("+", "").replace("%", "").strip())
                        except Exception:
                            return 0.0

                    price = clean(cols[2])
                    change_pct = clean(cols[4]) if len(cols) > 4 else 0.0
                    net_amt_raw = clean(cols[7]) if len(cols) >= 8 else clean(cols[3])
                    net_amt_eok = round(net_amt_raw / 100.0, 1) if net_amt_raw > 1000 else round(net_amt_raw, 1)
                    if trade_type == "순매도":
                        net_amt_eok = -abs(net_amt_eok)

                    records.append({
                        "순위": rank,
                        "종목코드": code,
                        "종목명": name,
                        "현재가": price,
                        "등락률(%)": change_pct,
                        "순매수대금(억)": net_amt_eok,
                        "시가총액_가중": max(price * 1000, 500),
                        "데이터_출처": f"Naver 실시간 (렌더링) ({target_date})"
                    })
                    rank += 1
                    if rank > top_n:
                        break

        if records:
            return pd.DataFrame(records)

    except Exception as e:
        logger.warning(f"Naver 렌더링 스크래핑 실패: {e}")

    return pd.DataFrame()

# ==============================================================================
# 6. PyKrx 엔진 (시장 전체 랭킹, 최종 폴백)
# ==============================================================================
def fetch_pykrx_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    """
    PyKrx로 투자주체별 매매상위 종목을 조회합니다.

    파라미터:
        target_date : "YYYYMMDD" 문자열. **과거 날짜 조회가 가능한
                      유일한 소스**입니다.
        market      : "KOSPI" | "KOSDAQ".
        investor    : "외국인" | "기관" 등.
        trade_type  : "순매수" | "순매도".
        top_n       : 가져올 상위 종목 수.

    반환값:
        수급 랭킹 DataFrame. 실패하거나 결과가 없으면 빈 DataFrame.

    주의사항:
        - PyKrx는 **KRX 웹을 비공식으로 긁는 라이브러리**입니다. KRX가
          응답 형식을 바꾸면 통째로 죽고, 실제로 그런 적이 있습니다.
          이 소스가 조용히 빈 결과만 주고 있다면 라이브러리 버전을
          먼저 의심하세요.
        - 그래서 마지막 수단으로 우리가 쌓아 온 누적 이력(observations)이
          있습니다. Naver·Daum이 과거 조회를 지원하지 않아 과거 데이터는
          PyKrx 하나에 기대고 있었기 때문입니다.
        - PYKRX_AVAILABLE이 False면(미설치) 호출 자체를 건너뜁니다.
    """
    if not PYKRX_AVAILABLE:
        logger.warning("PyKrx 조회 실패: pykrx 패키지가 설치되지 않았습니다.")
        return pd.DataFrame()

    mkt = "KOSPI" if "KOSPI" in market.upper() else "KOSDAQ"
    inv_map = {"외국인": "외국인", "기관": "기관합계", "연기금": "연기금", "금융투자": "금융투자", "투신": "투신", "개인": "개인"}
    inv = inv_map.get(investor, "외국인")

    try:
        df = stock.get_market_net_purchases_of_equities_by_ticker(target_date, target_date, mkt, inv)
        if df.empty:
            logger.warning(f"PyKrx 조회 실패 (날짜={target_date}): 순매수 데이터가 비어있습니다 (휴장일이거나 데이터 미제공).")
            return pd.DataFrame()

        df = df.reset_index().rename(columns={"티커": "종목코드"})
        if trade_type == "순매수":
            df = df[df["순매수거래대금"] > 0].sort_values("순매수거래대금", ascending=False).head(top_n)
        else:
            df = df[df["순매수거래대금"] < 0].sort_values("순매수거래대금", ascending=True).head(top_n)

        if df.empty:
            logger.warning(f"PyKrx 조회 실패 (날짜={target_date}): {trade_type} 기준을 만족하는 종목이 없습니다.")
            return pd.DataFrame()

        prices_df = stock.get_market_ohlcv(target_date, target_date, mkt)

        records = []
        rank = 1
        for _, row in df.iterrows():
            code = row["종목코드"]
            name = row["종목명"]
            amt_eok = round(row["순매수거래대금"] / 100000000.0, 1)

            price, fluc = 0, 0.0
            if prices_df is not None and not prices_df.empty and code in prices_df.index:
                p_row = prices_df.loc[code]
                price = float(p_row["종가"])
                fluc = float(p_row["등락률"])

            records.append({
                "순위": rank,
                "종목코드": code,
                "종목명": name,
                "현재가": price,
                "등락률(%)": fluc,
                "순매수대금(억)": amt_eok,
                "시가총액_가중": max(price * 1000, 500),
                "데이터_출처": f"PyKrx API ({target_date})"
            })
            rank += 1
        return pd.DataFrame(records)
    except Exception as e:
        logger.warning(f"PyKrx 조회 실패 (날짜={target_date}): {e}")
        return pd.DataFrame()


# ==============================================================================
# 7. 무중단 파이프라인 마스터 함수 (Smart Fallback, 시장 전체 랭킹)
#
# 장중 (평일 09:00~15:30):
#   KIS(가집계) -> Daum(실시간) -> Naver(실시간)
#
# 장마감 후 / 과거 영업일 (확정 데이터 우선):
#   Naver(확정) -> Daum(확정) -> PyKrx(확정, 최종 폴백)
#
# KIS는 장중 전용 TR이라 장마감 후에는 호출하지 않습니다.
# KRX 공식 OpenAPI는 투자자별 컬럼을 제공하지 않아 사용하지 않습니다.
# ==============================================================================
def _get_latest_completed_session_str(now_kst: datetime) -> str:
    """
    지금 이 시점에 Naver/Daum이 '현재 데이터'로 보여주는 거래일을
    계산합니다. Naver/Daum은 날짜 파라미터가 없으므로, 항상
    "가장 최근에 끝난 거래일"의 확정 데이터만 제공합니다.

    - 장중(평일 09:00~15:30): 오늘 (실시간 반영 중)
    - 장 마감 후(평일 15:30~24:00): 오늘 (당일 확정)
    - 장 시작 전(평일 00:00~09:00) 또는 주말: 가장 최근 평일
    """
    current_time = now_kst.time()
    is_weekday = now_kst.weekday() < 5

    if is_weekday and current_time >= time(9, 0):
        return now_kst.strftime("%Y%m%d")

    d = now_kst.date() - timedelta(days=1)
    while d.weekday() >= 5:  # 토(5)/일(6) 건너뛰기
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _previous_business_day(date_obj):
    """
    직전 영업일(주말을 건너뛴 전날)을 돌려줍니다.

    파라미터:
        date_obj : datetime.date 또는 datetime.datetime.

    반환값:
        하루 이상 뒤로 물러난 같은 타입의 날짜. 토·일은 건너뜁니다.

    주의사항:
        - 주말만 압니다. **공휴일은 모릅니다.** 설·추석처럼 며칠씩
          쉬는 구간에서는 여전히 빈 날짜를 조회하게 됩니다. 한국거래소
          휴장일 달력을 들이면 더 정확해지지만, 그 달력을 어디서 받아
          어떻게 갱신할지가 또 하나의 외부 의존이 되므로 지금은 값이
          가장 큰 주말만 처리합니다.
        - 폴백 루프의 "최대 7회" 예산을 실제 영업일 7일에 쓰기 위한
          함수입니다. 예전에는 달력 날짜로 하루씩 물러나서, 월요일에
          조회하면 7회 중 2회를 토·일에 날렸습니다. 그 두 번은 반드시
          빈 결과이므로 순수한 왕복 낭비였습니다.
    """
    previous = date_obj - timedelta(days=1)
    while previous.weekday() >= 5:          # 5=토, 6=일
        previous -= timedelta(days=1)
    return previous


def collect_market_radar_scanner(
    target_date_obj,
    market: str = "KOSPI",
    investor: str = "외국인",
    trade_type: str = "순매수",
    top_n: int = 30,
    interval_type: str = "TODAY",
) -> pd.DataFrame:
    """
    투자주체별 수급 상위 종목을 실제로 수집합니다.

    파라미터:
        target_date_obj : 조회 기준일(date). 여기서부터 과거로 물러납니다.
        market          : "KOSPI" | "KOSDAQ".
        investor        : "외국인" | "기관" 등 투자주체.
        trade_type      : "순매수" | "순매도".
        top_n           : 가져올 상위 종목 수.
        interval_type   : "TODAY"(당일) 또는 기간 집계 코드.
                          TODAY가 아니면 Daum 기간 집계를 먼저 시도하고,
                          실패하면 당일 경로로 자동 전환합니다.

    반환값:
        수급 랭킹 DataFrame. 모든 소스가 실패하면 **빈 DataFrame**입니다
        (None이 아닙니다). 호출부는 .empty로 판정하세요.

    주의사항:
        - **항상 네트워크를 씁니다.** 화면은 저장본을 우선 읽는
          get_market_radar_scanner()를 쓰세요.
        - 소스 우선순위는 KIS → Daum → Naver → LS → PyKrx이고, 전부
          실패하면 마지막으로 우리가 쌓아 둔 누적 이력(observations)을
          씁니다. Naver·Daum은 **과거 날짜 조회를 지원하지 않아서**,
          기준일이 최근 거래일이 아니면 아예 건너뜁니다.
        - 최악의 경우 영업일 7일 × 소스 5개를 시도하므로 **매우 느릴 수
          있습니다.** 주말은 건너뛰지만(_previous_business_day) 공휴일은
          모릅니다.
        - 성공한 소스를 로그에 남깁니다. 화면 숫자가 이상할 때 어느
          소스에서 왔는지부터 확인하세요.
    """
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_str = now_kst.strftime("%Y%m%d")
    current_time = now_kst.time()

    is_regular_session = (
        now_kst.weekday() < 5
        and time(9, 0) <= current_time < time(15, 30)
    )

    latest_completed_str = _get_latest_completed_session_str(now_kst)

    if interval_type != "TODAY":
        search_date_str = target_date_obj.strftime("%Y%m%d")
        df = fetch_daum_deal_ranking(
            search_date_str, market, investor, trade_type, top_n,
            interval_type=interval_type,
        )
        if df is not None and not df.empty:
            return df

        logger.warning(
            "Daum 기간별(%s) 조회 실패. 당일 데이터로 자동 전환합니다.",
            interval_type,
        )

    current_date_obj = target_date_obj
    max_lookback_days = 7

    for _ in range(max_lookback_days):
        search_date_str = current_date_obj.strftime("%Y%m%d")
        is_today = search_date_str == today_str
        is_live_session = is_today and is_regular_session

        can_use_intraday_sources = (
            is_live_session or is_today or search_date_str == latest_completed_str
        )

        if is_live_session:
            df = fetch_kis_deal_ranking(
                search_date_str, market, investor, trade_type, top_n,
            )
            if df is not None and not df.empty:
                return df

        if can_use_intraday_sources:
            df = fetch_daum_deal_ranking(
                search_date_str, market, investor, trade_type, top_n,
                interval_type="TODAY",
            )
            if df is not None and not df.empty:
                return df

            df = fetch_naver_html_ranking(
                search_date_str,
                market,
                investor,
                trade_type,
                top_n,
            )
            
            if df is not None and not df.empty:
                logger.info(
                    "수급 레이더 성공: source=Naver, date=%s, market=%s, investor=%s, trade_type=%s, rows=%s",
                    search_date_str,
                    market,
                    investor,
                    trade_type,
                    len(df),
                )
                return df
            
            logger.warning(
                "수급 레이더 Naver 실패 또는 빈 결과: date=%s, market=%s, investor=%s, trade_type=%s",
                search_date_str,
                market,
                investor,
                trade_type,
            )
        else:
            logger.info(
                "과거 날짜(%s) 조회: 날짜 미지원 소스(Naver/Daum) 건너뛰고 "
                "PyKrx만 사용",
                search_date_str,
            )

        # LS증권 OPEN API.
        #
        # 이 함수는 오랫동안 정의만 되어 있고 **어디에서도 호출되지
        # 않았습니다.** 그래서 LS 키를 아무리 정확히 넣어도 화면 데이터는
        # 하나도 달라지지 않았습니다. 연결 진단만 LS를 찌르고 있었으니,
        # 사용자 입장에서는 "연결도 안 되고 쓰이지도 않는" 상태였습니다.
        #
        # 이미 잘 동작하는 KIS/Daum/Naver 뒤에 둡니다. 그 셋 중 하나라도
        # 성공하면 LS는 호출되지 않으므로 기존 동작을 해치지 않고,
        # 셋이 모두 실패했을 때만 한 번 더 기회를 줍니다.
        if can_use_intraday_sources:
            df = fetch_ls_deal_ranking(
                search_date_str, market, investor, trade_type, top_n,
            )
            if df is not None and not df.empty:
                logger.info(
                    "수급 레이더 성공: source=LS, date=%s, market=%s, "
                    "investor=%s, trade_type=%s, rows=%s",
                    search_date_str, market, investor, trade_type, len(df),
                )
                return df

        if PYKRX_AVAILABLE:
            df = fetch_pykrx_deal_ranking(
                search_date_str, market, investor, trade_type, top_n,
            )

            if df is not None and not df.empty:
                logger.info(
                    "수급 레이더 성공: source=PyKrx, date=%s, market=%s, "
                    "investor=%s, trade_type=%s, rows=%s",
                    search_date_str, market, investor, trade_type, len(df),
                )
                return df

            logger.warning(
                "수급 레이더 PyKrx 실패 또는 빈 결과: date=%s, market=%s, "
                "investor=%s, trade_type=%s",
                search_date_str, market, investor, trade_type,
            )

        current_date_obj = _previous_business_day(current_date_obj)

    # ------------------------------------------------------------------
    # 마지막 수단: 수집기가 쌓아 온 우리 자신의 누적 이력.
    #
    # Naver·Daum은 과거 날짜 조회를 지원하지 않아 과거 조회는 PyKrx 하나에
    # 기대고 있었는데, PyKrx는 KRX 웹을 비공식으로 긁는 라이브러리라 KRX가
    # 응답 형식을 바꾸면 통째로 죽습니다(실제로 그런 상태입니다).
    #
    # 다행히 수집기가 돌 때마다 그날의 랭킹을 observations에 적재해 왔습니다.
    # 외부에서 다시 받을 수 없는 데이터를 우리가 이미 갖고 있으므로,
    # 빈 화면을 보여주는 대신 그것을 씁니다.
    # ------------------------------------------------------------------
    history = _read_ranking_from_history(
        target_date_obj, market, investor, trade_type, top_n,
    )
    if history is not None and not history.empty:
        logger.info(
            "수급 레이더: 외부 소스가 모두 실패해 누적 이력으로 대체합니다 "
            "(date=%s, rows=%s)",
            target_date_obj, len(history),
        )
        return history

    logger.error(
        "수급 스캐너 완전 실패: 시작일=%s, 시장=%s, 투자주체=%s, 방향=%s, 기간=%s "
        "(PYKRX_AVAILABLE=%s, 누적 이력에도 없음)",
        target_date_obj, market, investor, trade_type, interval_type,
        PYKRX_AVAILABLE,
    )
    return pd.DataFrame()


def _read_ranking_from_history(
    target_date_obj,
    market: str,
    investor: str,
    trade_type: str,
    top_n: int,
):
    """
    누적 이력(observations)에서 해당 조건의 랭킹을 꺼냅니다.

    요청한 날짜에 이력이 없으면 **그보다 앞선 가장 가까운 거래일**을 씁니다.
    어느 날짜를 썼는지는 '데이터_출처'에 남겨 화면이 밝힐 수 있게 합니다.
    """
    try:
        target_str = target_date_obj.strftime("%Y-%m-%d")
        available = [d for d in list_radar_history_dates() if d <= target_str]
        if not available:
            return None

        picked = max(available)
        df = read_radar_history(
            market=market,
            investor=investor,
            trade_type=trade_type,
            start_date=picked,
        )
        if df is None or df.empty:
            return None

        if "obs_date" in df.columns:
            df = df[df["obs_date"] == picked]
        if df.empty:
            return None

        if "순매수대금(억)" in df.columns:
            df = df.sort_values(
                "순매수대금(억)", ascending=(trade_type == "순매도"),
            )

        df = df.head(top_n).copy()
        df["데이터_출처"] = (
            f"누적 이력 (수집기가 {picked}에 저장한 값 · 외부 소스 전부 실패)"
        )
        return df.reset_index(drop=True)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("누적 이력 조회 실패: %s", e)
        return None


# ==============================================================================
# 7-1. 저장본 우선 읽기 경로 + 날짜별 이력 누적
# ==============================================================================
@st.cache_data(ttl=60, show_spinner=False)
def get_market_radar_scanner(
    target_date_obj,
    market: str = "KOSPI",
    investor: str = "외국인",
    trade_type: str = "순매수",
    top_n: int = 30,
    interval_type: str = "TODAY",
) -> pd.DataFrame:
    """
    화면용 진입점. 저장본 우선 + 날짜별 이력 누적.

    이 화면이 저장 계층에서 가장 크게 이득을 봅니다.
      - 수집 경로가 무거움: 최대 7영업일 × (Daum → Naver 렌더링 → PyKrx).
        저장본이 있으면 이 전부를 건너뜁니다.
      - **Naver/Daum은 과거 날짜 조회를 지원하지 않습니다.** 지금까지는 앱을
        끄면 그날 수급이 사라졌지만, 이제 수집할 때마다 observations에
        날짜별로 쌓이므로 이력을 직접 축적합니다.
        (read_radar_history()로 조회)
    """
    snap_name = datasets.snap_radar_scanner(
        market, investor, trade_type, interval_type,
    )

    def _collect():
        df = collect_market_radar_scanner(
            target_date_obj, market, investor, trade_type, top_n, interval_type,
        )
        if df is not None and not df.empty:
            _accumulate_radar_history(
                df, market, investor, trade_type, interval_type,
            )
        return df

    df = store.cached_or_live(
        snap_name,
        _collect,
        max_age_seconds=datasets.MAX_AGE_REALTIME,
        as_frame=True,
    )
    return df if df is not None else pd.DataFrame()


def _accumulate_radar_history(
    df: pd.DataFrame,
    market: str,
    investor: str,
    trade_type: str,
    interval_type: str,
) -> None:
    """
    수집된 랭킹을 "데이터 기준 거래일"로 observations에 누적합니다.

    기준일은 수집 시각이 아니라 _get_latest_completed_session_str()이
    계산한 거래일을 씁니다. Naver/Daum이 날짜 파라미터를 받지 않고
    "가장 최근에 끝난 거래일"만 주기 때문에, 수집 시각으로 찍으면
    토요일 새벽에 수집한 금요일 데이터가 토요일로 기록됩니다.
    """
    try:
        session_str = _get_latest_completed_session_str(
            datetime.now(ZoneInfo("Asia/Seoul"))
        )
        obs_date = (
            f"{session_str[:4]}-{session_str[4:6]}-{session_str[6:8]}"
        )

        records = df.to_dict(orient="records")
        for rec in records:
            rec["시장"] = market
            rec["투자주체"] = investor
            rec["매매구분"] = trade_type
            rec["기간구분"] = interval_type

        # 같은 거래일에 조건별로 여러 건이 들어오므로, entity에 조건을
        # 포함해야 서로 덮어쓰지 않습니다.
        for rec in records:
            rec["_entity"] = (
                f"{market}|{investor}|{trade_type}|{interval_type}|"
                f"{rec.get('종목코드', '?')}"
            )

        saved = store.put_observations(
            datasets.OBS_RADAR, obs_date, records, entity_key="_entity",
        )
        logger.info(
            "수급 레이더 이력 누적: date=%s, rows=%s (%s/%s/%s/%s)",
            obs_date, saved, market, investor, trade_type, interval_type,
        )
    except Exception as e:
        # 누적 실패가 화면을 막아서는 안 됩니다.
        logger.warning("수급 레이더 이력 누적 실패: %s", e)


def read_radar_history(
    *,
    market: str | None = None,
    investor: str | None = None,
    trade_type: str | None = None,
    start_date: str | None = None,
) -> pd.DataFrame:
    """
    누적된 수급 랭킹 이력을 조회합니다 (Naver/Daum이 제공하지 않는 과거 데이터).

    start_date는 'YYYY-MM-DD' 형식입니다.
    """
    df = store.read_observations(datasets.OBS_RADAR, start_date=start_date)
    if df.empty:
        return df

    if market and "시장" in df.columns:
        df = df[df["시장"] == market]
    if investor and "투자주체" in df.columns:
        df = df[df["투자주체"] == investor]
    if trade_type and "매매구분" in df.columns:
        df = df[df["매매구분"] == trade_type]

    return df.drop(columns=[c for c in ["_entity"] if c in df.columns])


def list_radar_history_dates() -> list[str]:
    """이력이 쌓인 거래일 목록."""
    return store.list_observation_dates(datasets.OBS_RADAR)


# ==============================================================================
# 8. Daum 종목 페이지 실제 투자자 순매매 데이터 (pykrx 교차 검증용 독립 소스)
# ==============================================================================
def fetch_daum_investor_daily_history(stock_code: str, start_date_obj, end_date_obj) -> pd.DataFrame:
    """
    Daum 금융의 종목 상세 페이지(quotes/A{코드})에서 "외국인·기관" 테이블을
    스크래핑합니다. pykrx와 완전히 독립된 소스이므로 교차 검증에 사용합니다.

    주의: 비공식 스크래핑이며, Daum이 페이지에 노출하는 기간은 보통 최근
    며칠~1개월 수준으로 제한적입니다.
    """
    headers = {
        **COMMON_HEADERS,
        "Referer": "https://finance.daum.net/",
    }

    ticker_code = stock_code.replace(".KS", "").replace(".KQ", "")
    url = f"https://finance.daum.net/quotes/A{ticker_code}"

    try:
        res = get_session().get(url, headers=headers, timeout=8)
        if res.status_code != 200:
            logger.warning(f"Daum 종목 페이지 실패 (종목={stock_code}): HTTP {res.status_code}")
            return pd.DataFrame()

        soup = BeautifulSoup(res.text, "html.parser")

        target_table = None
        for table in soup.find_all("table"):
            header_text = table.get_text()
            if "외국인" in header_text and "기관" in header_text and "일자" in header_text:
                target_table = table
                break

        if target_table is None:
            logger.warning(f"Daum 종목 페이지: 외국인·기관 테이블을 찾지 못했습니다 (종목={stock_code}).")
            return pd.DataFrame()

        rows = target_table.find_all("tr")
        if len(rows) < 2:
            return pd.DataFrame()

        def parse_signed_number(cell) -> float:
            text = cell.get_text().strip()
            is_negative = (
                "down" in " ".join(cell.get("class", [])).lower()
                or bool(cell.find(class_=re.compile("down", re.I)))
                or text.startswith("-")
                or text.startswith("▼")
            )
            cleaned = re.sub(r"[^\d.]", "", text)
            if not cleaned:
                return 0.0
            val = float(cleaned)
            return -val if is_negative else val

        records = []
        for row in rows[1:]:
            cols = row.find_all("td")
            if len(cols) < 6:
                continue

            date_text = cols[0].get_text().strip()
            try:
                month, day = date_text.split(".")
                ref_year = datetime.now(ZoneInfo("Asia/Seoul")).year
                row_date = datetime(ref_year, int(month), int(day)).date()
                if row_date > datetime.now(ZoneInfo("Asia/Seoul")).date():
                    row_date = row_date.replace(year=ref_year - 1)
            except Exception:
                continue

            foreigner_shares = parse_signed_number(cols[1])
            institution_shares = parse_signed_number(cols[3])

            close_text = re.sub(r"[^\d.]", "", cols[4].get_text().strip())
            close_price = float(close_text) if close_text else 0.0

            if close_price <= 0:
                continue

            records.append({
                "Date": pd.Timestamp(row_date),
                "Close": close_price,
                "Foreigner_Shares": foreigner_shares,
                "Institution_Shares": institution_shares,
            })

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records).sort_values("Date").reset_index(drop=True)
        df["Foreigner_Daily"] = df["Foreigner_Shares"] * df["Close"]
        df["Institution_Daily"] = df["Institution_Shares"] * df["Close"]

        mask = (
            (df["Date"].dt.date >= start_date_obj)
            & (df["Date"].dt.date <= end_date_obj)
        )
        df = df.loc[mask].reset_index(drop=True)

        return df[["Date", "Close", "Foreigner_Daily", "Institution_Daily"]]

    except Exception as e:
        logger.warning(f"Daum 종목 페이지 스크래핑 실패 (종목={stock_code}): {e}")
        return pd.DataFrame()


def _fetch_pykrx_investor_history(ticker_code: str, start_str: str, end_str: str) -> pd.DataFrame:
    if not PYKRX_AVAILABLE:
        return pd.DataFrame()

    try:
        df = stock.get_market_trading_value_by_date(start_str, end_str, ticker_code)
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.reset_index().rename(columns={"날짜": "Date"})
        df["Date"] = pd.to_datetime(df["Date"])

        col_map = {"외국인합계": "Foreigner_Daily", "기관합계": "Institution_Daily"}
        for src_col, new_col in col_map.items():
            df[new_col] = df[src_col] if src_col in df.columns else 0.0

        price_df = stock.get_market_ohlcv_by_date(start_str, end_str, ticker_code)
        if price_df is not None and not price_df.empty:
            price_df = price_df.reset_index().rename(columns={"날짜": "Date", "종가": "Close"})
            price_df["Date"] = pd.to_datetime(price_df["Date"])
            df = df.merge(price_df[["Date", "Close"]], on="Date", how="left")
        else:
            df["Close"] = None

        return df[["Date", "Close", "Foreigner_Daily", "Institution_Daily"]]
    except Exception as e:
        logger.warning(f"pykrx 수집 실패 (종목={ticker_code}): {e}")
        return pd.DataFrame()


def _cross_validate_investor_data(pykrx_df: pd.DataFrame, daum_df: pd.DataFrame) -> tuple[bool, float]:
    """
    두 독립 소스(pykrx, Daum)의 외국인 순매매 방향(+/-)이 겹치는 날짜에서
    얼마나 일치하는지 계산합니다.

    반환: (is_validated, agreement_ratio)
    """
    if pykrx_df.empty or daum_df.empty:
        return False, 0.0

    merged = pd.merge(
        pykrx_df[["Date", "Foreigner_Daily"]].rename(columns={"Foreigner_Daily": "F_pykrx"}),
        daum_df[["Date", "Foreigner_Daily"]].rename(columns={"Foreigner_Daily": "F_daum"}),
        on="Date",
        how="inner",
    )

    if merged.empty or len(merged) < 2:
        return False, 0.0

    same_sign = (
        (merged["F_pykrx"] > 0) & (merged["F_daum"] > 0)
    ) | (
        (merged["F_pykrx"] < 0) & (merged["F_daum"] < 0)
    )

    agreement_ratio = same_sign.mean()
    is_validated = agreement_ratio >= 0.7

    return is_validated, float(agreement_ratio)


# ==============================================================================
# 9. 기준일(0점) 누적 수급 데이터 로더 (pykrx ↔ Daum 실제 교차 검증)
# ==============================================================================
def estimate_flow_by_price_volume_heuristic(stock_code: str, start_date_obj, end_date_obj) -> pd.DataFrame:
    """
    주의: 이 함수는 실제 투자자별 데이터가 아닙니다.
    pykrx와 Daum 교차 검증이 모두 실패했을 때만 호출되는, 가격 변동률과
    거래량만으로 만든 통계적 추정치이며, 실제 KRX 공시 수급과 다를 수 있습니다.
    """
    ticker_str = f"{stock_code}.KS" if not stock_code.endswith((".KS", ".KQ")) else stock_code

    try:
        start_str = start_date_obj.strftime("%Y-%m-%d")
        end_str = (end_date_obj + timedelta(days=1)).strftime("%Y-%m-%d")

        tk = yf.Ticker(ticker_str)
        df = tk.history(start=start_str, end=end_str)

        if not df.empty:
            df = df.reset_index()
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
            elif "Datetime" in df.columns:
                df["Date"] = pd.to_datetime(df["Datetime"]).dt.tz_localize(None)

            pct_change = df["Close"].pct_change().fillna(0)
            vol = df["Volume"]

            df["Foreigner_Daily"] = (pct_change * vol * df["Close"] * 0.000000035).round(1)
            df["Institution_Daily"] = (pct_change.shift(1).fillna(0) * vol * df["Close"] * 0.00000002).round(1)
            df["Retail_Daily"] = (-df["Foreigner_Daily"] - df["Institution_Daily"]).round(1)

            df["Foreigner_Cum"] = df["Foreigner_Daily"].cumsum().round(1)
            df["Institution_Cum"] = df["Institution_Daily"].cumsum().round(1)
            df["Retail_Cum"] = df["Retail_Daily"].cumsum().round(1)

            df["is_estimated"] = True
            df["source"] = "가격/거래량 기반 통계적 추정치"
            df["cross_validated"] = False

            return df[[
                "Date", "Close", "Foreigner_Daily", "Institution_Daily", "Retail_Daily",
                "Foreigner_Cum", "Institution_Cum", "Retail_Cum", "is_estimated",
                "source", "cross_validated"
            ]]
    except Exception as e:
        logger.error(f"Fallback 수급 추정치 생성 실패: {e}")

    return pd.DataFrame()


# ==============================================================================
# Daum 종목별 외국인/기관 실제 누적 수급 수집
# ==============================================================================
DAUM_STOCK_INVESTOR_URL = "https://finance.daum.net/api/investor/days"


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_daum_stock_investor_flow(
    stock_code: str,
    start_date_obj,
    end_date_obj,
) -> pd.DataFrame:
    """
    Daum 금융 종목별 외국인/기관 매매 페이지의 실제 내부 API에서
    일별 외국인·기관 순매수(수량)와 종가를 가져와 기준일 대비 누적치를
    직접 계산합니다.

    실제 확인된 엔드포인트:
        https://finance.daum.net/api/investor/days
        ?page=1&perPage=30&symbolCode=A005930&pagination=true

    실제 확인된 응답 필드:
        date, foreignStraightPurchaseVolume, institutionStraightPurchaseVolume,
        tradePrice, foreignOwnShares, foreignOwnSharesRate

    [성능 개선]
    기존에는 1페이지 결과의 totalPages/조기종료 조건을 확인한 뒤에야
    다음 페이지를 요청할 수 있어(while page <= max_pages) 순차적으로만
    호출할 수 있었습니다.

    이제는 1페이지를 먼저 요청해 totalPages와 조기 종료 시점을 확인한 뒤,
    필요한 나머지 페이지 수를 한 번에 계산해 ThreadPoolExecutor로
    동시에 요청합니다. 1페이지가 이미 충분하면(조기 종료) 추가 요청 없이
    즉시 반환합니다.

    주의:
    - 개인(리테일) 순매수는 이 API가 직접 제공하지 않습니다.
      역산해서 만들어내지 않고, 외국인·기관만 정확하게 표시합니다.
    - Daum 공식 API가 아닌 웹페이지 내부 요청이므로, 페이지 구조 변경 시
      실패할 수 있습니다.
    - 실패 시 빈 DataFrame을 반환하며, 호출부는 반드시 pykrx 등 기존
      폴백 경로를 유지해야 합니다.
    """
    symbol_code = stock_code if stock_code.startswith("A") else f"A{stock_code}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": f"https://finance.daum.net/quotes/{symbol_code}",
        "X-Requested-With": "XMLHttpRequest",
    }

    lookback_days = (
        datetime.now(ZoneInfo("Asia/Seoul")).date() - start_date_obj
    ).days
    per_page = max(min(int(lookback_days * 1.6) + 10, 300), 60)
    max_pages = 6

    def fetch_page(page: int):
        """단일 페이지를 요청합니다. 실패 시 None을 반환합니다."""
        try:
            response = get_session().get(
                DAUM_STOCK_INVESTOR_URL,
                headers=headers,
                params={
                    "page": page,
                    "perPage": per_page,
                    "symbolCode": symbol_code,
                    "pagination": "true",
                },
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(
                    "Daum 종목별 투자자 수급 API HTTP 실패: code=%s, page=%s, status=%s",
                    symbol_code, page, response.status_code,
                )
                return None
            return response.json()
        except Exception as e:
            logger.warning(
                "Daum 종목별 투자자 수급 페이지 요청 실패: code=%s, page=%s, error=%s",
                symbol_code, page, e,
            )
            return None

    try:
        # ------------------------------------------------------------------
        # 1단계: 1페이지를 먼저 요청해 totalPages와 조기 종료 여부를 확인합니다.
        # 다음 페이지를 몇 개 더 받아야 하는지는 1페이지 응답을 봐야 알 수
        # 있으므로, 이 요청만은 병렬화할 수 없습니다.
        # ------------------------------------------------------------------
        first_payload = fetch_page(1)
        if not first_payload:
            return pd.DataFrame()

        first_rows = first_payload.get("data", [])
        if not isinstance(first_rows, list) or not first_rows:
            logger.warning("Daum 종목별 투자자 수급 API 빈 응답: code=%s", symbol_code)
            return pd.DataFrame()

        all_rows = list(first_rows)
        total_pages = first_payload.get("totalPages", 1)

        oldest_date_in_page1 = None
        try:
            oldest_date_in_page1 = pd.to_datetime(first_rows[-1].get("date", "")).date()
        except Exception:
            pass

        already_enough = (
            oldest_date_in_page1 is not None
            and oldest_date_in_page1 <= start_date_obj
        )

        # ------------------------------------------------------------------
        # 2단계: 1페이지로 부족하면, 필요한 나머지 페이지를 한 번에 계산해
        # ThreadPoolExecutor로 동시에 요청합니다.
        # ------------------------------------------------------------------
        if not already_enough and total_pages > 1:
            remaining_pages = list(range(2, min(total_pages, max_pages) + 1))

            with ThreadPoolExecutor(max_workers=min(len(remaining_pages), 5)) as executor:
                future_map = {
                    executor.submit(fetch_page, page): page
                    for page in remaining_pages
                }
                page_results = {}
                for future in as_completed(future_map):
                    page = future_map[future]
                    payload = future.result()
                    if payload:
                        page_results[page] = payload.get("data", [])

            # 페이지 순서(최신→과거)를 그대로 유지하기 위해 페이지 번호 순으로 병합
            for page in sorted(page_results.keys()):
                rows = page_results[page]
                if isinstance(rows, list):
                    all_rows.extend(rows)

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df["Date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates(subset="Date").sort_values("Date").reset_index(drop=True)

        df = df.rename(columns={
            "tradePrice": "Close",
            "foreignStraightPurchaseVolume": "Foreigner",
            "institutionStraightPurchaseVolume": "Institution",
            "foreignOwnSharesRate": "ForeignOwnershipRate",
            "accTradeVolume": "AccTradeVolume",
        })

        for col in [
            "Close",
            "Foreigner",
            "Institution",
            "ForeignOwnershipRate",
            "AccTradeVolume",
        ]:
            if col not in df.columns:
                df[col] = 0.0
        
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce",
            ).fillna(0.0)

        mask = (
            (df["Date"].dt.date >= start_date_obj)
            & (df["Date"].dt.date <= end_date_obj)
        )
        df = df.loc[mask].reset_index(drop=True)

        if df.empty:
            logger.warning(
                "Daum 종목별 투자자 수급: 요청 기간 내 데이터 없음. code=%s, %s~%s",
                symbol_code, start_date_obj, end_date_obj,
            )
            return pd.DataFrame()

        df["Foreigner_Cum"] = df["Foreigner"].cumsum()
        df["Institution_Cum"] = df["Institution"].cumsum()
        df["Retail_Cum"] = np.nan  # Daum은 개인 순매수를 직접 제공하지 않음

        df["is_estimated"] = False
        df["cross_validated"] = False
        df["source"] = "Daum 종목별 외국인/기관 실데이터 (비공식, 개인 미제공)"

        logger.info(
            "Daum 종목별 투자자 수급 수집 성공: code=%s, rows=%s, %s~%s",
            symbol_code, len(df), df["Date"].min().date(), df["Date"].max().date(),
        )

        return df[[
            "Date",
            "Close",
            "Foreigner",
            "Institution",
            "ForeignOwnershipRate",
            "AccTradeVolume",
            "Foreigner_Cum",
            "Institution_Cum",
            "Retail_Cum",
            "is_estimated",
            "cross_validated",
            "source",
        ]]

    except Exception as e:
        logger.warning("Daum 종목별 투자자 수급 수집 실패: code=%s, error=%s", symbol_code, e)
        return pd.DataFrame()


# ==============================================================================
# 종목 수급 확증 점수
# ==============================================================================
def calculate_stock_flow_confirmation(
    flow_df: pd.DataFrame,
    rank: int,
    top_n: int,
    trade_type: str,
) -> dict:
    """
    선택 종목의 Daum 실제 외국인/기관 수급 시계열과 시장 전체 레이더 순위를
    결합하여 수급 확증 점수(0~100)를 계산합니다.

    점수 구성:
    - 시장 전체 순매수/순매도 랭킹: 최대 20점
    - 외국인 최근 5거래일 방향 일치: 최대 25점
    - 기관 최근 5거래일 방향 일치: 최대 25점
    - 외국인 보유율 변화: 최대 15점
    - 당일 거래량 대비 외국인+기관 수급 강도: 최대 15점

    주의:
    - 실제 Daum 종목별 수급 데이터일 때만 점수를 계산합니다.
    - 개인 수급은 Daum 종목별 API가 직접 제공하지 않으므로 점수에 사용하지 않습니다.
    - 순매수/순매도 화면 모두 동일한 규칙을 사용하도록 direction 계수를 적용합니다.
    """
    unavailable = {
        "available": False,
        "total_score": None,
        "grade": "평가 불가",
        "grade_color": "gray",
        "reason": "",
        "rank_score": 0,
        "foreign_score": 0,
        "institution_score": 0,
        "ownership_score": 0,
        "intensity_score": 0,
        "foreign_5d_sum": 0,
        "institution_5d_sum": 0,
        "foreign_aligned_days": 0,
        "institution_aligned_days": 0,
        "sample_days": 0,
        "ownership_change_bp": 0.0,
        "flow_intensity_pct": 0.0,
        "positive_reasons": [],
        "warning_reasons": [],
    }

    if flow_df is None or flow_df.empty:
        unavailable["reason"] = "종목별 수급 데이터가 없습니다."
        return unavailable

    source = str(
        flow_df["source"].iloc[0]
        if "source" in flow_df.columns
        else ""
    )
    is_estimated = bool(
        flow_df["is_estimated"].iloc[0]
        if "is_estimated" in flow_df.columns
        else True
    )

    # Daum 실데이터가 아니거나 통계적 추정치이면 점수를 산출하지 않습니다.
    if is_estimated or "Daum 종목별" not in source:
        unavailable["reason"] = (
            "수급 확증 점수는 Daum 종목별 외국인·기관 실데이터가 "
            "확보된 경우에만 계산합니다."
        )
        return unavailable

    required_columns = {
        "Date",
        "Foreigner",
        "Institution",
        "ForeignOwnershipRate",
        "AccTradeVolume",
    }

    missing_columns = required_columns - set(flow_df.columns)
    if missing_columns:
        unavailable["reason"] = (
            "확증 점수 계산에 필요한 데이터가 부족합니다: "
            + ", ".join(sorted(missing_columns))
        )
        return unavailable

    df = (
        flow_df.copy()
        .sort_values("Date")
        .drop_duplicates(subset=["Date"])
        .reset_index(drop=True)
    )

    recent_5d = df.tail(5).copy()
    sample_days = len(recent_5d)

    if sample_days < 2:
        unavailable["reason"] = (
            "최근 수급 데이터가 2거래일 미만이라 확증 점수를 계산할 수 없습니다."
        )
        return unavailable

    # 순매수 화면은 +1, 순매도 화면은 -1 방향을 확증 방향으로 사용합니다.
    direction = 1 if trade_type == "순매수" else -1

    foreign_5d_sum = float(recent_5d["Foreigner"].sum())
    institution_5d_sum = float(recent_5d["Institution"].sum())

    foreign_aligned_days = int(
        (direction * recent_5d["Foreigner"] > 0).sum()
    )
    institution_aligned_days = int(
        (direction * recent_5d["Institution"] > 0).sum()
    )

    # ------------------------------------------------------------------
    # 1. 시장 전체 레이더 순위 점수: 최대 20점
    # ------------------------------------------------------------------
    if top_n > 0 and rank > 0:
        rank_score = round(
            max(0.0, (top_n - rank + 1) / top_n) * 20
        )
    else:
        rank_score = 0

    # ------------------------------------------------------------------
    # 2. 5거래일 수급 일치 점수: 최대 25점
    # ------------------------------------------------------------------
    def directional_flow_score(
        flow_sum: float,
        aligned_days: int,
        max_score: int = 25,
    ) -> int:
        """
        누적 수급 방향이 레이더 방향과 일치해야 기본 점수를 얻고,
        같은 방향이었던 거래일 비율로 점수를 세분화합니다.
        """
        aligned_flow_sum = direction * flow_sum

        # 최근 5일 누적 방향이 레이더 방향과 반대면 확증 점수는 0점입니다.
        if aligned_flow_sum <= 0:
            return 0

        consistency = aligned_days / sample_days

        if consistency >= 0.8:
            return max_score
        if consistency >= 0.6:
            return round(max_score * 0.7)
        if consistency >= 0.4:
            return round(max_score * 0.4)

        return round(max_score * 0.2)

    foreign_score = directional_flow_score(
        foreign_5d_sum,
        foreign_aligned_days,
    )
    institution_score = directional_flow_score(
        institution_5d_sum,
        institution_aligned_days,
    )

    # ------------------------------------------------------------------
    # 3. 외국인 보유율 변화 점수: 최대 15점
    # Daum은 비율을 소수로 제공: 0.467 = 46.7%
    # 1bp = 0.01%p이므로 10,000을 곱합니다.
    # ------------------------------------------------------------------
    ownership_start = float(
        recent_5d["ForeignOwnershipRate"].iloc[0]
    )
    ownership_end = float(
        recent_5d["ForeignOwnershipRate"].iloc[-1]
    )
    ownership_change_bp = (
        ownership_end - ownership_start
    ) * 10_000

    aligned_ownership_bp = direction * ownership_change_bp

    if aligned_ownership_bp >= 10:
        ownership_score = 15
    elif aligned_ownership_bp >= 5:
        ownership_score = 10
    elif aligned_ownership_bp > 0:
        ownership_score = 5
    else:
        ownership_score = 0

    # ------------------------------------------------------------------
    # 4. 당일 거래량 대비 수급 강도: 최대 15점
    # 외국인+기관의 당일 순매수 수량 / 당일 전체 거래량
    # ------------------------------------------------------------------
    latest = recent_5d.iloc[-1]
    acc_trade_volume = float(latest["AccTradeVolume"])

    today_flow_volume = (
        float(latest["Foreigner"])
        + float(latest["Institution"])
    )

    if acc_trade_volume > 0:
        flow_intensity_pct = (
            direction
            * today_flow_volume
            / acc_trade_volume
            * 100
        )
    else:
        flow_intensity_pct = 0.0

    if flow_intensity_pct >= 3.0:
        intensity_score = 15
    elif flow_intensity_pct >= 1.5:
        intensity_score = 10
    elif flow_intensity_pct > 0:
        intensity_score = 5
    else:
        intensity_score = 0

    total_score = int(
        rank_score
        + foreign_score
        + institution_score
        + ownership_score
        + intensity_score
    )

    # ------------------------------------------------------------------
    # 5. 점수 등급
    # ------------------------------------------------------------------
    if total_score >= 80:
        grade = "강한 수급 확증"
        grade_color = "green"
    elif total_score >= 60:
        grade = "수급 확증 우위"
        grade_color = "blue"
    elif total_score >= 40:
        grade = "수급 혼조"
        grade_color = "gray"
    elif total_score >= 20:
        grade = "약한 수급 확증"
        grade_color = "orange"
    else:
        grade = "반대 수급 우세"
        grade_color = "red"

    positive_reasons = []
    warning_reasons = []

    if rank_score >= 15:
        positive_reasons.append(
            f"시장 전체 {trade_type} 상위 {rank}위"
        )
    elif rank_score <= 5:
        warning_reasons.append(
            f"시장 전체 순위가 {rank}위로 상대적으로 낮음"
        )

    if foreign_score >= 18:
        positive_reasons.append(
            f"외국인 최근 {sample_days}거래일 수급이 "
            f"{foreign_aligned_days}일 동일 방향"
        )
    elif foreign_score == 0:
        warning_reasons.append(
            "외국인 최근 누적 수급이 당일 레이더 방향과 반대"
        )

    if institution_score >= 18:
        positive_reasons.append(
            f"기관 최근 {sample_days}거래일 수급이 "
            f"{institution_aligned_days}일 동일 방향"
        )
    elif institution_score == 0:
        warning_reasons.append(
            "기관 최근 누적 수급이 당일 레이더 방향과 반대"
        )

    if ownership_score >= 10:
        positive_reasons.append(
            f"외국인 보유율 {ownership_change_bp:+.1f}bp 변화"
        )
    elif ownership_score == 0:
        warning_reasons.append(
            f"외국인 보유율 변화가 레이더 방향과 불일치 "
            f"({ownership_change_bp:+.1f}bp)"
        )

    if intensity_score >= 10:
        positive_reasons.append(
            f"거래량 대비 수급 강도 {flow_intensity_pct:+.2f}%"
        )
    elif intensity_score == 0:
        warning_reasons.append(
            f"당일 거래량 대비 수급 강도가 약함 "
            f"({flow_intensity_pct:+.2f}%)"
        )

    return {
        "available": True,
        "total_score": total_score,
        "grade": grade,
        "grade_color": grade_color,
        "reason": "",
        "rank_score": rank_score,
        "foreign_score": foreign_score,
        "institution_score": institution_score,
        "ownership_score": ownership_score,
        "intensity_score": intensity_score,
        "foreign_5d_sum": foreign_5d_sum,
        "institution_5d_sum": institution_5d_sum,
        "foreign_aligned_days": foreign_aligned_days,
        "institution_aligned_days": institution_aligned_days,
        "sample_days": sample_days,
        "ownership_change_bp": ownership_change_bp,
        "flow_intensity_pct": flow_intensity_pct,
        "positive_reasons": positive_reasons,
        "warning_reasons": warning_reasons,
    }


@st.cache_data(ttl=1800, show_spinner=False)
def get_stock_cumulative_flow_from_base(stock_code: str, start_date_obj, end_date_obj) -> pd.DataFrame:
    """
    외국인/기관/개인 누적 수급 시계열을 생성합니다.

    [교차 검증 방식]
    1. pykrx(1차)와 Daum 종목 페이지 스크래핑(2차, 완전 독립 소스)을 모두 수집합니다.
    2. 두 소스가 겹치는 날짜의 외국인 순매매 방향(+/-) 일치율을 계산합니다.
       - 일치율 70% 이상: cross_validated=True
       - 일치율 70% 미만/데이터 부족: cross_validated=False (경고와 함께 pykrx 값 사용)
    3. pykrx 자체가 실패하면 Daum 단독 데이터를 사용(단일 소스로 명시)
    4. 둘 다 실패하면 가격/거래량 기반 추정치(is_estimated=True)로 대체
    """

    daum_df = fetch_daum_stock_investor_flow(stock_code, start_date_obj, end_date_obj)
    if daum_df is not None and not daum_df.empty:
        return daum_df
    
    ticker_code = stock_code.replace('.KS', '').replace('.KQ', '')
    start_str = start_date_obj.strftime("%Y%m%d")
    end_str = end_date_obj.strftime("%Y%m%d")

    pykrx_raw = _fetch_pykrx_investor_history(ticker_code, start_str, end_str)
    daum_raw = fetch_daum_investor_daily_history(stock_code, start_date_obj, end_date_obj)

    if not pykrx_raw.empty:
        is_validated, agreement_ratio = _cross_validate_investor_data(pykrx_raw, daum_raw)

        df = pykrx_raw.copy()
        df["Retail_Daily"] = -(df["Foreigner_Daily"] + df["Institution_Daily"])
        df["Foreigner_Cum"] = df["Foreigner_Daily"].cumsum()
        df["Institution_Cum"] = df["Institution_Daily"].cumsum()
        df["Retail_Cum"] = df["Retail_Daily"].cumsum()

        df["is_estimated"] = False
        df["cross_validated"] = is_validated

        if is_validated:
            df["source"] = f"pykrx (실제 데이터, Daum 교차검증 일치율 {agreement_ratio:.0%})"
            logger.info(f"교차 검증 성공 (종목={stock_code}, 일치율={agreement_ratio:.0%})")
        elif not daum_raw.empty:
            df["source"] = f"pykrx (실제 데이터, Daum 교차검증 불일치율 높음: 일치율 {agreement_ratio:.0%})"
            logger.warning(f"교차 검증 불일치 (종목={stock_code}, 일치율={agreement_ratio:.0%})")
        else:
            df["source"] = "pykrx (실제 데이터, 단일 소스·교차검증 불가)"
            logger.info(f"Daum 데이터 부족으로 교차 검증 불가 (종목={stock_code})")

        return df[[
            "Date", "Close",
            "Foreigner_Daily", "Institution_Daily", "Retail_Daily",
            "Foreigner_Cum", "Institution_Cum", "Retail_Cum",
            "is_estimated", "source", "cross_validated",
        ]]

    if not daum_raw.empty and len(daum_raw) >= 2:
        df = daum_raw.copy()
        df["Retail_Daily"] = -(df["Foreigner_Daily"] + df["Institution_Daily"])
        df["Foreigner_Cum"] = df["Foreigner_Daily"].cumsum()
        df["Institution_Cum"] = df["Institution_Daily"].cumsum()
        df["Retail_Cum"] = df["Retail_Daily"].cumsum()

        df["is_estimated"] = False
        df["cross_validated"] = False
        df["source"] = "Daum 금융 (실제 데이터, pykrx 실패로 단일 소스 사용·교차검증 불가)"

        logger.warning(f"pykrx 실패, Daum 단독 데이터로 대체 (종목={stock_code})")

        return df[[
            "Date", "Close",
            "Foreigner_Daily", "Institution_Daily", "Retail_Daily",
            "Foreigner_Cum", "Institution_Cum", "Retail_Cum",
            "is_estimated", "source", "cross_validated",
        ]]

    logger.error(f"pykrx, Daum 모두 실패. 가격/거래량 기반 추정치로 대체합니다 (종목={stock_code}).")
    return estimate_flow_by_price_volume_heuristic(stock_code, start_date_obj, end_date_obj)

# 임시 함수
def debug_daum_investor_periods() -> dict:
    """
    [진단 전용] Daum 외국인/기관매매 페이지의 기간 선택 <select> 드롭다운을
    실제로 선택(select_option)했을 때, investor_purchase API 요청이
    어떻게 바뀌는지 옵션별로 구분해서 캡처합니다.
    """
    from playwright.sync_api import sync_playwright

    results_by_option = {}

    # 진단 전용 경로이므로 공용 브라우저 풀을 오염시키지 않도록
    # 일회성 브라우저를 쓰고, 예외가 나도 반드시 닫습니다.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=COMMON_HEADERS["User-Agent"])

            captured = []

            def on_request(req):
                if "investor_purchase" in req.url:
                    captured.append(req.url)

            page.on("request", on_request)

            page.goto(
                "https://finance.daum.net/domestic/influential_investors",
                wait_until="networkidle",
                timeout=15000,
            )
            page.wait_for_timeout(1000)

            # 페이지 안의 모든 select 요소와 그 안의 option 값을 먼저 조사
            select_info = page.evaluate(
                """
                () => {
                    const selects = Array.from(document.querySelectorAll('select'));
                    return selects.map(sel => ({
                        name: sel.name || sel.id || '(이름없음)',
                        options: Array.from(sel.options).map(o => ({
                            value: o.value,
                            text: o.text,
                        })),
                    }));
                }
                """
            )
            results_by_option["__select_구조__"] = select_info

            captured.clear()
            results_by_option["초기 로드(당일 추정)"] = list(dict.fromkeys(captured))

            # 기간 관련 값으로 추정되는 option value 시도
            candidate_values = ["TODAY", "5", "20", "DAYS_5", "DAYS_20"]

            for value in candidate_values:
                captured.clear()
                try:
                    page.select_option("select", value=value, timeout=3000)
                    page.wait_for_timeout(1500)
                    results_by_option[f"value={value}"] = list(
                        dict.fromkeys(captured)
                    )
                except Exception as e:
                    results_by_option[f"value={value}"] = [f"선택 실패: {e}"]

        finally:
            browser.close()

    return results_by_option
