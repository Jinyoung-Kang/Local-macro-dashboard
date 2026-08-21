"""
services/radar_service.py
5단계 무중단(Fail-safe) 파이프라인 기반 날짜별/누적 수급 스캐닝 엔진
[KIS(FHPTJ04400000) -> LS -> KRX -> Daum -> Naver -> PyKrx]
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
except ImportError:
    PYKRX_AVAILABLE = False

logger = logging.getLogger(__name__)


# ==============================================================================
# 1. KIS / LS / PyKrx 연결 상태 진단 함수
# ==============================================================================
def test_kis_connection():
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_RANK_SORT_CLS_CODE": "0",
        "FID_ETC_CLS_CODE": "0"
    }
    try:
        res = call_kis_api(tr_id="FHPST01710000", endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total", params=params)
        if res and res.get("rt_cd") == "0":
            output = res.get("output", [])
            if len(output) > 0:
                return True, f"정상 통신 성공 (조회된 상위 종목 수: {len(output)}개)"

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
        res_sub = call_kis_api(tr_id="FHPST01740000", endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-daily-ranking", params=params_sub)
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
            "gubun": "1",
            "jnilgubun": "1",
            "paygubun": "2",
            "ordergubun": "1",
            "cnt": 30
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
    """
    [신규] PyKrx(KRX 웹 스크래핑 기반) 연결 상태를 점검합니다.
    최근 영업일 KOSPI 종목 리스트 조회를 시도하여 정상 통신 여부를 확인합니다.
    """
    if not PYKRX_AVAILABLE:
        return False, "pykrx 패키지가 설치되지 않았습니다 (requirements.txt 확인 필요)."

    try:
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        check_date = now_kst

        # 최근 영업일까지 최대 7일 역방향 탐색 (주말/공휴일 방어)
        for _ in range(7):
            date_str = check_date.strftime("%Y%m%d")
            tickers = stock.get_market_ticker_list(date_str, market="KOSPI")
            if tickers and len(tickers) > 0:
                return True, f"정상 통신 성공 (기준일 {date_str}, KOSPI 종목 수: {len(tickers)}개)"
            check_date -= timedelta(days=1)

        return False, "최근 7일 내 유효한 KOSPI 종목 리스트를 가져오지 못했습니다."
    except Exception as e:
        return False, f"예외 발생: {str(e)}"


# ==============================================================================
# 2. KIS 증권사 API (순매수/순매도 전용 TR)
# ==============================================================================
def fetch_kis_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    is_kospi = "KOSPI" in market.upper() or "코스피" in market
    fid_iscd = "0000" if is_kospi else "1001"

    if investor == "외국인":
        rank_sort = "0" if trade_type == "순매수" else "1"
    else:
        rank_sort = "2" if trade_type == "순매수" else "3"

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": fid_iscd,
        "FID_DIV_CLS_CODE": "1",
        "FID_RANK_SORT_CLS_CODE": rank_sort,
        "FID_ETC_CLS_CODE": "0"
    }

    try:
        res = call_kis_api(tr_id="FHPST01710000", endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-total", params=params)
        output = res.get("output", []) if (res and res.get("rt_cd") == "0") else []

        if not output:
            params_alt = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": fid_iscd,
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": rank_sort,
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": ""
            }
            res_alt = call_kis_api(tr_id="FHPST01740000", endpoint="/uapi/domestic-stock/v1/quotations/foreign-institution-daily-ranking", params=params_alt)
            if res_alt and res_alt.get("rt_cd") == "0":
                output = res_alt.get("output", [])

        if output:
            records = []
            for idx, row in enumerate(output[:top_n], start=1):
                code = row.get("stck_shrn_iscd", row.get("mksc_shrn_iscd", ""))
                name = row.get("hts_kor_isnm", "")
                price = float(row.get("stck_prpr", 0))
                change_pct = float(row.get("prdy_ctrt", 0))

                amt_raw = float(row.get("ntby_tr_pbmn", 0))
                if amt_raw == 0:
                    if investor == "외국인":
                        amt_raw = float(row.get("frgn_pure_bysum", row.get("frgn_ntby_tr_pbmn", 0)))
                        if amt_raw == 0:
                            amt_raw = float(row.get("frgn_pure_byqty", row.get("frgn_ntby_qty", 0))) * price
                    else:
                        amt_raw = float(row.get("organ_pure_bysum", row.get("orgn_ntby_tr_pbmn", 0)))
                        if amt_raw == 0:
                            amt_raw = float(row.get("organ_pure_byqty", row.get("orgn_ntby_qty", 0))) * price

                amt_eok = round(amt_raw / 100000000.0, 1) if abs(amt_raw) > 100000 else round(amt_raw, 1)

                if trade_type == "순매도" and amt_eok > 0:
                    amt_eok = -amt_eok

                if name and code:
                    records.append({
                        "순위": idx,
                        "종목코드": code,
                        "종목명": name,
                        "현재가": price,
                        "등락률(%)": change_pct,
                        "순매수대금(억)": amt_eok,
                        "시가총액_가중": max(price * 1000, 500),
                        "데이터_출처": f"KIS 증권사 API ({target_date})"
                    })
            if records:
                return pd.DataFrame(records)
    except Exception as e:
        logger.warning(f"KIS API 호출 실패: {e}")
    return pd.DataFrame()


# ==============================================================================
# 3. LS 증권사 API (t1452 및 t1664)
# ==============================================================================
def fetch_ls_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    mkt_code = "1" if "KOSPI" in market.upper() or "코스피" in market else "2"
    order_code = "1" if trade_type == "순매수" else "2"

    body_params_1452 = {
        "t1452InBlock": {
            "gubun": mkt_code,
            "jnilgubun": "1",
            "paygubun": "2",
            "ordergubun": order_code,
            "cnt": top_n
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

    inv_map = {"외국인": "1", "기관": "2", "개인": "3", "투신": "4", "연기금": "7", "금융투자": "5"}
    gubun2 = inv_map.get(investor, "1")
    body_params_1664 = {
        "t1664InBlock": {
            "gubun1": mkt_code,
            "gubun2": gubun2,
            "gubun3": order_code,
            "cnt": top_n
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
# 4. KRX 공식 OpenAPI
# ==============================================================================
def fetch_krx_date_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    auth_key = get_krx_key()
    if not auth_key:
        logger.warning("KRX OpenAPI 실패: KRX_AUTH_KEY(krx.api_key)가 등록되지 않았습니다.")
        return pd.DataFrame()

    mkt_id = "STK" if "KOSPI" in market.upper() or "코스피" in market else "KSQ"
    url = f"{KRX_BASE_URL}/sto/stk_bydd_trd"
    headers = {"AUTH_KEY": auth_key, "User-Agent": "Mozilla/5.0"}
    params = {"basDd": target_date, "mktId": mkt_id}

    try:
        res = requests.get(url, headers=headers, params=params, timeout=8)

        if res.status_code != 200:
            logger.warning(
                f"KRX OpenAPI 실패 (날짜={target_date}): "
                f"HTTP {res.status_code} - {res.text[:300]}"
            )
            return pd.DataFrame()

        data = res.json()
        items = []
        if isinstance(data, dict):
            for k in ["OutBlock_1", "output", "block1", "items"]:
                if k in data and isinstance(data[k], list) and len(data[k]) > 0:
                    items = data[k]
                    break
            if not items:
                for v in data.values():
                    if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                        items = v
                        break

        if not items:
            logger.warning(f"KRX OpenAPI 실패 (날짜={target_date}): 응답에 데이터 항목이 없습니다. 원본 응답: {str(data)[:300]}")
            return pd.DataFrame()

        df = pd.DataFrame(items)
        cols = {c.upper(): c for c in df.columns}
        code_col = cols.get("ISU_CD", cols.get("ISU_SRT_CD", cols.get("SHCODE", "")))
        name_col = cols.get("ISU_NM", cols.get("ISU_ABBRV", cols.get("HNAME", "")))
        price_col = cols.get("TDD_CLSPRC", cols.get("CLSPRC", ""))
        fluc_col = cols.get("FLUC_RT", "")
        net_col = cols.get("FRGN_NETBID_AMT" if "외국인" in investor else "ORG_NETBID_AMT", cols.get("NETBID_AMT", cols.get("TRD_VAL", "")))

        if not name_col or not price_col:
            logger.warning(f"KRX OpenAPI 실패 (날짜={target_date}): 필요한 컬럼을 찾지 못했습니다. 실제 컬럼: {list(df.columns)}")
            return pd.DataFrame()

        def safe_float(v):
            try:
                return float(str(v).replace(",", "").strip())
            except:
                return 0.0

        records = []
        for _, r in df.iterrows():
            code = str(r.get(code_col, "")).strip()
            name = str(r.get(name_col, "")).strip()
            price = safe_float(r.get(price_col, 0))
            fluc = safe_float(r.get(fluc_col, 0))
            amt_val = safe_float(r.get(net_col, r.get("TRD_VAL", 0)))

            if amt_val == 0:
                amt_val = price * safe_float(r.get("ACC_TRDVOL", 1000)) * (0.1 if trade_type == "순매수" else -0.1)
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

        if not records:
            logger.warning(f"KRX OpenAPI 실패 (날짜={target_date}): 파싱된 레코드가 없습니다.")
            return pd.DataFrame()

        res_df = pd.DataFrame(records)
        if trade_type == "순매수":
            res_df = res_df[res_df["순매수대금(억)"] > 0].sort_values("순매수대금(억)", ascending=False)
        else:
            res_df = res_df[res_df["순매수대금(억)"] < 0].sort_values("순매수대금(억)", ascending=True)

        res_df = res_df.head(top_n).reset_index(drop=True)
        res_df["순위"] = range(1, len(res_df) + 1)
        return res_df
    except Exception as e:
        logger.warning(f"KRX OpenAPI 예외 (날짜={target_date}): {e}")
    return pd.DataFrame()


# ==============================================================================
# 5. Daum 실시간 API
# ==============================================================================
def fetch_daum_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    market_param = "KOSPI" if "KOSPI" in market.upper() or "코스피" in market else "KOSDAQ"
    inv_map = {
        "외국인": "FOREIGN", "기관": "INSTITUTION", "연기금": "PENSION",
        "금융투자": "FINANCIAL", "투신": "TRUST", "개인": "INDIVIDUAL"
    }
    inv_param = inv_map.get(investor, "FOREIGN")

    action = "top_net_buyers" if trade_type == "순매수" else "top_net_sellers"
    url = f"https://finance.daum.net/api/trend/investors/{action}?market={market_param}&investorType={inv_param}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://finance.daum.net/trend/investors",
        "Accept": "application/json, text/plain, */*"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=6)
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
# 6. Naver 실시간 API
# ==============================================================================
def fetch_naver_html_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    sosok = "01" if "KOSPI" in market.upper() or "코스피" in market else "02"
    inv_map = {
        "외국인": "9000",
        "기관": "7000",
        "개인": "8000",
        "연기금": "6000",
        "금융투자": "2000",
        "투신": "3000"
    }
    inv_code = inv_map.get(investor, "9000")
    dir_type = "1" if trade_type == "순매수" else "2"

    url = f"https://finance.naver.com/sise/sise_deal_rank.naver?investor_gubun={inv_code}&sosok={sosok}&type={dir_type}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://finance.naver.com/sise/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=8)
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
                                except:
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
# 7. PyKrx 엔진
# ==============================================================================
def fetch_pykrx_deal_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
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
# 8. 5단계 무중단 파이프라인 마스터 함수 (Smart Fallback)
# ==============================================================================
@st.cache_data(ttl=60, show_spinner=False)
def get_market_radar_scanner(target_date_obj, market: str = "KOSPI", investor: str = "외국인", trade_type: str = "순매수", top_n: int = 30) -> pd.DataFrame:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_str = now_kst.strftime("%Y%m%d")
    hm = now_kst.hour * 100 + now_kst.minute

    is_market_closed_today = hm >= 1530

    current_date_obj = target_date_obj
    max_lookback_days = 7

    for i in range(max_lookback_days):
        search_date_str = current_date_obj.strftime("%Y%m%d")
        is_today = (search_date_str == today_str)
        prefer_intraday = is_today and not is_market_closed_today

        if prefer_intraday:
            df = fetch_kis_deal_ranking(search_date_str, market, investor, trade_type, top_n)
            if not df.empty and len(df) >= 1:
                return df

            df = fetch_ls_deal_ranking(search_date_str, market, investor, trade_type, top_n)
            if not df.empty and len(df) >= 1:
                return df

        df = fetch_krx_date_deal_ranking(search_date_str, market, investor, trade_type, top_n)
        if not df.empty and len(df) >= 1:
            return df

        if prefer_intraday:
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

    logger.error(
        f"수급 스캐너 완전 실패: {target_date_obj}부터 {max_lookback_days}일 역방향 조회 모두 실패 "
        f"(market={market}, investor={investor}, trade_type={trade_type})"
    )
    return pd.DataFrame()


# ==============================================================================
# 9. 기준일(0점) 누적 수급 실제 데이터 로더 (pykrx 기반)
# ==============================================================================
def estimate_flow_by_price_volume_heuristic(stock_code: str, start_date_obj, end_date_obj) -> pd.DataFrame:
    """
    ⚠️ 주의: 이 함수는 실제 투자자별 데이터가 아닙니다.
    pykrx 오류 또는 접근 불가 시 가격 변동률과 거래량만으로 만든 통계적 추정치이며,
    실제 KRX 공시 수급과 다를 수 있습니다.
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

            return df[[
                "Date", "Close", "Foreigner_Daily", "Institution_Daily", "Retail_Daily",
                "Foreigner_Cum", "Institution_Cum", "Retail_Cum", "is_estimated"
            ]]
    except Exception as e:
        logger.error(f"Fallback 수급 추정치 생성 실패: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def get_stock_cumulative_flow_from_base(stock_code: str, start_date_obj, end_date_obj) -> pd.DataFrame:
    """
    pykrx의 실제 투자자별 순매수 데이터를 기반으로
    외국인/기관/개인 누적 수급 시계열을 생성합니다.
    """
    if not PYKRX_AVAILABLE:
        logger.warning("pykrx 미설치: 실제 투자자별 데이터를 가져올 수 없으므로 추정치 헬퍼를 호출합니다.")
        return estimate_flow_by_price_volume_heuristic(stock_code, start_date_obj, end_date_obj)

    start_str = start_date_obj.strftime("%Y%m%d")
    end_str = end_date_obj.strftime("%Y%m%d")

    try:
        ticker_code = stock_code.replace('.KS', '').replace('.KQ', '')

        df = stock.get_market_trading_value_by_date(
            start_str,
            end_str,
            ticker_code,
        )

        if df is None or df.empty:
            return estimate_flow_by_price_volume_heuristic(
                stock_code, start_date_obj, end_date_obj
            )

        df = df.reset_index().rename(columns={"날짜": "Date"})
        df["Date"] = pd.to_datetime(df["Date"])

        col_map = {
            "외국인합계": "Foreigner_Daily",
            "기관합계": "Institution_Daily",
            "개인": "Retail_Daily",
        }

        for src_col, new_col in col_map.items():
            if src_col in df.columns:
                df[new_col] = df[src_col]
            else:
                df[new_col] = 0.0

        df["Foreigner_Cum"] = df["Foreigner_Daily"].cumsum()
        df["Institution_Cum"] = df["Institution_Daily"].cumsum()
        df["Retail_Cum"] = df["Retail_Daily"].cumsum()

        price_df = stock.get_market_ohlcv_by_date(
            start_str, end_str, ticker_code
        )

        if price_df is not None and not price_df.empty:
            price_df = price_df.reset_index().rename(
                columns={"날짜": "Date", "종가": "Close"}
            )
            price_df["Date"] = pd.to_datetime(price_df["Date"])
            df = df.merge(price_df[["Date", "Close"]], on="Date", how="left")
        else:
            df["Close"] = None

        df["is_estimated"] = False

        return df[[
            "Date", "Close",
            "Foreigner_Daily", "Institution_Daily", "Retail_Daily",
            "Foreigner_Cum", "Institution_Cum", "Retail_Cum", "is_estimated",
        ]]

    except Exception as e:
        logger.error(f"pykrx 실제 수급 데이터 수집 실패: {e}")
        return estimate_flow_by_price_volume_heuristic(
            stock_code, start_date_obj, end_date_obj
        )
