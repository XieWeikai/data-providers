# 检查、报错与转换拦截（0.6.3）

[返回使用手册](../README.md) · [参数详解](PARAMETERS.md) · [内部设计](DESIGN.md)

`tianji-provider check` 回答的是“这份原始录制能否按当前契约和配置形成可靠输出”。它不判断示范动作是否成功，不编辑或删除 MCAP。检查算法来自 [diagnostics.py](../src/tianji_provider/diagnostics.py)、[reader.py](../src/tianji_provider/reader.py)、[alignment.py](../src/tianji_provider/alignment.py) 和 [media.py](../src/tianji_provider/media.py)。0.6.2 的 [fused.py](../src/tianji_provider/fused.py) 在准备缓存的同时保留相同检查、错误判定与完整视频覆盖，不因使用 NVENC、更多 worker 或融合流水线而放宽规则。

## 报告判定与执行范围

| 等级 | 含义 | 对该条轨迹的判定 |
|---|---|---|
| `error` | 无法满足当前读取、对齐或视频契约 | `valid=false` |
| `warning` | 需要注意，但该项未影响必须支持的输出点 | 不单独使 valid=false |
| `info` | 记录正常处理，例如 state 补 action、首尾裁剪 | 不单独使 valid=false |

Check 会检查所有可识别录制。每条先分别尝试数值读取和视频时间戳读取，深度模式还完整解码视频；两路都可用才尝试对齐和缺口分析。普通录制数据错误不阻止其余录制检查，结果按目录顺序发布；并行 worker 崩溃等基础设施错误会终止整条命令，不伪装为检查通过或继续挂起。读取失败的分支不会假装完成后续检查，实际范围见 `completed_checks`。

正常 source/prepare 的发现条件是存在 `data/metadata.yaml`；check 更宽松，将 `my_bag-*` 或有 data/video 目录的不完整录制也纳入报告。因此独立 prepare 不能代替 check 的“不完整录制也显式报错”保障。融合 API 使用 check 的发现规则；每帧视频在决定是否输出之前都检查，已裁去的头尾不豁免尺寸、解码或映射错误，数值失败也继续检查该条视频。

独立 check 不生成数据缓存，只返回报告/退出码。融合 API 可能在检查整批期间写部分准备文件，最终是否生成成功清单由下面的明确选择规则决定；不能把“检查并准备”理解成先跳过 check 再强行转换。

## 项目与错误代码

错误 `code` 标识失败阶段或问题类别，具体 topic、原始异常和细节在 `message`、`exception_type` 等字段中。多个问题可能共用一个阶段代码。

| 项目 | 失败条件 | code / 典型消息 |
|---|---|---|
| 数据文件与清单 | metadata 无法解析、引用 MCAP 不存在或路径越界、数值消息不能解码 | `numeric_read_error` / 文件错误、`Missing or unsafe MCAP path` |
| 必需观测/辅助 topic | 必需反馈等没有消息，或 metadata 中声明了空的非豁免 topic | `numeric_read_error` / `empty or non-finite signal`、`cannot intersect empty recorded topics` |
| Schema 与维度 | 消息类型错误，关节反馈不是约定的 14 个命名关节，关节命令不是 7 维，夹爪反馈不是 5 项 | `numeric_read_error` / `expected ...` |
| 数值与目标范围 | 用于输出的数值含 NaN/Inf；夹爪目标不在 [0,1] | `numeric_read_error` / `non-finite signal`、`target must be normalized to [0, 1]` |
| 夹爪标定 | 缺少合法 2×2 标定、上限不大于下限、单位错误、中途改变 | `numeric_read_error` / `missing/invalid recorded gripper calibration`、`unsupported gripper units`、`calibration changed` |
| 坐标系 | EEF 不是 base_link，左右 wrench 不是 flange_L/flange_R | `numeric_read_error` / `expected base_link` 等 |
| 数值时钟 | Header 非法/零值，与 MCAP 相差超过 1 秒，或同一 topic 时间倒退 | `numeric_read_error` / `Invalid/zero ROS header timestamp`、`clocks differ`、`clock reversal` |
| 视频定位 | 无法找到唯一的四宫格 bag 或其引用文件 | `video_read_error` / `expected exactly one quad-camera bag` |
| 视频格式 | 非支持的 CompressedImage/CDR v1/H.264，schema 或负载长度错误 | `video_read_error` / `Unsupported video format`、`Malformed CompressedImage payload length` 等 |
| 视频时钟 | 少于两帧、时间不严格递增、Header 与记录时间不同 | `video_read_error` / `Video timestamps must be strictly increasing`、`Video header/record clock mismatch` |
| 完整视频解码 | 解码失败、帧与消息数量/顺序不匹配、视频尺寸中途改变、宽高不能被 4 整除 | `video_decode_error` / `frame/packet mapping is ambiguous`、`Video dimensions changed` 等 |
| 跨录制尺寸 | 本批录制的四宫格尺寸不同 | `inconsistent_video_dimensions` / `Expected ..., got ...` |
| 时间交集 | 无共同候选区间，或候选网格不足两点 | `alignment_error`，也可能有 `timeline_error` / `no common time interval`、`at least two output frames` |
| 观测缺口 | 超过 max_gap_ms 的观测间隔影响所需网格点；wrench 包括窗口内所有高频点 | `observation_gap` 和通常伴随的 `alignment_error` |
| 视频过旧 | 输出点距过去最近视频帧超过 max_gap_ms | `video_gap` 和通常伴随的 `alignment_error` |
| 命令覆盖 | 保留区间存在四路命令同时无效的点 | `command_coverage_gap` 和通常伴随的 `alignment_error` |
| 四路命令全空 | 完全没有可构建命令覆盖范围的消息 | `alignment_error` / `No recorded control command`，也可能有 `timeline_error`；此时尚无输出网格，不一定产生 command_coverage_gap |
| EEF 姿态 | 四元数模长与 1 的偏差超过 0.01，或旋转插值无法完成 | `alignment_error` / `EEF orientation is not a unit quaternion` 等 |
| 静止裁剪 | 开启时整条无超阈值运动，或裁剪后不足两点 | `alignment_error` / `No motion exceeds ...`、`Motion interval contains fewer than two output frames` |
| 其他时间线计算 | 结构化缺口分析本身无法完成 | `timeline_error`，查看 message 中原始原因 |

上表结构、数值、时钟、解码和对齐失败是 error。`observation_gap` / `video_gap` 若超阈值间隔没有影响所需采样点，则为 warning；不是看到 code 中有 gap 就一定失败。

`--wrench-fps` 必须是主 fps 的整数倍且不小于主 fps，两者都在 1–1000；非法配置在逐条检查前以退出码 2 拒绝。默认 50/200 每行包含 4 个力采样，检查不会因不滤波而放宽缺口阈值。

关节与夹爪反馈的“所需”范围包含首尾裁剪前窗口，不能把没测到的首尾当成静止删掉。成功确定输出窗口后，其他观测与视频按保留点判断。对齐先失败时，缺口分析可能使用完整候选网格提供排查信息，应结合 alignment_error 理解范围。

NaN 校验针对被提取到输出中的数值；不是对所有辅助消息的所有字段做通用质量检查。夹爪 action 及其空流豁免已改为 `/control/gripperValueL/R`。`/info/gripper_target_L/R` 不作为备用控制源；若仍被录制，和其他辅助 topic 一样检查时钟及范围。空命令豁免只适用于四路 action 命令，不等于任意名为 control 的 topic 都能豁免。

## 不阻止转换的处理记录

| code | 等级 | 含义 |
|---|---|---|
| `command_pause_filled` | info | 该侧 action 在指定输出范围将用同帧 state；包括整段空命令、尚未开始、过期 |
| `stationary_edges_trimmed` | info | 输出首尾静止部分被裁去，中途网格不删除 |
| `observation_gap` / `video_gap` | warning | 检出的超阈值间隔不影响该信号所需采样点 |

独立 check 的 info 只说明按当前参数计算出的处理，不代表它已经写出了 action 或裁剪视频。融合 API 会同时准备部分数据，但仍以完整报告和成功 manifest 判断结果；存在 info 也可能同时有其他 error，以整条 valid 为准。

## 如何读一条报告

顶层有 `format=tianji-diagnostics-v1`、provider 版本、配置、deep_video、总数、通过数、失败数、valid_frames、总耗时和 results。每条 result 有 bag/path、valid、frames、issues、completed_checks、video、alignment、耗时。

缺口明细的 `affected_frames` 是受影响的主行数，`affected_samples` 是受影响的采样点数；wrench 一行可有多个坏点。`sampling_fps` 为 wrench-fps 或普通字段的 fps，首末受影响时间对于 wrench 是窗口内部的高频时间点。

缺口明细包含 topic、绝对纳秒起止、相对候选起点秒数、实际 gap_ms、阈值、affected_frames、首末受影响输出点及处理方式。命令补齐包含补齐范围/帧数，`last_message_ns=null` 表示此前没有消息，`command_stream_empty=true` 表示该流整段为空。

一个反馈缺口可能同时产生 `alignment_error`（整体失败）与左右两条 `observation_gap`（详细定位），不是三个独立事故。失败轨迹的 `frames=0` 表示没有可接受输出，不代表原始视频为空。报告只覆盖当次数据、版本和参数，修改 FPS/阈值/裁剪后必须重查。

融合结果是 `{report, manifest, selection}`。report 保留同一诊断格式及完整的原输入数量，不把有效子集伪装成所有输入通过；其 `seconds` 包含合并的准备工作，不能直接当作纯 check 性能。report/selection 由 API 返回，调用者负责保存 JSON；成功清单额外包含 `source_selection`。progress 在父进程按原输入顺序回调，单条完整视频检查完成后才报告其最终状态。

## 融合 API 的选择与失败规则

融合入口 `check_and_prepare()` 当前只提供 Python API / 配套 Web 调用，没有同名 CLI，也没有 provider 的 `--prepare-valid-only` 参数。

| 调用方式或失败 | 报告与产物 | 后续行为 |
|---|---|---|
| 默认 `prepare_valid_only=False`，全部通过 | 完整报告 valid=True，写 v4 成功清单 | 可从缓存继续 LeTools 转换 |
| 默认模式，至少一条数据 error | 完整报告 valid=False，返回 manifest=None，不写成功清单 | 整批不进入最终转换；可能保留部分未完成视频/准备文件 |
| 显式 `prepare_valid_only=True`，部分通过 | 完整报告仍 valid=False，只给通过项生成成功缓存，拒绝项目仍完整记录 | 调用者明确选择有效子集；不是放宽检查，也不删原始失败数据 |
| 显式有效子集模式，全部失败 | 完整报告，manifest=None | 没有可转换的缓存 |
| 编码器/磁盘/worker/取消/源变化等运行错误 | 抛出异常，可能只有未完成文件，不能保证返回完整报告 | 不写成功清单，不发布最终目标 |

有效子集的 episode/global index 连续重编，清单记录原输入索引到输出索引及拒绝清单；文件夹名称可以保留原录制编号。Web 0.3.0 起固定检查全部输入、只转换通过项，并明确显示跳过原因和前后统计；全部异常和运行故障仍停止。Web 0.2.0 要求选中项全部通过。125 条检查中只有 114 条通过时，验收必须写明“检查 125、拒绝 11、转换 114”，不能写成 125 条全部转换成功。

融合入口检查选择及 MCAP/YAML/JSON 的路径、文件身份、大小、mtime/ctime 快照，若处理期间发生变化会拒绝生成清单；这不是内容哈希或文件锁。目标要求全新且位于原始录制之外，中断目录不能自动覆盖或断点续传。普通独立 prepare 只有自己的读取/对齐校验和缓存复用检查，不自动获得融合入口这套快照机制。

## 退出码与前置错误

| check 退出码 | 含义 |
|---|---|
| 0 | 本次所有可识别录制通过，无 error；可能含 warning/info |
| 1 | 完成报告，至少一条录制异常 |
| 2 | 参数、源路径、报告写入等命令级错误 |

无效根目录、误选准备缓存、未找到可识别录制、非法参数等可能在逐条检查之前失败，不会都有 `issues[].code`。Prepare/inspect 正常退出为 0；被 CLI 捕获的配置/读取/运行错误为 2，它们不使用 check 的逐条通过/失败汇总协议。未预期的程序异常可能显示 traceback，不能把命令级异常都当作一条新的数据缺口。

NVENC 探测和编码发生在 prepare/融合入口，独立 check 不要求 GPU 编码成功。auto 在独立探测失败、超时或崩溃时使用软件；0.6.3 还会对运行期间硬件 codec 的 ExternalError/MemoryError（如 CUDA 上下文分配失败）执行一次整批软件重试，并保留原因及失败缓存。显式 h264_nvenc 不可用会抛出 `Explicit h264_nvenc request cannot run`，运行错误也不回退。ignored options、非法参数、写盘/封装错误、源解码或未知 worker 崩溃仍直接失败。已有缓存配方与显式请求不符也会报错；这些是执行/缓存错误，不是新的 observation_gap。成功的输入检查不能证明任意显卡都支持当前批次的并发会话数。

## 检查如何影响 LeTools

独立 check 只返回报告/退出码，不给 LeTools 安装一个全局“禁止转换”标记。单独运行 `letools convert` 或 `prepare` 不会查找旧 check.json。需要用 [使用手册](../README.md#推荐流程check--prepare--convert--validate) 的 `&&` 命令链或调用方逻辑，让 check 失败时停止后续操作，并保持语义配置一致。

当前 8095 平台 0.4.1 调用 `check_and_prepare(..., prepare_valid_only=True)`，合并阶段名为 `check_prepare`；仅检查按钮仍调用独立 `check_dataset(..., deep_video=True)`。所有选中轨迹都检查，逐条数据错误保留在报告中，只有通过项进入转换；全部异常不生成目标。Provider Python API 自身仍默认 `prepare_valid_only=False`（整批必须通过）。0.6.3 在 auto 模式下允许硬件 codec 运行故障触发一次整批软件重试；它不改变数据错误的判定和筛选。磁盘、源变化、未知 worker 崩溃、显式 NVENC 失败或软件重试失败均停止。改变阈值是改变可接受数据标准，不是修复缺失观测；实际流程由平台部署版本决定。

平台还检查选择数量与报告数量一致、源文件在检查/准备期间的大小/mtime 等是否变化，并拒绝覆盖目标。融合 API 的快照和最后写清单不代替平台的最终验证与不覆盖发布。转换阶段失败仍会终止；check 通过不能预测磁盘不足、编码器故障或目标冲突。

LeTools 自己另有目标数据集验证，当前 deep validation 检查 episode/帧总数、文件存在、Parquet 行数、episode_index/frame_index 和视频引用所需时长等。它不等于完整逐帧解码或证明视觉质量。8095 在 deep validation 通过后才将额外暂存输出发布到目标目录，失败保留诊断暂存结果；CLI 最后单独执行 validate 不会撤回已发布目录。

## 常见问题定位

- 单侧没动、整段无命令：当前版本仍可用 state 补齐；0.6.2 未修改这条规则。仍出现旧的空 A/B 命令错误，应核对实际环境版本与进程，而不是删除录制。
- 100.85 ms 夹爪反馈缺口：默认 100 ms 阈值下会失败；改夹爪**命令**阈值无效。命令和观测是两类信号。
- 全静止轨迹失败：先看是否为运动阈值/裁剪条件，不一定是文件损坏；若需要静止示例可关闭 trim 后重新检查。
- 一条失败有多项错误：先看结构性读取错误；后续 alignment/timeline 可能尚无法完成，不能据此声称其他数值分支都已验证。
- 全部通过仍转换失败：查看失败阶段的运行日志，区分输入诊断、缓存不匹配、编码、文件写入、后端验证和发布。
- 想保留现有数据：check 不删除数据；错误不是自动删除建议。原始数据和旧最终数据集不会因本工具的诊断结果被移除。
- 看到缓存里有视频但没有 tianji-cache.json：融合可能在检查期间准备过其他录制，文件存在不代表整批通过；查看返回报告或运行错误，不能自行补一个清单绕过失败。
- 只点过一次“仅检查”再正式转换：旧报告不会自动成为复用缓存，正式转换仍重新检查；融合只共享本次调用内部的读取和解码。
