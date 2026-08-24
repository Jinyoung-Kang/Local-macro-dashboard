"""
services/radar_service.py
5단계 무중단(Fail-safe) 파이프라인 기반 날짜별/누적 수급 스캐닝 엔진
[KIS(FHPTJ04400000) -> LS -> Daum -> Naver -> PyKrx]
공식 지원 투자주체(외국인/기관/투신/은행/보험/종금/기금/기타기관/기타법인) 매핑 탑재

변경사항 (2026-08-24):
- KRX 공식 OpenAPI(/sto/stk_bydd_trd) 단계를 파이프라인에서 완전히 제거했습니다.
  해당 엔드포인트는 종목별 일일 시세(OHLCV)만 제공하며 투자자별 순매수 컬럼
  (FRGN_NETBID_AMT 등)이 없어 항상 실패했습니다. 자격증명 문제가 아니라
  잘못된 API 선택이었으므로, fetch_krx_date_deal_ranking() 함수 자체를
  삭제하고 파이프라인은 KIS -> LS -> Daum -> Naver -> PyKrx 5단계로 운영합니다.
"""
import logging
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from bs4 import BeautifulSoup

from services.ls_service import call_ls_api
from services.kis_service import call_kis_api

try:
    from pykrx import stock
    PYKRX_AVAILABLE = True
except ImportError:
    PYKRX_AVAILABLE = False

logger = logging.getLogger(__name__)

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


def _to_float(value, default=0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


# ==============================================================================
# 1. KIS / LS / PyKrx 연결 상태 진단 함수
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

    return False, "KIS FHPTJ04400000 가집계 데이터가 비어있거나 API 호출에 실패했습니다."


def test_ls_connection():
    body_params_1452 = {
        "t1452InBlock": {
            "gubun": "1",
            "jnilgubun": "1",
            "paygubun": "2",
            "ordergubun": "1",
            "cnt": 30,
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
    if not PYKRX_AVAILABLE:
        return False, "pykrx 패키지가 설치되지 않았습니다 (requirements.txt 확인 필요)."

    try:
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        check_date = now_kst

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
    - 과거 날짜 조회에는 사용하지 않습니다.
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
    mkt_code = "1" if "KOSPI" in market.upper() or "코스피" in market else "2"
    order_code = "1" if trade_type == "순매수" else "2"

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
                    code = row.get("shcode", "")
                    name = row.get("hname", "")
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
                    code = row.get("shcode", "")
                    name = row.get("hname", "")
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
                            "데이터_출처": f"LS 증권사 API ({target_date})"
                        })
                if records:
                    return pd.DataFrame(records)
    except Exception as e:
        logger.warning(f"LS API 호출 실패: {e}")

    return pd.DataFrame()


# ==============================================================================
# 4. Daum 실시간 API (시장 전체 랭킹)
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
# 5. Naver 실시간 API (시장 전체 랭킹)
# ==============================================================================
def fetch_naver_html_ranking(target_date: str, market: str, investor: str, trade_type: str, top_n: int) -> pd.DataFrame:
    sosok = "01" if "KOSPI" in market.upper() or "코스피" in market else "02"
    inv_map = {
        "외국인": "9000",
        "기관": "7000",
        "개인": "8000",
        "연기금": "6000",
        "금융투자": "2000",
        "투신": "3000",
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
# 6. PyKrx 엔진 (시장 전체 랭킹)
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
# 7. 5단계 무중단 파이프라인 마스터 함수 (Smart Fallback, 시장 전체 랭킹)
# KRX 공식 OpenAPI 단계는 잘못된 엔드포인트 사용으로 제거되었습니다.
# 현재 순서: KIS(장중) -> Daum(장중) -> Naver -> PyKrx(과거 확정 데이터)
# ==============================================================================
@st.cache_data(ttl=20, show_spinner=False)
def get_market_radar_scanner(
    target_date_obj,
    market: str = "KOSPI",
    investor: str = "외국인",
    trade_type: str = "순매수",
    top_n: int = 30,
) -> pd.DataFrame:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_str = now_kst.strftime("%Y%m%d")
    current_time = now_kst.time()

    is_regular_session = (
        now_kst.weekday() < 5
        and time(9, 0) <= current_time < time(15, 30)
    )

    current_date_obj = target_date_obj
    max_lookback_days = 7

    for _ in range(max_lookback_days):
        search_date_str = current_date_obj.strftime("%Y%m%d")
        is_today = search_date_str == today_str

        # 장중에는 KIS 장중 가집계 우선
        if is_today and is_regular_session:
            df = fetch_kis_deal_ranking(
                search_date_str, market, investor, trade_type, top_n,
            )
            if df is not None and not df.empty:
                return df

            # KIS 장애 시 장중 참고 소스
            df = fetch_daum_deal_ranking(
                search_date_str, market, investor, trade_type, top_n,
            )
            if df is not None and not df.empty:
                return df

            df = fetch_naver_html_ranking(
                search_date_str, market, investor, trade_type, top_n,
            )
            if df is not None and not df.empty:
                return df

        if PYKRX_AVAILABLE:
            df = fetch_pykrx_deal_ranking(
                search_date_str, market, investor, trade_type, top_n,
            )
            if df is not None and not df.empty:
                return df

        current_date_obj -= timedelta(days=1)

    logger.error(
        "수급 스캐너 완전 실패: 시작일=%s, 시장=%s, 투자주체=%s, 방향=%s",
        target_date_obj, market, investor, trade_type,
    )
    return pd.DataFrame()


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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://finance.daum.net/",
    }

    ticker_code = stock_code.replace(".KS", "").replace(".KQ", "")
    url = f"https://finance.daum.net/quotes/A{ticker_code}"

    try:
        res = requests.get(url, headers=headers, timeout=8)
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
