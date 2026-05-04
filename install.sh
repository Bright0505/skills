#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "用法："
  echo "  ./install.sh                                  # 互動式選擇"
  echo "  ./install.sh <skill-name>                     # 安裝到 Claude Code (~/.claude/skills/)"
  echo "  ./install.sh <skill-name> --project           # 安裝到當前專案 (.claude/skills/)"
  echo "  ./install.sh <skill-name> --agents            # 安裝到中性路徑 (~/.agents/skills/)"
  echo "  ./install.sh <skill-name> --agents --project  # 安裝到當前專案 (.agents/skills/)"
  exit 1
}

# 掃描所有含 meta.json 的子目錄
find_skills() {
  find "$REPO_DIR" -maxdepth 2 -name "meta.json" | sort | while read -r meta; do
    dir="$(dirname "$meta")"
    name="$(basename "$dir")"
    desc="$(python3 -c "import json,sys; d=json.load(open('$meta')); print(d.get('description',''))" 2>/dev/null || echo "")"
    echo "$name|$desc"
  done
}

install_skill() {
  local skill_name="$1"
  local target_dir="$2"
  local scripts_base="$3"
  local skill_dir="$REPO_DIR/$skill_name"

  if [[ ! -d "$skill_dir" ]]; then
    echo "錯誤：找不到 skill '$skill_name'" >&2
    exit 1
  fi

  if [[ ! -f "$skill_dir/SKILL.md" ]]; then
    echo "錯誤：'$skill_name' 缺少 SKILL.md" >&2
    exit 1
  fi

  mkdir -p "$target_dir"
  cp "$skill_dir/SKILL.md" "$target_dir/$skill_name.md"
  echo "✓ SKILL.md 已安裝到 $target_dir/$skill_name.md"

  # 若有腳本則一併安裝
  if [[ -d "$skill_dir/scripts" ]] && compgen -G "$skill_dir/scripts/*" > /dev/null 2>&1; then
    local scripts_target="$scripts_base/$skill_name"
    mkdir -p "$scripts_target"
    cp -r "$skill_dir/scripts/." "$scripts_target/"
    echo "✓ scripts 已安裝到 $scripts_target/"
  fi

  echo ""
  echo "在 Claude Code 中使用：/$skill_name"
}

# --- 主邏輯 ---

skill_name=""
project_mode=false
agents_mode=false

for arg in "$@"; do
  case "$arg" in
    --project) project_mode=true ;;
    --agents)  agents_mode=true ;;
    --help|-h) usage ;;
    -*) echo "未知選項：$arg"; usage ;;
    *) skill_name="$arg" ;;
  esac
done

if $agents_mode; then
  if $project_mode; then
    target_dir="$(pwd)/.agents/skills"
    scripts_base="$(pwd)/.agents/skill-scripts"
  else
    target_dir="$HOME/.agents/skills"
    scripts_base="$HOME/.agents/skill-scripts"
  fi
else
  if $project_mode; then
    target_dir="$(pwd)/.claude/skills"
    scripts_base="$(pwd)/.claude/skill-scripts"
  else
    target_dir="$HOME/.claude/skills"
    scripts_base="$HOME/.claude/skill-scripts"
  fi
fi

# 若未指定 skill，進入互動式選單
if [[ -z "$skill_name" ]]; then
  skills=()
  while IFS= read -r line; do
    skills+=("$line")
  done < <(find_skills)

  if [[ ${#skills[@]} -eq 0 ]]; then
    echo "此 repo 中找不到任何 skill。" >&2
    exit 1
  fi

  echo "可用 skills："
  echo ""
  for i in "${!skills[@]}"; do
    IFS='|' read -r name desc <<< "${skills[$i]}"
    printf "  %d. %-20s %s\n" $((i+1)) "$name" "$desc"
  done
  echo ""
  read -rp "請選擇 (例：1 / 1,3 / 1-3): " choice

  # 展開選擇為 index 清單（1-based）
  selected=()
  IFS=',' read -ra parts <<< "$choice"
  for part in "${parts[@]}"; do
    part="${part// /}"
    if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      from="${BASH_REMATCH[1]}"
      to="${BASH_REMATCH[2]}"
      if (( from > to )); then
        echo "無效範圍：$part（需從小到大）" >&2
        exit 1
      fi
      for (( n=from; n<=to; n++ )); do selected+=("$n"); done
    elif [[ "$part" =~ ^[0-9]+$ ]]; then
      selected+=("$part")
    else
      echo "無效輸入：$part" >&2
      exit 1
    fi
  done

  # 去重並排序
  unique_selected=()
  while IFS= read -r idx; do
    unique_selected+=("$idx")
  done < <(printf '%s\n' "${selected[@]}" | sort -un)

  for idx in "${unique_selected[@]}"; do
    if (( idx < 1 || idx > ${#skills[@]} )); then
      echo "超出範圍：$idx（共 ${#skills[@]} 個）" >&2
      exit 1
    fi
    IFS='|' read -r skill_name _ <<< "${skills[$((idx-1))]}"
    install_skill "$skill_name" "$target_dir" "$scripts_base"
  done
  exit 0
fi

install_skill "$skill_name" "$target_dir" "$scripts_base"
