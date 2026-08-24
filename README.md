## 로컬 실행

```bash
cd ~/Projects/macro-dashboard-v2
source venv/bin/activate
streamlit run app.py
```

## Git 동기화

GitHub 웹과 로컬 폴더는 독립된 사본입니다. 한쪽 수정은 다른 쪽에 자동 반영되지 않습니다.

**로컬 → GitHub (푸시)**
```bash
cd ~/Projects/macro-dashboard-v2
git add .
git commit -m "설명"
git push -u origin main
```

**GitHub → 로컬 (풀)**
```bash
cd ~/Projects/macro-dashboard-v2
git pull origin main
```

> GitHub 웹에서 파일을 고친 뒤에는 로컬 작업 전에 반드시 `git pull`부터 실행하세요.

**푸시가 거부될 때** (`! [rejected] main -> main (fetch first)`)
```bash
git pull origin main --rebase
git push -u origin main
```
