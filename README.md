# claude-skills

Claude Code skills 的集中管理倉庫。

## 可用 Skills

| Name | Description |
|------|-------------|
| [gemini-review](./gemini-review/) | Gemini 二次 code review，提供獨立第二意見 |

## 安裝

```bash
git clone https://github.com/Bright0505/claude-skills.git
cd claude-skills
chmod +x install.sh
```

### 互動式選擇

```bash
./install.sh
```

### 直接指定

```bash
# 安裝到全域 (~/.claude/skills/)
./install.sh gemini-review

# 安裝到當前專案 (.claude/skills/)
./install.sh gemini-review --project
```

## 新增 Skill

在 repo 根目錄建立子目錄，結構如下：

```
my-skill/
├── SKILL.md       # 必要：Claude Code 讀取的 skill 定義
├── meta.json      # 必要：供 install.sh 掃描的元資料
└── scripts/       # 可選：輔助腳本
    └── helper.sh
```

`meta.json` 格式：

```json
{
  "name": "my-skill",
  "description": "一句話說明這個 skill 做什麼",
  "version": "1.0.0",
  "tags": ["tag1", "tag2"]
}
```

新增完成後 `install.sh` 會自動偵測，不需修改任何其他檔案。
