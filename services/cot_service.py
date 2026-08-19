# services/cot_service.py
import pandas as pd
import requests
import streamlit as st

@st.cache_data(ttl=3600*12, show_spinner=False)
def fetch_cftc_cot_legacy(contract_code: str, limit: int = 300):
    """
    CFTC Legacy COT (Futures Only) API 호출
    세 가지 투자자 그룹(Non-Commercial, Commercial, Non-Reportable)의 포지션 파싱
    """
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "cftc_contract_market_code": contract_code,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": limit
    }
    
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if not data:
                return pd.DataFrame(), "해당 자산의 COT 데이터를 찾을 수 없습니다."
            
            df = pd.DataFrame(data)
            
            # 기준 날짜 포맷팅
            df['date'] = pd.to_datetime(df['report_date_as_yyyy_mm_dd']).dt.tz_localize(None)
            
            # 1. 비상업적 투기세력 (Non-Commercial / 스마트머니)
            df['nc_long'] = pd.to_numeric(df.get('noncomm_positions_long_all', 0), errors='coerce').fillna(0)
            df['nc_short'] = pd.to_numeric(df.get('noncomm_positions_short_all', 0), errors='coerce').fillna(0)
            df['nc_net'] = df['nc_long'] - df['nc_short']
            
            # 2. 상업적 헤지세력 (Commercial / 실수요자)
            df['comm_long'] = pd.to_numeric(df.get('comm_positions_long_all', 0), errors='coerce').fillna(0)
            df['comm_short'] = pd.to_numeric(df.get('comm_positions_short_all', 0), errors='coerce').fillna(0)
            df['comm_net'] = df['comm_long'] - df['comm_short']
            
            # 3. 비보고 대상 소규모 투자자 (Non-Reportable / 개미)
            df['nr_long'] = pd.to_numeric(df.get('nonrept_positions_long_all', 0), errors='coerce').fillna(0)
            df['nr_short'] = pd.to_numeric(df.get('nonrept_positions_short_all', 0), errors='coerce').fillna(0)
            df['nr_net'] = df['nr_long'] - df['nr_short']
            
            cols = [
                'date', 
                'nc_long', 'nc_short', 'nc_net',
                'comm_long', 'comm_short', 'comm_net',
                'nr_long', 'nr_short', 'nr_net'
            ]
            return df[cols], None
            
        return pd.DataFrame(), f"CFTC API 호출 실패 (HTTP {resp.status_code})"
    except Exception as e:
        return pd.DataFrame(), f"API 통신 예외 발생: {str(e)}"
