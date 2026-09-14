# Local Macro Dashboard - v3

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

# 수정 후 pull(다운로드)
git pull origin claude/cool-ptolemy-n9b7vs
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

[kis]
app_key    = "KIS_APP_KEY"          # https://apiportal.koreainvestment.com
app_secret = "KIS_APP_SECRET"

[ls]                                # 선택. https://openapi.ls-sec.co.kr
app_key    = "LS_APP_KEY"           # LS증권 홈 > 매매시스템 > API > 사용등록/해지
app_secret = "LS_APP_SECRET"        # ⚠️ "Open API"로 발급 (모의투자 키는 서버가 다릅니다)
# base_url = "https://openapi.ls-sec.co.kr"        # 포트가 바뀌면 여기로 지정

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
| `kis.app_key` / `kis.app_secret` | 장중 수급 가집계와 **교차 검증** 비활성화 |
| `ls.app_key` / `ls.app_secret` | 수급 레이더 폴백 체인에서 LS 단계만 건너뜀 (KIS/Daum/Naver로 충분히 동작) |
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
| 🤖 AI 종합 데이터 분석 | 수집 데이터 기반 AI 리포트 · 유형 3종 · 엔진 점검 |
| 🤖 AI API 연결 테스트 | AI 엔진 연결 진단 |
| 🔌 토스증권 API 테스트 | 토스 Open API 연결 진단 |

---

## 5. 수집/표시 분리 구조

예전에는 사용자가 화면을 열 때 수집이 시작돼, 캐시가 만료된 순간 접속한
사람이 전체 수집 시간을 그대로 기다렸습니다. 지금은 **쓰는 쪽과 읽는 쪽이
프로세스 단위로 분리**돼 있습니다.

```
   프로세스 ①  느린 쪽                프로세스 ②  빠른 쪽
 ┌───────────────────────────┐     ┌───────────────────────────┐
 │       collector.py        │     │    streamlit run app.py   │
 │   (터미널 1 · 주기 실행)   │     │      (터미널 2 · 화면)     │
 └─────────────┬─────────────┘     └─────────────▲─────────────┘
               │ 쓰기                             │ 읽기 (~0.1초)
               │                                 │
               ▼                                 │
        ┌──────────────────────────────────────────────┐
        │            data/dashboard.db  (SQLite · WAL) │
        │  snapshots · timeseries · observations       │
        │  collector_runs · collector_task_runs        │
        └──────────────────────────────────────────────┘
```

WAL 모드라서 ①의 쓰기와 ②의 읽기가 서로를 막지 않습니다. ②를 껐다 켜도
데이터는 남고, ①을 꾸준히 돌리는 것이 곧 백업입니다(→ "누적되는 이력").

**계층 구조** — 위에서 아래로만 의존합니다. 화살표를 거스르는 import가
생기면 순환 참조이거나 계층이 무너진 것입니다.

```
 app.py ─────── 라우팅 · 인증
   │
   ▼
 views/*.py ─── 화면. 계산하지 않고 그리기만 합니다
   │            views/_ui.py = 화면 공용 조각(새로고침 버튼)
   ▼
 services/*_service.py ─── 지표별 수집·가공
   │            collect_*()  = 항상 네트워크   ← collector.py가 부름
   │            fetch_/get_*() = 저장본 우선    ← 화면이 부름
   ▼
 services/store.py ─── 저장 계층 (cached_or_live)
   │
   ├── datasets.py     데이터셋 이름·신선도 기준
   ├── http_client.py  공용 HTTP 세션
   ├── browser_pool.py 공용 헤드리스 Chromium
   └── secrets.py      시크릿 조회  ◀── config.py도 여기에 의존합니다
                                        (config.py는 지표 매핑·해석
                                         테이블이라 어느 계층에서나
                                         읽어 갑니다 · 13곳에서 import)
```

**한 번의 조회가 지나가는 길** — 화면의 모든 데이터 조회는
`store.cached_or_live()` 한 곳을 지납니다. "왜 느린가 / 왜 새로고침해도
안 바뀌는가 / 왜 옛날 값이 보이는가"의 답이 전부 여기 있습니다.

```
 화면이 fetch_*() 호출
        │
        ▼
 cached_or_live(name, live_fn, max_age_seconds)
        │
        ├─ live_only 모드? ───────────────────────▶ 무조건 수집
        │
        ├─ 저장본을 읽는다
        │        │
        │        ├─ store_only 모드? ────────────▶ 오래됐어도 그대로 반환
        │        │                                 (없으면 빈 값. 외부 호출 없음)
        │        │
        │        └─ 신선한가?  ── 예 ────────────▶ 저장본 반환  ← 평상시 여기
        │                        │
        │                    아니오 / 새로고침 요청됨
        │                        │
        │                        ▼
        │                   live_fn() 실행 (느림)
        │                        │
        │                 ┌──────┴──────┐
        │              성공            실패
        │                 │              │
        │                 ▼              ▼
        └──────── 저장 후 반환    오래된 저장본 반환
                                  (화면이 비지 않게. 없으면 빈 값)
```

> **그래서 반환값이 "지금 수집한 값"이라는 보장이 없습니다.** 화면은 반드시
> 수집 시각을 함께 표시해야 합니다. 오래된 값을 최신처럼 보여주는 것이 이
> 구조의 가장 큰 위험입니다.

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

### 문제가 생겼을 때 — 진단 4단계

```bash
# 1) 무엇이 왜 실패했는지
python collector.py --status
python collector.py --status -v       # 스냅샷 상세 + 전체 누락 목록

# 2) 특정 작업의 실행 이력
python collector.py --history krx_futures
python collector.py --history all

# 3) 두 공식 출처(KRX·KIS)가 같은 값을 말하는지 대조
python collector.py --verify

# 4) 그 작업만 다시 실행
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

### 데이터 교차 검증 (KRX · KIS)

이 프로젝트는 공식 API와 비공식 스크래핑을 섞어 씁니다. 비공식 소스는 대상
페이지 구조가 바뀌면 **예외 없이 조용히 틀린 값**을 주기 시작하고, 화면만
봐서는 알아챌 방법이 없습니다. KRX·KIS 두 공식 출처를 기준선으로 두고 같은
수치를 대조합니다.

```bash
python collector.py --verify
```

| 대조 항목 | 출처 A | 출처 B | 출처 C | 언제 |
|---|---|---|---|---|
| KOSPI200 선물 종가 | KRX (화면이 쓰는 값) | KIS | — | 장 마감 후 |
| KOSPI200 미결제약정 | KRX (화면이 쓰는 값) | KIS | — | 장 마감 후 |
| 선물 등락률 | 종가 계산값 | KRX 보고값(FLUC_RT) | — | 장 마감 후 |
| KOSPI200 현물 지수 | KRX Open API | KIS | yfinance `^KS200` | 장 마감 후 |
| 외국인 순매수 1위 종목 | KIS 장중 가집계 | Daum (화면이 쓰는 값) | — | 정규장 중 |

**시간 조건이 항목마다 반대인 이유** — KRX는 *일별 확정 종가*를, KIS는
*현재가*를 줍니다. 장중에 이 둘을 비교하면 항상 다르게 나오므로 시세 대조는
장 마감 후에만 합니다. 반대로 KIS 수급 가집계 TR은 장중 전용이라 마감 후에는
빈 데이터를 돌려줍니다. 그래서 수급 대조는 정규장 중에만 가능합니다.

**"확인 못 함"과 "일치"는 절대 섞지 않습니다.** 키가 없거나 한쪽 수집이
실패해 비교 자체를 못 한 경우를 "일치"로 표시하면 검증이 거짓말이 됩니다.
판정은 `일치 / 불일치 / 수집 실패 / 확인 못 함` 네 가지로 구분됩니다.

화면에서도 같은 검증을 돌릴 수 있습니다:
**🗄️ 데이터 저장소 상태 → 🔍 데이터 교차 검증**.

종료 코드: `0` 불일치 없음 · `1` 불일치 발견 · `2` 키가 없어 검증 불가.

> **⚠️ 추정치는 검증 대상에서 제외됩니다.**
> KRX 수집이 실패해 KODEX 200 기반 추정치(`is_estimated=True`)로 화면이
> 그려지고 있으면, 그 값을 KRX 확정치인 양 비교하지 않고 그 사실을 그대로
> 보고합니다.

### 읽기 모드 (`DASHBOARD_READ_MODE`)

| 값 | 동작 | 쓰는 상황 |
|---|---|---|
| `auto` (기본) | 저장본이 신선하면 사용, 오래되면 직접 수집 후 저장 | 평상시 |
| `store_only` | 저장본만 사용. 오래됐어도 그대로 보여주고, 화면이 외부를 **절대** 기다리지 않음 | 수집기를 항상 켜 둘 때 |
| `live_only` | 저장 계층 무시 | 디버깅 |

### 새로고침 버튼

사이드바의 **데이터 수동 새로고침 🚀**, 그리고 KRX·COT·수급 레이더 화면의
**새로고침** 버튼은 저장본을 "낡은 것"으로 표시해 **지금 보고 있는 화면의
데이터를 실제로 다시 수집**합니다. 그래서 누르면 그 화면의 수집 시간만큼
기다리게 됩니다.

- 수집에 실패한 소스는 새로고침 1회당 한 번만 재시도합니다. 실패해도 기존
  저장본을 계속 보여주므로 화면이 비지 않습니다.
- `store_only` 모드에서는 외부를 부르지 않는다는 약속이 우선이라, 저장본을
  다시 읽기만 합니다(수집은 `collector.py`의 몫). 이때는 그 사실을 알리는
  안내가 뜹니다.

### LS증권 OPEN API 연결이 안 될 때

진단은 **세 단계를 구분해서** 보고합니다. 단계마다 해야 할 일이 다릅니다.

| 진단 메시지 | 실제 의미 | 조치 |
|---|---|---|
| `secrets.toml에 [ls] app_key가 없습니다` | 키 미등록 | `secrets.toml` 작성 |
| `LS 서버에 접속하지 못했습니다` | **망에서 서버에 닿지 못함** — 키와 무관 | 8080 포트 차단 여부 확인, VPN 해제 |
| `OAuth 토큰 발급 거절` | 서버는 닿았는데 키를 거절 | 앱키·시크릿, "Open API" 사용등록 확인 |
| `인증 성공 · 조회 데이터 없음` | **연결 성공** | 정규장에 재확인 |

**포트 주의** — 문서에는 오랫동안 `:8080`이 적혀 있었지만 **서버가 그 포트를
더 이상 열어두지 않습니다.**

```
$ curl -v https://openapi.ls-sec.co.kr:8080/oauth2/token
connect to 61.106.5.137 port 8080 ... failed: Connection refused   (31ms)
```

31ms 즉시 refused는 방화벽 드롭(타임아웃)이 아니라 서버가 포트를 닫아 둔
것입니다. 그래서 표준 **443을 먼저** 쓰고, 옛 환경을 위해 8080을 보조로
남겨 둡니다. 포트가 또 바뀌면 `secrets.toml`의 `[ls] base_url`로 지정하세요.

**엔드포인트 현황**

| TR | 경로 | 상태 |
|---|---|---|
| `t1664` | `/stock/investor` | ✅ 정상 응답 |
| `t1452` | `/stock/market-sum` | ❌ HTTP 404 (경로 없음) |

`t1664`를 먼저 호출합니다. `HTTP 4xx/5xx`는 "데이터 없음"과 달리 장 시간과
무관한 **경로·권한 문제**이므로 진단이 따로 표시합니다.

### 수급 레이더 폴백 체인

```
KIS(장중 가집계) → Daum(API) → Naver(렌더링) → LS(OPEN API) → PyKrx → 누적 이력
```

앞쪽이 성공하면 뒤는 호출되지 않습니다. LS는 KIS/Daum/Naver 뒤에 있으므로,
평소에는 쓰이지 않고 앞의 셋이 모두 실패했을 때만 동원됩니다.

> **LS 키가 없어도 됩니다.** 없으면 그 단계만 건너뜁니다.

### 외부 소스가 모두 실패하면 — 누적 이력으로 대체

Naver·Daum은 **과거 날짜 조회를 지원하지 않아**, 과거 수급 조회는 pykrx
하나에 기대고 있었습니다. 그런데 pykrx는 KRX 웹을 비공식으로 긁는
라이브러리라 KRX가 차단·형식 변경을 하면 통째로 멈춥니다.

이때 빈 화면을 보여주는 대신, **수집기가 쌓아 온 우리 자신의 이력**
(`observations`)에서 가장 가까운 이전 거래일 랭킹을 꺼내 씁니다. 화면에는
어느 날짜의 저장본인지와 함께 "지금 시점의 수급이 아니다"라는 경고가
반드시 함께 뜹니다.

그래서 **수집기를 꾸준히 돌리는 것이 곧 백업**입니다.

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

> **선물 등락률은 종가에서 직접 계산합니다**
> KRX 응답의 `FLUC_RT`를 그대로 쓰지 않습니다. 그 필드가 오지 않을 때 예전
> 코드가 **0.0으로 메웠고**, 화면이 매일 `+0.00%`를 보여줬습니다. 더 나쁜
> 것은 4대 국면 판정이 `등락률 >= 0`을 쓰기 때문에 **하락한 날에도 '신규 롱'
> (강세)으로 뒤집혀** 표시된 점입니다. 실제 2026-09-11에는 1,112.00 →
> 1,088.30 (**-2.13%**)이었는데 화면은 `+0.00%` · '신규 롱'이었습니다.
> 종가 시계열은 KIS와 소수점까지 일치하는 것이 교차 검증으로 확인됐으므로,
> 등락률은 종가에서 계산하고 KRX 보고값은 대조용으로만 둡니다.
> 등락률을 모르면 국면은 **"판정 불가"**이며, 어느 쪽으로도 기울지 않습니다.

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

| 파일 | 건수 | 내용 |
|---|---:|---|
| `tests/test_store.py` | 98 | 저장 계층, 직렬화 왕복, 읽기 모드, 스키마 검증, 수집기 진단, 지표명 정제, 전일 종가 일봉 폴백, 수동 새로고침 |
| `tests/test_verification.py` | 52 | 교차 검증 판정 규칙, 장중/마감 시간 게이트, "확인 못 함"을 "일치"로 위장하지 않는지, 등락률 결측이 국면 판정을 뒤집지 않는지, 외부 소스 전멸 시 누적 이력 대체, LS 인증 단계 구분과 실패 토큰 비캐싱 |
| `tests/test_regressions.py` | 24 | 과거에 실제로 앱을 망가뜨렸던 버그들을 고정 |
| `tests/test_browser_pool.py` | 3 (skip) | 헤드리스 브라우저가 스레드 교체를 견디는지 (로컬 HTTP 서버만 사용, Chromium 없으면 자동 skip) |

모두 외부 네트워크를 쓰지 않으므로 언제든 돌 수 있습니다
(**현재 174 passed · 3 skipped**. skip 3건은 Chromium 미설치 환경 기준입니다).

`test_regressions.py`는 **구조가 되돌아가는 것**도 막습니다. 아래 성질이
깨지면 테스트가 실패합니다.

- `services/` 안에서 `requests.get/post`를 직접 부르는 곳이 없을 것
  (커넥션 재사용을 우회하는 자리를 막습니다)
- `get_secret()` 구현이 `services/secrets.py` 하나뿐일 것
- 새로고침 처리가 `views/_ui.py` 밖에서 다시 구현되지 않을 것
- 일봉 전일 종가 보완이 순차 루프가 아니라 병렬 프리페치로 이뤄질 것
- 수급 폴백이 주말을 건너뛸 것

---

## 9. 구조

```
app.py                  라우팅 · 인증 · 공통 CSS
collector.py            수집기 (Streamlit과 분리된 독립 프로세스)
config.py               지표 매핑 · 해석 테이블 · 키 헬퍼(get_krx_key 등)
data/dashboard.db       수집 결과 저장소 (gitignore, 백업 권장)
data/collector.lock     수집기 중복 실행 방지 락

services/
  ── 기반 계층 ──────────────────────────────────────────────
  store.py              SQLite 저장 계층 (저장본 우선 읽기)
  datasets.py           데이터셋 이름·신선도 기준의 단일 출처
  secrets.py            Streamlit Secrets·환경변수 조회의 단일 출처
  http_client.py        공용 HTTP 세션 2종 (스크래핑용 / API용)
  browser_pool.py       공용 헤드리스 Chromium (전용 워커 스레드 소유)

  ── 지표 수집 ──────────────────────────────────────────────
  macro_service.py      매크로 지표 · FRED
  advanced_macro_service.py      심화 지표 5종 + 해석 임계치
  market_scraper_service.py      TradingView/Yahoo 참고 시세
  night_futures_scraper_service.py    KOSPI200 야간선물
  foreign_index_futures_scraper_service.py  닛케이·항셍 선물
  liquidity_service.py  연준 순유동성
  sector_service.py     섹터·자산군 로테이션
  krx_service.py        KRX 파생 · 한국판 COT
  radar_service.py      국내 수급 레이더 (다단 폴백)
  sec_service.py        SEC 13F
  cot_service.py        CFTC COT
  consensus_service.py  13F 교집합 산출

  ── 증권사 API ─────────────────────────────────────────────
  kis_service.py        한국투자증권 Open API (토큰 · 지수 · 선물 시세)
  kis_websocket_service.py       KIS 실시간 웹소켓
  ls_service.py         LS증권 OPEN API (수급 폴백 4단계)
  toss_service.py       토스증권 Open API (연결 진단 전용)

  ── 검증 · AI ──────────────────────────────────────────────
  verification_service.py  KRX·KIS 교차 검증 (판정: 일치/불일치/실패/확인못함)
  ai_service.py         AI 엔진 라우팅 (NVIDIA / Cloudflare / Cerebras)
  prompts.py            AI 프롬프트 템플릿
  dashboard_snapshot_service.py  전체 원본 데이터 텍스트 생성

views/                  메뉴별 화면 (12개)
  _ui.py                화면 공용 조각 (새로고침 버튼 등)
  data_status_view.py   저장소 상태 · 신선도 · 누적 이력 · 교차 검증 패널

tests/                  회귀 + 저장 계층 + 교차 검증 + 브라우저 풀 테스트
```

**코드 작성 규칙**

- 수집 함수는 `collect_*`(항상 네트워크)와 `fetch_*`/`get_*`(저장본 우선)이
  짝을 이룹니다. **화면은 항상 후자**, `collector.py`는 전자를 씁니다.
- 데이터셋 이름은 `services/datasets.py`에서만 정의합니다. 양쪽이 문자열을
  각자 타이핑하면 "수집은 되는데 화면에 안 보이는" 버그가 생깁니다.
- 새 HTTP 호출은 `requests.get()` 대신 **공용 세션**을 쓰세요. 요청마다
  DNS → TCP → TLS 핸드셰이크를 반복하지 않기 위함입니다.
  용도에 따라 둘 중 하나입니다.

  | 함수 | 재시도 | 쓰는 곳 |
  |---|---|---|
  | `http_client.get_session()` | 5xx·429에 2회 | 공개 웹 스크래핑, 공개 API |
  | `http_client.get_api_session()` | **없음** | 증권사·AI API (KIS·LS·토스·AI) |

  인증 API에서 재시도를 끈 이유는 호출부가 상태코드와 예외를 직접 해석해
  서로 다른 안내를 내보내기 때문입니다. 특히 LS는 "즉시 connection
  refused냐 타임아웃이냐"로 포트 폐쇄와 방화벽 드롭을 구분하는데, 어댑터가
  뒤에서 조용히 재시도하면 그 판단 근거가 사라지고 실패가 몇 배 느려집니다.
- 시크릿은 `services/secrets.get_secret()` 하나만 씁니다. 예전에는 같은
  구현이 세 곳에 복사돼 있어서, 한 곳을 고쳐도 나머지가 옛 규칙으로
  남았습니다("키를 분명히 넣었는데 앱은 없다고 한다"는 버그의 원인).
- 화면에 새로고침 버튼을 놓을 때는 `views/_ui.refresh_button()`을 쓰세요.
  `st.cache_data.clear()`만 부르면 **아직 신선한 저장본이 그대로 반환돼
  화면이 전혀 바뀌지 않습니다.** `store.request_refresh()`와 짝이어야
  실제로 다시 수집합니다.
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
| 새로고침을 눌러도 숫자가 그대로 | `store_only` 모드면 정상입니다(저장본만 다시 읽습니다). `auto`인데도 그대로면 그 소스의 수집이 실패한 것이며, 기존 저장본을 계속 보여줍니다 |
| 새로고침이 오래 걸림 | 정상입니다. 화면의 데이터를 실제로 다시 수집합니다 |
| `--status`에 누락 데이터셋이 많음 | 해당 군을 아직 안 돌렸습니다. `--only fast` / `--only slow` / `--only weekly` |
| "비정상 종료" 표시 | 수집기가 Ctrl+C·절전으로 죽었습니다. 다시 실행하면 정리됩니다 |
| `다른 수집기가 이미 실행 중입니다` | 중복 실행 방지. 기존 프로세스를 끄거나 `--force` |
| KRX 선물이 "추정치" | `krx.api_key` 미설정. KODEX 200 기반 폴백입니다 |
| 수급 레이더 종목 조회 실패 | `--only fast`로 재수집하세요 (과거 버전이 저장한 종목코드 손상 가능성) |
| `playwright` 관련 오류 | `playwright install chromium` 을 실행했는지 확인 |
| `--verify`가 "KIS OAuth2 토큰 발급 실패" | `[kis] app_key`/`app_secret` 오타, 또는 실전/모의 서버 불일치 |
| `--verify`가 "확인 못 함"만 나옴 | 시간 조건 때문입니다. 시세 대조는 장 마감 후, 수급 대조는 정규장 중에만 가능합니다 |
| `--verify`에서 불일치 발견 | 비공식 소스(Daum·Naver·TradingView)의 페이지 구조 변경을 먼저 의심하세요 |
| 수급 레이더에서 `PyKrx/KRX` 빨간 카드 | KRX가 pykrx에 JSON 대신 차단 페이지를 주고 있습니다(`Expecting value: line 1 column 1`). **업그레이드로는 해결되지 않습니다**(1.2.8이 최신). 당일 조회는 KIS/Daum/Naver로 정상이며, 과거 조회는 누적 이력으로 대체됩니다 |
| LS API가 `해당자료가 없습니다` | **인증은 성공한 상태입니다**(토큰 발급 OK). 시세 TR은 정규장에만 데이터를 줍니다. 평일 09:00~15:30에 다시 확인하세요 |
| LS API가 `LS 서버에 접속하지 못했습니다` | **키 문제가 아닙니다.** 443·8080 양쪽 모두 닿지 않은 상태입니다. `curl -v --max-time 10 https://openapi.ls-sec.co.kr/oauth2/token` 로 확인하세요 |
| LS API가 `OAuth 토큰 발급 거절` | 이때가 진짜 키 문제입니다. LS 홈에서 **"Open API"**로 사용등록했는지 확인하세요(모의투자 Open API 키는 서버가 달라 실전 URL에서 거절됩니다) |
| 키를 고쳤는데도 계속 실패 | 해결됐습니다. 실패한 토큰을 더 이상 캐시하지 않으므로 재시작 없이 재시도됩니다 |
| 연결 상태 테스트 결과와 실제 수집이 다름 | 진단은 화면이 쓰는 경로를 그대로 호출합니다(Daum=`investor_purchase` API, Naver=렌더링). 그래도 어긋나면 진단 함수가 다른 경로를 보고 있다는 뜻이니 알려주세요 |
| 수급 레이더가 `외부 데이터 소스가 모두 실패` 경고 | 수집기가 저장해 둔 이력을 보여주는 중입니다. 출처에 적힌 **날짜**를 확인하세요 — 지금 시점의 수급이 아닙니다 |
| `cannot switch to a different thread` | 헤드리스 브라우저 스레드 문제로, 해결됐습니다. 그래도 보이면 `git pull` 후 앱을 재시작하세요 |
| KRX 선물 카드가 `전일 대비 미제공` | KRX가 등락률 필드를 주지 않고 직전 거래일 종가도 없는 경우입니다. 0.00%로 위장하지 않습니다 |
| AI 리포트 본문이 비어 있음 | 해결됐습니다. 예전에는 실패해도 오류가 표시되지 않았습니다. 이제 제공자가 준 오류 원문이 그대로 나옵니다 |
| AI 리포트가 잠시 뒤 사라짐 | 해결됐습니다. 자동 새로고침이 스크립트를 다시 실행할 때 리포트가 날아갔습니다. 이제 세션에 남습니다 |
| AI 리포트가 `HTTP 404` | 그 모델 ID가 내 계정에서 서비스되지 않습니다. 화면의 **🩺 엔진 점검**으로 어떤 엔진이 실제로 응답하는지 확인하세요 |
| AI 리포트가 `HTTP 400` | Context가 모델의 컨텍스트 한도를 넘었을 수 있습니다. `CFTC COT 상세 데이터 포함`을 끄거나 긴 입력을 감당하는 엔진으로 바꾸세요 |
| AI 리포트 유형을 바꿔도 같은 내용 | 해결됐습니다. 예전에는 세 유형이 같은 system prompt를 받았습니다 |

---

## 12. 기여 가이드

혼자 쓰는 로컬 대시보드지만, 6개월 뒤의 자신도 "남"입니다. 아래는 그때의
자신을 위한 절차입니다.

### 작업 전

```bash
source venv/bin/activate
python -m pytest tests/ -q          # 손대기 전에 초록인지 확인 (기준선)
```

기준선이 빨간 상태에서 시작하면, 나중에 실패가 내 변경 탓인지 원래
그랬는지 구분할 수 없습니다.

### 작업 중 — 이 저장소의 원칙 4가지

1. **가짜 숫자를 만들지 마세요.** 수집에 실패하면 빈 결과를 반환합니다.
   전일 대비를 모를 때 `0.00%`로 두면 "보합"이라는 **틀린 정보**가 됩니다.
   "모른다"는 `None`/`N/A`로 남기세요.
2. **불가피한 추정치는 반드시 표시합니다.** `df.attrs["is_proxy"]`
   (또는 `is_estimated`)를 심어 화면과 AI 리포트가 경고를 띄우게 하세요.
   `^MOVE`는 실제 ICE BofA 지수가 아니라 `^TNX` 변동성 역산값입니다.
   표시가 빠지면 "MOVE 140 이상 = 채권 발작" 같은 임계치 해석이 추정치에
   그대로 적용됩니다.
3. **추정치를 누적 테이블에 넣지 마세요.** 한 번 섞이면 확정치와 구분할
   수 없습니다. 스냅샷에는 `status="estimated"`로 저장하되 `timeseries`는
   건드리지 않습니다.
4. **순차 루프 안에서 네트워크를 타지 마세요.** 왕복이 그대로 직렬로
   쌓입니다. 필요한 대상을 먼저 모아 한 번에 병렬로 받으세요
   (`macro_service._prefetch_daily_prev_closes`가 그 패턴입니다).

### 새 지표를 추가할 때

1. `services/datasets.py`에 이름과 신선도 기준을 **먼저** 정의합니다.
   수집기와 화면이 문자열을 각자 타이핑하면 "수집은 되는데 화면에는 안
   보이는" 버그가 납니다.
2. `services/<도메인>_service.py`에 `collect_*()`(항상 네트워크)와
   `fetch_*()`/`get_*()`(저장본 우선, `store.cached_or_live` 경유) 한 쌍을
   만듭니다.
3. `collector.py`에 `_task_*()`를 만들고 `ALL_TASKS`에 등록합니다.
   **빈 결과면 `EmptyResult`를 올리세요** — 기존 저장본을 빈 값으로
   덮지 않기 위해서입니다.
4. `store.missing_datasets()`의 기대 목록에 추가합니다. 빠뜨리면
   `--status`가 누락을 알려주지 못합니다.
5. 화면은 `fetch_*()`만 부릅니다. 화면에서 `collect_*()`를 부르면 수집/표시
   분리가 깨집니다.

### 작업 후

```bash
python -m pytest tests/ -q          # 초록 확인
python collector.py --task <이름>    # 새 태스크를 실제로 한 번 돌려 봅니다
python collector.py --status        # 저장됐는지, 누락으로 안 잡히는지
```

고친 버그가 **다시 생기지 않도록** `tests/test_regressions.py`에 한 건
추가하세요. 이 파일은 "과거에 실제로 앱을 망가뜨렸던 것"만 모읍니다.
네트워크를 쓰는 테스트는 넣지 마세요 — 장중/휴장·레이트리밋에 따라 흔들려
신뢰할 수 없게 됩니다.

### 커밋

```bash
git add -A
git commit -m "무엇을 왜 바꿨는지"
git push -u origin <브랜치>
```

커밋 메시지는 **왜**를 적습니다. 무엇을 바꿨는지는 diff가 이미 말해 줍니다.

---

## 13. 라이선스

이 프로젝트는 [MIT 라이선스](LICENSE)를 따릅니다.

```
Copyright (c) 2026 Jinyoung Kang
```

자유롭게 사용·복제·수정·병합·게시·배포·서브라이선스·판매할 수 있습니다.
조건은 하나입니다 — **저작권 고지와 라이선스 전문을 함께 포함**해야 합니다.
소프트웨어는 어떠한 보증도 없이 "있는 그대로" 제공됩니다.

### 서드파티 고지

이 프로젝트는 아래를 사용하며, 각각의 라이선스를 따릅니다:
Streamlit · pandas · NumPy · Plotly · yfinance · pykrx · requests ·
BeautifulSoup4 · lxml · Playwright · websocket-client
(전체 목록은 `requirements.txt`).

**데이터 출처의 이용 약관은 별개입니다.** 이 대시보드는 Yahoo Finance,
TradingView, Naver, Daum 등을 스크래핑합니다. 비공식 경로이며 각 서비스의
이용 약관이 적용됩니다. **개인적인 조회 용도로만 쓰세요.** 재배포하거나
상업적으로 이용하기 전에는 각 출처의 약관을 확인해야 합니다. 공식 API
(FRED · KRX · KIS · LS · 토스 · SEC · CFTC)는 각자의 이용 조건을 따릅니다.
