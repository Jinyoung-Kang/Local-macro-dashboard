# Local Macro Dashboard

글로벌 매크로 지표·연준 순유동성·섹터 로테이션·COT·KRX 파생·SEC 13F·국내 수급
레이더를 한 화면에서 보는 Streamlit 대시보드입니다.

---

## 1. 최초 설치 (macOS / Apple Silicon 기준)

```bash
git clone https://github.com/jinyoung-kang/local-macro-dashboard.git
cd local-macro-dashboard

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

# ⚠️ 필수: 렌더링 스크래핑용 Chromium 다운로드 (최초 1회)
# 이 단계를 건너뛰면 "수급 레이더"의 Naver 렌더링 수집이 실패합니다.
playwright install chromium
```

## 2. 실행

```bash
cd local-macro-dashboard
source venv/bin/activate
streamlit run app.py
```

브라우저가 자동으로 열리지 않으면 http://localhost:8501 로 접속하세요.

---

## 3. 비밀 키 설정 (선택)

`secrets.toml` **없이도 앱은 정상 실행됩니다.** 키가 없는 기능만 비활성화되고,
비밀번호는 기본값(`admin1234@`)으로 동작합니다.

키를 쓰려면 `.streamlit/secrets.toml`을 만드세요. 이 파일은 `.gitignore`에
들어 있어 커밋되지 않습니다.

```toml
[auth]
password = "원하는_접속_비밀번호"

[fred]
api_key = "FRED_API_KEY"        # https://fred.stlouisfed.org/docs/api/api_key.html

[krx]
api_key = "KRX_OPEN_API_AUTH_KEY"   # http://data.krx.co.kr

[ai]
nvidia_api_key   = "..."   # https://build.nvidia.com
cerebras_api_key = "..."
cloudflare_account_id = "..."
cloudflare_api_token  = "..."
```

환경변수(`FRED_API_KEY`, `KRX_AUTH_KEY`, `APP_PASSWORD` …)로도 동일하게 인식됩니다.

---

## 4. 테스트

```bash
pip install pytest
python -m pytest tests/ -v
```

`tests/test_regressions.py`는 네트워크 없이 도는 회귀 테스트입니다.
과거에 실제로 앱을 망가뜨렸던 버그들을 고정해 둔 것이므로, 리팩토링 후
반드시 통과해야 합니다.

---

## 5. 데이터 출처와 신뢰도

이 대시보드는 공식 API와 비공식 웹 스크래핑을 **섞어서** 씁니다.
투자 판단 전에 각 수치의 출처 배지를 반드시 확인하세요.

| 구분 | 출처 | 신뢰도 |
|---|---|---|
| 금리·신용 스프레드·유동성 | FRED 공식 API | 공식 (일별 확정치) |
| 환율·원자재·지수 | yfinance | 15분 지연 |
| 미국채 2Y/10Y/30Y | TradingView 공개 scanner | **비공식 참고** |
| 국내 수급·파생 | KRX Open API, pykrx, Daum, Naver | 공식 + 비공식 혼합 |
| 13F 포트폴리오 | SEC EDGAR | 공식 (분기 공시, 45일 지연) |
| **MOVE 지수** | `^TNX` 변동성 역산 | ⚠️ **추정치 — 실제 MOVE 아님** |

> **⚠️ MOVE 지수 주의**
> Yahoo Finance는 ICE BofA MOVE 지수를 제공하지 않습니다. 화면의
> "MOVE 대용 추정치"는 10년물 금리 변동성으로 역산한 값이며 실제 MOVE와
> 수치가 다릅니다. 해석 표의 임계치(80/120/140)는 실제 MOVE 기준이므로
> 이 추정치에 그대로 적용하지 마세요. 실제 값이 필요하면 ICE/Bloomberg
> 유료 피드를 연결하고 `services/macro_service.py`의 `^MOVE` 분기를
> 교체해야 합니다.

비공식 스크래핑은 대상 웹페이지의 구조가 바뀌면 조용히 실패할 수 있습니다.
수집 실패 시 화면에 "수집 실패"로 표시되며, 숫자를 임의로 만들어내지 않습니다.

---

## 6. 구조

```
app.py                  라우팅 · 인증 · 공통 CSS
config.py               지표 매핑 · 시크릿 로더 · 해석 테이블
services/
  http_client.py        공용 HTTP 세션 (커넥션 풀 + 재시도)   ← 모든 수집기가 공유
  browser_pool.py       공용 헤드리스 Chromium (재사용)
  macro_service.py      매크로 지표 · FRED
  market_scraper_service.py  TradingView/Yahoo 참고 시세
  liquidity_service.py  연준 순유동성 (WALCL/WTREGEN/RRP)
  sector_service.py     섹터·자산군 로테이션
  krx_service.py        KRX 파생 · 한국판 COT
  radar_service.py      국내 수급 레이더 (다단 폴백)
  sec_service.py        SEC 13F
  ai_service.py         AI 엔진 라우팅 (NVIDIA / Cloudflare / Cerebras)
views/                  메뉴별 화면
tests/                  회귀 테스트
```

**수집 계층 규칙**
- 새 HTTP 호출은 `requests.get()`을 직접 쓰지 말고
  `services/http_client.get_session()`을 사용하세요. 요청마다 TCP/TLS
  핸드셰이크를 반복하지 않기 위함입니다.
- JS 렌더링이 필요한 페이지는 `services/browser_pool.fetch_rendered_html()`을
  쓰세요. `sync_playwright()`를 직접 호출하면 Chromium이 매번 새로 기동됩니다.
- 수집 실패 시 **그럴듯한 가짜 숫자를 만들지 마세요.** 빈 결과를 반환하거나,
  불가피하게 추정치를 쓸 때는 `df.attrs["is_proxy"]`로 표시해 화면과 AI
  리포트가 경고를 띄울 수 있게 하세요.

---

## 7. Git 동기화

GitHub 웹과 로컬 폴더는 독립된 사본입니다. 한쪽 수정은 다른 쪽에 자동
반영되지 않습니다.

```bash
# 로컬 → GitHub
git add .
git commit -m "설명"
git push -u origin <브랜치명>

# GitHub → 로컬
git pull origin <브랜치명>
```

> GitHub 웹에서 파일을 고친 뒤에는 로컬 작업 전에 반드시 `git pull`부터
> 실행하세요.

푸시가 거부될 때 (`! [rejected] ... (fetch first)`):

```bash
git pull origin <브랜치명> --rebase
git push -u origin <브랜치명>
```
