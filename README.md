## 로컬 실행

\`\`\`bash
cd ~/Projects/macro-dashboard-v2
source venv/bin/activate
streamlit run app.py
\`\`\`

## GitHub에 변경사항 올리기 (로컬 → 원격)

\`\`\`bash
cd /Users/jinyoung/Projects/macro-dashboard-v2
git push -u origin main
\`\`\`

## GitHub 웹과 로컬 동기화 원칙

GitHub 웹 저장소와 로컬 폴더는 서로 독립된 사본입니다.
한쪽에서 수정해도 다른 쪽에는 자동으로 반영되지 않습니다.

\`\`\`text
GitHub 웹에서 수정 → 커밋 (원격에만 반영됨)
        ↓
   로컬은 그대로 (변경 없음)
        ↓
로컬에서 git pull 실행해야 비로소 반영됨
\`\`\`

### 로컬에 반영하려면 반드시 필요한 절차

\`\`\`bash
cd /Users/jinyoung/Projects/macro-dashboard-v2
git pull origin main
\`\`\`

**주의:** GitHub 웹에서 파일을 수정한 뒤 로컬 작업을 다시 시작하기 전에는
항상 위 \`git pull\` 명령을 먼저 실행해야 합니다. 이 절차를 건너뛰고
로컬에서 바로 커밋·푸시하면 원격과 로컬 히스토리가 어긋나
\`! [rejected] main -> main (fetch first)\` 오류가 발생할 수 있습니다.

이 경우 아래 순서로 해결합니다.

\`\`\`bash
git pull origin main --rebase
git push -u origin main
\`\`\`
