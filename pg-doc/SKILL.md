---
name: pg-doc
description: PostgreSQL schema 文件、依賴分析與 lineage 助手。Python 直連 PG 產出五類報告：(1) 指定 prefix 的 table 清單與每張表欄位 md、(2) 多個 view/MV 的 base table 引用頻率分群、(3) 欄位散落分析、(4) 表大小與冷熱排行、(5) MV 完整 lineage 圖。當使用者問到「資料表清單 / schema 文件 / table 欄位文件 / MV 引用了哪些表 / base table 依賴 / 欄位散落在哪 / 找冷表 / lineage」等關鍵字時觸發。
---

# pg-doc Skill

PostgreSQL schema 的文件化與依賴分析助手。Python 直連 PG，繞過任何 LLM token 上限，可處理任意大小的 schema。

## 使用方式

```
/pg-doc <subcommand> [options]
```

實際呼叫的執行檔位於：`$HOME/.claude/skill-scripts/pg-doc/pg_doc.py`

## Capability Map

| Subcommand | 用途 | 觸發詞範例 |
|---|---|---|
| `inventory` | 給定 prefix → 表清單 + 每張表欄位 md | 資料表清單、schema 文件、欄位文件 |
| `mv-deps` | 多個 view/MV → base table 頻率分群 | MV 引用了哪些表、base table 依賴 |
| `column-search` | 給定欄位 → 散落分析（Phase 2） | 欄位散落在哪、找這個欄位 |
| `size` | 表大小 + 冷熱排行（Phase 2） | 找冷表、表大小排行 |
| `lineage` | 遞迴展 view/MV → Mermaid + 樹（Phase 2） | lineage、依賴樹 |

預設輸出：`<CWD>/docs/pg/{tables, mv-deps, lineage, size, column-search}/`

## 連線設定

腳本依下列順序自動找 PG 連線（無需手動設定通常即可運作）：

1. `--conn <DSN>` 旗標或 `DATABASE_URL` 環境變數
2. 從 CWD 向上找 `.mcp.json`，抓 PG 類 MCP 的 connection string
3. `~/.claude.json` → `projects[*].mcpServers`

若多個 MCP 同時是 PG，可加 `--mcp-name=<name>` 明確指定。

### 故障排除

連線失敗時跑診斷模式：
```bash
python3 ~/.claude/skill-scripts/pg-doc/_db.py
```
會印出 resolved DSN（密碼遮罩）與 `current_database()`/`current_user`/`version()`。

若回報 `No PostgreSQL connection found`：
- 設 `export DATABASE_URL=postgresql://user:pass@host:5432/db`
- 或確認當前目錄（或 parent）有 `.mcp.json` 含 `@modelcontextprotocol/server-postgres` 類設定

## 安裝依賴

第一次使用前安裝 psycopg2：
```bash
pip3 install --user psycopg2-binary
```

## Workflow B：Inventory

```bash
python3 ~/.claude/skill-scripts/pg-doc/pg_doc.py inventory \
    --prefix=huaying_ \
    --exclude=_cold --exclude=_log \
    --output=docs/pg
```

輸出：
- `docs/pg/tables/_inventory.md`：表清單總覽（含 type、comment）
- `docs/pg/tables/<comment>.md`：每張表的欄位文件

可用旗標：
- `--prefix`：表名前綴（可重複指定多個）
- `--exclude`：suffix 排除（可重複，常見 `_cold`, `_log`, `_bak`, `_tmp`）
- `--schema`：schema（預設 `public`，可重複）
- `--relkinds`：r,v,m,f,p 逗號分隔（預設全包，**必含 `p`** 才能抓到 partitioned table）
- `--skip-columns`：只產 inventory，不產欄位文件
- `--output`：輸出根目錄（預設 `docs/pg`）

### 重要：partitioned table

`pg_class.relkind='p'` 是 partitioned table。預設 `relkind` 條件已包含；若手動寫 query 記得加上 `'p'`，否則會漏掉分區父表（如 sa390/sa395 這類有大量資料的核心表）。

## Workflow C：MV Dependency Analysis

```bash
python3 ~/.claude/skill-scripts/pg-doc/pg_doc.py mv-deps \
    --views=mv_rp830,mv_rp850,mv_rp220,mv_im3a9 \
    --filter-prefix=huaying_ \
    --output=docs/pg
```

輸出：`docs/pg/mv-deps/<timestamp>.md`，含
- 每個 view 用了哪些 base table
- 依使用次數降序分群（次數相同同群）
- 每張 base table 的 comment + 被哪些 view 引用

可用旗標：
- `--views`：view/MV 名（可逗號分隔，可重複）
- `--filter-prefix`：只計算符合 prefix 的 base table
- `--schema`：預設 `public`
- `--out-name`：自訂輸出檔名（預設時間戳）

### 解析邏輯

腳本對每個 view 跑 `pg_get_viewdef(name, true)`，用 regex 抽 `FROM/JOIN <name>`，並排除：
- CTE alias（`WITH name AS (...)` 中宣告的名字）
- 同名重複只計一次（per view）

不會解析 `EXISTS (SELECT ... FROM ...)` 內的 subquery alias，但 PG 自動展開的 viewdef 通常不會出現這種問題。

## 常用排除規則

| Suffix | 通常代表 |
|---|---|
| `_cold` | 冷資料 / 歸檔表 |
| `_log` | 異動 log |
| `_bak` | 備份 |
| `_tmp` / `_test` | 暫存 / 測試 |

## 檔名 Sanitization

每張表的欄位 md 以 `comment` 命名（去除 schema/前綴）。檔名規則：
- 保留中文與其他 unicode
- 排除 OS 不相容字元：`/ \ : * ? " < > |` 與控制字元
- 兩端 trim 空白與點

## stdout 摘要格式

每次跑完，stdout 會印：
```
✓ pg-doc inventory: 51 tables documented
  output: /path/to/docs/pg/tables
  prefix=huaying_
  excluded suffixes=_cold,_log
  dropped=8
  - ar170(電子發票主檔).md
  - ar175(電子發票明細表).md
  ... and N more
```

讓 Claude 接著能根據結果補充說明、串接後續任務。

## 待實作（Phase 2）

- `column-search`：欄位反查
- `size`：大小 + 冷熱分析
- `lineage`：遞迴 lineage 樹 + Mermaid

實作後會補在 `pg_doc.py` 的 subcommand。
