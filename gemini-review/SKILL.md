---
name: gemini-review
description: 用 Antigravity CLI (agy) 對當前 plan 與 git 異動執行第三方 review，整理 findings 後與用戶討論修改。
disable-model-invocation: true
---

# Gemini Review Skill

呼叫第二個模型（Gemini，透過 Antigravity CLI）對「當前計劃 + git 異動」做獨立審視，補捉 Claude 自己看不到的盲區。

## 用法

```
/gemini-review [<plan-file-path>] [--base <branch>] [--max-rounds <N>]
```

| 參數 | 說明 |
|------|------|
| `<plan-file-path>` | 可選。要送給 Gemini 的計畫檔（任意 .md 路徑） |
| `--base <branch>` | 可選。Diff 比對的 base，預設 `main` |
| `--max-rounds <N>` | 可選。共識驗證的軟上限輪數（含初審），預設 `3`。到上限不會默默停，而是攤出趨勢問用戶 |

範例：
- `/gemini-review` — review 當前 vs main 的所有異動
- `/gemini-review ~/.claude/plans/3-foo.md` — 帶 plan
- `/gemini-review ~/.claude/plans/3-foo.md --base dev` — 從 dev 拉的 feature
- `/gemini-review --max-rounds 2` — 驗證軟上限設 2 輪

---

## 執行流程

### Step 1 — 解析參數

從 skill args 抽出：
- 第一個非 `--` 開頭的 token → `plan_path`（可空）
- `--base <x>` → `base`，預設 `main`
- `--max-rounds <N>` → `max_rounds`，預設 `3`；須為正整數，否則回退預設

驗證：
- `git rev-parse --verify "$base"`，失敗就停下並回報「base branch 不存在」
- 若有 `plan_path` 但檔案不存在 → 警告用戶，繼續執行（無 plan）

初始化共識迴圈狀態（Claude 在上下文中維護，不需落地成檔）：
- `round = 1`
- `findings_history`：每輪 Gemini findings 全文，供驗證 prompt 與震盪比對
- `actionable_trend`：每輪結束時待修 (high+medium) 數量陣列，供軟上限檢查點顯示趨勢

### Step 2 — 確認 Antigravity CLI 可用

```bash
which agy || echo "MISSING"
```

若 `MISSING` → 中止並提示用戶：

> Antigravity CLI 未安裝。請執行 `curl -fsSL https://antigravity.google/cli/install.sh | bash`，然後重試。

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

> **此模板只用於 `round == 1`（初審）。** `round ≥ 2` 改用 Step 4b 的驗證模板。

### Step 4b — 組「驗證 Prompt」（僅 round ≥ 2）

第二輪起不再從零重審，而是驗證上一輪 findings 的修正結果並只查修正本身的回歸。帶三樣脈絡（findings_history 的上一輪原文、Claude 自述的本輪修正、修正後完整 diff，diff 蒐集方式同 Step 3）：

```
你是一位資深 reviewer。這是一次「修正驗證」，不是全新審查。請用繁體中文回覆。

[上一輪提出的 findings]
<findings_history 中上一輪被處理的 F### 原文>

[Claude 本輪做的修正摘要]
<Claude 自述：針對哪些 F### 改了什麼>

[修正後的完整 diff]
<base...HEAD + 未 commit 變更，蒐集方式同 Step 3>

請執行：
1. 逐筆確認上述「被處理的 findings」是否真的解決（每筆標 RESOLVED / NOT_RESOLVED + 一句理由）
2. 僅針對「本輪修正所新增/變更的程式碼」判斷是否衍生新回歸。
   不要重提上一輪已列、用戶未處理的項目；不要開啟與本次修正無關的新範圍。

輸出必須**嚴格**遵守（無 preamble、無 emoji、無客套話）：

## Verification
- F001: RESOLVED — <理由>
- F003: NOT_RESOLVED — <理由>

## Regressions
（若無，寫「No new issues introduced by the fixes.」；若有，沿用 F### 編號往下接，格式同初審 finding）
## F010 [high/code] file_path:location
**Issue**: ...
**Suggestion**: ...

## Verdict
RESOLVED | CONVERGING | NEEDS_WORK
（RESOLVED = 處理項全解決且無新回歸；CONVERGING = 主要已解決、殘留僅 low/次要；NEEDS_WORK = 有 high/medium 回歸或未解決項）
```

### Step 5 — 執行 Gemini

用暫存檔餵 prompt（避免 argv 過長）：

```bash
prompt_file=$(mktemp -t gemini-review-prompt.XXXXXX)
review_output=$(mktemp -t gemini-review-output.XXXXXX.md)

cat > "$prompt_file" <<'PROMPT_EOF'
<round == 1 用 Step 4 初審模板；round ≥ 2 用 Step 4b 驗證模板>
PROMPT_EOF

run_agy() {
  agy -p "$(cat "$prompt_file")" \
    --sandbox \
    --model "$1" \
    > "$review_output" 2>&1
}

model_used="gemini-3.1-pro-preview"
if ! run_agy "$model_used"; then
  # pro-preview 不可用（model-not-found / quota / 429 / 退出碼非 0）→ 自動降級
  model_used="gemini-3.5-flash"
  run_agy "$model_used"
fi

rm -f "$prompt_file"
echo "MODEL_USED=$model_used"
echo "REVIEW_OUTPUT=$review_output"
```

注意：
- `--sandbox` 強制 read-only（agy 的沙箱模式，取代舊的 `--approval-mode plan`）
- `agy -p` 直接輸出 plain text，無需 `--output-format` flag
- 主模型 `gemini-3.1-pro-preview`，失敗時**自動**降級 `gemini-3.5-flash`
- 每輪都用同一個 `run_agy()`（含降級）；每輪 `$review_output` 各自 mktemp、全部保留

若兩個模型都退出碼非 0 → 把 `$review_output` 內容顯示給用戶並停下。Step 6 呈現時用 `$model_used` 告知用戶這次實際用了哪個模型。

### Step 6 — 解析輸出 + 呈現

> 本步驟同時服務初審（round 1）與驗證輪（round ≥ 2）。驗證輪的 Verdict 解析與終結判斷在 Step 6b。

1. 用 Read tool 讀 `$review_output`
2. **round ≥ 2**：先抽 `## Verification`（逐筆 RESOLVED/NOT_RESOLVED）與 `## Verdict`，回報給用戶；findings 改抽 `## Regressions` 區塊內的 F### block。**round == 1**：抽 `## Summary`（第一個 `## Summary` 後到下一個 `##` 之間）。
3. 抽 findings：每段 `^## F\d+ \[(?<sev>\w+)/(?<cat>\w+)\] (?<loc>.+)$` 起始的 block，內含 `**Issue**:` 與 `**Suggestion**:`。把本輪 findings 全文存入 `findings_history[round]`。
4. 按 severity 分組（high → medium → low）顯示，例如：

```
## Gemini Review 結果

**Model**: <model_used>

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

6. 按勾選結果逐筆執行修改（Edit / Write）。每完成一筆向用戶回報。記下本輪實際修了哪些 F###（供 Step 4b 驗證 prompt 的「修正摘要」與 Step 6b 震盪比對用）。記 `actionable_trend` 追加本輪剩餘待修 (high+medium) 數。

7. 若本輪用戶**未勾選任何 finding**（或 Gemini 本就 `No issues found.`）→ 直接進 Step 7 收尾，不進驗證輪。

### Step 6b — 共識驗證迴圈（終結判斷）

本輪有實際修正後，依序處理：

1. **問用戶是否進驗證輪**：用 AskUserQuestion 問「要把這次修正送 Gemini 驗證嗎？」（進驗證輪 / 直接收尾）。選「直接收尾」→ Step 7。

2. 選「進驗證輪」→ `round += 1`，回 **Step 4b** 組驗證 prompt → **Step 5** 跑（同 `run_agy` 降級）→ **Step 6** 解析。取得 `## Verdict` 後判斷：

   | Verdict / 情況 | 動作 |
   |------|------|
   | **RESOLVED** | 停。回報「Gemini 確認修正完成、無新回歸」→ Step 7 |
   | **CONVERGING** | 停。把殘留 low/次要項列給用戶（不再自動修）→ Step 7 |
   | **CONTESTED**（震盪，優先於 NEEDS_WORK 判斷） | 見第 3 點 |
   | **NEEDS_WORK 且非震盪** | 回 Step 6 呈現 `## Regressions` 的新 findings 供勾選 → 修 → 回到本步驟第 1 點（再問是否驗證） |

3. **震盪偵測 (CONTESTED)**：若 `## Regressions` 中任一新 finding 命中**之前輪次已修過的同一 file:loc 或同一主題**（比對 `findings_history` 與「本輪修了哪些」記錄）→ 判定震盪。**停掉自動迴圈**，把 Gemini 的意見與 Claude 的修正理由兩邊並陳，用 AskUserQuestion 交用戶一刀切：採 Gemini 說法再改 / 維持 Claude 現狀 / 自己手動處理。處理完即 Step 7（不再自動續輪）。

4. **軟上限檢查點**：每次「即將再起新一輪驗證」前，若 `round >= max_rounds` 且尚未 RESOLVED/CONVERGING → **不默默停**，顯示趨勢並用 AskUserQuestion 問：

   ```
   已進行 N 輪，待修 (high+med) 趨勢：5 → 2 → 2
   要 [繼續一輪 / 我手動收尾 / 接受現狀] ?
   ```
   - 繼續一輪 → `max_rounds += 1`，續驗證輪。
   - 手動收尾 / 接受現狀 → Step 7。

### Step 7 — 收尾

- 簡短總結含輪次：「共 N 輪，最終 Verdict = X，處理 P 筆、跳過 Q 筆、爭議交付 R 筆」
- 不自動 commit，提醒用戶自行 commit/push
- 每輪的 `$review_output` 都留在 `/tmp`（mktemp 預設位置，各輪檔名不同），讓用戶之後若想回顧仍找得到；逐一告訴用戶路徑

---

## Fallback

| 狀況 | 處理 |
|------|------|
| `agy` 指令不存在 | 提示 `curl -fsSL https://antigravity.google/cli/install.sh \| bash` 並停下 |
| Base branch 不存在 | 報錯停下 |
| Plan 路徑不存在 | 警告但繼續（當作無 plan） |
| Gemini 退出碼非 0 | 顯示 `$review_output` 給用戶並停下 |
| 輸出無 `## Summary` 或無 `## F` heading | 把原文丟給用戶，問要不要手動處理 |
| 輸出含 `No issues found.` | 告知用戶「Gemini 無發現問題」，結束 |
| `gemini-3.1-pro-preview` 不可用或 429 | 自動降級 `gemini-3.5-flash` 重跑；兩者皆失敗才停下並顯示 `$review_output` |
| 驗證輪缺 `## Verdict` 或格式不符 | 把該輪原文丟給用戶，問要不要手動判定收斂 |
| 偵測到 CONTESTED 震盪 | 停自動迴圈，Gemini 意見與 Claude 理由兩邊並陳，交用戶裁決 |

---

## 設計理由

- **跨專案通用**：本 skill 只用 git + 檔案系統概念，無任何專案特定路徑或 hardcoded 假設
- **Read-only agy**：`--sandbox` 讓 agy 在沙箱中執行（取代舊的 `--approval-mode plan`）；只有 Claude 在用戶確認後才寫
- **Markdown 而非 JSON**：節省 token、無 escape 問題、stdout 直接可讀，regex 解析仍然穩定
- **暫存檔輸出**：Gemini 輸出可能很長，存檔再用 Read 讀比塞進 Bash 結果欄位乾淨
- **驗證取代重審**：第二輪起帶「上輪 findings + 修正摘要 + 只查新增碼」，把 round ≥ 2 從「全新審查」降成「驗證」，新問題只來自有限的修正本身，數學上收斂——根治無狀態重審的無限掃描
- **共識而非數字**：RESOLVED/CONVERGING 是機器間達成「沒事了」；CONTESTED 是機器吵不定時把裁決權交回用戶；軟上限只負責「該回頭問用戶」，停不停看趨勢由用戶決定，不鎖死也不放任
