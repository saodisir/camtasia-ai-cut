# Camtasia 原生工程格式实测记录

这份文件记录在本机 Camtasia 2026.0.7 上真正验证过的事实，用来避免重新猜格式。Camtasia 没有公开工程格式文档，以下结论只对这一版和这一类工程成立，换版本要重新取样。

## 包结构

`.cmproj` 是目录，不是单文件。用户桌面 `test.cmproj` 的实际内容：

```
test.cmproj/
├── project.tscproj        # 946 KB，纯文本 JSON，真正的工程主体
├── docPrefs               # plist，面板状态与播放头位置
├── bookmarks.plist        # 空 plist
├── ~project.tscproj       # 0 字节占位
├── audiate/
├── media/                 # 内置 shader 等素材
└── recordings/<时间戳>/录制 11-5-2026 1.trec   # 32 MB，录屏本体
```

`sourceBin` 里的素材路径是包内相对路径，例如 `./recordings/1793845292.242292/录制 11-5-2026 1.trec`，主工程文件里没有绝对路径。所以正确做法是整包复制，只改写副本里的 `project.tscproj`，不要试图把素材挪到包外或重新拼接。

## 关键字段

| 字段 | 实测值 | 说明 |
| --- | --- | --- |
| `version` | `"10.0"` | 内置模板最高只有 `8.0`，差两代 |
| `editRate` | `705600000` | 每秒刻度数，内置旧模板是 `30` |
| `authoringClientName` | `Camtasia / Mac / 2026.0.7` | |
| `width` / `height` | `1920.0` / `1080.0` | |
| `metadata.ProjectThumbnail` | 长 base64 字符串 | 清空会导致转换异常 |
| `metadata.projectDuration` | `"00:33"` | 分秒字符串，不是秒数 |
| `timeline.parameters` | `{}` | |
| `captionAttributes.enabled` | `true` | 即使没有字幕轨也保持原值 |
| `timeline...csml.tracks` | 2 条 | track 0 放片段，track 1 为空 |

`csml.tracks[0].medias[0]._type` 是 `Group`，不是旧模板里的 `IMFile`。一个 `Group` 内部有三条子轨：`VMFile`、`ScreenVMFile`、`UnifiedMedia`。样本工程只有这一个 Group，`duration = 23966880000`，除以 `705600000` 得 `33.967s`，与 `projectDuration` 的 `"00:33"` 对齐。

## `.trec` 的流

`.trec` 本身是标准 QuickTime 容器（`ftyp qt  `、`mdat`、`moov`），`ffprobe` 能直接读。样本工程：

| index | codec | 内容 |
| --- | --- | --- |
| 0 | `tscc2` 3416x2048 | 全分辨率主画面，专有编码 |
| 1 | `h264` 640x480 | 低分辨率代理流，可 stream copy |
| 2 | `aac` 44100Hz mono | 麦克风 |

短片 `mean_volume = -40.7 dB`，本机另一段 743.1s 长录屏 `mean_volume = -43.4 dB`。这个电平低于 `video-editor` 自动静音阈值的 `-30.5 dB` 下限，是「人声被当成静音」的根因。分析副本用 `-map 0:1 -map 0:2 -c:v copy` 抽代理流和音频，再对音频做 `volume=+12dB,alimiter=limit=0.95`，短片提到 `-28.7 dB`、长片提到 `-30.9 dB`。代理流只进 ASR 分析，最终工程仍引用包内原始 `.trec`。

## `Group` 是窗口，不是扁平片段

这是最容易出错的一点。为了确认「片段读源片 `[a, b)`」该写在哪里，做过 A/B 两版工程并在 Camtasia 里真机打开。

| 版本 | 写法 | 真机结果 |
| --- | --- | --- |
| A `inner` | `Group.mediaStart = 0`，内层 media 各自带 `mediaStart = a` 且 `duration` 裁短 | 画面取源不对 |
| B `group` | `Group.mediaStart = a`，内层 media 保持完整源片 `duration` 与原始字段 | 用户确认「能打开，没有失败，时间轴是两段」；完整剪辑版「基本上没有问题，把该剪掉的东西都剪掉了」 |

结论：`Group` 是嵌套时间轴，也就是复合片段。外层是落在主时间轴上的一个窗口，`mediaStart` 表示这个窗口在内部完整时间轴上的起点，内层内容保持原样。因此 `cmproj_apply_cuts.py` 默认且只用 `group` 模式，`inner` 仅留作诊断。

同步偏移是个真实陷阱：如果同时给 `Group.mediaStart` 和内层 media 的 `mediaStart` 都写上 `a`，偏移会被算两次。第一版 `AI初剪-原生格式.cmproj` 就有这个问题，已弃用。

## 时间换算

毫秒到刻度用 `ticks = ms * editRate / 1000`。多个片段不能各自独立取整，否则累计误差会让相邻片段产生 1 刻度的重叠或缝隙。做法是先算累计毫秒数组，整体转成刻度，再取差：

```python
cum = [0.0]
for a, b in ranges:
    cum.append(cum[-1] + (b - a))
ticks_at = [round(c * rate / 1000) for c in cum]
# start[i] = ticks_at[i], duration[i] = ticks_at[i+1] - ticks_at[i]
```

这样 `start[i] + duration[i] == start[i+1]` 严格成立。第 4 期那份 404 段的计划第一次生成时出现过 54 处片段重叠，原因就是每段独立取整。

## 失败路径：单文件 `.tscproj`

最早用 app 内置模板 `BlendModePreview.tscproj` 拼单文件工程，用户打开时弹窗「项目是使用较旧版本创建的，要打开此版本，必须转换项目」，点转换后失败。Camtasia 日志里有 `Corrupt Project Opened`。清掉模板残留、把 `version` 提到 `8.0` 重出仍不可用。事后对照原生工程，差异不止一处：

| 项 | 拼出来的单文件 | 原生 `.cmproj` |
| --- | --- | --- |
| 载体 | 单个 `.tscproj` 文件 | 目录包 |
| `version` | `6.0` → `8.0` | `10.0` |
| `editRate` | `30` → `705600000` | `705600000` |
| 片段类型 | `IMFile` | `Group` |
| 素材 | 包外绝对路径 | 包内 `./recordings/...` |
| `metadata` | 被清空 | 含 ProjectThumbnail 等 |

模板还残留 `activeThemeId`、`timeline.parameters.toc` 的缩略图关键帧、`captionAttributes.enabled=true` 但无字幕轨、`description='preview project for blend mode presets'`，转换过程更新注释和资产时会撞上这些悬空引用。结论是不要从模板拼工程，直接复制用户自己保存的原生包。

## 体积

一个 `Group` 序列化后约 491.8KB，其中 `tracks` 占 490.8KB，主要是 `UnifiedMedia.effects` 241KB 和 `audio` 239KB。`effects` 是三条音频效果链，含 `VSTEffect-DFN3NoiseRemoval` 和长 base64 状态；`audio` 是完整音频 media 定义。所有 Group 的这两个字段字节完全相同，但删掉会改变工程语义，不能精简。

12 分钟长片实测 296 个保留片段，外推 `project.tscproj` 约 147MB。紧凑 JSON 实测只省 3.5%（11 个 Group 的 5996.9KB 降到 5786.5KB），不解决根因。这个量级尚未在 Camtasia 真机验证，脚本在 `group` 布局超过 11 个片段时会打印规模警告（`flat` 布局的门槛是 260 段，已真机验证）。

## 第二种布局：扁平双轨 `flat`

把 `.trec` 录屏导入 Camtasia 再另存为 `.cmproj`，得到的不是 Group，而是扁平双轨。真实工作流里多半都是这种结构，也是更常见的入口。

| | track 0 | track 1 |
| --- | --- | --- |
| `_type` | `ScreenVMFile` | `UnifiedMedia` |
| `src` | 1 | 内含 `VMFile` + `AMFile`，各自 `src=1` |
| `duration` | 524331360000 = 743.100s | 524284320000 = 743.033s |
| 序列化字节 | 1190 B | 1601 B |

`sourceBin` 只有一条，指向包内 `./recordings/<时间戳>/<录屏>.trec`，`metadata.projectDuration = "12:23"`。`effects` 是空列表，没有 Group 里那 480KB 的重复大字段。**260 个保留区间、两轨同步切，实测 `project.tscproj` 只有 1.46MB**，上一节 147MB 的体积硬伤在这个布局下不存在。

### 源片有效时长取公共范围

画面轨比音频轨长 66.7ms，分析 MP4 又因 AAC 补齐比 `.trec` 长约 79ms。plan 尾部有一段 `[743054.1, 743100]` 的保留区间，起点已经超出音频轨，直接照抄会得到一个 `mediaDuration=0` 的空片段，静态自检会报 `duration/mediaDuration mismatch`。正确做法是把 `flat` 的源片有效时长定为 `min(video, unified) / editRate * 1000`，也就是 743.033s，让 `ranges_from_plan` 在内存里丢掉这段没有对应媒体的尾巴。plan 文件不动。

### 偏移层级已定论（2026-11-05 真机验证）

`UnifiedMedia` 是扁平容器，不是窗口。真正读源片的是内部 `VMFile` / `AMFile`，偏移必须写在内层，外层 `mediaStart` 保持 0。这跟 `Group` 的结论正好相反。

| 模式 | 写法 | 真机结果 |
| --- | --- | --- |
| `outer` | `UnifiedMedia.mediaStart = 源片偏移`，内层保持完整 743.033s | 打开即弹「未能完成操作（OSStatus 错误 -19）」，日志 `Corrupt Project Opened`，不可用 |
| `inner`（默认，正确） | `UnifiedMedia.mediaStart = 0`，内层 `VMFile` / `AMFile` 各自带 `mediaStart = 源片偏移`、`duration = 片段长度` | 用户确认「可以没问题」，260 段正常打开 |

`outer` 坏在容器 `duration` 只有 1 到 2 秒，内层却保留完整 743 秒且 `mediaStart=0`，内部媒体远超容器范围，Camtasia 打开时判定工程损坏。`inner` 让内层长度与容器一致，结构自洽。

日志证据：`~/Library/Application Support/TechSmith/Camtasia/ruilogV5_*.logtmp` 里 `2026-11-05T05:05:56` 与 `05:07:14`（UTC，本地 13:05 / 13:07）两条 `Corrupt Project Opened$-1`，对应打开 A 版失败，用户截图时间 13:07:24。B 版之后无错误记录。

对照 `Group`：`Group` 是嵌套时间轴，外层是窗口，`Group.mediaStart` 承载偏移，内层保持完整源片；`flat` 的 `UnifiedMedia` 是容器，内层是独立媒体定义，必须各自指定源片位置。两种布局不能互套结论。

脚本已把 `--flat-offset-mode` 默认值改为 `inner`。用新默认参数生成的 260 段与验证通过的 B 版逐片段完全一致（track0 / track1 都比对过）。`outer` 保留仅作诊断。

## 剪辑力度迭代：B / C 两版对照（2026-11-05）

用户反馈 B 版「有些重复的句子和口水词没有完全删掉」，希望比较剪得更狠的一版。这里要区分两件事：A 和 B 用的是同一份 `edit_plan.json`，A 只是工程写法错（外层窗口），剪辑内容一模一样，不存在「验 A 的剪辑效果」；要比较剪辑力度必须重跑 `semantic_plan.json`。

C 版的做法是在 B 的 21 条语义删减上保守补 6 条，逐字匹配自检无误后重跑 `cut --dry-run --semantic-plan`，全部 27 条被采纳，再过能量门禁，最后用同一个 `inner` 默认格式生成工程。

| | B | C |
| --- | --- | --- |
| 语义删减 | 21 条 | 27 条（+6） |
| Agent 标记删除的 atom | 31 | 44 |
| 停顿切除 | 284 | 284（未动参数） |
| 保留区间 | 261 段 | 249 段 |
| 计划时长 | 443.664s | 425.715s |
| 工程时长 | 443.607s | 425.657s |
| 压缩率 | 40.3% | 42.7% |
| 门禁实质发言检查 | 187 句，PASS | 178 句，PASS |
| `project.tscproj` | 1.54MB | 1.48MB |

两版工程结构逐项比对：`version`、`editRate`、`sourceBin`、片段字段形状、`UnifiedMedia.mediaStart=0`、内层偏移写法全部一致；`metadata` 唯一差异是 `projectDuration` 从 `07:23` 变 `07:05`，属时长变化的预期结果。也就是说两版唯一变量是剪辑内容，可以直接在 Camtasia 里比较观感。

补删的 6 条都是碎句、半句、重复起头和多遍收尾，没有动任何含独有信息的句子。C 版门禁里实质发言从 187 句降到 178 句，是这 9 句被整体删掉，不是漏切；被排除的短犹豫音从 27 个降到 23 个。

## 剪辑力度迭代：D 版整簇扫描（2026-11-05）

用户进一步指出，C 版开场仍留着连续几次「以前呢」的重复起头，要求从文字整体判断哪一段是废案、哪一句才落地，并且删后语义要能接上。核对 `video-editor/scripts/semantic_planner.py` 后确认：语义判断本来就是 `calling-agent`，百炼只做 ASR，`windows[].prompt` 就是官方给 LLM 准备的完整转录提示词。缺的不是外部模型，是 Agent 上次按「逐句挑废句」做，没有按整簇读。

D 版在 C 的 27 条上做两件事：把第 1 条由「删 U0005、替代句 U0006」改写成整簇「删 U0005–U0007、替代句 U0012」；另加 4 条跨句重说簇 U0042、U0055、U0164、U0189，共 31 条。改写而不是新增，是因为 C 第 1 条把 U0006 当作 `replacement_ids`，若直接新增删除 U0006，校验器会丢弃那条旧删减，U0005 反而留下变成孤立废话。

| | B | C | D |
| --- | --- | --- | --- |
| 语义删减 | 21 | 27 | 31 |
| 停顿切除 | 284 | 284 | 284 |
| 保留区间 | 261 | 249 | 245 |
| 计划时长 | 443.664s / 40.3% | 425.715s / 42.7% | 414.276s / 44.3% |
| 工程时长 | 443.607s | 425.657s | 414.218s |
| 门禁实质发言 | 187 句 PASS | 178 句 PASS | 173 句 PASS |
| `project.tscproj` | 1.54MB | 1.48MB | 1.39MB |

开场删后文字对比，能直接看出整簇判断的效果：

- C：`…这个软件呃是一个呃剪切板和便签布的一个合并的那么一个软件。以前呢我用过很多的这个剪切板的这个以前呢我用过很多剪切板的这种工具。以前呢。众所周知，就是麦克的这个剪切板呢并不是特别的好用。以前呢我也用过很多…`
- D：`…这个软件呃是一个呃剪切板和便签布的一个合并的那么一个软件。众所周知，就是麦克的这个剪切板呢并不是特别的好用。以前呢我也用过很多呃捷线版的这种工具，有的是付费的…`

「用过很多工具，有付费有免费，都试用过」这条信息由 U0012 加 U0014–U0018 完整承接，没有丢。其余四处衔接也逐句拼读验过：U0041→U0043、U0054→U0056、U0163→U0165、U0185→U0191 都成句。

三版工程结构完全一致，唯一变量是剪辑内容；源工程 `project.tscproj` sha256 始终为 `9e4863bf…0ca472`。用户 2026-11-05 确认 B、C 都保留、默认考虑 C，因此 skill 默认改为「C 档力度 + 整簇扫描」，即 D 这套判断方式，而不是绑定某个固定工程。

**边界**：`semantic_plan.json` 的粒度是 atom，`removed_quote` 必须逐字覆盖完整区间，没有删半句的字段。同一个 atom 里说两遍（U0060「他是解决了…他是解决…」、U0155「你可以认为就他就你可以认为他…」、U0222「大家可我大家可以去…」）无法处理，含独有信息时整句保留。

**真机结果（2026-11-05 14:17）**：用户在 Camtasia 打开 D 版，确认「基本可以，需要剪辑的部分就很少了」。D 档 244 段 `flat` + `inner` 工程通过验收，整簇扫描定为默认动作。B、C、D 三个工程包都保留，A 版失败产物也留着作证伪证据。
