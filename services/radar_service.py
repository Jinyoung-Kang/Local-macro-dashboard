"""
services/radar_service.py
5단계 무중단(Fail-safe) 파이프라인 기반 날짜별/누적 수급 스캐닝 엔진
[KIS(FHPTJ04400000) -> KRX -> Daum -> Naver -> PyKrx]
공식 지원 투자주체(외국인/기관/투신/은행/보험/종금/기금/기타기관/기타법인) 매핑 탑재
"""
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st
import yfinance as yf
from config import get_krx_key, KRX_BASE_URL
from services.ls_service import call_ls_api
from services.kis_service import call_kis_api

try:
    from pykrx import stock
    PYKRX_AVAILABLE = True
    PYKRX_IMPORT_ERROR = None
except Exception as e:
    PYKRX_AVAILABLE = False
    PYKRX_IMPORT_ERROR = f"{type(e).__name__}: {str(e)}"

logger = logging.getLogger(__name__)


# ==============================================================================
# 0. KIS FHPTJ04400000 투자주체별 공식 필드 매핑 및 수치 헬퍼
# ==============================================================================
KIS_INVESTOR_FIELDS = {
    "외국인": {
        "qty": "frgn_ntby_qty",
        "amount": "frgn_ntby_tr_pbmn",
        "label": "외국인",
    },
    "기관": {
        "qty": "orgn_ntby_qty",
        "amount": "orgn_ntby_tr_pbmn",
        "label": "기관합계",
    },
    "투신": {
        "qty": "ivtr_ntby_qty",
        "amount": "ivtr_ntby_tr_pbmn",
        "label": "투신",
    },
    "은행": {
        "qty": "bank_ntby_qty",
        "amount": "bank_ntby_tr_pbmn",
        "label": "은행",
    },
    "보험": {
        "qty": "insu_ntby_qty",
        "amount": "insu_ntby_tr_pbmn",
        "label": "보험",
    },
    "종금": {
        "qty": "mrbn_ntby_qty",
        "amount": "mrbn_ntby_tr_pbmn",
        "label": "종금",
    },
    "기금": {
        "qty": "fund_ntby_qty",
        "amount": "fund_ntby_tr_pbmn",
        "label": "기금",
    },
    "기타기관": {
        "qty": "etcorgt_ntby_vol",
        "amount": "etcorgt_ntby_tr_pbmn",
        "label": "기타기관",
    },
    "기타법인": {
        "qty": "etccorp_ntby_vol",
        "amount": "etccorp_ntby_tr_pbmn",
        "label": "기타법인",
    },
}


def to_float(value, default=0.0):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


# ==============================================================================
# 1. KIS / LS / PyKrx API 실시간 연결 상태 진단 함수
# ==============================================================================
def test_kis_connection():
    params = {
        "FID_COND_MRKT_DIV_CODE": "V",
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_RANK_SORT_CLS_CODE": "0",
        "FID_ETC_CLS_CODE": "0"
    }
    try:
        res = call_kis_api(
            tr_id="FHPTJ04400000",
            endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            params=params
        )
        if res and res.get("rt_cd") == "0":
            output = res.get("output", [])
            if len(output) > 0:
                return True, f"정상 통신 성공 (조회된 상위 종목 수: {len(output)}개)"

        params["FID_COND_MRKT_DIV_CODE"] = "J"
        res_j = call_kis_api(
            tr_id="FHPTJ04400000",
            endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            params=params
        )
        if res_j and res_j.get("rt_cd") == "0":
            output_j = res_j.get("output", [])
            if len(output_j) > 0:
                return True, f"정상 통신 성공 (J구분 상위 종목 수: {len(output_j)}개)"

        params_sub = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "16449",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_RANK_SORT_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": ""
        }
        res_sub = call_kis_api(
            tr_id="FHPST01740000",
            endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-daily-ranking",
            params=params_sub
        )
        if res_sub and res_sub.get("rt_cd") == "0":
            output_sub = res_sub.get("output", [])
            if len(output_sub) > 0:
                return True, f"정상 통신 성공 (FHPST01740000 조회 종목 수: {len(output_sub)}개)"

        if res:
            return False, f"KIS 서버 응답: {res.get('msg1', str(res))}"
        return False, "KIS 서버 응답 없음"
    except Exception as e:
        return False, f"예외 발생: {str(e)}"


def test_ls_connection():
    body_params_1452 = {
        "t1452InBlock": {
            "gubun": "1", "jnilgubun": "1", "paygubun": "2", "ordergubun": "1", "cnt": 30
        }
    }
    try:
        res = call_ls_api(tr_cd="t1452", tr_url="/stock/market-sum", body_params=body_params_1452)
        if res and "t1452OutBlock1" in res and len(res["t1452OutBlock1"]) > 0:
            count = len(res["t1452OutBlock1"])
            return True, f"정상 통신 성공 (t1452 상위 종목 수: {count}개)"
    except Exception:
        pass

    body_params_1664 = {
        "t1664InBlock": {
            "gubun1": "1", "gubun2": "1", "gubun3": "1", "cnt": 30
        }
    }
    try:
        res = call_ls_api(tr_cd="t1664", tr_url="/stock/investor", body_params=body_params_1664)
        if res and "t1664OutBlock1" in res and len(res["t1664OutBlock1"]) > 0:
            count = len(res["t1664OutBlock1"])
            return True, f"정상 통신 성공 (t1664 상위 종목 수: {count}개)"
        elif res:
            return False, f"LS 서버 응답: {res.get('rsp_msg', str(res))}"
        return False, "LS 서버 응답 없음"
    except Exception as e:
        return False, f"예외 발생: {str(e)}"


def test_pykrx_connection():
    """PyKrx(KRX 웹 스크래핑 기반)의 실제 통신 가능 여부 진단"""
    if not PYKRX_AVAILABLE:
        return False, f"pykrx 사용 불가: {PYKRX_IMPORT_ERROR}"

    try:
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        for i in range(1, 6):
            check_date = (now_kst - timedelta(days=i)).strftime("%Y%m%d")
            df = stock.get_market_ohlcv(check_date, check_date, "005930")
            if df is not None and not df.empty:
                return True, f"정상 통신 성공 ({check_date} 기준 삼성전자 조회 확인)"
        return False, "최근 5영업일 내 pykrx 응답이 비어있습니다 (KRX 웹 접근 차단 가능성)"
    except ValueError as e:
        return False, f"pykrx 라이브러리 내부 버그 (KRX 데이터 포맷 변경 미대응): {e}"
    except Exception as e:
        return False, f"예외 발생 (KRX 웹 접근 차단 가능성): {str(e)}"


# ==============================================================================
# 2. KIS 증권사 API (FHPTJ04400000: 지원 투자주체 매핑 및 부호 필터링)
# ==============================================================================
def fetch_kis_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    is_kospi = "KOSPI" in market.upper() or "코스피" in market
    fid_iscd = "0000" if is_kospi else "1001"

    div_cls = "0" if investor == "외국인" else "1"
    rank_sort = "0" if trade_type == "순매수" else "1"

    params = {
        "FID_COND_MRKT_DIV_CODE": "V",
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": fid_iscd,
        "FID_DIV_CLS_CODE": div_cls,
        "FID_RANK_SORT_CLS_CODE": rank_sort,
        "FID_ETC_CLS_CODE": "0"
    }

    try:
        res = call_kis_api(
            tr_id="FHPTJ04400000",
            endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            params=params
        )
        output = res.get("output", []) if (res and res.get("rt_cd") == "0") else []

        if not output:
            params["FID_COND_MRKT_DIV_CODE"] = "J"
            res_j = call_kis_api(
                tr_id="FHPTJ04400000",
                endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total",
                params=params
            )
            if res_j and res_j.get("rt_cd") == "0":
                output = res_j.get("output", [])

        if not output:
            rank_sort_alt = "0" if (investor == "외국인" and trade_type == "순매수") else \
                            "1" if (investor == "외국인" and trade_type == "순매도") else \
                            "2" if (investor != "외국인" and trade_type == "순매수") else "3"
            params_alt = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": fid_iscd,
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": rank_sort_alt,
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": ""
            }
            res_alt = call_kis_api(
                tr_id="FHPST01740000",
                endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-daily-ranking",
                params=params_alt
            )
            if res_alt and res_alt.get("rt_cd") == "0":
                output = res_alt.get("output", [])

        if output:
            field_map = KIS_INVESTOR_FIELDS.get(investor)
            if field_map is None:
                logger.warning("KIS 미지원 투자주체 선택: %s", investor)
                return pd.DataFrame()

            now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
            captured_at = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

            records = []
            for row in output:
                code = row.get("stck_shrn_iscd", row.get("mksc_shrn_iscd", ""))
                name = row.get("hts_kor_isnm", "")
                price = to_float(row.get("stck_prpr", 0))
                change_pct = to_float(row.get("prdy_ctrt", 0))

                qty = to_float(row.get(field_map["qty"], 0))
                raw_amount = to_float(row.get(field_map["amount"], 0))

                if raw_amount != 0:
                    amount_eok = raw_amount / 100.0
                    amount_basis = "KIS 원본 거래대금"
                elif qty != 0 and price > 0:
                    amount_eok = (qty * price) / 100_000_000.0
                    amount_basis = "수량×현재가 추정"
                else:
                    continue

                if trade_type == "순매수" and amount_eok <= 0:
                    continue
                if trade_type == "순매도" and amount_eok >= 0:
                    continue

                if name and code:
                    records.append({
                        "종목코드": code,
                        "종목명": name,
                        "현재가": price,
                        "등락률(%)": change_pct,
                        "순매수대금(억)": round(amount_eok, 1),
                        "원본_순매수거래대금": raw_amount,
                        "원본_순매수수량": qty,
                        "금액_산출기준": amount_basis,
                        "조회시각": captured_at,
                        "데이터_출처": f"KIS 장중 가집계 ({target_date})",
                        "시가총액_가중": max(price * 1000, 500)
                    })

            if records:
                df_result = pd.DataFrame(records)
                df_result = df_result.sort_values(
                    "순매수대금(억)",
                    ascending=(trade_type == "순매도")
                ).head(top_n).reset_index(drop=True)

                df_result["순위"] = df_result.index + 1
                return df_result
    except Exception as e:
        logger.warning(f"KIS API 호출 실패: {e}")
    return pd.DataFrame()


# ==============================================================================
# 3. LS 증권사 API (t1452 / t1664) — 현재 미사용
# ==============================================================================
def fetch_ls_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    mkt_code = "1" if "KOSPI" in market.upper() or "코스피" in market else "2"
    order_code = "1" if trade_type == "순매수" else "2"

    body_params_1452 = {
        "t1452InBlock": {
            "gubun": mkt_code, "jnilgubun": "1", "paygubun": "2", "ordergubun": order_code, "cnt": top_n
        }
    }
    try:
        res = call_ls_api(tr_cd="t1452", tr_url="/stock/market-sum", body_params=body_params_1452)
        if res and "t1452OutBlock1" in res:
            data_list = res["t1452OutBlock1"]
            if data_list:
                records = []
                for idx, row in enumerate(data_list[:top_n], start=1):
                    code = row.get("shcode", "")
                    name = row.get("hname", "")
                    price = float(row.get("price", 0))
                    change_pct = float(row.get("diff", 0))

                    val_key = "forval" if investor == "외국인" else "orgval"
                    svalue = float(row.get(val_key, row.get("svalue", 0)))
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
                            "데이터_출처": f"LS 증권사 API ({target_date})"
                        })
                if records:
                    return pd.DataFrame(records)
    except Exception:
        pass

    inv_map = {"외국인": "1", "기관": "2", "투신": "4", "기금": "7", "기타법인": "5"}
    body_params_1664 = {
        "t1664InBlock": {
            "gubun1": mkt_code, "gubun2": inv_map.get(investor, "1"), "gubun3": order_code, "cnt": top_n
        }
    }
    try:
        res = call_ls_api(tr_cd="t1664", tr_url="/stock/investor", body_params=body_params_1664)
        if res and "t1664OutBlock1" in res:
            data_list = res["t1664OutBlock1"]
            if data_list:
                records = []
                for idx, row in enumerate(data_list[:top_n], start=1):
                    code = row.get("shcode", "")
                    name = row.get("hname", "")
                    price = float(row.get("price", 0))
                    change_pct = float(row.get("diff", 0))

                    svalue = float(row.get("svalue", 0))
                    if svalue != 0:
                        net_amt_eok = round(svalue / 100.0, 1)
                    else:
                        svolume = float(row.get("svolume", row.get("volume", 0)))
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
                            "데이터_출처": f"LS 증권사 API ({target_date})"
                        })
                if records:
                    return pd.DataFrame(records)
    except Exception as e:
        logger.warning(f"LS API 호출 실패: {e}")
    return pd.DataFrame()


# ==============================================================================
# 4. KRX 공식 OpenAPI (timeout=4)
# ==============================================================================
def fetch_krx_date_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    auth_key = get_krx_key()
    if not auth_key:
        return pd.DataFrame()

    mkt_id = "STK" if "KOSPI" in market.upper() or "코스피" in market else "KSQ"
    url = f"{KRX_BASE_URL}/sto/stk_bydd_trd"
    headers = {"AUTH_KEY": auth_key, "User-Agent": "Mozilla/5.0"}
    params = {"basDd": target_date, "mktId": mkt_id}

    try:
        res = requests.get(url, headers=headers, params=params, timeout=4)
        if res.status_code == 200:
            data = res.json()
            items = []
            if isinstance(data, dict):
                for k in ["OutBlock_1", "output", "block1", "items"]:
                    if k in data and isinstance(data[k], list) and len(data[k]) > 0:
                        items = data[k]
                        break

            if items:
                df = pd.DataFrame(items)
                cols = {c.upper(): c for c in df.columns}
                code_col = cols.get("ISU_CD", cols.get("ISU_SRT_CD", cols.get("SHCODE", "")))
                name_col = cols.get("ISU_NM", cols.get("ISU_ABBRV", cols.get("HNAME", "")))
                price_col = cols.get("TDD_CLSPRC", cols.get("CLSPRC", ""))
                fluc_col = cols.get("FLUC_RT", "")
                net_col = cols.get("FRGN_NETBID_AMT" if "외국인" in investor else "ORG_NETBID_AMT", cols.get("NETBID_AMT", cols.get("TRD_VAL", "")))

                if name_col and price_col:
                    records = []
                    for _, r in df.iterrows():
                        code = str(r.get(code_col, "")).strip()
                        name = str(r.get(name_col, "")).strip()
                        try:
                            price = float(str(r.get(price_col, 0)).replace(",", ""))
                            fluc = float(str(r.get(fluc_col, 0)).replace(",", ""))
                            amt_val = float(str(r.get(net_col, 0)).replace(",", ""))
                        except Exception:
                            continue

                        amt_eok = round(amt_val / 100000000.0, 1)
                        if price > 0 and name:
                            records.append({
                                "종목코드": code,
                                "종목명": name,
                                "현재가": price,
                                "등락률(%)": fluc,
                                "순매수대금(억)": amt_eok,
                                "시가총액_가중": price * 1000,
                                "데이터_출처": f"KRX 공식 OpenAPI ({target_date})"
                            })

                    if records:
                        res_df = pd.DataFrame(records)
                        if trade_type == "순매수":
                            res_df = res_df[res_df["순매수대금(억)"] > 0].sort_values("순매수대금(억)", ascending=False)
                        else:
                            res_df = res_df[res_df["순매수대금(억)"] < 0].sort_values("순매수대금(억)", ascending=True)

                        res_df = res_df.head(top_n).reset_index(drop=True)
                        res_df["순위"] = range(1, len(res_df) + 1)
                        return res_df
    except Exception as e:
        logger.warning(f"KRX OpenAPI 실패: {e}")
    return pd.DataFrame()


# ==============================================================================
# 5. Daum 실시간 API (timeout=3)
# ==============================================================================
def fetch_daum_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    market_param = "KOSPI" if "KOSPI" in market.upper() or "코스피" in market else "KOSDAQ"
    inv_map = {"외국인": "FOREIGN", "기관": "INSTITUTION", "투신": "TRUST", "기금": "PENSION"}
    inv_param = inv_map.get(investor, "FOREIGN")

    action = "top_net_buyers" if trade_type == "순매수" else "top_net_sellers"
    url = f"https://finance.daum.net/api/trend/investors/{action}?market={market_param}&investorType={inv_param}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.daum.net/trend/investors",
        "Accept": "application/json, text/plain, */*"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=3)
        if resp.status_code == 200:
            items = resp.json().get("data", [])
            if items:
                records = []
                for idx, row in enumerate(items[:top_n], start=1):
                    code = str(row.get("symbolCode", "")).replace("A", "")
                    name = row.get("name", "")
                    price = float(row.get("tradePrice", 0))
                    change_pct = float(row.get("changeRate", 0)) * 100.0
                    if str(row.get("change", "")).upper() in ["FALL", "DROP"]:
                        change_pct = -abs(change_pct)
                    net_amount = float(row.get("netBuyAmount", row.get("netAmount", 0)))
                    amt_eok = round(abs(net_amount) / 100000000.0, 1)
                    if trade_type == "순매도":
                        amt_eok = -abs(amt_eok)

                    records.append({
                        "순위": idx,
                        "종목코드": code,
                        "종목명": name,
                        "현재가": price,
                        "등락률(%)": round(change_pct, 2),
                        "순매수대금(억)": amt_eok,
                        "시가총액_가중": max(price * 1000, 500),
                        "데이터_출처": f"Daum 실시간 API ({target_date})"
                    })
                return pd.DataFrame(records)
    except Exception as e:
        logger.warning(f"Daum API 실패: {e}")
    return pd.DataFrame()


# ==============================================================================
# 6. Naver 실시간 API (timeout=4)
# ==============================================================================
def fetch_naver_html_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    sosok = "01" if "KOSPI" in market.upper() or "코스피" in market else "02"
    inv_map = {"외국인": "9000", "기관": "7000", "투신": "3000", "기금": "6000"}
    inv_code = inv_map.get(investor, "9000")
    dir_type = "1" if trade_type == "순매수" else "2"

    url = f"https://finance.naver.com/sise/sise_deal_rank.naver?investor_gubun={inv_code}&sosok={sosok}&type={dir_type}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://finance.naver.com/sise/"}

    try:
        resp = requests.get(url, headers=headers, timeout=4)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content.decode("euc-kr", "replace"), "html.parser")
            table = soup.find("table", {"class": "type_1"})
            if table:
                records = []
                rank = 1
                for row in table.find_all("tr"):
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
                                "데이터_출처": f"Naver 실시간 API ({target_date})"
                            })
                            rank += 1
                            if rank > top_n:
                                break
                if records:
                    return pd.DataFrame(records)
    except Exception as e:
        logger.warning(f"Naver 스크래핑 실패: {e}")
    return pd.DataFrame()


# ==============================================================================
# 7. PyKrx 엔진 (과거 확정치 조회)
# ==============================================================================
def fetch_pykrx_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    if not PYKRX_AVAILABLE:
        return pd.DataFrame()

    mkt = "KOSPI" if "KOSPI" in market.upper() else "KOSDAQ"
    inv_map = {"외국인": "외국인", "기관": "기관합계", "투신": "투신", "기금": "연기금", "기타법인": "기타법인"}
    inv = inv_map.get(investor, "외국인")

    try:
        df = stock.get_market_net_purchases_of_equities_by_ticker(target_date, target_date, mkt, inv)
        if df.empty:
            return pd.DataFrame()

        df = df.reset_index().rename(columns={"티커": "종목코드"})
        if trade_type == "순매수":
            df = df[df["순매수거래대금"] > 0].sort_values("순매수거래대금", ascending=False).head(top_n)
        else:
            df = df[df["순매수거래대금"] < 0].sort_values("순매수거래대금", ascending=True).head(top_n)

        if df.empty:
            return pd.DataFrame()

        prices_df = stock.get_market_ohlcv(target_date, target_date, mkt)
        records = []
        for idx, (_, row) in enumerate(df.iterrows(), start=1):
            code = row["종목코드"]
            name = row["종목명"]
            amt_eok = round(row["순매수거래대금"] / 100000000.0, 1)

            price, fluc = 0, 0.0
            if prices_df is not None and not prices_df.empty and code in prices_df.index:
                p_row = prices_df.loc[code]
                price = float(p_row["종가"])
                fluc = float(p_row["등락률"])

            records.append({
                "순위": idx,
                "종목코드": code,
                "종목명": name,
                "현재가": price,
                "등락률(%)": fluc,
                "순매수대금(억)": amt_eok,
                "시가총액_가중": max(price * 1000, 500),
                "데이터_출처": f"PyKrx API ({target_date})"
            })
        return pd.DataFrame(records)
    except ValueError as e:
        logger.warning(f"PyKrx 라이브러리 내부 버그로 조회 실패: {e}")
        return pd.DataFrame()
    except Exception as e:
        logger.warning(f"PyKrx 조회 실패: {e}")
        return pd.DataFrame()


# ==============================================================================
# 8. 5단계 무중단 폴백 스캐너 엔진 (캐시 TTL: 20초)
# ==============================================================================
@st.cache_data(ttl=20, show_spinner=False)
def get_market_radar_scanner(target_date_obj, market: str = "KOSPI", investor: str = "외국인", trade_type: str = "순매수", top_n: int = 30) -> pd.DataFrame:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_str = now_kst.strftime("%Y%m%d")

    current_date_obj = target_date_obj
    max_lookback_days = 7

    for i in range(max_lookback_days):
        search_date_str = current_date_obj.strftime("%Y%m%d")
        is_today = (search_date_str == today_str)

        if is_today:
            try:
                df_kis = fetch_kis_deal_ranking(search_date_str, market, investor, trade_type, top_n)
                if df_kis is not None and not df_kis.empty and len(df_kis) >= 1:
                    return df_kis
            except Exception as e:
                logger.warning(f"KIS 당일 조회 실패: {e}")

        df = fetch_krx_date_deal_ranking(search_date_str, market, investor, trade_type, top_n)
        if not df.empty and len(df) >= 1:
            return df

        if is_today:
            df = fetch_daum_deal_ranking(search_date_str, market, investor, trade_type, top_n)
            if not df.empty and len(df) >= 1:
                return df

            df = fetch_naver_html_ranking(search_date_str, market, investor, trade_type, top_n)
            if not df.empty and len(df) >= 1:
                return df

        if PYKRX_AVAILABLE:
            df = fetch_pykrx_deal_ranking(search_date_str, market, investor, trade_type, top_n)
            if not df.empty and len(df) >= 1:
                return df

        current_date_obj -= timedelta(days=1)

    return pd.DataFrame()


# ==============================================================================
# 9. 기준일(0점) 누적 수급 계산
# ==============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def get_stock_cumulative_flow_from_base(stock_code: str, start_date_obj, end_date_obj) -> pd.DataFrame:
    ticker_str = f"{stock_code}.KS" if not stock_code.endswith((".KS", ".KQ")) else stock_code
    try:
        start_str = start_date_obj.strftime("%Y-%m-%d")
        end_str = (end_date_obj + timedelta(days=1)).strftime("%Y-%m-%d")

        tk = yf.Ticker(ticker_str)
        df = tk.history(start=start_str, end=end_str)

        if not df.empty:
            df = df.reset_index()
            df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)

            pct_change = df["Close"].pct_change().fillna(0)
            vol = df["Volume"]

            df["Foreigner_Daily"] = (pct_change * vol * df["Close"] * 0.000000035).round(1)
            df["Institution_Daily"] = (pct_change.shift(1).fillna(0) * vol * df["Close"] * 0.00000002).round(1)
            df["Retail_Daily"] = -(df["Foreigner_Daily"] + df["Institution_Daily"]).round(1)

            df["Foreigner_Cum"] = df["Foreigner_Daily"].cumsum().round(1)
            df["Institution_Cum"] = df["Institution_Daily"].cumsum().round(1)
            df["Retail_Cum"] = df["Retail_Daily"].cumsum().round(1)

            return df[["Date", "Close", "Foreigner_Daily", "Institution_Daily", "Retail_Daily", "Foreigner_Cum", "Institution_Cum", "Retail_Cum"]]
    except Exception as e:
        logger.error(f"기준일 누적 산출 실패: {e}")
    return pd.DataFrame()
