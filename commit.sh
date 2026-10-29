#!/usr/bin/env bash
# 오늘(ET) 수집분과 대시보드(docs/)만 커밋·푸시한다. 워크플로가 부분 체크아웃이라 다른 경로는 건드리지 않는다.
set -u
DAY=$(TZ=America/New_York date +%F)
BR=$(git rev-parse --abbrev-ref HEAD)
shopt -s nullglob
paths=(data/*/"$DAY" docs)
git add -A -- "${paths[@]}"
git diff --cached --quiet && { echo "nothing new"; exit 0; }
git commit -q -m "$1"
for i in 1 2 3; do
  git pull -q --rebase -X theirs origin "$BR" && git push -q origin "HEAD:$BR" && { echo "pushed: $1"; exit 0; }
  git rebase --abort 2>/dev/null
  sleep $((i * 5))
done
echo "push 실패: $1" >&2
exit 1
