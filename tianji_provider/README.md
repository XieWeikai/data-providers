# Tianji provider for LeTools

本文描述 **tianji-provider 0.5.0**：读取 Thor / Tianji / Marvin Pro 的 ROS 2 MCAP 录制，将机器人信号和四路画面对齐，向 LeTools 提供 `DatasetSource`，由 LeTools 输出 LeRobot v2.1 / v3.0。Provider 不连接或控制机器人，读取源数据不需要 ROS 运行环境。

| 文档 | 内容 |
|---|---|
| 本页 | 功能、安装、使用流程、输入输出、缓存与限制 |
| [参数详解](docs/PARAMETERS.md) | 全部 provider 参数的默认值、单位、范围、边界行为、调节影响及缓存关系 |
| [内部设计](docs/DESIGN.md) | SourceProvider / DatasetSource 接口、模块分工、时间算法、数据与媒体生命周期 |
| [检查与报错](docs/CHECKS.md) | 检查项目、错误代码、严重级别、CLI 退出码及对后续转换的影响 |
| [验证记录](VALIDATION.md) | 按版本记录的测试范围、实录结果和性能；历史结果不代表当前规则 |

## 功能与边界

安装包同时包含标准 LeTools 插件和 Tianji 专用辅助命令。**标准插件的核心是创建 `DatasetSource`；check、inspect、prepare 是本包额外提供的工具，不是所有 LeTools provider 必须实现的接口。**

| 功能 | 当前能力 | 写入范围 |
|---|---|---|
| 插件发现 | Python entry point 注册为 `tianji`；独立安装，不修改 LeTools 源码 | 指定 Python 环境 |
| 数据读取 | 使用 MCAP 内嵌 ROS 2 schema，读取单条或一组录制 | 原始录制只读 |
| 字段映射 | 重排关节、区分反馈和命令、按录制标定归一化夹爪、校验坐标系 | 生成的数据源表格 |
| 时间对齐 | 观测/辅助 topic/视频取交集，四路 action 命令取有效覆盖并集，固定 FPS 输出 | 内存中的对齐结果 |
| 命令补齐 | 某侧命令缺失、过期、晚开始、早结束或整段为空时，使用对应同帧 state | 输出 action，不回写 MCAP |
| 静止裁剪 | 默认裁去首尾静止，保留中途暂停，一条录制一个 episode | 输出窗口，不删原始帧 |
| `check` | 逐条检查数值、时钟、标定、对齐与完整视频，汇总异常 | 可选 JSON 报告 |
| `inspect` | 查看对齐、补齐、裁剪结果、输出帧数和追溯摘要 | 可选 JSON 报告 |
| `prepare` | 写可复用的 Parquet、逐帧审计、四路 H.264 及统计 | 独立缓存目录 |
| 直接转换 | 向 LeTools 提供无损 RGB PPM 帧序列，由 LeTools 编码 | 最终目录由 LeTools 写入 |
| 缓存转换 | 缓存作为 DatasetSource，复用编码视频写出 v2.1/v3.0 | 最终目录由 LeTools 写入 |
| 审计与性能描述 | 时间映射、补齐范围、大小/locality 描述、planner identity | 报告、缓存和元数据 |

本包没有原始 MCAP 编辑、自动删除异常录制、机器人控制、动作成功识别、训练或标注界面。Visualizer、Annotation、Doctor、可选 editor 属于其他工具；8095 是单独的 Web 项目。当前明确拒绝分布式执行，仅支持已验证的本地转换。

## 当前处理规则

- Task 必填（独立 check 除外），目标 FPS 默认 50，接受 1–1000 的整数。
- 四路 action 命令为 `/tj/control/joint_cmd_A/B`、`/info/gripper_target_L/R`。每个保留时刻至少一路有效，其余填对应同帧 state；默认有效期为关节 100 ms、夹爪目标 150 ms，含阈值端点。
- 真实观测缺口仍严格检查，默认最大间隔/视频帧年龄 100 ms。四路命令同时失效的保留时刻也会失败，不跨越缺口拼接。
- 一条成功处理的录制对应一个 episode。只裁首尾，不拆轨迹，不删中间网格点，不修改输入。
- Prepare 固定 H.264 / libx264、CRF 18、veryfast、yuv420p、无 B 帧，保留原始单路尺寸。LeTools 随后复用编码视频，不再有损编码。
- 当前缓存格式 **`tianji-thor-v3`** 与目标 **LeRobot v3.0** 无关。旧 `tianji-thor-v1/v2` 缓存必须从原始录制重新 prepare；旧视频即使已是 H.264，也不能复用旧时间轴缓存。已有最终 LeRobot 数据集不会被改写或删除。

## 安装与注册

要求 Python >= 3.12。LeTools 与 provider 必须安装在同一个 Python 环境。插件通过 [pyproject.toml](pyproject.toml) 中的 `letools.source_providers` entry point 注册，不需要修改 LeTools 的 import。

### Marvin 的 8095 专用环境

| 内容 | 路径 |
|---|---|
| LeTools / 专用环境 | `/home/user/projects/letools-tianji` / 其 `.venv` |
| Provider 仓库 / 包 | `/home/user/projects/data-providers` / 其 `tianji_provider` |
| Web 平台 | `/home/user/projects/data-web-platform` |

下面用于已有 LeTools 专用环境的安装/更新，不创建或替换环境，不操作全局 `~/.local/bin/letools`：

```bash
TIANJI_ENV="$HOME/projects/letools-tianji/.venv"
uv pip install --python "$TIANJI_ENV/bin/python" \
  -e "$HOME/projects/data-providers/tianji_provider"
"$TIANJI_ENV/bin/letools" providers list
"$TIANJI_ENV/bin/python" -c \
  'import tianji_provider; print(tianji_provider.__version__, tianji_provider.__file__)'
```

新环境应先按 data-web-platform 部署说明安装完整 LeTools/native 依赖；不要为更新 provider 重建使用中的 `.venv`。依赖已满足且只需重装当前包时可加 `--no-deps`，它不能代替首次依赖安装。确认发现的 `tianji` 版本为 0.5.0、模块路径为预期仓库；不要复制其他全局环境的 provider 配置进来。

Web 服务在启动时发现 providers。更新后确认无运行/排队任务，再重启并读取实际环境：

```bash
systemctl --user restart data-web-platform.service
curl -fsS http://127.0.0.1:8095/api/environment
```

API 应显示新 provider 版本、专用 Python 和 `error: null`。重启会中断进行中的任务。仅修改 Markdown 文档无需重装或重启。

### 通用项目环境与 uv tool 环境

在已安装 LeTools 的项目环境中，从 data-providers 仓库根目录执行：

```bash
uv pip install --python /path/to/letools-env/bin/python -e ./tianji_provider
/path/to/letools-env/bin/letools providers list
```

以下仅适用于希望更新 **uv tool 管理的那份 LeTools** 的用户，不是 Marvin 8095 的安装命令：

```bash
uv pip install --python "$(uv tool dir)/letools/bin/python" -e ./tianji_provider
"$(uv tool dir)/letools/bin/letools" providers list
```

`tianji-provider` 脚本在同一环境的 `bin/` 中，不一定自动出现在 PATH；可用完整路径或激活该环境。Editable 安装使新 Python 进程读取源码变化，但已运行进程和显示用的包版本元数据不会因编辑文件自动刷新。

## 输入与输出字段

```text
recordings/
└── my_bag-.../
    ├── data/
    │   ├── metadata.yaml
    │   └── data_0.mcap
    └── video/
        └── <quad-video-bag>/
            ├── metadata.yaml
            └── <video-file>.mcap
```

文件名和分片顺序以 metadata 的 `relative_file_paths` 为准。每条录制必须有唯一的四宫格视频 bag。视频 topic 是 `/recorder/quad_tile/compressed_undistorted`，使用标准 `sensor_msgs/msg/CompressedImage`、CDR v1、H.264，要求每个消息与一个解码帧按顺序明确对应。

| 输出字段 | 形状/顺序 | 来源与单位 |
|---|---|---|
| `observation.state` | float32[16]：左 7 关节、左夹爪、右 7 关节、右夹爪 | `/tj/joint_states`、`/info/gripper_feedback_L/R.data[0]`；rad、归一化电机行程 |
| `observation.action` | float32[16]，与 state 对应 | A/B 左/右关节目标、`/info/gripper_target_L/R`；无有效命令的分量补同帧 state |
| `observation.wrench` | float32[12]：左 Fx/Fy/Fz/Tx/Ty/Tz、右相同 | `/tj/info/wrench_left/right`；N、N·m；flange_L / flange_R |
| `observation.eef` | float32[14]：左 x/y/z/qx/qy/qz/qw、右相同 | `/tj/info/eef_left/right`；base_link 下的反馈 FK 位姿；m、单位四元数 |
| `observation.image.head_left` | video[H/2,W/2,3] | 四宫格左上 |
| `observation.image.head_right` | 同上 | 四宫格右上 |
| `observation.image.left_wrist` | 同上 | 四宫格左下 |
| `observation.image.right_wrist` | 同上 | 四宫格右下 |
| `timestamp` | float32 标量 | 每个 episode 从 0 开始的 `k/fps`，单位秒 |
| `frame_index` | int64 标量 | episode 内从 0 连续编号 |
| `episode_index` | int64 标量 | 录制按稳定顺序编号 |
| `index` | int64 标量 | 整个数据集连续行号 |
| `task_index` | int64 标量 | 当前一次转换一个 task，值为 0 |

字段保留用户指定的 `observation.action` 和单数 `observation.image`。训练代码若默认寻找 `action`、`observation.images`，需显式配置映射。

关节按消息 `name` 重排，A 对应左臂、B 对应右臂。夹爪 action 使用驱动已发布目标，不使用可能尚未采用的 `/control/gripperValueL/R`；后两者作为录制的辅助信号仍可能限制交集。

夹爪归一化读取 `/info/web_gripper` JSON 的 `calibration`，公式是 `(motor_rad - min_rad) / (max_rad - min_rad)`。已验证样本为 `[0,1.6]` rad，代码不硬编码；反馈轻微越出 0–1 不裁剪。它不是毫米开口宽度。标定缺失、中途改变或单位不匹配会失败。

1920×1080 四宫格拆为四路 960×540；实际尺寸由输入决定。四宫格宽高须可被 4 整除，以便 YUV420 裁分。不额外缩放、翻转、去畸变或移除水印。四路共享合成帧时间戳，不意味着传感器曝光硬件同步。

## 推荐流程：check → prepare → convert → validate

以下为 **Bash** 示例。先激活正确环境，替换路径；缓存和输出使用新目录，输入为单条录制或待转换录制的父目录。

```bash
source "$HOME/projects/letools-tianji/.venv/bin/activate"
TIANJI_RAW=/path/to/selected-recordings
TIANJI_CACHE=/path/to/new-cache-50
TIANJI_OUTPUT=/path/to/new-lerobot-v21
TIANJI_REPORT=/path/to/reports/check.json
TIANJI_TASK='Pick and place'
TIANJI_ARGS=(--task "$TIANJI_TASK" --fps 50
  --gap-policy error --max-gap-ms 100
  --command-max-age-ms 100 --gripper-max-gap-ms 150
  --state-resampling lowpass --trim-stationary
  --motion-joint-threshold-rad 0.002 --motion-gripper-threshold 0.01)

tianji-provider check "$TIANJI_RAW" "${TIANJI_ARGS[@]}" \
  --deep-video --report "$TIANJI_REPORT" &&
tianji-provider prepare "$TIANJI_RAW" "$TIANJI_CACHE" "${TIANJI_ARGS[@]}" &&
letools convert "$TIANJI_CACHE" "$TIANJI_OUTPUT" \
  --source-format tianji "${TIANJI_ARGS[@]}" --to v2.1 \
  --workers 2 --video-workers 4 &&
letools validate "$TIANJI_OUTPUT" --deep
```

`&&` 确保前一步非零退出就停止后续命令。**Check 报告不会被下一次独立 convert 自动读取。** Prepare/直接转换自身仍有读取和对齐校验，但不自动执行完整逐条诊断报告流程。各阶段应使用相同的 provider 参数。

同一缓存可以另行写出 v3.0：换新输出目录，将 `--to` 改为 `v3.0`，保持 provider 配置一致。上述 CLI 的最后一次 deep validation 发生在目标已写出之后；若失败，不会撤销已写目录。8095 则在额外暂存目录完成 deep validation 后才发布最终目录。

### 单独检查或查看对齐

```bash
tianji-provider check /path/to/recordings --report /path/to/reports/check.json
tianji-provider check /path/to/recordings --no-deep-video
tianji-provider inspect /path/to/recordings \
  --task 'Pick and place' --fps 50 --report /path/to/reports/alignment.json
```

Check 可省略 task，配置中使用占位值 `validation`，不会生成训练数据集。`--no-deep-video` 只验证时间戳和首帧，不证明后续视频完整。Check 在一条失败后继续扫描其余轨迹。

Inspect 解析数值、读取视频时间戳和首帧，计算输出帧数、裁剪、补齐范围和视频最大年龄；不编码、不完整解码视频、不计算全部图像统计。它遇到错误会停止，不能替代 check 的逐条汇总。

### 直接从 MCAP 转换

```bash
letools convert /path/to/recordings /path/to/new-lerobot-v21 \
  --source-format tianji --task 'Pick and place' --fps 50 \
  --to v2.1 --workers 2 --video-workers 2 \
  --video-codec libx264 --video-pixel-format yuv420p
```

直接路径使用无损 RGB PPM `FrameSequence`，由 LeTools 编码；四路相机各自顺序解码原视频，另有图像统计解码，适合少量数据或调试。示例的 libx264 在已验证后端默认使用 CRF 23，不等同于 prepare 的 CRF 18。PPM 是无损中间帧，最终 H.264 仍有损。默认 48 帧批次、960×540 时，PPM 缓冲约 71 MiB/相机，不代表进程总内存上限。

## 准备缓存与复用

```text
cache/
├── tianji-cache.json          # 所有步骤成功后最后写入的完成清单
├── episode_000000/
│   ├── data.parquet           # 已对齐、补齐、裁剪的表格
│   ├── alignment.npz          # 绝对纳秒时间、原视频包索引和视频时间
│   ├── head_left.mp4
│   ├── head_right.mp4
│   ├── left_wrist.mp4
│   └── right_wrist.mp4
└── episode_000001/...
```

缓存是 provider 中间格式，不是最终 LeRobot。清单记录配置、源指纹、字段、episode 路径、统计、对齐报告、编码设置及分阶段耗时。复用要求：

1. 格式为当前 `tianji-thor-v3`。
2. 除 task 外的 `TianjiConfig` 完全一致；即使某参数本次未实际影响输出，也参与比较。
3. 再次对原始目录执行 prepare 时，MCAP 路径、大小、修改时间指纹一致，引用资源存在。指纹不是内容哈希。

只改 task 可复用视频缓存并重新生成目标元数据；改 FPS、任何对齐/裁剪参数需新缓存。直接打开缓存不会重新核对原始录制指纹，也不重新诊断原始数据。已有不匹配目录和未完成缓存拒绝覆盖；失败可能留下不完整目录，可保留排查并选择新目录重试。

Prepare 在视频编码/统计阶段每条四宫格完整解码一次，同时编码四路；完成选中帧后仍继续解码尾部检查映射。对齐阶段另读时间戳和首帧。如果前面执行 deep check，那是额外一次完整解码，不能把 check+prepare 全流程称为只解码一次。

数值 MCAP 在一次原始 source 加载中每条读取一次，check 和后续 prepare 是不同加载。原始 source 保留全部已对齐低维表格与审计数组，内存随总帧数增长；RGB 图像流式处理。Prepare 最多一个解码器、四个编码器，各编码器一线程；LeTools workers 不改变 prepare 的逐条循环。

数字统计基于最终 float32，图像统计使用选中 RGB 像素通道直方图，重复帧计权，范围归一化到 0–1；这是 H.264 编码前统计。对齐摘要随 `meta/info.json` 的 `tianji` 字段输出，逐行 `alignment.npz` 留在缓存；需要追溯时应保留缓存。

## 与 8095 的配合

Web worker 使用相同配置执行 `check → prepare → convert → deep validation → publish`。网页“仅检查”在 check 后返回，不编码输出。

- 正式转换重新深度检查；选中任意一条有 error 就整批停止，不自动跳过或删除异常轨迹，warning/info 不直接阻止。
- 平台额外核对检查/准备期间源文件是否变化，要求新目标目录，在暂存输出 deep validation 成功后发布。
- 网页、任务队列、目录选择、进度、取消、数据集登记和工具入口由 data-web-platform 提供；不是 provider 标准接口的一部分。
- 页面发现列表来自指定 LeTools 环境；其他 provider 被发现不代表网页已适配其参数表单。
- 独立 provider CLI/API 不会自动获得 Web 的源文件变化保护和发布流程。页面历史默认 task 为 `Plug in the Ethernet cable`，其他任务必须主动填写对应文本。

## 限制、参考与测试

仅支持核验过的 `thor-v1` 消息与相机布局，不猜测其他机器人的关节顺序、单位、标定或时钟补偿。不会判断抓取成功、画面模糊、遮挡或碰撞。没有 Header 的夹爪数据仍含接收延迟；统一网格不等于硬件同步。

初始实机核对日期 2026-09-20：Thor 的 JointcmdArm 定义、关节 mux、夹爪驱动、EEF/wrench 发布和 CAMERA_SLOTS 共同确定契约。历史样本水印与视频消息时间相差约 8–23 ms，不用于推断全局补偿或证明当前全部采集精度。

方法参考：[ROS message_filters](https://docs.ros.org/en/ros2_packages/rolling/api/message_filters/message_filters.html)、[SciPy resample_poly](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html)、[SciPy Slerp](https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.transform.Slerp.html)。算法细节见 [内部设计](docs/DESIGN.md)。

在已安装 provider 与 pytest 的环境，从 data-providers 仓库根目录运行：

```bash
python -m pytest -q tianji_provider/tests
```

自带测试覆盖命令空流与错开边界、阈值、全路缺口、观测缺失、首尾裁剪、坏命令和旧缓存拒绝。历史实录脚本、旧测试和大体积产物不随仓库分发，位置及验证范围见 [VALIDATION.md](VALIDATION.md)。仅更新文档不会改变 0.5.0 的已安装行为。
