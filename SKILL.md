---
name: camtasia-ai-cut
description: "把 Camtasia 原生 .cmproj 录屏工程交给 video-editor 做 AI 初剪，产出一个新的可继续编辑的 .cmproj 工程副本，跳过导出成片再回灌的步骤。用户录完屏想直接拿到 AI 粗剪时间轴并在 Camtasia 里接着编辑时使用。不导出 MP4，不生成字幕或封面，不修改原工程，不改动官方 video-editor skill。"
---

# Camtasia AI Cut

给录屏做 AI 初剪，交付物不是新视频，而是一份能在 Camtasia 里直接打开、继续手工编辑的原生工程。原来的做法是录屏导出 MP4，用 `video-editor` 剪一遍，再把结果带回 Camtasia 重新剪辑，最后导出。这里把中间的两次导出去掉：分析链仍由 `video-editor` 完整执行，只是把它的 `kept_intervals` 落到一份新的 `.cmproj` 副本上。

这个 Skill 是自用工具，不修改 `video-editor`，不修改 Camtasia，不读写 Oil Studio 的安装包，也不覆盖用户的原工程。

---

## 与 video-editor 的流程对齐

主体流程不是新造的。`video-editor` 的默认智能剪辑有 7 个阶段，本 Skill 原样复用其中 6 个，只替换最后的输出目标。

| video-editor 阶段 | 本 Skill | 说明 |
| --- | --- | --- |
| 1 音频提取与声学校准 | 复用，前置一步预处理 | 增加分析副本的响度归一化，理由见下文 |
| 2 高精度 ASR 字级识别 | 原样复用 | 百炼 ASR，由 `scripts/run.sh` 负责凭据 |
| 3 自适应停顿检测与证据标注 | 原样复用 | 300ms 门限、能量与 VAD、ASR 对齐 |
| 4 Agent 语义口误规划 | 原样复用 | Agent 读 `semantic_context.json`，写 `semantic_plan.json` |
| 5 切点波形极小值吸附 | 原样复用 | 体现在 `kept_intervals` 里，本 Skill 不改 |
| 6 平台编码与 15ms 微淡化 | 省略 | 不渲染成片，所以不需要编码和淡化 |
| 7 生成 Markdown 审计报告 | 原样复用 | `<name>_edit_report.md` 照常产出 |

流程差异只有三处：

1. 前置：`prepare_analysis.py` 从 `.cmproj` 包内的 `.trec` 造一份只用于分析的 MP4。
2. 替换输出：`cmproj_apply_cuts.py` 复制整包并改写副本的 `project.tscproj`，代替 `render` / 成片导出。
3. 增加验收：`verify_edit_plan.py` 在计划落工程之前做一次只读门禁。它不改 plan，不替代 skill 的报告，可以不跑。

明确不做的事：不手改 `kept_intervals`，不用 `--fast-copy`，不调 `review` 改计划，不把语义判断交给外部模型。`video-editor` 的 `semantic_planner` 固定为 `calling-agent`，语义判断由当前 Agent 读本地转录完成，这一点保持一致。`--dry-run` 也是 skill 自带的模式，不是本 Skill 另立的分支。

---

## 环境

- macOS，Camtasia 2026.0.7（原生 `.cmproj` 格式 `version 10.0`）。
- `ffmpeg` / `ffprobe`，脚本会自动找 PATH 或 `/opt/homebrew/bin`。
- 已安装并可运行的官方 skill：`~/.workbuddy/skills/video-editor`，含 `.venv` 与百炼 ASR 凭据。
- 桥接脚本用系统 `/usr/bin/python3`（3.9），只依赖标准库。`video-editor` 自己用它的虚拟环境，两者不混用。
- 源工程支持两种原生结构，`cmproj_apply_cuts.py` 自动识别：`group`（track 0 一个 Group）和 `flat`（track 0 一个 ScreenVMFile，track 1 一个 UnifiedMedia）。把 `.trec` 导入 Camtasia 再另存，得到的通常是 `flat`，也是真实工作流里更常见的那种。格式细节见 `references/cmproj-format.md`。
- 语义判断细则可选参考 `~/.agents/skills/oil-studio/references/editing-judgment.md`；本 Skill 的 `references/semantic-rules.md` 自带精简版，不依赖那份文件是否存在。

---

## 工作流

下面用四个变量表示一次任务：

```bash
SKILL=~/.workbuddy/skills/camtasia-ai-cut
VE=~/.workbuddy/skills/video-editor
PROJECT="/绝对路径/录制.cmproj"          # 用户刚录完、未编辑的原生工程，只读
BASE="/绝对路径/一次剪辑的工作目录"       # 自己指定，脚本不会自动创建到源工程旁边
ANALYSIS="$BASE/分析源.mp4"
WORK="$BASE/work"
OUT="$BASE/AI初剪.cmproj"
STEM="分析源"                            # = ANALYSIS 的文件名去掉扩展名
```

### 步骤 1 造分析副本

```bash
mkdir -p "$BASE"
/usr/bin/python3 "$SKILL/scripts/prepare_analysis.py" \
    --project "$PROJECT" --out "$ANALYSIS"
```

先看一遍输出再继续：确认 `video_codec` 是 h264 代理流，`mean` 电平、增益、`duration` 都正常，报告的工程时长与 `.trec` 接近。加 `--probe-only` 可以只探测不写文件。没有配套工程、只有独立 `.trec` 时改用 `--trec`，但那样只能跑到步骤 4，出不了工程。

为什么要提亮：本机录屏实测短片 mean `-40.7 dB`、12 分钟长片 `-43.4 dB`。`video-editor` 的自动静音阈值有 `-30.5 dB` 下限，电平过低时真实人声会整体落到阈值以下，被当成静音切掉。短片未提亮时停顿压缩切掉 `26.16s`，把增益封进分析副本后只切 `7.13s`。增益只作用于这份分析 MP4，`.cmproj`、包内 `.trec`、`edit_plan.json` 的语义都不受影响。脚本按 `target_db - mean_db` 自动算增益，上限 24dB，并用 `alimiter` 防止削波。

产物是 `$ANALYSIS` 和同目录的 `分析源_manifest.json`。

### 步骤 2 跑 video-editor 原生分析

```bash
bash "$VE/scripts/run.sh" cut "$ANALYSIS" --work-dir "$WORK" --dry-run
```

产出 `$WORK/semantic_context.json`、`audio_16k.wav`、`transcript.json`。长片会分滑窗，不要只读第一个窗口。

### 步骤 3 Agent 写语义计划并生成剪辑计划

Agent 通读全部 `semantic_context.json`，按 `references/semantic-rules.md` 和 `video-editor/SKILL.md` 的 `semantic_plan.json` 格式写出 `$WORK/semantic_plan.json`。字段规则以官方 skill 为准，尤其是 `removed_quote` 要逐字覆盖完整待删原话、`replacement_ids` 只能指向真正重述的片段、置信度只填 `high` 或 `medium`。拿不准就返回空的 `edits`。

这一步的「大语言模型」就是当前 Agent。`video-editor` 的 `semantic_planner` 固定为 `calling-agent`，`asr_backend` 固定为 `bailian`：百炼只把声音转成文字，文字层面的重讲、废案、口误判断明确交给调用 Skill 的 Agent 完成，`windows[].prompt` 就是官方为这件事准备的完整转录提示词。`build_semantic_context()` 和 `plan_video_cuts()` 都不发起模型请求，语义判断环节不需要另接外部模型。

默认按整簇扫描，不要逐句挑废句。长片里最该删的往往不是单个口水词，而是连续两三次用同一个词起头、却始终没成句的一簇，例如「以前吧我就是。以前呢我用过很多的这个…以前呢。呃众所周知呢。」，真正落地的是后面主谓宾完整的那一句。按 `references/semantic-rules.md` 的「重说簇整段扫描」找簇、找落地句、整簇写成一条删减。Clothespeg 743s 长片实测：逐句挑废句的版本保留了一整段重复开场，整簇扫描后开场干净且前后衔接成句。

默认剪辑力度以 C 档为基线，叠加整簇扫描，也就是 D 这套判断方式。B 档只做停顿压缩与明显口误，偏松；C 档补碎句、半句、多遍收尾；D 档在 C 上再做整簇清理。用户 2026-11-05 确认 B、C 都保留、默认考虑 C，随后 D 档 244 段工程真机打开通过，反馈「基本可以，需要剪辑的部分就很少了」。因此每次直接按「C 档力度 + 整簇扫描」出稿，需要更松时回退到 B。

```bash
bash "$VE/scripts/run.sh" cut "$ANALYSIS" --work-dir "$WORK" --dry-run \
    --semantic-plan "$WORK/semantic_plan.json"
```

产出 `$WORK/${STEM}_edit_plan.json` 和 `$WORK/${STEM}_edit_report.md`。这一步的所有时间坐标都是分析副本上的源片毫秒，正是 `cmproj_apply_cuts.py` 需要的输入。

不要编辑 `kept_intervals`。如果结果不合理，回到 `semantic_plan.json` 或重跑分析，不要在计划上手工打补丁。换一份分析副本重跑 ASR 时，atom 编号可能整体位移，旧的 `semantic_plan.json` 不能直接套，要按新上下文逐字重建。

### 步骤 4 验收门禁

```bash
/usr/bin/python3 "$SKILL/scripts/verify_edit_plan.py" \
    --context "$WORK/semantic_context.json" \
    --plan "$WORK/${STEM}_edit_plan.json" \
    --semantic-plan "$WORK/semantic_plan.json"
```

退出码 1 表示有实质句子的语音被吞，停下来查原因，不要继续生成工程。

门禁用的是音频能量覆盖率，不是 ASR 时间覆盖率。百炼返回的字级时间戳在句子区间内线性均分，长片实测有句子的时间覆盖率只有 `42%~50%`，而同一段的实际音频能量覆盖率是 `94.8%~99.9%`。用时间覆盖率当判据会把正确的计划误判成失败。短于 4 个字的犹豫音单独统计，不进判据。`--pause-threshold` 保持默认 300ms，不需要为让门禁通过去调大它：实测扫到 1800ms 时压缩率从 43.6% 掉到 29.7%，误判句子数也没清零。

### 步骤 5 生成工程副本

```bash
/usr/bin/python3 "$SKILL/scripts/cmproj_apply_cuts.py" \
    --source "$PROJECT" --out "$OUT" \
    --edit-plan "$WORK/${STEM}_edit_plan.json" \
    --title "AI 初剪"
```

脚本自动识别源工程结构，先做三项校验，任一不满足就退出而不写文件：布局属于 `group` 或 `flat` 且是未编辑的单片段工程；plan 的 `original_duration_s` 与源片时长差在 1 秒以内；输出路径不存在。通过后复制整个包，只改写副本的 `project.tscproj`，每个保留区间生成一组片段，用累计刻度保证片段首尾严格相接。

- `group` 布局：`--offset-mode` 保持默认 `group`。这是 2026-11-05 在 Camtasia 里真机验证过的写法，`Group` 是窗口，`Group.mediaStart` 承载源片偏移，内层 media 不动。
- `flat` 布局：画面轨和音频轨同步切。`ScreenVMFile` 直接把源片偏移写在自己的 `mediaStart` 上；`UnifiedMedia` 是扁平容器，偏移必须由内部 `VMFile` / `AMFile` 各自承载，外层 `mediaStart` 保持 0。`--flat-offset-mode` 默认 `inner`，这是 2026-11-05 真机验证通过的写法。`outer`（外层当窗口）会让 Camtasia 弹「OSStatus 错误 -19」并记 `Corrupt Project Opened`，只留作诊断。

静态自检会检查每条轨的片段数、接缝、零时长、id 唯一、素材是否存在，以及 `duration == mediaDuration`。源工程始终只读，最终工程继续引用包内原来的 `.trec`，画面质量不受分析副本的 640x480 代理流影响。

### 步骤 6 真机验收

在 Camtasia 打开 `$OUT`，逐项确认后再继续手工编辑和导出：能否无转换提示直接打开，时间轴片段数是否等于 `kept_intervals` 数量，随机抽查几段的画面是否对应源片正确时刻，播放接缝有无黑帧或音画错位。

重点抽查音画是否同步。画面轨和音频轨按同一份 `kept_intervals` 同步切，静态自检只能证明时间轴自洽，播放几段确认声音跟画面对得上再继续编辑。

---

## 输出规范

1. `AI初剪.cmproj`：完整目录包，可直接双击打开。原工程不变。
2. `分析源.mp4` / `分析源_manifest.json`：分析中间产物，不属于交付物，可随时删除。
3. `$WORK/`：`video-editor` 原生产物，包含 `semantic_context.json`、`semantic_plan.json`、`${STEM}_edit_plan.json`、`${STEM}_edit_report.md`、`audio_16k.wav`、`transcript.json`。保留报告用于复核每一处删减的理由。

---

## 已知限制

- 只接受未编辑的原生工程，且必须是 `group` 或 `flat` 两种布局之一。别的排布会拒绝，避免只切一条轨造成音画错位。
- `group` 布局单个片段约 491.8KB，其中 `UnifiedMedia.effects` 241KB、`audio` 239KB，不能删，删了会改变工程语义。12 分钟长片外推约 147MB，紧凑 JSON 只能省 3.5%，这个量级没真机验证过。`flat` 布局一对片段只有约 2.8KB，260 段实测 `project.tscproj` 1.46MB，体积不再是问题。
- `flat` 布局的偏移层级已在 2026-11-05 真机定论：`UnifiedMedia` 是容器，内层承载偏移。`outer` 写法会让 Camtasia 弹「OSStatus 错误 -19」并记 `Corrupt Project Opened`，不要再用于交付。260 段的 `flat` 工程已真机打开通过；脚本按布局打印规模警告（`flat` 超过 260 段、`group` 超过 11 段时）。
- 要调剪辑力度，只能改 `semantic_plan.json` 后从步骤 3 重跑 `cut --dry-run --semantic-plan`，再过门禁、再生成工程。不要手改 `kept_intervals`。补删后两版工程除剪辑内容外写法完全一致，可以直接在 Camtasia 里对比效果。
- 分析 MP4 因 AAC 帧补齐会比 `.trec` 长几十毫秒，`flat` 工程里音频轨又比画面轨短 66.7ms。脚本取两条轨的公共范围作为源片有效时长，在内存里丢掉没有对应媒体的尾段，不改 plan 文件。
- `silero-vad` 未安装时 `video-editor` 会打印警告并退回能量检测，短片和长片都已在该状态下跑通，结果可用。
- `edit_plan.json` 绑定的是分析副本的文件签名。换了 `$ANALYSIS` 就要从步骤 2 重跑，不能复用旧计划。
- `semantic_plan.json` 的删减粒度是 atom（子句），`removed_quote` 必须逐字覆盖 `remove_start_id` 到 `remove_end_id` 的全部文字，没有「删半句」的字段。跨句的连续起头废案可以整簇删；同一句里把自己说两遍的无能为力，例如「他是解决了麦克自己截屏的这个他是解决麦克自带的这个截屏的一个问题。」。这类句内重复要么靠停顿压缩碰巧切掉，要么先在 Camtasia 里手工处理。`semantic_max_local_cleanup_ms` 只是 2.5 秒安全上限，不是句内切分能力。
- 整簇删减前必须检查簇内 atom 有没有被别的删减当成 `replacement_ids`。校验器会丢弃「替代片段同时被删」的候选，连带让原删减失效。Clothespeg D 版就踩过：C 版删 U0005 时把 U0006 当替代句，直接新增删 U0006 会让 U0005 反而留下变成孤立废话，正确做法是把那条改写成整簇 U0005–U0007、替代句改指落地句 U0012。
- 不做字幕、封面、章节或导出。这些仍由 Camtasia 本身和其他 skill 负责。

---

## 资源导航

- `scripts/prepare_analysis.py`：`.cmproj` / `.trec` 到分析 MP4 的桥接，含电平自动增益与 manifest。
- `scripts/cmproj_apply_cuts.py`：`kept_intervals` 到原生 `.cmproj` 副本的桥接，自动识别 `group` / `flat`，含前置校验与静态自检。
- `scripts/verify_edit_plan.py`：只读验收门禁，音频能量覆盖率判据。
- `references/semantic-rules.md`：Agent 写 `semantic_plan.json` 时的判断规则。
- `references/cmproj-format.md`：两种原生布局的实测事实、A/B 结论、失败路径与体积记录。
