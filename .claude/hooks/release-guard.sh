#!/usr/bin/env bash
# Claude Code PreToolUse 钩子：强制发版流程（见 CLAUDE.md）。
#   1. release-* 分支上禁止 git commit
#   2. push 到 release-x.y.z 时，该分支必须与 origin/main 完全一致（只能从 main 切出）
#   3. 禁止直接 push 到 main（main 只通过 PR 合并）
# 只检查 Bash 工具里的 git 命令；退出码 2 = 阻止并把 stderr 反馈给 Claude。
set -u
input=$(cat)
cmd=$(printf '%s' "$input" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null) || exit 0
case "$cmd" in *git*) ;; *) exit 0;; esac

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0
branch=$(git branch --show-current 2>/dev/null)

deny() { echo "发版流程钩子阻止：$1" >&2; exit 2; }

# 把整条命令按 && ; | 和换行切成独立的调用段，只对「以 git 开头的段」判断子命令与参数，
# 提交信息、注释里出现的 push/main 字样不会被误判。
segments=$(printf '%s' "$cmd" | sed -E 's/&&|\|\||;|\|/\n/g')
while IFS= read -r seg; do
  seg=$(printf '%s' "$seg" | sed -E 's/^[[:space:]]+//')
  printf '%s' "$seg" | grep -Eq '^(sudo[[:space:]]+)?git([[:space:]]|$)' || continue
  # 去掉 git 及其全局选项（-C dir、-c k=v 等），拿到子命令和参数
  rest=$(printf '%s' "$seg" | sed -E 's/^(sudo[[:space:]]+)?git[[:space:]]+//; s/^(-(C|c)[[:space:]]+[^[:space:]]+[[:space:]]+)*//; s/^(--[a-z-]+(=[^[:space:]]+)?[[:space:]]+)*//')
  sub=${rest%%[[:space:]]*}
  args=${rest#"$sub"}

  # 1. release 分支上不许提交
  if [[ "$sub" == "commit" && "$branch" == release-* ]]; then
    deny "当前在 $branch 上，release 分支不接受提交。改动放功能分支 → 合并 main → 重新从 main 切 release。"
  fi

  [[ "$sub" == "push" ]] || continue

  # 3. 不许直接 push main：参数里显式出现 main / :main，或在 main 分支上裸 push
  if printf '%s' "$args" | grep -Eq '(^|[[:space:]]|:)main([[:space:]]|$)'; then
    deny "main 只通过 PR 合并，不直接 push。用 gh pr create / gh pr merge。"
  fi
  if [[ "$branch" == "main" ]] && ! printf '%s' "$args" | grep -Eq '[[:space:]][^-[:space:]]+[[:space:]]+[^-[:space:]]+'; then
    deny "当前在 main 上裸 push 会推 main。main 只通过 PR 合并。"
  fi

  # 2. push release-x.y.z 必须等于 origin/main
  target=$(printf '%s' "$args" | grep -Eo '(^|[[:space:]]|:)release-[0-9]+(\.[0-9]+)*' | head -1 | sed -E 's/^[[:space:]:]//')
  if [[ -z "$target" && "$branch" =~ ^release-[0-9]+(\.[0-9]+)*$ ]]; then
    target="$branch"
  fi
  if [[ -n "$target" ]]; then
    git fetch -q origin main 2>/dev/null
    rel=$(git rev-parse --verify --quiet "$target^{commit}")
    main=$(git rev-parse --verify --quiet "origin/main^{commit}")
    if [[ -n "$rel" && -n "$main" && "$rel" != "$main" ]]; then
      deny "$target ($(git rev-parse --short "$rel")) 与 origin/main ($(git rev-parse --short "$main")) 不一致。release 分支必须从最新 main 切出：git checkout main && git pull && git checkout -b $target"
    fi
  fi
done <<< "$segments"
exit 0
