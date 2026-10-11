# 参数详解（0.6.0）

[返回使用手册](../README.md) · [内部设计](DESIGN.md) · [检查与报错](CHECKS.md)

本页以 [provider.py](../src/tianji_provider/provider.py) 的 `TianjiConfig` / `add_arguments()` 和 [cli.py](../src/tianji_provider/cli.py) 为准。先区分三组参数：

1. **Provider 语义参数**：决定 task、时间网格、对齐、补齐和裁剪，四种入口共用。
2. **辅助命令参数**：如 check 的深度检查开关、报告路径，不是转换配置。
3. **LeTools 后端参数**：目标版本、并发、编码等；由 LeTools 解析，不能传给 `tianji-provider prepare`。

## 命令与位置参数

| 入口 | 位置参数 | 用途 |
|---|---|---|
| `tianji-provider check SOURCE` | SOURCE：原始录制或录制父目录 | 逐条诊断；不接受 prepare 缓存作为检查对象 |
| `tianji-provider inspect SOURCE` | SOURCE：原始录制或当前格式缓存 | 打开 source 计算/读取对齐报告；原始输入不完整时可能直接停止 |
| `tianji-provider prepare SOURCE DESTINATION` | SOURCE：原始录制；DESTINATION：缓存目录 | 写中间数据；缓存必须在原始根目录之外，不能与它相同或处于其子目录 |
| `letools convert SOURCE DESTINATION --source-format tianji --to ...` | SOURCE：原始录制或缓存；DESTINATION：最终 LeRobot 目录 | 通过插件接口转换，目标布局由 LeTools 决定 |

`prepare` 对已存在且格式、源指纹、配置均匹配的完整缓存可直接复用；其他已存在目录拒绝覆盖。LeTools 默认也拒绝覆盖已有目标；本文例子均使用新目录。目录发现并非递归搜索任意深度：普通 source 使用 `data/metadata.yaml` 识别根目录或其直接子目录；check 会将可识别但不完整的录制也列入报告。

## 共同语义参数总表

以下 CLI 参数对 check、inspect、prepare 和 `letools convert --source-format tianji` 均适用，task 的必填要求有区别。浮点阈值必须有限，不能是 NaN/Inf。

| 参数 | 默认值 | 范围/单位 | 改变的主要内容 |
|---|---|---|---|
| `--task` | check：`validation`；其他：必填 | 去除两端空白后非空字符串 | 数据集任务文本 |
| `--fps` | `50` | 整数 `[1,1000]`，帧/秒 | 输出网格、表格行数、视频重复帧和统计 |
| `--wrench-fps` | `200` | 整数 `[1,1000]`，且 ≥ fps、可被 fps 整除 | wrench 独立采样率及每行窗口长度 N |
| `--gap-policy` | `error` | 仅允许 `error` | 不支持 split/drop/自动补观测等策略 |
| `--max-gap-ms` | `100.0` | `(0,1000]` ms | 观测插值间隔及视频最大帧年龄 |
| `--command-max-age-ms` | `100.0` | `(0,1000]` ms | 左右关节目标有效期 |
| `--gripper-max-gap-ms` | `150.0` | `(0,1000]` ms | 左右夹爪目标有效期；不是夹爪反馈阈值 |
| `--state-resampling` | `lowpass` | `lowpass` / `linear` | 关节/夹爪反馈及 EEF 平移的重采样；不影响 wrench |
| `--trim-stationary` / `--no-trim-stationary` | 开启 | 布尔开关，不接 `true/false` 值 | 是否裁去首尾静止部分 |
| `--motion-joint-threshold-rad` | `0.002` | `(0,1]` rad | 识别关节运动的端点位移阈值 |
| `--motion-gripper-threshold` | `0.01` | `(0,1]`，归一化行程 | 识别夹爪运动的端点位移阈值 |

### --task：语言任务描述

同一次 source/转换的所有 episode 使用同一任务文本，`task_index=0`。例如抓取放置使用 `Pick and place`，不要沿用插网线示例的 task。它不会改变机器人轨迹、识别动作或影响 check 的数据质量结论。

Prepare、inspect 和转换必须显式填写；独立 check 可以省略，因为它不生成训练数据，此时报告配置写 `validation`。Provider 去掉两端空白并拒绝空字符串；网页另有限制最多 4096 字符，这不是 provider CLI 的长度限制。

Task 是唯一可以更改而不重建准备缓存的 `TianjiConfig` 字段。使用相同缓存、不同 task 重新转换时，会生成新的目标元数据，不重编码视频；不会修改已经生成的旧数据集。

### --fps：输出频率

50 表示每 20 ms 一个输出时间点；30 表示约每 33.333 ms 一个，但同时必须指定可被 30 整除的 wrench-fps（如 210），默认 200 不适用。Provider 用 int64 纳秒独立计算每个网格点，避免大 epoch 转浮点和逐点累加造成漂移；输出 episode 的相对 `timestamp` 再写为 `k/fps`。

这不是相机录制帧率，也不要求各 ROS topic 以同一频率发布。30 Hz 视频输出 50 FPS 会重复部分画面，不能生成新的真实曝光信息。调高 FPS 通常增加表格行数、视频输出帧数、计算和存储；调低 FPS 会改变重采样与抗混叠行为。某个缺口是否影响采样点也可能随网格改变，所以修改 FPS 必须重新检查和 prepare。

CLI 不接受 `29.97` 这类非整数。时间交集或裁剪后的区间至少要容纳两个输出点，否则会失败；不能只通过 metadata 声明一个不同 FPS 来改变数据。

### --wrench-fps：窗口内的力采样率

默认 200，与主频 50 形成每行 `[4,12]` 的 `observation.wrench_windows`。必须为 1–1000 的整数、≥ fps 且为 fps 的整数倍；相等时 N=1，50/1000 时 N=20。非法组合在扫描数据前拒绝，不自动取整。调高会增加数值表和审计数组大小，不会提高主帧或视频的目标频率；窗口起点相位改变可能略微影响边缘帧数和裁剪。

每条录制从共同候选区间起点建立唯一的高频纳秒网格，每 N 点形成一行，末点为主帧时刻。只保留完整窗口，首帧需要 `(N-1)/wrench_fps` 秒历史，尾部不足 N 点不补齐；后续窗口无重叠、无内部空洞。默认该行的偏移为 `[-0.015,-0.010,-0.005,0]` 秒。

各侧在每个高频网格点分别取不晚于它的最后一条真实 wrench，禁止滤波、求均值和数值插值；`--state-resampling` 不作用于本字段。原始约 1 kHz 降至 200 Hz 仍会丢弃未选中的样本、可能遗漏窄峰或出现混叠，不是无损压缩；设 1000 也仍是时间网格选择，不承诺逐条保存抖动消息。细节及训练读取见 [高频 wrench 窗口](WRENCH_WINDOWS.md)。修改此参数必须重建缓存，旧低频数据无法恢复原始力信息。

### --gap-policy：缺口策略

目前只有 `error`。这个参数保留了显式配置，但并不提供多种可选处理策略。旧的 `split` 会被拒绝；没有 `ignore`、`drop` 或强制转换模式。

`error` 针对不能可靠支持输出的观测缺口等错误；不是要求每路命令永不暂停。命令缺失采用下面的有效期与 state 补齐规则。任何失败都不会触发自动删除原始数据。

### --max-gap-ms：观测间隔与视频年龄

对关节反馈、夹爪反馈、EEF、wrench，限制相邻原始观测间隔。Wrench 使用独立高频网格，窗口内部每个点都检查，不能只检查主帧末点；检查失败不通过滤波、保持或插值掩盖。目标网格点若位于超阈值的两条观测之间，不允许跨缺口插值；恰好命中真实采样时间的点不依赖跨缺口插值。关节/夹爪反馈检查裁剪前候选区间，避免把未知首尾判成静止；其他观测检查必须保留的输出区间。

对视频，它限制的是 `输出时间 - 最近一个不晚于该时间的视频帧时间`。短暂缺帧可以继续用旧画面，年龄大于阈值且影响所需输出点才失败。不能把视频原始帧间隔与数值反馈的插值间隔规则混为一谈。

默认 100 ms 时，100.85 ms 的反馈间隔只要包含所需网格点，就会报错；不会按整数毫秒自动向下取整。提高这个值会允许更长区间的观测插值和更旧画面，可能掩盖真实缺失；降低则更严格。它不改变命令有效期，也不替代缺失的整个观测 topic。该值变化需重新检查和 prepare。

### --command-max-age-ms：关节命令有效期

左右臂独立查找不晚于输出时刻的最后一条命令。年龄 `<=` 阈值时用该目标，年龄 `>` 阈值、尚无历史命令或整段为空时，action 对应 7 维填当前输出帧的关节 state。不能引用未来命令，也不会永远沿用未到达的旧目标。

例如 t=1.000 s 的命令在默认 100 ms 下，到 t=1.100 s 仍有效；t=1.120 s 若还没有新命令，则填该时刻 state。最后一条命令即使已超过命令流的最后消息时间，也能继续保持到阈值届满。

调大意味着更久保留旧目标，调小意味着更早填反馈，会直接改变训练 action。它还会改变命令组覆盖范围，因此可能改变候选首尾和检查结论。修改后需要新缓存。

### --gripper-max-gap-ms：夹爪目标有效期

规则与关节命令相同，作用于 `/control/gripperValueL/R`，默认 150 ms；无有效目标时填对应的归一化夹爪反馈 state。名称虽然带 `gap`，代码实际按“最后命令的年龄”判断，不是限制夹爪反馈两条消息之间的间隔。

夹爪反馈的质量限制仍由 `--max-gap-ms` 控制。把本参数调到 200 ms 不会让 150 ms 的夹爪反馈缺口通过默认 100 ms 检查。调节会改变 action 和命令组覆盖，必须重建缓存。

### 四路命令有效期如何共同起作用

参与命令组的是 A/B 关节目标和 L/R 夹爪目标，共四路；`/info/gripper_target_L/R` 不算 action 命令组。一条命令覆盖从其时间戳到有效期结束的闭区间。四路覆盖取并集，再与观测交集相交；每个最终保留的网格点必须至少一路有效。

某侧整段没动且无命令时，该侧 action 可以全程填 state；所有四路全空则失败。保留区间中四路同时过期的点也失败，不删除该点或拆段。提高命令有效期与提高观测间隔是两种不同的语义变更。

### --state-resampling：连续观测如何采样

`lowpass`：在每个无超阈值缺口的连续段内，按中位采样间隔估算原始频率。若该频率高于输出 FPS 且段内至少 8 个样本，先插值到规则网格，再用零相位 FIR / `resample_poly` 做抗混叠降采样，最后采到目标时刻；不满足这些条件时使用插值。

`linear`：连续数值在每个有效段内线性插值，不加上述抗混叠低通。两者都不跨被拒绝的观测缺口插值。低通可抑制降采样混叠，但可能平滑快速变化；linear 保留更多原始变化，也可能在降采样时混叠。

本参数只作用于关节、夹爪反馈和 EEF 平移。Wrench 始终按 `--wrench-fps` 取原始过去样本，不经过上述处理。EEF 旋转始终用 SLERP；命令始终用因果保持/同帧 state；视频始终取过去最近帧。因为补命令使用重采样后的 state，本参数也会间接改变补齐的 action。默认滤波与插值是离线处理，会用相邻未来观测，不能宣称为实时因果滤波。修改需新缓存。

### --trim-stationary / --no-trim-stationary：首尾裁剪

默认开启；显式写 `--trim-stationary` 与不填写相同，`--no-trim-stationary` 关闭。开关不接值，不要写 `--trim-stationary false`。

实现先在候选 50 FPS（或所设 FPS）网格上得到 14 维关节和两路夹爪反馈，然后以首/尾各 200 ms 样本中位数作为端点姿态。任一分量偏离相应端点姿态超过对应运动阈值，就判定为运动；保留运动边界外一个采样点，所有表格与相机按同一行范围裁剪，wrench 随整行窗口保留；首行的 wrench 历史可早于该行末点，但不会越出候选共同区间。不是按相邻帧速度检测，因此缓慢累计移动也可能被识别。

开启时如果整条没有可辨认运动，或保留区间不足两帧，会明确失败。需要静止示例可关闭裁剪后重新检查；关闭也不豁免坏观测和全路命令覆盖要求。内部暂停始终保留，一条录制不拆成多个 episode。

### --motion-joint-threshold-rad：关节运动阈值

默认 0.002 rad，约 0.115°，应用于任一关节相对首/尾中位姿态的绝对偏移。只有严格大于阈值才判定变化。它不是每帧角速度阈值，不是机器人控制精度或关节安全限位。

调大更可能把小动作视为静止、裁去更多边缘；调小更容易保留细小动作，也更容易被反馈噪声影响。关闭静止裁剪时不参与运动判断，但仍必须满足配置范围并参与缓存配置匹配。

### --motion-gripper-threshold：夹爪运动阈值

默认 0.01，即归一化电机行程的 1%，不是 0.01 rad，也不是 0.01 mm。反馈先按录制中的 min/max 标定归一化，再与首/尾中位姿态比较，严格大于阈值才算变化。任一夹爪运动也能保留轨迹，即使所有关节静止。

调大/调小的取舍与关节阈值类似；两种阈值采用“任一分量超过即算运动”，不是要求双臂同时运动。关闭裁剪时该值仍参与缓存匹配。

## 辅助命令专有参数

| 参数 | 适用命令 | 默认 | 意义和边界 |
|---|---|---|---|
| `--workers N` | check、prepare | 自动：最多 8，且可用 CPU 数减 2，最低 1 | 正整数，实际不超过录制数；1 为串行。check 并行完整轨迹检查，prepare 数值加载和视频分别按轨迹并行。执行参数不影响字段、质量或缓存身份 |
| `--report PATH` | check、inspect | 不写文件 | 将完整 JSON 写入该文件，必要时创建父目录；同名文件会被重写，建议使用独立报告路径，不能指向原始数据文件 |
| `--deep-video` | check | 开启 | 完整解码视频，验证所有帧映射、数量、顺序与尺寸；不编码输出 |
| `--no-deep-video` | check | 不启用此快速模式 | 仍读取全部视频消息时间戳/结构，但只解码首帧检查尺寸，不能排除后续坏帧 |
| `-h` / `--help` | 所有命令/子命令 | 无 | 显示相应帮助后退出，不扫描数据 |

`--workers`、`--report`、深度检查开关不属于 `TianjiConfig`，不改变对齐缓存。Prepare 没有 `--report` 或 `--no-deep-video`；其清单和审计路径由缓存结构固定。Inspect 没有 deep-video 开关；其快速范围由实现决定。

## 仅 Python API 存在的配置

`TianjiConfig.preset` 默认且只允许 `thor-v1`，定义已验证的 topic/坐标系/字段契约。**当前 CLI 没有 `--preset` 参数**，也没有任意 topic 名称映射开关。不能通过换一个字符串适配另一种机器人。

`TianjiSource(..., _collect_image_stats=False)` 是内部控制选项，供 inspect 和 prepare 延后/省略图像统计使用；不是公共 CLI 调参入口。`prepare(..., workers=None)` 与 `check_dataset(..., workers=None)` 可控制录制并发；check 的 `deep_video` 控制解码范围，`progress` 在父进程按稳定顺序回调。原始 `TianjiSource` 的首次 metadata/episodes 访问也默认并行，内部 `_workers=1` 可用于串行对照或受限交互环境；provider.open 返回 source 仍然不立即读取或启动进程。缓存 source 不启动原始读取池。

多进程采用 spawn，直接 Python 调用 check/prepare 或首次加载原始 DataSource 的脚本都必须放在 `if __name__ == '__main__':` 保护下；交互环境可显式 workers=1 或 `TianjiSource(..., _workers=1)`。这些都不是输出信号参数，也不改变缓存身份。

## 常见 LeTools 参数：不属于 Tianji provider

下表说明本流程常用的上游选项，依据已验证的 LeTools `c20118b`；完整上游参数以所安装 LeTools 的 `letools convert --source-format tianji --help` 为准。该版本号是上游提交，不是 provider 的包版本。

| 参数 | 用途 | 与 provider 的关系 |
|---|---|---|
| `--source-format tianji` | 选择插件，默认 auto 不能代替显式原始源选择 | 让 LeTools 注入 Tianji 参数并构造 TianjiSource；辅助 CLI 不需要此选项 |
| `--to v2.1` / `--to v3.0` | 必填，选择目标布局；CLI 也接受 `2.1` / `3.0` | 不改变对齐算法；同一缓存可分别输出两个版本 |
| `--workers N` | LeTools 数据写入并发 | 不加速之前独立执行的 check/prepare；平台默认 2 |
| `--video-workers N` | LeTools 媒体任务并发 | 不改变 prepare 固定编码设置或单条循环；平台默认 4 |
| `--video-codec libx264` | 对 FrameSequence 编码为 H.264 | 直接路径适用；prepare 自己已固定 libx264，缓存不是 MP4 转码输入 |
| `--video-pixel-format yuv420p` | 指定帧编码像素格式 | 与上一项配合，直接路径适用；不能通过标签修改缓存码流 |
| `--video-batch-frames N` | 每批读取的图像帧数，正整数，已验证默认 48 | 直接路径影响 PPM 缓冲和吞吐，不改变时间映射 |
| `--video-codec-threads N` | 每个帧编码器的线程数，正整数，已验证默认 1 | 与 video-workers 是不同层次，并发相乘可能争用 CPU；不控制 prepare |
| `--data-file-size-mb` / `--video-file-size-mb` | 目标分片大小规划参数 | 属于后端布局，不是目标画质、信号阈值或 prepare 缓存大小 |
| `--overwrite` | LeTools 显式允许替换已有目标 | 本文例子不使用；8095 禁止覆盖；provider prepare 没有此开关 |
| `--no-validate` | 跳过 LeTools 自己的转换后验证 | 不跳过 provider 的读取/对齐检查；8095 没有开放这个绕过开关 |

LeTools workers 未显式指定时由其当前版本默认值决定，与网页固定表单默认值不同。已编码的 prepare 缓存应省略图像编码参数，保持复用路径；不能期待给它指定 codec 参数就重新编码 MP4。固定 CRF 18/veryfast/bf=0 是 prepare 的实现策略，当前 provider 没有 `--crf`、`--quality` 或编码器 preset CLI。

## 缓存与参数联动

| 改动 | 可以复用同一准备缓存吗？ |
|---|---|
| 只改 task | 可以，重新输出新的目标数据集 |
| 改 fps、wrench-fps 或任意对齐/运动阈值 | 不可以 |
| 改 lowpass/linear、开启/关闭裁剪 | 不可以 |
| 关闭裁剪后只改运动阈值 | 仍不可以，配置按完整字段匹配 |
| 改 check/prepare workers | 可以，不改变数据语义和缓存配置 |
| 改 report 路径或 check 深度 | 不影响缓存配置，但诊断覆盖范围不同 |
| 改目标 v2.1/v3.0、LeTools workers | 可以，不改变 provider 时间语义 |
| 旧 tianji-thor-v1/v2/v3 缓存换成 0.6.0 打开 | 不可以，必须重建为 tianji-thor-v4 |

参数改变不自动修复或删除数据。任何新策略都应先重新 check，再以相同配置 prepare/convert；不要仅根据旧报告决定新参数的转换是否可靠。
