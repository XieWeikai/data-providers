# Tianji provider for LeTools

本文描述 **tianji-provider 0.6.2**：读取 Thor / Tianji / Marvin Pro 的 ROS 2 MCAP 录制，将机器人信号和四路画面对齐，向 LeTools 提供 `DatasetSource`，由 LeTools 输出 LeRobot v2.1 / v3.0。Provider 不连接或控制机器人，读取源数据不需要 ROS 运行环境。0.6.2 增加长任务优先调度、可探测的 NVENC 编码，以及显式融合检查与准备的 API；保持既有字段、采样、补命令、裁剪、检查阈值和精确统计。视频编码器可以改变，缓存格式仍为 `tianji-thor-v4`。

| 文档 | 内容 |
|---|---|
| 本页 | 功能、安装、使用流程、输入输出、缓存与限制 |
| [参数详解](docs/PARAMETERS.md) | 全部 provider 参数的默认值、单位、范围、边界行为、调节影响及缓存关系 |
| [高频 wrench 窗口](docs/WRENCH_WINDOWS.md) | 窗口形状、因果取样、时间戳、无滤波与降采样限制、训练读取 |
| [内部设计](docs/DESIGN.md) | SourceProvider / DatasetSource 接口、模块分工、时间算法、数据与媒体生命周期 |
| [检查与报错](docs/CHECKS.md) | 检查项目、错误代码、严重级别、CLI 退出码及对后续转换的影响 |
| [验证记录](VALIDATION.md) | 按版本记录的测试范围、实录结果和性能；历史结果不代表当前规则 |
| [性能优化实验](docs/PERFORMANCE.md) | 每项方案的 A/B 耗时、保真验证及 ≥5% 接受门槛 |
| [第二阶段优化](docs/PERFORMANCE_ROUND2.md) | 最多五轮继续优化的方案、实际收益和接受/撤回记录 |

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
| `check_and_prepare()` | 一次调用共享数值读取、对齐和完整视频帧流，执行全部检查并准备缓存；只提供 Python API / 配套 Web 入口 | 返回报告、选择记录及成功清单；写独立缓存目录 |
| 直接转换 | 向 LeTools 提供无损 RGB PPM 帧序列，由 LeTools 编码 | 最终目录由 LeTools 写入 |
| 缓存转换 | 缓存作为 DatasetSource，复用编码视频写出 v2.1/v3.0 | 最终目录由 LeTools 写入 |
| 审计与性能描述 | 时间映射、补齐范围、大小/locality 描述、planner identity | 报告、缓存和元数据 |

本包没有原始 MCAP 编辑、自动删除异常录制、机器人控制、动作成功识别、训练或标注界面。Visualizer、Annotation、Doctor、可选 editor 属于其他工具；8095 是单独的 Web 项目。当前明确拒绝分布式执行，仅支持已验证的本地转换。

配套 Web 仓库：[XieWeikai/letools-data-platform](https://github.com/XieWeikai/letools-data-platform)。其部署、网页参数、队列和使用说明独立维护；私有仓库需要 GitHub 访问权限。LeTools 的“输入前端”指本插件提供的 SourceProvider/DatasetSource，“输出后端”指 LeRobot v2.1/v3.0 写入器，与浏览器/FastAPI 的 Web 前后端是不同层次。

## 当前处理规则

- Task 必填（独立 check 除外）。主 FPS 默认 50，`--wrench-fps` 默认 200；两者均为 1–1000 的整数，wrench-fps 必须不小于 FPS 且为其整数倍。默认力字段为 `observation.wrench_windows`，每行 `[4,12]`。
- 四路 action 命令为 `/tj/control/joint_cmd_A/B`、`/control/gripperValueL/R`。每个保留时刻至少一路有效，其余填对应同帧 state；默认有效期为关节 100 ms、夹爪目标 150 ms，含阈值端点。
- Wrench 始终在独立高频网格取过去最近的真实测量，不滤波、不平均、不插值。每行包含 `N=wrench_fps/fps` 个从旧到新的采样，末点对齐该行时间戳；不再输出 `observation.wrench` 或 `observation.wrench_history`。
- 真实观测缺口仍严格检查，默认最大间隔/视频帧年龄 100 ms。四路命令同时失效的保留时刻也会失败，不跨越缺口拼接。
- 一条成功处理的录制对应一个 episode。只裁首尾，不拆轨迹，不删中间网格点，不修改输入。
- Prepare 和融合 API 默认 `video_encoder="auto"`：独立探测 NVENC，成功后使用 H.264 NVENC / VBR CQ16 / p4 / HQ / Main；不可用时使用原 libx264 / CRF18 / veryfast。两者均为 yuv420p、无 B 帧、GOP=fps，保留原始单路尺寸和帧序列。LeTools 随后复用编码视频，不再有损编码。CQ 与 CRF 不是同一尺度。
- 当前缓存格式 **`tianji-thor-v4`** 与目标 **LeRobot v3.0** 无关。旧 `tianji-thor-v1/v2/v3` 缓存必须从原始录制重新 prepare；旧视频即使已是 H.264，也不能复用旧时间轴缓存。已有最终 LeRobot 数据集不会被改写或删除。

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

新环境应先按 data-web-platform 部署说明安装完整 LeTools/native 依赖；不要为更新 provider 重建使用中的 `.venv`。**从 0.6.0 升级时新增了 opencv-python-headless，并要求 mcap-ros2-support>=0.5.7,<0.6；首次升级不要直接使用 --no-deps 跳过依赖。** 全部依赖已满足且只需重装当前包时才可加 `--no-deps`。确认发现的 `tianji` 版本为 0.6.2、模块路径为预期仓库；不要复制其他全局环境的 provider 配置进来。

NVENC 还依赖机器的 NVIDIA 驱动、硬件和当前 PyAV/FFmpeg 的编码器支持；仅在编码器名称列表中出现不代表能用。`auto` 通过独立进程实际编码、flush、解码和检查时间戳，只在探测失败时回退软件编码。没有 NVIDIA 硬件也可使用本包，不要求为 provider 重装系统驱动。

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
| `observation.action` | float32[16]，与 state 对应 | A/B 左/右关节目标、`/control/gripperValueL/R`；无有效命令的分量补同帧 state |
| `observation.wrench_windows` | float32[N,12]，N=wrench_fps/fps；第一轴时间从旧到新，第二轴左 Fx/Fy/Fz/Tx/Ty/Tz、右相同 | `/tj/info/wrench_left/right`；N、N·m；flange_L / flange_R；未滤波的过去最近测量 |
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

关节按消息 `name` 重排，A 对应左臂、B 对应右臂。夹爪 action 使用 `/control/gripperValueL/R` 的归一化输入控制命令（Float32，0–1），不使用 `/info/gripper_target_L/R` 的驱动状态目标。输入命令不一定等于执行器最终采用的目标，这正是两类 topic 的语义区别。旧 `/info/gripper_target_L/R` 若仍被录制，仅作为辅助 topic 参与时钟与范围检查；不会被当作备用 action。控制命令缺失时补对应 state。

夹爪归一化读取 `/info/web_gripper` JSON 的 `calibration`，公式是 `(motor_rad - min_rad) / (max_rad - min_rad)`。已验证样本为 `[0,1.6]` rad，代码不硬编码；反馈轻微越出 0–1 不裁剪。它不是毫米开口宽度。标定缺失、中途改变或单位不匹配会失败。

1920×1080 四宫格拆为四路 960×540；实际尺寸由输入决定。四宫格宽高须可被 4 整除，以便 YUV420 裁分。不额外缩放、翻转、去畸变或移除水印。四路共享合成帧时间戳，不意味着传感器曝光硬件同步。

## 推荐流程：check → prepare → convert → validate

以下为兼容的**独立 CLI 路线**，使用 Bash。先激活正确环境，替换路径；缓存和输出使用新目录，输入为单条录制或待转换录制的父目录。两个独立命令仍会重复读取数值和解码视频；需要共享这些处理时使用下面的融合 API 或配套 Web。当前没有融合子命令。

```bash
source "$HOME/projects/letools-tianji/.venv/bin/activate"
TIANJI_RAW=/path/to/selected-recordings
TIANJI_CACHE=/path/to/new-cache-50
TIANJI_OUTPUT=/path/to/new-lerobot-v21
TIANJI_REPORT=/path/to/reports/check.json
TIANJI_TASK='Pick and place'
TIANJI_ARGS=(--task "$TIANJI_TASK" --fps 50 --wrench-fps 200
  --gap-policy error --max-gap-ms 100
  --command-max-age-ms 100 --gripper-max-gap-ms 150
  --state-resampling lowpass --trim-stationary
  --motion-joint-threshold-rad 0.002 --motion-gripper-threshold 0.01)

tianji-provider check "$TIANJI_RAW" "${TIANJI_ARGS[@]}" \
  --deep-video --report "$TIANJI_REPORT" &&
tianji-provider prepare "$TIANJI_RAW" "$TIANJI_CACHE" "${TIANJI_ARGS[@]}" \
  --video-encoder auto &&
letools convert "$TIANJI_CACHE" "$TIANJI_OUTPUT" \
  --source-format tianji "${TIANJI_ARGS[@]}" --to v2.1 \
  --workers 2 --video-workers 4 &&
letools validate "$TIANJI_OUTPUT" --deep
```

`&&` 确保前一步非零退出就停止后续命令。**Check 报告不会被下一次独立 convert 自动读取。** Prepare/直接转换自身仍有读取和对齐校验，但不自动执行完整逐条诊断报告流程。各阶段应使用相同的 provider 参数。

同一缓存可以另行写出 v3.0：换新输出目录，将 `--to` 改为 `v3.0`，保持 provider 配置一致。上述 CLI 的最后一次 deep validation 发生在目标已写出之后；若失败，不会撤销已写目录。8095 则在额外暂存目录完成 deep validation 后才发布最终目录。

### 融合检查与准备：Python API

保存为 Python 脚本运行；多进程使用 spawn，调用必须放在 main guard 下：

```python
import json
from pathlib import Path

from tianji_provider.fused import check_and_prepare
from tianji_provider.provider import TianjiConfig


def main():
    result = check_and_prepare(
        "/path/to/selected-recordings",
        "/path/to/new-cache-50",
        TianjiConfig(task="Pick and place", fps=50, wrench_fps=200),
        workers=None,
        video_encoder="auto",
        prepare_valid_only=False,
    )
    report_path = Path("/path/to/reports/check.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result["report"], ensure_ascii=False, indent=2))
    if result["manifest"] is None:
        raise SystemExit("所选轨迹检查未全部通过，未生成可用缓存")
    print("缓存已完成，可接上面的 LeTools 缓存转换命令")


if __name__ == "__main__":
    main()
```

默认 `prepare_valid_only=False` 要求所选录制全部通过。融合过程可能已在新缓存目录生成部分视频，但任一轨迹检查失败就返回完整报告、`manifest=None`，不写 `tianji-cache.json`，不生成或发布最终 LeRobot 数据集。编码器、磁盘或进程故障直接抛出异常；失败目录拒绝覆盖，可保留诊断并换新目录重试。

只有调用者明确要输出有效子集时，才传 `prepare_valid_only=True`。全部输入仍执行相同 deep check；存在失败时 `report["valid"]` 仍为 False，错误不会变成 warning。成功清单只引用通过项，episode/global index 连续重编，`selection` 和清单的 `source_selection` 保存拒绝清单与原输入到输出的映射；全部失败时依然没有 manifest。原始数据不删、不修、不拆，缓存中的录制文件夹编号可能有空档，应按清单读取。此选项暂未提供 CLI，配套 Web 始终使用默认严格模式。

融合入口要求全新缓存目录，不自动复用已有缓存。它没有全局隐式状态；单独运行一次 check 后再调用它，也不会自动继承旧结果。每条数值读取和对齐一次，完整视频解码一次；视频时间戳另行扫描，自动编码器探测可能额外读取首帧。每帧检查在输出选择前执行，被裁剪尾部仍检查。详细参数与结果字段见[参数详解](docs/PARAMETERS.md#python-api-执行参数)。

### 单独检查或查看对齐

```bash
tianji-provider check /path/to/recordings --report /path/to/reports/check.json
tianji-provider check /path/to/recordings --no-deep-video
tianji-provider inspect /path/to/recordings \
  --task 'Pick and place' --fps 50 --wrench-fps 200 --report /path/to/reports/alignment.json
```

Check 可省略 task，配置中使用占位值 `validation`，不会生成训练数据集。`--no-deep-video` 只验证时间戳和首帧，不证明后续视频完整。Check 在一条失败后继续扫描其余轨迹。

Inspect 解析数值、读取视频时间戳和首帧，计算输出帧数、裁剪、补齐范围和视频最大年龄；不编码、不完整解码视频、不计算全部图像统计。它遇到错误会停止，不能替代 check 的逐条汇总。

### 直接从 MCAP 转换

```bash
letools convert /path/to/recordings /path/to/new-lerobot-v21 \
  --source-format tianji --task 'Pick and place' --fps 50 --wrench-fps 200 \
  --to v2.1 --workers 2 --video-workers 2 \
  --video-codec libx264 --video-pixel-format yuv420p
```

直接路径使用无损 RGB PPM `FrameSequence`，由 LeTools 编码；四路相机各自顺序解码原视频，另有图像统计解码，适合少量数据或调试。示例的 libx264 在已验证后端默认使用 CRF 23，不等同于 provider 的软件 CRF18 或 NVENC CQ16 配方。`prepare --video-encoder` 不作用于这个 LeTools 直接路径。PPM 是无损中间帧，最终 H.264 仍有损。默认 48 帧批次、960×540 时，PPM 缓冲约 71 MiB/相机，不代表进程总内存上限。

### 编码速度、画质与资源取舍

`--video-encoder libx264` 保留原有 CRF18 软件编码，适合没有可用 NVENC 或希望生成较小文件的情况；`--video-encoder h264_nvenc` 要求探测成功，否则明确失败。`auto` 只在探测阶段回退，真实批量编码中发生故障不会混用编码器继续完成。

已验证的 24 条批次中，NVENC CQ16 视频体积约为原软件配方的 **3.896 倍**；融合批次的进程树采样峰值 RSS 约 **7.77 GB**。这是指定机器和样本的测量，不是固定倍率或内存上限。两条完整质量样本、9,084 个相机帧相对同一原始图像的 RGB/YUV 平方误差均不大于软件 CRF18；不能据此承诺任意数据上的无损视频。完整证据和范围见[第二阶段实验](docs/PERFORMANCE_ROUND2.md)。并发越高会同时占用更多 CPU、主存及编码会话；NVENC 最多 12 条录制、48 个相机会话，单会话探测不保证任何显卡都支持这个并发，可降低 workers 或固定软件编码。

## 准备缓存与复用

```text
cache/
├── tianji-cache.json          # 所有步骤成功后最后写入的完成清单
├── episode_000000/
│   ├── data.parquet           # 已对齐、补齐、裁剪的表格
│   ├── alignment.npz          # 主帧/力网格/实际力样本绝对时间，以及视频索引和时间
│   ├── head_left.mp4
│   ├── head_right.mp4
│   ├── left_wrist.mp4
│   └── right_wrist.mp4
└── episode_000001/...
```

缓存是 provider 中间格式，不是最终 LeRobot。清单记录配置、源指纹、字段、episode 路径、统计、对齐报告、编码设置及分阶段耗时。复用要求：

1. 格式为当前 `tianji-thor-v4`。
2. 除 task 外的 `TianjiConfig` 完全一致；即使某参数本次未实际影响输出，也参与比较。
3. 再次对原始目录执行 prepare 时，MCAP 路径、大小、修改时间指纹一致，引用资源存在。指纹不是内容哈希。
4. `prepare` 还核对清单中的编码配方：`auto` 只接受本包已验证的 libx264 CRF18 配方或当前 NVENC CQ16/Main 配方；显式编码器请求必须匹配对应配方。旧软件清单没有 `options` 字典仍可按既有质量字段识别，其他 CQ/profile/配方不能借 auto 绕过检查。

只改 task 可复用视频缓存并重新生成目标元数据；改 FPS、wrench-fps、任何对齐/裁剪参数需新缓存。0.6.0/0.6.1 的有效 v4 软件缓存可继续使用；改用显式 NVENC 请求不能把已有软件码流自动改成硬件码流，需新目录。命中可复用缓存不探测 GPU。直接打开缓存的 `TianjiSource` 只核对格式、语义配置和资源，不重新核对原始指纹、不诊断原始数据，也不执行 `prepare` 的编码配方选择/白名单。已有不匹配目录和未完成缓存拒绝覆盖。

融合入口的运行期快照比普通 prepare 的复用指纹更广：记录选中路径及真实路径、MCAP/YAML/JSON 文件身份、大小、mtime/ctime，在开始、检查完成和清单写入前比较。它不是源内容哈希或文件锁，也不是跨调用复用旧检查结果的凭据。报告和选择信息由 API 返回，调用者负责另存 JSON；成功 manifest 内包含 `source_selection`，逐点审计仍在 NPZ。

Prepare 在视频编码/统计阶段每条四宫格完整解码一次，同时编码四路；完成选中帧后仍继续解码尾部检查映射。对齐阶段另读时间戳和首帧。如果前面执行 deep check，那是额外一次完整解码，不能把 check+prepare 全流程称为只解码一次。

数值 MCAP 在一次原始 source 加载中每条读取一次，独立 check 和后续 prepare 是不同加载。原始 source 保留全部已对齐低维表格与审计数组，内存随总帧数增长；RGB 图像流式处理。普通 prepare 的数值和视频分别按录制并行，融合 API 则在同一录制任务内检查并准备。自动进程数为 `min(12,max(1,可用CPU数-2),录制数)`；显式正整数可覆盖 CPU 自动选择，但 NVENC 视频/融合任务仍最多 12。该公式不是 CPU 绑定或保证两核空闲。普通 prepare 按输出帧数、融合 API 按 metadata 时长提示优先启动长任务，输出与报告顺序不变。每个视频 worker 一个解码器、四个单线程编码器；`--workers 1` 或 API `workers=1` 可串行。

数字统计基于最终 float32；wrench 的 min/max/mean/std 保留 `[N,12]`，逐窗口位置/分量跨行统计，count 为行数。图像统计使用选中 RGB 像素通道直方图，重复帧计权，范围归一化到 0–1；这是 H.264 编码前统计。对齐摘要随 `meta/info.json` 的 `tianji` 字段输出，逐行 `alignment.npz` 留在缓存；需要追溯时应保留缓存。

## 与 8095 的配合

Check 默认按录制并行，但逐条结果、跨录制尺寸校验和进度回调保持稳定目录顺序。新并发参数及 Python 调用要求见 [参数详解](docs/PARAMETERS.md)。

0.6.2 配套 Web 转换流程为 `check_prepare → convert → deep validation → publish`，使用同一配置的融合 API，阶段显示为“检查并准备”。网页“仅检查”仍调用独立 check，不编码输出。只有 provider 更新而平台 worker 尚未更新时，旧网页仍会走两个独立阶段；实际部署版本以 `/api/environment` 和任务阶段为准。

- 正式转换重新执行全部深度检查，融合 API 固定 `prepare_valid_only=False`；选中任意一条有 error 就不生成成功缓存、不进入 LeTools 转换或最终发布。可能保留未完成准备文件，但不自动跳过或删除异常轨迹，warning/info 不直接阻止。
- 平台额外核对检查/准备期间源文件是否变化，要求新目标目录，在暂存输出 deep validation 成功后发布。
- 网页、任务队列、目录选择、进度、取消、数据集登记和工具入口由 data-web-platform 提供；不是 provider 标准接口的一部分。
- 页面发现列表来自指定 LeTools 环境；其他 provider 被发现不代表网页已适配其参数表单。
- 融合 API 自带运行期源快照检查，独立 check/prepare CLI 不自动获得相同保护；它们都不代替 Web 的最终暂存、deep validation 和不覆盖发布流程。页面历史默认 task 为 `Plug in the Ethernet cable`，其他任务必须主动填写对应文本。

## 限制、参考与测试

仅支持核验过的 `thor-v1` 消息与相机布局，不猜测其他机器人的关节顺序、单位、标定或时钟补偿。不会判断抓取成功、画面模糊、遮挡或碰撞。没有 Header 的夹爪数据仍含接收延迟；统一网格不等于硬件同步。

初始实机核对日期 2026-09-20：Thor 的 JointcmdArm 定义、关节 mux、夹爪驱动、EEF/wrench 发布和 CAMERA_SLOTS 共同确定契约。历史样本水印与视频消息时间相差约 8–23 ms，不用于推断全局补偿或证明当前全部采集精度。

方法参考：[ROS message_filters](https://docs.ros.org/en/ros2_packages/rolling/api/message_filters/message_filters.html)、[SciPy resample_poly](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html)、[SciPy Slerp](https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.transform.Slerp.html)。算法细节见 [内部设计](docs/DESIGN.md)。

在已安装 provider 与 pytest 的环境，从 data-providers 仓库根目录运行：

```bash
python -m pytest -q tianji_provider/tests
```

自带测试覆盖命令空流与错开边界、阈值、全路缺口、观测缺失、首尾裁剪、坏命令、旧缓存拒绝、多速率力窗口、嵌套 Parquet、编译解码差分、并行顺序/取消/崩溃与精确像素统计，也覆盖编码器探测/配方匹配、融合诊断等价、有效子集重编号和失败不发布清单。真实 MCAP 集成测试通过环境变量显式启用，命令见 [验证记录](VALIDATION.md)。历史脚本、旧测试和大体积产物不随仓库分发。0.6.0 曾改变字段和采样语义；0.6.2 保持数值与信号规则，v4 缓存兼容性按本页语义配置、资源和编码配方规则判断。
