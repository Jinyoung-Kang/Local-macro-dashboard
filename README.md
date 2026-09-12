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

이 대시보드는 **수집기와 화면이 분리**돼 있습니다. 터미널 두 개를 쓰세요.

```bash
# 터미널 1 — 수집기 (백그라운드에서 미리 수집해 SQLite에 적재)
cd local-macro-dashboard && source venv/bin/activate
python collector.py --loop

# 터미널 2 — 화면
cd local-macro-dashboard && source venv/bin/activate
streamlit run app.py
```

브라우저가 자동으로 열리지 않으면 http://localhost:8501 로 접속하세요.

수집기 없이 `streamlit run app.py`만 실행해도 **동작은 합니다.** 다만 화면이
직접 수집하므로 느립니다(메뉴당 수십 초). 사이드바에 그 사실이 표시됩니다.

---

## 2-1. 수집/표시 분리 구조

기존에는 사용자가 화면을 열 때 수집이 시작돼, 캐시 TTL이 만료된 순간 접속한
사람이 전체 수집 시간을 그대로 기다렸습니다. 지금은 이렇게 분리돼 있습니다.

```
[collector.py · 주기 실행]  →  [data/dashboard.db]  →  [Streamlit · 읽기만]
     느린 외부 수집                 SQLite 파일 1개        체감 ~0.1초
```

### 측정 결과 (메뉴 렌더링 시간)

| 메뉴 | 분리 전 | 분리 후 |
|---|---:|---:|
| 거시경제 매크로 지표 | 26.4s | **0.10s** |
| 섹터 & 자산군 로테이션 | 17.9s | **0.19s** |
| 기관 13F 포트폴리오 | 45.9s | **1.65s** |
| 기관 13F Money 교집합 | 92.3s | **0.11s** |
| 글로벌 투기세력 (COT) | 7.2s | **0.02s** |
| 연준 순유동성 | 6.1s | **0.44s** |

### 수집기 사용법

```bash
python collector.py                 # 1회 전체 수집
python collector.py --only fast     # 시세·수급만 (5분 주기 권장)
python collector.py --only slow     # FRED·KRX·COT (1시간 주기 권장)
python collector.py --only weekly   # SEC 13F (12시간 주기 권장)
python collector.py --loop          # 상주 모드 (위 세 주기를 동시에 관리)
python collector.py --status        # 저장 상태 확인
python collector.py --list          # 수집 작업 목록
python collector.py --purge-days 400  # 오래된 누적 이력 정리
```

**macOS 자동 시작 (launchd)**

```bash
python collector.py --install-launchd   # plist 예시 출력 → 안내대로 저장/등록
```

### 읽기 모드 (`DASHBOARD_READ_MODE`)

| 값 | 동작 | 쓰는 상황 |
|---|---|---|
| `auto` (기본) | 저장본이 신선하면 사용, 오래되면 직접 수집 후 저장 | 평상시 |
| `store_only` | 저장본만 사용. 화면이 외부를 **절대** 기다리지 않음 | 수집기를 항상 켜 두는 경우 |
| `live_only` | 저장 계층 무시 (분리 이전과 동일) | 디버깅 |

```bash
DASHBOARD_READ_MODE=store_only streamlit run app.py
```

### 누적되는 이력 (분리의 부수 효과)

**Naver·Daum·KRX는 과거 날짜 조회를 지원하지 않습니다.** 지금까지는 앱을 끄면
그날 수급 데이터가 사라졌지만, 이제 수집기가 도는 동안 날짜별로 축적됩니다.
외부에서 다시 받을 수 없는 데이터이므로 `data/dashboard.db`는 백업할 가치가
있습니다. 화면의 **🗄️ 데이터 저장소 상태** 메뉴에서 조회할 수 있습니다.

> **⚠️ 추정치는 누적하지 않습니다.** FRED/KRX 접속 실패 시 쓰이는 통계적
> 추정치(`is_estimated=True`)는 누적 이력 테이블에 기록되지 않습니다. 한 번
> 섞이면 나중에 실제 확정치와 구분할 수 없기 때문입니다.

### 저장 형태

| 테이블 | 용도 |
|---|---|
| `snapshots` | "최신 상태" 1건 (매크로 카드, 스크래퍼 결과, 13F, COT …) |
| `timeseries` | (데이터셋, 시리즈, 날짜) → 값. FRED·KRX·순유동성 이력 누적 |
| `observations` | (데이터셋, 날짜, 종목) → 레코드. 수급 랭킹 이력 누적 |
| `collector_runs` | 수집 실행 로그 (성공/실패, 실패 상세) |

동시성은 SQLite **WAL 모드**로 처리합니다. 수집기가 쓰는 동안 화면이 막히지
않습니다(검증: 동시 48회 쓰기 + 268회 읽기, 오류 0건).

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

- `tests/test_regressions.py` — 과거에 실제로 앱을 망가뜨렸던 버그들을 고정
- `tests/test_store.py` — 저장 계층, 직렬화 왕복, 읽기 모드, 스키마 검증

둘 다 네트워크를 쓰지 않으므로 언제든 돌 수 있습니다. 리팩토링 후 반드시
통과해야 합니다.

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
collector.py            ★ 수집기 (Streamlit과 분리된 독립 프로세스)
config.py               지표 매핑 · 시크릿 로더 · 해석 테이블
data/dashboard.db       ★ 수집 결과 저장소 (gitignore, 백업 권장)
services/
  store.py              ★ SQLite 저장 계층 (저장본 우선 읽기)
  datasets.py           ★ 데이터셋 이름·신선도 기준의 단일 출처
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
  data_status_view.py   ★ 저장소 상태 · 신선도 · 누적 이력 조회
tests/                  회귀 테스트 + 저장 계층 테스트
```

**수집 계층 규칙**
- 수집 함수는 `collect_*`(항상 네트워크)와 `fetch_*`/`get_*`(저장본 우선)으로
  짝을 이룹니다. **화면은 항상 후자를 쓰고**, `collector.py`는 전자를 씁니다.
  새 데이터 소스를 추가할 때도 이 짝을 유지하세요.
- 데이터셋 이름은 `services/datasets.py`에서만 정의합니다. 수집기와 화면이
  문자열을 각자 타이핑하면 "수집은 되는데 화면에 안 보이는" 버그가 생깁니다.
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
