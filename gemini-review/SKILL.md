---
name: gemini-review
description: 用 Gemini CLI 對當前 plan 與 git 異動執行第三方 review，整理 findings 後與用戶討論修改。
disable-model-invocation: true
---

# Gemini Review Skill

呼叫第二個模型（Gemini）對「當前計劃 + git 異動」做獨立審視，補捉 Claude 自己看不到的盲區。

## 用法

```
/gemini-review [<plan-file-path>] [--base <branch>]
```

| 參數 | 說明 |
|------|------|
| `<plan-file-path>` | 可選。要送給 Gemini 的計畫檔（任意 .md 路徑） |
| `--base <branch>` | 可選。Diff 比對的 base，預設 `main` |

範例：
- `/gemini-review` — review 當前 vs main 的所有異動
- `/gemini-review ~/.claude/plans/3-foo.md` — 帶 plan
- `/gemini-review ~/.claude/plans/3-foo.md --base dev` — 從 dev 拉的 feature

---

## 執行流程

### Step 1 — 解析參數

從 skill args 抽出：
- 第一個非 `--` 開頭的 token → `plan_path`（可空）
- `--base <x>` → `base`，預設 `main`

驗證：
- `git rev-parse --verify "$base"`，失敗就停下並回報「base branch 不存在」
- 若有 `plan_path` 但檔案不存在 → 警告用戶，繼續執行（無 plan）

### Step 2 — 確認 Gemini 可用

```bash
which gemini || echo "MISSING"
```

若 `MISSING` → 中止並提示用戶：

> Gemini CLI 未安裝。請執行 `npm install -g @google/gemini-cli`，然後重試。

### Step 3 — 蒐集材料

把以下內容收集到變數，組成單一 prompt 字串：

```bash
# 已 commit 的 diff (含 .md 文件)
git diff "$base"...HEAD

# 未 commit 部分
git status --short
git diff --cached
git diff

# Untracked 檔案（限 50KB 內，超過標註 [SKIPPED: too large]）
for f in $(git ls-files --others --exclude-standard); do
  size=$(wc -c < "$f")
  if [ "$size" -lt 51200 ]; then
    echo "=== UNTRACKED: $f ==="
    cat "$f"
  else
    echo "=== UNTRACKED: $f [SKIPPED: $size bytes] ==="
  fi
done
```

Plan 檔用 Read tool 讀全文。

### Step 4 — 組 Prompt

模板：

```
你是一位資深 reviewer，請對以下材料做獨立審視。請用繁體中文回覆。

[PLAN]
<plan 內容；無則寫「（本次未提供 plan）」>

[BASE BRANCH]
<base>

[GIT DIFF — base...HEAD]
<commit 過的 diff>

[UNCOMMITTED CHANGES]
<staged + unstaged + untracked 整合>

請從以下面向審視：
1. Plan 與實作是否一致（plan 說要做的、diff 是否真的做到？diff 做的、plan 是否提到？）
2. Code 品質：重複、過度設計、邊界錯誤、可讀性、安全性（SQL injection、命令注入等）
3. Doc 一致性：README/CHANGELOG/docs 下 markdown 是否與程式行為一致
4. 風險：可能的回歸、未涵蓋的 edge case、缺失測試

輸出必須**嚴格**遵守以下 markdown 格式（不要有額外 preamble、不要 emoji、不要客套話）：

## Summary
（一段話總結整體狀況，30-80 字）

## F001 [severity/category] file_path:location
**Issue**: 問題描述（一到三句）
**Suggestion**: 建議的修改方向（一到三句）

## F002 [severity/category] file_path:location
**Issue**: ...
**Suggestion**: ...

規則：
- severity ∈ {high, medium, low}
- category ∈ {plan, code, doc, risk}
- file_path 用相對路徑；無特定檔案時寫 `(general)`
- location 寫行號區間（如 `42-58`）或函式名；無則省略冒號（`README.md`）
- 編號從 F001 連號往下
- 若無問題，仍輸出 `## Summary` + 一句「No issues found.」，不輸出任何 F### heading
```

### Step 5 — 執行 Gemini

用暫存檔餵 prompt（避免 argv 過長）：

```bash
prompt_file=$(mktemp -t gemini-review-prompt.XXXXXX)
review_output=$(mktemp -t gemini-review-output.XXXXXX.md)

cat > "$prompt_file" <<'PROMPT_EOF'
<完整 prompt 內容>
PROMPT_EOF

GEMINI_CLI_TRUST_WORKSPACE=true gemini -p "$(cat "$prompt_file")" \
  --output-format text \
  --approval-mode plan \
  --model gemini-2.5-pro \
  > "$review_output" 2>&1

rm -f "$prompt_file"
echo "REVIEW_OUTPUT=$review_output"
```

注意：
- `--approval-mode plan` 強制 read-only
- `--output-format text` → stdout 即模型 markdown
- 預設 `--model gemini-3.1-pro-preview`

若 gemini 退出碼非 0 → 把 `$review_output` 內容顯示給用戶並停下。

### Step 6 — 解析輸出 + 呈現

1. 用 Read tool 讀 `$review_output`
2. 抽 Summary：第一個 `## Summary` 後到下一個 `##` 之間
3. 抽 findings：每段 `^## F\d+ \[(?<sev>\w+)/(?<cat>\w+)\] (?<loc>.+)$` 起始的 block，內含 `**Issue**:` 與 `**Suggestion**:`
4. 按 severity 分組（high → medium → low）顯示，例如：

```
## Gemini Review 結果

**Summary**: <summary 文字>

### High (2)
- **F001** [code] src/foo.py:42-58
  Issue: ...
  Suggestion: ...
- **F003** [risk] (general)
  ...

### Medium (1)
- **F002** ...

### Low (0)
（無）
```

5. 用 AskUserQuestion（multiSelect: true）讓用戶勾選要修的 findings。每筆 option 用 `F001 — <severity> — <一句話 issue>` 當 label。

> 若 findings 超過 4 個（AskUserQuestion options 上限），分批問：每次 4 個，問完一批處理一批，再問下一批。

6. 按勾選結果逐筆執行修改（Edit / Write）。每完成一筆向用戶回報。

### Step 7 — 收尾

- 全部 findings 處理完 → 簡短總結「處理了 X 筆、跳過 Y 筆」
- 不自動 commit，提醒用戶自行 commit/push
- 暫存檔 `$review_output` 留在 `/tmp`（mktemp 預設位置），讓用戶之後若想回顧仍找得到；告訴用戶路徑

---

## Fallback

| 狀況 | 處理 |
|------|------|
| `gemini` 指令不存在 | 提示 `npm install -g @google/gemini-cli` 並停下 |
| Base branch 不存在 | 報錯停下 |
| Plan 路徑不存在 | 警告但繼續（當作無 plan） |
| Gemini 退出碼非 0 | 顯示 `$review_output` 給用戶並停下 |
| 輸出無 `## Summary` 或無 `## F` heading | 把原文丟給用戶，問要不要手動處理 |
| 輸出含 `No issues found.` | 告知用戶「Gemini 無發現問題」，結束 |
| 模型 `gemini-2.5-pro` 不可用或 429 | 提示用戶稍後重試，或改用 `--model gemini-2.5-flash` |

---

## 設計理由

- **跨專案通用**：本 skill 只用 git + 檔案系統概念，無任何專案特定路徑或 hardcoded 假設
- **Read-only Gemini**：`--approval-mode plan` 防 Gemini 動檔；只有 Claude 在用戶確認後才寫
- **Markdown 而非 JSON**：節省 token、無 escape 問題、stdout 直接可讀，regex 解析仍然穩定
- **暫存檔輸出**：Gemini 輸出可能很長，存檔再用 Read 讀比塞進 Bash 結果欄位乾淨
