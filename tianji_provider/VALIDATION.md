# 验证记录

## 0.4.0：无损中间帧与明确的 H.264 画质

旧直接转换路径的 PyAV MJPEG `qscale` 选项没有生效，JPEG 中间帧先损失细节，再由目标 H.264 压缩。0.4.0 改用 RGB24 PPM 作为直接转换的无损中间帧；推荐的显式准备流程直接输出 H.264 CRF 18 / veryfast / yuv420p，并在打开编码器后拒绝未被识别的选项。版本仍兼容合法的 tianji-thor-v2 准备缓存，原始源 planner identity 更新以避免复用旧图像计划。

- 五组测试共 **62 项通过（11.66 秒）**，包括既有对齐、命令补 state、首尾裁剪及异常诊断。
- 四路相机分别检查全部像素与原始裁剪一致，覆盖跨批次重复帧、切片及新解码器随机读取。测试发现 PNG 在当前后端丢失包 keyframe 标志后不能安全重复解码，因此最终采用独立 RGB PPM 帧。
- 真实照片细节合成输入覆盖直接路径及准备路径，分别生成 H.264 v2.1 / v3.0；检查全部输出帧的尺寸、50 FPS PTS、画质，并验证两版本选中帧的解码像素一致。测试质量下限分别为 36 dB（当前后端默认 CRF 23）和 39.5 dB（准备流程 CRF 18）。
- 故障注入证明无效 H.264 编码选项会报错，且不会留下可被当成完整缓存的清单。
- 既有 LeRobot-to-LeRobot 转换各运行 3 次，1,799 帧、3 个 episode，data/video workers 均为 2。后两次中位数：未加载插件 **0.0953 秒**，加载插件 **0.0884 秒**；全部深度校验通过。小样本未观察到回归，不代表吞吐提升。
- 重装后在 `/private/tmp` 验证全局命令，`letools providers list` 显示 tianji-provider 0.4.0。测试脚本及所有中间结果保存在开发机 `../tmp`，不随仓库分发。

完整测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/Users/xieweikai/Downloads/tianji_dataset/tmp \
  /Users/xieweikai/.local/share/uv/tools/letools/bin/python -m pytest \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_alignment.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_end_to_end.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_hold_trim.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_diagnostics.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_video_quality.py \
  --basetemp=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-ppm-v040 \
  -o cache_dir=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-cache -q
```

此前格式/帧数/时间戳验证不能证明画面细节没有损失；旧版直接转换结果需要重新生成。以下记录保留其原始验证范围。


## 0.3.0：独立异常检测

新增只读 `tianji-provider check` 命令，默认完整解码视频；逐轨迹继续检查并输出结构化 JSON 报告。复用转换的严格对齐规则，命令暂停填 state 和首尾静止裁剪仅记为 info，不改变原有转换行为。包版本 0.3.0，provider API 2，缓存格式仍为 tianji-thor-v2。

- 四组测试共 **55 项通过**（3.35 秒）。新增诊断测试覆盖真实问题对应的 100.849 ms、687 ms、3.023 s 反馈缺口及 266.667 ms 视频缺口，验证精确位置和受影响采样点；多信号同时中断均被列出，命令长暂停不判错。
- 合成 ROS 2 MCAP 的完整视频检查通过，76 个视频消息对应 76 个解码帧；检查前后输入文件大小和修改时间不变。缺失目录不会被静默跳过，单条失败后仍继续检查其他录制。
- 故障注入覆盖数值读取失败原因保留、视频解码失败/尺寸变化、无效四元数、空交集，以及 CLI 的 0/1/2 退出码和快速模式检查范围。已有 v2.1/v3.0 转换、状态补命令和静止裁剪测试同时通过。
- 既有 LeRobot-to-LeRobot 路径各运行 3 次，1,799 帧，data/video workers 均为 2；后两次中位数为未加载插件 **0.0808 秒**、加载插件 **0.0782 秒**，均通过深度校验。测试期间另有原始录制扫描；小样本未观察到回归，不表示吞吐提升。
- 重装后从其他目录验证 `letools providers list` 显示 tianji-provider 0.3.0，`tianji-provider check --help` 可调用。LeTools 源码工作区保持无改动。
- 在删除前完整检测 32 条真实录制，耗时 567.87 秒，63,986 个四宫格视频帧全部解码成功。严格规则恰好检出之前确认的 4 条时间缺口异常，没有新增异常；28 条通过，共 73,215 个输出采样点，首尾裁剪 204 帧，中间丢帧为 0。完整结果保存在 `../tmp/anomaly-check-before-delete-v030.json`。这次运行以退出码 1 正确表示存在异常数据。

测试与产物按要求仅保存在开发机的 `../tmp`，未随仓库分发。当前完整测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/Users/xieweikai/Downloads/tianji_dataset/tmp \
  /Users/xieweikai/.local/share/uv/tools/letools/bin/python -m pytest \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_alignment.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_end_to_end.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_hold_trim.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_diagnostics.py \
  --basetemp=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-diagnostics-v030 \
  -o cache_dir=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-cache -q
```


## 0.2.0：保持单条轨迹、命令补 state、首尾裁剪

当前默认行为：一条录制一个 episode；命令暂停按侧用同帧反馈补 action；只裁首尾静止区间。关节/夹爪反馈缺失不会被误判为静止尾部；保留区间内其他观测缺失明确报错。包版本 0.2.0，provider API 2，缓存格式 tianji-thor-v2；旧版缓存被拒绝。

- 34 项测试通过，覆盖独立左右臂/夹爪补齐、命令年龄边界与恢复、30/50/60 FPS、不丢中间帧、首尾裁剪、中途暂停保留、仅夹爪运动、慢动作、全静止输入、观测阈值和命令阈值隔离、旧缓存拒绝，以及缺失反馈不能充当静止依据。
- 包含带命令暂停和首尾静止的 H.264 v2.1/v3.0 后端测试：两版表格逐值相同，四路视频帧数和 PTS 对齐，完整解码像素一致。
- 全部 32 条真实录制完成时间/数值扫描；反馈缺失保护修改后重查了所有 4 条受影响录制。严格模式通过 28 条，共 73,215 帧、28 个 episode，首尾合计裁剪 204 帧（4.08 秒），中间丢弃帧数为 0。每一条用 state 补齐的 action 均已与对应 state 逐值比较。
- 另 4 条明确报错，用户选择暂不转换：夹爪反馈约 100.85 ms；起始反馈约 687 ms；末尾多路反馈约 3.02 s；视频约 266.67 ms。最长反馈缺口前后关节变化约 0.57 rad，不能解释为静止。原始文件保留。

真实视频样本使用 my_bag-26-09-16-15-10-24-806bb9cb：裁开头 0.38 秒后保留 **1 个 episode、2,485 帧**；原命令暂停区间没有切断。左臂 100 帧、右臂 95 帧 action 使用同帧 state。独立逐行原始命令查找及反馈补齐检查全部通过。

该样本准备缓存耗时 64.02 秒（含读取、对齐、四路 H.264 编码和完整像素统计）。v2.1 / v3.0 均 deep-validate 通过，零错误、零警告；8 个 H.264 视频全部解码，帧数和 50 FPS PTS 一致，两版本逐像素相同。12 处首/中/末相机画面对比原始四宫格，最大平均像素绝对误差 2.0115/255。

测试与审计文件仍只在开发机器的 ../tmp：test_tianji_hold_trim.py、all-bags-hold-trim-final.json、observation-gaps.json、real-hold-trim-verification.json；测试产物未随仓库分发。没有修改 LeTools 源码或原始录制。

既有 LeRobot-to-LeRobot 路径小样本回归：相同 1,799 帧，data/video workers 均为 2，各 3 次；后两次中位数为未加载插件 0.0787 秒、加载新版插件 0.0690 秒，全部深度校验通过。测试期间存在其他只读数据检查负载，这一小样本仅未观察到回归，不代表吞吐提升。

## 0.1.0 历史验证（切段策略已停用）

以下 75 个 episode 等结果属于旧版本，不代表当前行为。旧结果保留用于说明原始开发与媒体兼容性验证。


本文保留初始开发环境的历史验证结果。文中的 `../tmp`、绝对路径及测试脚本指开发机器上的本地产物，未随本仓库分发；不是克隆仓库后自带的测试资源。

验证日期：2026-09-20。环境：本机 macOS 26.1 / Apple Silicon，10 个逻辑 CPU，Python 3.12；LeTools 0.1.0、native 0.2.0、PyAV 16、SciPy 1.18.1、mcap 1.4.0。原始数据与缓存均在本机 Downloads 所在文件系统上。没有操作机器人，也没有改动 letools 源码。

## 全量时间与信号检查

`../tmp/all-bags-alignment.json` 保存全部 32 条 bag 的逐路报告：

- 32 条均成功读取，必需字段、关节名称、EEF/wrench 坐标系、夹爪标定检查通过。
- 默认 50 FPS，所有已录制 topic 与视频的时间交集内共 88,059 个网格点。
- 默认缺口处理保留 83,865 帧，分成 75 个连续 episode；排除 4,194 帧，包括不满足时效要求的点和不足两帧的孤立片段。
- 最大视频帧年龄为 99.422 ms，低于默认 100 ms 阈值；这是被选视频消息时间到目标网格的差，不是曝光同步精度。
- 本次全量扫描约 429 秒，其中部分时间与真实样本视频编码并行，不能据此推断独占吞吐。

这里只完成全量数值/时间扫描，以及每条视频开头的尺寸/解码检查；没有对全部 32 条做完整视频转换。原始数据没有被裁剪、改写或删除。

## 实际视频与 LeRobot 输出

样本：`my_bag-26-09-20-11-22-42-29fbb87d`，原始文件总计 163,618,525 字节。输出 3 个片段，帧数分别为 392、1123、284，总计 1,799 帧；每段均含四路 960×540 RGB 视频。

测试产物均在 `../tmp`：

- `real-cache-50/`：显式准备缓存，27,396,583 字节。
- `real-v21-50/`、`real-v30-50/`：两个版本的真实转换结果。
- `real-verification.json`：独立验证明细。

验证包括：

1. 两个版本 deep validation 均通过，零错误、零警告。
2. 表格数值、task、索引、各字段统计和缓存一致；所有输出时间点位于每个输入信号的首末时间之间。
3. 对全部 1,799 行，重新从原始 MCAP 做标量“最后一个不晚于目标时间的样本”查找，左右臂和左右夹爪 action 与转换结果逐值一致。
4. 完整解码 v2.1 的 12 段视频，逐帧验证帧数、尺寸和 50 FPS 呈现时间。
5. 完整解码 v3 的四路合并视频，与 v2.1 每帧 RGB 哈希比较，**全部逐像素一致**。
6. 各段每路相机首、中、末帧，与原始四宫格对应帧/区域比较，共 36 次。H.264 有损编码后，最大平均像素绝对误差为 2.093/255；未发现错相机、错帧或错误排序。

H.264 合并后 AVCC/Annex-B 编码包表示可能改变，因此 LeTools 的原始 packet digest 检查对本例 v3 报不一致；不能将这一检查当成解码图像不一致。这里保留该限制，并用完整解码后的逐像素比较验证。v2.1 与准备缓存的 packet digest 也一致。

样本的显式准备视频阶段为 43.25 秒，约 41.6 个输出时间点/秒（四路共约 166.4 帧/秒）。该时间包含视频解码、四路编码、完整像素统计和准备文件写入，不包含之前的数值读取。四个编码器各 1 线程，H.264 解码器 2 线程，逐 bag 处理。缓存写出 v2.1 为 2.73 秒、v3.0 为 3.06 秒，均设置 data workers=2、video workers=2；这些是一次本地测量，不是大规模性能保证。

另一次独立的数值/时间检查耗时 9.54 秒；通过 `resource.getrusage` 记录到进程峰值 RSS 为 542,572,544 字节（约 517 MiB）。该数值包含 Python/LeTools/SciPy 和解码读取的内存，不是视频编码阶段的峰值。

## 可控数据测试

`../tmp/test_tianji_alignment.py` 与 `../tmp/test_tianji_end_to_end.py` 最终共 **20 项通过**。完整合成轨迹位于 `../tmp/synthetic/`，测试产物在 `../tmp/pytest-final/`。

- 纳秒 epoch 精度、30/50/59/120/1000 Hz 网格无累计漂移。
- 交集为空、越界外推、倒退时间、重复时间保留最后一次更新。
- 命令不读取未来值，区分状态插值缺口与命令过期，连续片段切分。
- 137 Hz 分量降到 50 Hz 时被抑制，3 Hz 分量保持幅相；SLERP 正确跨越 ±180° 和四元数符号翻转。
- 必填 task、默认 FPS、非法配置、不可变配置、provider.open 不扫描/写文件、不同 provider 的 CLI 参数隔离。
- 真实 ROS 2 CDR 格式的合成 MCAP，反序关节名称、已知轨迹、已知接收延迟、四色相机区域；分别在 30/50/60 FPS 下转换 v2.1 与 v3.0 并验证数据和视频。
- 快速视频 CDR envelope 解析与 mcap-ros2-support 官方 schema decoder 逐包比较。
- 显式缓存重复使用、修改 task、FPS 不匹配、已有/不完整目录拒绝覆盖。

运行命令：

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/Users/xieweikai/Downloads/tianji_dataset/tmp \
  /Users/xieweikai/.local/share/uv/tools/letools/bin/python -m pytest \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_alignment.py \
  /Users/xieweikai/Downloads/tianji_dataset/tmp/test_tianji_end_to_end.py \
  --basetemp=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-rerun \
  -o cache_dir=/Users/xieweikai/Downloads/tianji_dataset/tmp/pytest-cache -q
```

## 原有转换路径回归

同一真实 v2.1 样本转换到 v3.0，2 个数据 worker / 2 个视频 worker。通过测试进程内过滤 entry-point 的方式模拟未加载插件基线，不修改安装包。每组 3 次，深度验证在计时区域外：

| 条件 | 第一次（含冷启动开销） | 后两次中位数 |
|---|---:|---:|
| 未发现 Tianji 插件 | 0.752 s | 0.0734 s |
| 已发现 Tianji 插件 | 0.732 s | 0.0703 s |

这个小样本没有观察到回归；毫秒级差异不足以说明性能提升。原始结果见 `../tmp/benchmark-baseline.json` 和 `../tmp/benchmark-plugin.json`。未执行分布式或集群吞吐测试。

最后检查 `/Users/xieweikai/other/letools` 的 Git 工作区无变化，HEAD 仍为 `44b71c999a12f7453e989e7536938a2e57d2a862`。插件通过独立安装包 entry-point 注册，`tianji-provider` 与 `letools` 均可从其他目录调用。
