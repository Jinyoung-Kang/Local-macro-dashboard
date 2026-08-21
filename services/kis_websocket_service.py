"""
services/kis_websocket_service.py
KIS(한국투자증권) Open API 웹소켓 기반 KOSPI200 야간선물(CME 연계) 실시간 체결가 수신 모듈

[중요 사전 확인 사항]
1. KIS_APP_KEY/KIS_APP_SECRET가 실전투자 계좌용인지 확인 (모의투자는 야간선물 미지원 가능성 높음)
2. NIGHT_FUTURES_TICKER를 KIS Developers 포털의 국내선물옵션 마스터파일에서
   현재 분기의 실제 야간선물 최근월물 종목코드로 반드시 교체할 것
3. H0MFCNT0 응답 필드 순서(FIELD_INDEX_MAP)를 KIS Developers 포털의
   "실시간시세 > 국내선물옵션 실시간체결가" 문서에서 최종 확인할 것
   (아래는 국내 선물 계열 TR의 일반적인 필드 순서를 기준으로 작성한 추정값입니다)
"""
import json
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import websocket

from services.kis_service import KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL

logger = logging.getLogger(__name__)

# ==============================================================================
# 0. 사용자가 반드시 확인/교체해야 하는 설정값
# ==============================================================================

# ⚠️ 반드시 KIS 마스터파일 또는 HTS에서 확인한 현재 분기 야간선물 종목코드로 교체하세요.
# (예시일 뿐이며 실제 코드가 아닙니다. 만기가 지나면 반드시 갱신해야 합니다.)
NIGHT_FUTURES_TICKER = "101W3000"

# 실전투자 웹소켓 서버 (모의투자는 ws://ops.koreainvestment.com:31000)
KIS_WS_URL = "ws://ops.koreainvestment.com:21000"

# H0MFCNT0 실시간 체결가 필드 순서 (⚠️ KIS 문서에서 최종 확인 필요, 추정값)
FIELD_INDEX_MAP = {
    "종목코드": 0,
    "체결시간": 1,
    "체결가": 2,
    "전일대비": 3,
    "전일대비율": 4,
    "누적거래량": 8,
}


# ==============================================================================
# 1. 웹소켓 접속키(approval_key) 발급
# ==============================================================================
def _get_ws_approval_key() -> str:
    """실시간 웹소켓 전용 접속키 발급 (Access Token과는 별도)"""
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        logger.warning("KIS_APP_KEY/KIS_APP_SECRET 미등록: 야간선물 웹소켓을 시작할 수 없습니다.")
        return ""

    url = f"{KIS_BASE_URL}/oauth2/Approval"
    headers = {"content-type": "application/json; charset=utf-8"}
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "secretkey": KIS_APP_SECRET,
    }

    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200:
            return res.json().get("approval_key", "")
        logger.warning(f"KIS 웹소켓 approval_key 발급 실패: HTTP {res.status_code} - {res.text[:300]}")
    except Exception as e:
        logger.warning(f"KIS 웹소켓 approval_key 발급 예외: {e}")

    return ""


# ==============================================================================
# 2. 야간선물 실시간 스트리머 (싱글턴, 백그라운드 스레드)
# ==============================================================================
class NightFuturesStreamer:
    """
    백그라운드 스레드에서 KIS 웹소켓에 연결을 유지하며,
    KOSPI200 야간선물 체결가를 self._snapshot에 계속 갱신합니다.
    Streamlit 화면은 이 클래스의 get_snapshot()만 읽으면 됩니다.
    """

    def __init__(self, ticker: str = NIGHT_FUTURES_TICKER):
        self.ticker = ticker
        self._lock = threading.Lock()
        self._snapshot = {
            "price": None,
            "prev_close": None,
            "updated_at": None,
            "is_connected": False,
            "error": None,
        }
        self._stop_flag = False
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def get_snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def stop(self):
        self._stop_flag = True

    def _set_snapshot(self, **kwargs):
        with self._lock:
            self._snapshot.update(kwargs)

    def _run_forever(self):
        """연결이 끊기면 자동 재연결 (5초 대기 후 재시도)"""
        while not self._stop_flag:
            try:
                self._connect_and_listen()
            except Exception as e:
                logger.warning(f"야간선물 웹소켓 연결 종료/오류: {e}")
                self._set_snapshot(is_connected=False, error=str(e))
            time.sleep(5)

    def _connect_and_listen(self):
        approval_key = _get_ws_approval_key()
        if not approval_key:
            self._set_snapshot(is_connected=False, error="approval_key 발급 실패")
            time.sleep(30)
            return

        ws = websocket.WebSocket()
        ws.connect(KIS_WS_URL, ping_interval=60)

        subscribe_msg = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": "H0MFCNT0",
                    "tr_key": self.ticker,
                }
            },
        }
        ws.send(json.dumps(subscribe_msg))
        self._set_snapshot(is_connected=True, error=None)
        logger.info(f"야간선물 웹소켓 연결 성공, 구독 종목: {self.ticker}")

        while not self._stop_flag:
            raw = ws.recv()
            if not raw:
                continue

            # PINGPONG 프레임 처리
            if raw[0] in ("0", "1") and "PINGPONG" in raw:
                ws.send(raw)
                continue

            self._handle_message(raw)

    def _handle_message(self, raw: str):
        """
        정상 데이터 프레임 형식: 암호화유무|TR_ID|데이터건수|필드1^필드2^필드3...
        """
        try:
            if "|" not in raw:
                return

            parts = raw.split("|")
            if len(parts) < 4:
                return

            tr_id = parts[1]
            if tr_id != "H0MFCNT0":
                return

            fields = parts[3].split("^")

            price_idx = FIELD_INDEX_MAP["체결가"]
            if len(fields) <= price_idx:
                logger.warning(f"H0MFCNT0 필드 수 부족: {len(fields)}개 (예상 인덱스 {price_idx})")
                return

            price = float(fields[price_idx])
            now_kst = datetime.now(ZoneInfo("Asia/Seoul"))

            with self._lock:
                prev_price = self._snapshot.get("price")
                self._snapshot.update({
                    "price": price,
                    "prev_close": prev_price if prev_price is not None else price,
                    "updated_at": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
                    "is_connected": True,
                    "error": None,
                })
        except Exception as e:
            logger.warning(f"H0MFCNT0 메시지 파싱 실패: {e} / raw={raw[:200]}")


# ==============================================================================
# 3. Streamlit용 싱글턴 접근 함수
# ==============================================================================
@st.cache_resource(show_spinner=False)
def _get_streamer_singleton() -> NightFuturesStreamer:
    """
    앱 프로세스당 단 하나의 웹소켓 연결만 생성되도록 보장합니다.
    (여러 브라우저 세션이 접속해도 백그라운드 스레드는 1개만 유지됩니다)
    """
    return NightFuturesStreamer()


def get_night_futures_snapshot() -> dict:
    """
    화면(macro_service.py 등)에서 호출하는 진입점.
    반환: {"price", "prev_close", "updated_at", "is_connected", "error"}
    """
    streamer = _get_streamer_singleton()
    return streamer.get_snapshot()
