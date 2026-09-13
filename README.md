# Local Macro Dashboard

글로벌 매크로 지표·연준 순유동성·섹터 로테이션·CFTC COT·KRX 파생·SEC 13F·
국내 수급 레이더를 한 화면에서 보는 Streamlit 대시보드입니다.

**실행 환경** — macOS (Apple Silicon) 로컬 · Python 3.11+ · Streamlit 1.49+

---

## 1. 최초 설치

```bash
cd ~/Projects/macro-dashboard-v2        # 로컬 작업 폴더

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

# ⚠️ 필수: 렌더링 스크래핑용 Chromium 다운로드 (최초 1회)
# 건너뛰면 "수급 레이더"의 Naver 렌더링 수집이 실패합니다.
playwright install chromium
```

---

## 2. 매일 실행

수집기와 화면이 **분리**돼 있습니다. 터미널 두 개를 씁니다.

```bash
# 터미널 1 — 수집기 (백그라운드에서 미리 수집해 SQLite에 적재)
cd ~/Projects/macro-dashboard-v2 && source venv/bin/activate
python collector.py --loop

# 터미널 2 — 화면
cd ~/Projects/macro-dashboard-v2 && source venv/bin/activate
streamlit run app.py
```

http://localhost:8501 로 접속합니다. 기본 비밀번호는 `admin1234@`이며
`.streamlit/secrets.toml`에서 바꿀 수 있습니다 (3장 참고).

수집기 없이 `streamlit run app.py`만 해도 **동작은 합니다.** 다만 화면이
직접 수집하므로 메뉴당 수십 초가 걸립니다. 사이드바에 그 사실이 표시됩니다.

> **수집기를 항상 켜 두는 경우**, 화면이 외부를 절대 기다리지 않게 할 수
> 있습니다: `DASHBOARD_READ_MODE=store_only streamlit run app.py`

---

## 3. 비밀 키 설정 (선택)

`secrets.toml` **없이도 앱은 정상 실행됩니다.** 키가 없는 기능만 비활성화됩니다.

`.streamlit/secrets.toml` (이 파일은 `.gitignore`에 있어 커밋되지 않습니다):

```toml
[auth]
password = "원하는_접속_비밀번호"

[fred]
api_key = "FRED_API_KEY"            # https://fred.stlouisfed.org/docs/api/api_key.html

[krx]
api_key = "KRX_OPEN_API_AUTH_KEY"   # http://data.krx.co.kr

[ai]
nvidia_api_key   = "..."            # https://build.nvidia.com
cerebras_api_key = "..."
cloudflare_account_id = "..."
cloudflare_api_token  = "..."
```

환경변수(`FRED_API_KEY`, `KRX_AUTH_KEY`, `APP_PASSWORD` …)로도 인식됩니다.

**키가 없을 때의 동작**

| 키 | 없으면 |
|---|---|
| `fred.api_key` | FRED 웹 CSV로 폴백 (대부분 정상 동작) |
| `krx.api_key` | KRX 선물이 KODEX 200 기반 **추정치**로 폴백 (`is_estimated=True` 표시) |
| `ai.*` | AI 리포트 메뉴만 비활성화 |

---

## 4. 화면 구성 (12개 메뉴)

분석 메뉴 → 데이터 상태 → AI → 연결 진단 순서입니다.

| 메뉴 | 내용 |
|---|---|
| 📊 거시경제 매크로 지표 | 환율·국채·원자재·지수, 장단기 금리차, 신용 리스크, **심화 지표 5종** |
| 🏢 연준 순유동성 트래커 | WALCL − TGA − ON RRP |
| 🔄 섹터 & 자산군 로테이션 | S&P 11개 섹터 + 자산군 모멘텀 순위 |
| 📑 기관 13F 포트폴리오 분석 | 기관별 분기 보유 종목 추이 |
| 🎯 기관 13F Money 교집합 | 여러 기관이 공통 보유한 종목 |
| 🏛️ 글로벌 투기세력 (COT) | CFTC 비상업 순포지션 6개 자산 |
| 🇰🇷 국내 파생 & 투기세력 (KRX) | KOSPI200 선물 OI·베이시스·한국판 COT (계약수 기준) |
| 📡 외국인/기관 수급 레이더 | 코스피 투자자별 순매수 상위 |
| 🗄️ 데이터 저장소 상태 | 수집 현황·신선도·실패 원인·누적 이력 |
| 🤖 AI 종합 데이터 분석 | 수집 데이터 기반 AI 리포트 |
| 🤖 AI API 연결 테스트 | AI 엔진 연결 진단 |
| 🔌 토스증권 API 테스트 | 토스 Open API 연결 진단 |

---

## 5. 수집/표시 분리 구조

예전에는 사용자가 화면을 열 때 수집이 시작돼, 캐시가 만료된 순간 접속한
사람이 전체 수집 시간을 그대로 기다렸습니다. 지금은 이렇게 분리돼 있습니다.

```
[collector.py · 주기 실행]  →  [data/dashboard.db]  →  [Streamlit · 읽기만]
     느린 외부 수집                 SQLite 파일 1개        체감 ~0.1초
```

### 측정 결과 (메뉴 렌더링 시간)

| 메뉴 | 분리 전 | 분리 후 |
|---|---:|---:|
| 거시경제 매크로 지표 | 26.4s | **0.13s** |
| 섹터 & 자산군 로테이션 | 17.9s | **0.21s** |
| 기관 13F 포트폴리오 | 45.9s | **0.17s** |
| 기관 13F Money 교집합 | 92.3s | **0.11s** |
| 글로벌 투기세력 (COT) | 7.2s | **0.02s** |
| 연준 순유동성 | 6.1s | **0.40s** |

### 수집 작업 (11개)

| 군 | 작업 | 권장 주기 |
|---|---|---|
| `fast` | `scraper_markets` · `macro_collected` · `radar_rankings` | 5분 |
| `slow` | `fred_series` · `fed_liquidity` · `krx_futures` · `sector_history` · `volatility_history` · `cot_history` · `daum_futures_trend` | 1시간 |
| `weekly` | `sec_13f` | 12시간 |

```bash
python collector.py                 # 1회 전체 수집
python collector.py --only fast     # 시세·수급만
python collector.py --only slow     # FRED·KRX·COT
python collector.py --only weekly   # SEC 13F
python collector.py --loop          # 상주 (세 주기 동시 관리)
python collector.py --list          # 작업 목록
python collector.py --purge-days 400  # 오래된 누적 이력 정리
```

**macOS 자동 시작 (launchd)**

```bash
python collector.py --install-launchd   # plist 예시 출력 → 안내대로 저장/등록
```

### 문제가 생겼을 때 — 진단 3단계

```bash
# 1) 무엇이 왜 실패했는지
python collector.py --status
python collector.py --status -v       # 스냅샷 상세 + 전체 누락 목록

# 2) 특정 작업의 실행 이력
python collector.py --history krx_futures
python collector.py --history all

# 3) 그 작업만 다시 실행
python collector.py --task krx_futures
```

`--status`는 세 가지를 함께 보여줍니다.

- **태스크별 최근 결과** — ✅ 정상 / ⚠️ 데이터 없음 / ❌ 오류 + 실패 이유.
  터미널을 닫아도 DB에 남습니다.
- **있어야 하는데 없는 데이터셋** — 기대 목록과 비교해 빠진 것을 이름으로 표시.
- **실제 실행 상태** — 수집기가 죽으면 기록은 `running`에 남습니다. PID 생존
  여부와 heartbeat로 검사해 `비정상 종료`로 보고합니다.

수집기는 **중복 실행을 막습니다** (`data/collector.lock`). 죽은 프로세스의
락은 자동 회수되며, 필요하면 `--force`로 무시할 수 있습니다.

### 읽기 모드 (`DASHBOARD_READ_MODE`)

| 값 | 동작 | 쓰는 상황 |
|---|---|---|
| `auto` (기본) | 저장본이 신선하면 사용, 오래되면 직접 수집 후 저장 | 평상시 |
| `store_only` | 저장본만 사용. 화면이 외부를 **절대** 기다리지 않음 | 수집기를 항상 켜 둘 때 |
| `live_only` | 저장 계층 무시 | 디버깅 |

### 누적되는 이력

**Naver·Daum·KRX는 과거 날짜 조회를 지원하지 않습니다.** 예전에는 앱을 끄면
그날 수급이 사라졌지만, 이제 수집기가 도는 동안 거래일별로 축적됩니다.
외부에서 다시 받을 수 없는 데이터이므로 **`data/dashboard.db`는 백업할 가치가
있습니다.** 화면의 **🗄️ 데이터 저장소 상태**에서 조회할 수 있습니다.

> **⚠️ 추정치는 누적하지 않습니다.** FRED/KRX 접속 실패 시의 통계적
> 추정치(`is_estimated=True`)는 누적 테이블에 기록되지 않습니다. 한 번 섞이면
> 실제 확정치와 구분할 수 없기 때문입니다.

### 저장 형태

| 테이블 | 용도 |
|---|---|
| `snapshots` | "최신 상태" 1건 (매크로 카드, 13F, COT …) |
| `timeseries` | (데이터셋, 시리즈, 날짜) → 값. FRED·KRX·순유동성 누적 |
| `observations` | (데이터셋, 날짜, 종목) → 레코드. 수급 랭킹 누적 |
| `collector_runs` | 수집 실행 로그 (PID·heartbeat) |
| `collector_task_runs` | 태스크별 결과 (상태·소요시간·실패 이유) |

동시성은 SQLite **WAL 모드**로 처리합니다 (검증: 동시 48회 쓰기 + 268회 읽기,
오류 0건).

---

## 6. 심화 매크로 지표

명목금리·하이일드만으로는 보이지 않는 구조를 메우는 5종입니다.
모두 FRED 공식 시계열이라 스크래핑처럼 조용히 깨지지 않습니다.

| 지표 | FRED ID | 왜 보는가 |
|---|---|---|
| 장단기 금리차 10Y-3M | `T10Y3M` | 뉴욕 연준 침체확률 모델이 쓰는 스프레드. 10Y-2Y보다 예측력이 높다는 것이 연준 리서치의 정설 |
| 10년 실질금리 | `DFII10` | 명목금리에서 인플레 기대를 걷어낸 값. 금·장기 성장주 밸류에이션에 직접 작용 |
| 10년 기대인플레이션 | `T10YIE` | 금리 상승의 원인이 성장/긴축인지 인플레 기대인지 분해 |
| 투자등급 회사채 스프레드 | `BAMLC0A0CM` | 신용 경색은 IG에서 먼저 번짐. 하이일드만 보면 초기 단계를 놓침 |
| 시카고 연준 금융상황지수 | `NFCI` | STLFSI4와 구성이 달라, 두 지수가 갈라지는 것 자체가 신호 |

**임계치** (역사적 분포 기반 참고치이며 투자 판단 근거가 아닙니다)

| 지표 | 정상 | 경계 | 위험 |
|---|---|---|---|
| 10Y-3M | > +0.5%p | 0 ~ +0.5%p | 음수 (역전) |
| 10년 실질금리 | < 1.0% | 1.0 ~ 2.0% | > 2.0% |
| IG 스프레드 | 1.0 ~ 1.5% | 1.5 ~ 2.0% | > 2.0% |
| NFCI | < 0 | 0 ~ 0.5 | > 0.5 |

추이 차트는 10Y-3M이 **역전(음수)** 된 구간을 붉은 음영으로 표시합니다.

---

## 7. 데이터 출처와 신뢰도

공식 API와 비공식 웹 스크래핑을 **섞어서** 씁니다.
투자 판단 전에 각 수치의 출처 배지를 반드시 확인하세요.

| 구분 | 출처 | 신뢰도 |
|---|---|---|
| 금리·신용 스프레드·유동성 | FRED 공식 API | 공식 (일별 확정치) |
| 심화 지표 5종 | FRED 공식 API | 공식 (일간/주간) |
| 환율·원자재·지수 | yfinance | 15분 지연 |
| 미국채 2Y/10Y/30Y | TradingView 공개 scanner | **비공식 참고** |
| 국내 수급·파생 | KRX Open API, pykrx, Daum, Naver | 공식 + 비공식 혼합 |
| 13F 포트폴리오 | SEC EDGAR | 공식 (분기 공시, 45일 지연) |
| CFTC COT | CFTC 공개 API | 공식 (주 1회, 화요일 기준) |
| **MOVE 지수** | `^TNX` 변동성 역산 | ⚠️ **추정치 — 실제 MOVE 아님** |

> **⚠️ MOVE 지수**
> Yahoo Finance는 ICE BofA MOVE 지수를 제공하지 않습니다. 화면의
> "MOVE 대용 추정치"는 10년물 금리 변동성으로 역산한 값이며 실제 MOVE와
> 다릅니다. 해석 표의 임계치(80/120/140)는 실제 MOVE 기준이므로 이 추정치에
> 그대로 적용하지 마세요. 실제 값이 필요하면 ICE/Bloomberg 유료 피드를
> 연결하고 `services/macro_service.py`의 `^MOVE` 분기를 교체해야 합니다.

> **미국채 전일 종가**
> TradingView는 현재 수익률만 주고 전일 종가를 주지 않는 경우가 많습니다.
> 이때는 FRED `DGS2/DGS10/DGS30`(미 재무부 공식 일별 확정치)의 직전 영업일
> 값으로 보완하고, 카드에 `(FRED 확정치)`라고 표시합니다 — 현재가와 전일값의
> 출처가 다르다는 뜻입니다. 어느 출처도 없으면 0.00%로 위장하지 않고
> "전일 대비 미제공"으로 표시합니다.

> **KRX 투자자별 선물 수급은 계약수만 제공합니다**
> Daum의 `/api/investor/future/days`는 금액 기준을 주지 않습니다. 예전의
> "수급 표시 기준: 금액(억원)" 선택지는 `type=PRICE`가 먹힌다는 가정 위에
> 있었는데, 실제 응답은 계약수 그대로였고 그 값을 1억으로 나누는 바람에
> 화면이 전부 0으로 표시됐습니다. 확인할 수 없는 모드를 남겨 두는 대신
> 선택지를 제거하고 계약수 기준만 표시합니다.

> **분봉이 정체된 지표의 전일 종가**
> 카드 수치는 1분봉에서 나오는데, 주말·비유동 시간대에는 피드가 마지막 봉을
> 그대로 반복해 마지막 두 봉이 같아집니다(엔/원 100엔당이 대표적입니다).
> 이때는 "변화 없음(0.00%)"으로 위장하지 않고, 같은 심볼의 **일봉**에서
> 직전 거래일 종가를 가져와 `(일봉 종가)` 표시와 함께 보여 줍니다. 일봉에도
> 없으면 "전일 대비 미제공"으로 둡니다.

비공식 스크래핑은 대상 페이지 구조가 바뀌면 조용히 실패할 수 있습니다.
**수집 실패 시 숫자를 임의로 만들어내지 않습니다.**

---

## 8. 테스트

```bash
pip install pytest
python -m pytest tests/ -v
```

- `tests/test_regressions.py` — 과거에 실제로 앱을 망가뜨렸던 버그들을 고정
- `tests/test_store.py` — 저장 계층, 직렬화 왕복, 읽기 모드, 스키마 검증,
  수집기 진단, 지표명 정제, 전일 종가 일봉 폴백

둘 다 네트워크를 쓰지 않으므로 언제든 돌 수 있습니다 (현재 108건).

---

## 9. 구조

```
app.py                  라우팅 · 인증 · 공통 CSS
collector.py            수집기 (Streamlit과 분리된 독립 프로세스)
config.py               지표 매핑 · 시크릿 로더 · 해석 테이블
data/dashboard.db       수집 결과 저장소 (gitignore, 백업 권장)
data/collector.lock     수집기 중복 실행 방지 락
services/
  store.py              SQLite 저장 계층 (저장본 우선 읽기)
  datasets.py           데이터셋 이름·신선도 기준의 단일 출처
  http_client.py        공용 HTTP 세션 (커넥션 풀 + 재시도)
  browser_pool.py       공용 헤드리스 Chromium (재사용)
  macro_service.py      매크로 지표 · FRED
  advanced_macro_service.py  심화 지표 5종 + 해석 임계치
  market_scraper_service.py  TradingView/Yahoo 참고 시세
  liquidity_service.py  연준 순유동성
  sector_service.py     섹터·자산군 로테이션
  krx_service.py        KRX 파생 · 한국판 COT
  radar_service.py      국내 수급 레이더 (다단 폴백)
  sec_service.py        SEC 13F
  cot_service.py        CFTC COT
  ai_service.py         AI 엔진 라우팅 (NVIDIA / Cloudflare / Cerebras)
  dashboard_snapshot_service.py  전체 원본 데이터 텍스트 생성
views/                  메뉴별 화면
  data_status_view.py   저장소 상태 · 신선도 · 누적 이력
tests/                  회귀 테스트 + 저장 계층 테스트
```

**코드 작성 규칙**

- 수집 함수는 `collect_*`(항상 네트워크)와 `fetch_*`/`get_*`(저장본 우선)이
  짝을 이룹니다. **화면은 항상 후자**, `collector.py`는 전자를 씁니다.
- 데이터셋 이름은 `services/datasets.py`에서만 정의합니다. 양쪽이 문자열을
  각자 타이핑하면 "수집은 되는데 화면에 안 보이는" 버그가 생깁니다.
- 새 HTTP 호출은 `requests.get()` 대신 `services/http_client.get_session()`.
  요청마다 TCP/TLS 핸드셰이크를 반복하지 않기 위함입니다.
- JS 렌더링이 필요하면 `services/browser_pool.fetch_rendered_html()`.
  `sync_playwright()`를 직접 부르면 Chromium이 매번 새로 기동됩니다.
- 수집 실패 시 **그럴듯한 가짜 숫자를 만들지 마세요.** 빈 결과를 반환하거나,
  불가피한 추정치는 `df.attrs["is_proxy"]`로 표시해 화면과 AI 리포트가
  경고를 띄울 수 있게 하세요.

---

## 10. Git 동기화

GitHub 웹과 로컬 폴더는 독립된 사본입니다.

```bash
# 로컬 → GitHub
git add .
git commit -m "설명"
git push -u origin <브랜치명>

# GitHub → 로컬
git pull origin <브랜치명>
```

> GitHub 웹에서 파일을 고친 뒤에는 로컬 작업 전에 반드시 `git pull`부터.

푸시가 거부될 때 (`! [rejected] ... (fetch first)`):

```bash
git pull origin <브랜치명> --rebase
git push -u origin <브랜치명>
```

---

## 11. 자주 겪는 문제

| 증상 | 원인 / 해결 |
|---|---|
| 메뉴가 수십 초씩 걸림 | 수집기가 안 돌고 있습니다. `python collector.py --loop` |
| `--status`에 누락 데이터셋이 많음 | 해당 군을 아직 안 돌렸습니다. `--only fast` / `--only slow` / `--only weekly` |
| "비정상 종료" 표시 | 수집기가 Ctrl+C·절전으로 죽었습니다. 다시 실행하면 정리됩니다 |
| `다른 수집기가 이미 실행 중입니다` | 중복 실행 방지. 기존 프로세스를 끄거나 `--force` |
| KRX 선물이 "추정치" | `krx.api_key` 미설정. KODEX 200 기반 폴백입니다 |
| 수급 레이더 종목 조회 실패 | `--only fast`로 재수집하세요 (과거 버전이 저장한 종목코드 손상 가능성) |
| `playwright` 관련 오류 | `playwright install chromium` 을 실행했는지 확인 |
