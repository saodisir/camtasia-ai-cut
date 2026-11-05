# Camtasia AI Cut

给 Camtasia 录屏工程做 AI 初剪的 WorkBuddy Skill。交付物不是 MP4，是一份能在 Camtasia 里直接打开、继续手工编辑的 `.cmproj` 副本。

## 它改了哪一步

省掉的是两次导出。以前的路线是录屏导出 MP4，用 `video-editor` 剪一遍，再把结果带回 Camtasia 重剪，最后导出。这里让 `video-editor` 照常跑完整条分析链，只把它的 `kept_intervals` 落到新的 `.cmproj` 副本上，成片仍由 Camtasia 自己导出。

`video-editor` 默认智能剪辑有 7 个阶段，本 Skill 复用 6 个，跳过编码与微淡化。多出来的是前置造分析用 MP4，以及写工程前的一道只读验收门禁。

## 装之前要有

| 依赖 | 说明 |
| --- | --- |
| macOS + Camtasia 2026.0.7 | 原生 `.cmproj` 格式 `version 10.0` |
| `ffmpeg` / `ffprobe` | 脚本会在 PATH 或 `/opt/homebrew/bin` 里找 |
| `video-editor` skill | 装在 `~/.workbuddy/skills/video-editor`，能跑 `scripts/run.sh`，带百炼 ASR 凭据 |
| 系统 `/usr/bin/python3`（3.9） | 本仓库 3 个脚本只用标准库，和 `video-editor` 的虚拟环境分开 |

把目录放到 `~/.workbuddy/skills/camtasia-ai-cut/`。WorkBuddy 会在对话里按需加载 `SKILL.md`，在对话里说明要剪哪个工程就行。六步命令、参数、门禁判据和已知限制都写在 [SKILL.md](SKILL.md)。

## 目录里有什么

| 文件 | 作用 |
| --- | --- |
| `SKILL.md` | 完整操作手册 |
| `scripts/prepare_analysis.py` | 从 `.cmproj` / `.trec` 造分析用 MP4，含电平增益与 manifest |
| `scripts/cmproj_apply_cuts.py` | 把 `kept_intervals` 写进 `.cmproj` 副本，自动识别 `group` / `flat` 布局 |
| `scripts/verify_edit_plan.py` | 写工程前的只读门禁，用音频能量覆盖率判据 |
| `references/semantic-rules.md` | Agent 写 `semantic_plan.json` 的判断规则 |
| `references/cmproj-format.md` | 两种原生布局的实测事实、体积记录与失败路径 |

## 边界

- 源工程只读，输出是副本；不做字幕、封面、章节，也不导出成片。
- 只接未编辑的原生工程，布局必须是 `group` 或 `flat`，别的排布会直接拒绝，避免只切一条轨造成音画错位。
- 语义判断由调用 Skill 的 Agent 读本地转录完成，不另接外部模型；`kept_intervals` 不手改。
- 不修改 `video-editor`，不修改 Camtasia。
