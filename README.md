# data-providers

[LeTools](https://github.com/XieWeikai/letools) 的独立数据源 provider 集合。每个 provider 单独打包、按需安装，通过 `letools.source_providers` entry point 注册，无需修改 LeTools 源码。

## 已有 provider

| 目录 | source-format | 输入 | 文档 |
|---|---|---|---|
| `tianji_provider/` | `tianji` | Tianji / Marvin Pro Thor 的 ROS 2 MCAP 轨迹与四宫格 H.264 视频 | [使用说明](tianji_provider/README.md) · [验证记录](tianji_provider/VALIDATION.md) |

Tianji 文档入口：[全部参数详解](tianji_provider/docs/PARAMETERS.md) · [高频 wrench 窗口](tianji_provider/docs/WRENCH_WINDOWS.md) · [插件接口与内部设计](tianji_provider/docs/DESIGN.md) · [检查项目与报错](tianji_provider/docs/CHECKS.md) · [第二阶段性能实验](tianji_provider/docs/PERFORMANCE_ROUND2.md)。当前使用手册对应 0.6.2；VALIDATION 保留各版本的历史测量。

`SourceProvider` 负责参数和创建数据源，`DatasetSource` 提供元数据、episode 表格及媒体给 LeTools。Tianji 包另外提供 check、inspect、prepare 辅助工具；这些不是每个 provider 必须实现的标准命令，也不编辑原始 MCAP。LeRobot 目标布局由 LeTools 后端负责，8095 网页的任务编排在独立 data-web-platform 项目中。

Tianji provider 支持目标 FPS（默认 50）、必填任务文本、观测与视频的时间交集，以及四路相机拆分。0.5.0 起四路控制命令取有效覆盖并集，每个保留时刻至少一路有效，其余以同帧 state 补 action，支持某一路整段无命令的单臂操作。每条录制保持为一个 episode，默认裁剪首尾静止部分并保留中途暂停。输出由 LeTools 的 LeRobot v2.1 / v3.0 后端负责；旧 v1/v2/v3 准备缓存需重建，0.6.0/0.6.1 生成的同配置合法 v4 缓存可继续在 0.6.2 使用。

提供 `tianji-provider check /path/to/recordings --report /path/to/tmp/diagnostics.json` 独立异常检测：默认完整解码视频，检查反馈/视频缺口及数据结构、时钟、标定等问题，逐轨迹继续扫描。检测命令不要求 task，不修改或删除输入。

0.6.0 起夹爪 action 使用 `/control/gripperValueL/R`。Wrench 不滤波，只保存 `observation.wrench_windows`，形状 `[wrench_fps/fps,12]`。默认主频 50 Hz、`--wrench-fps 200`，每行 4 个过去原始力样本；wrench-fps 必须 ≥ fps 且为其整数倍。当前缓存格式为 `tianji-thor-v4`。

直接转换使用无损 RGB 中间帧，避免 JPEG 导致的细节损失。0.6.2 新增 `check_and_prepare()` 融合API供8095转换使用，复用完整检查和准备过程中的读取；独立check/prepare命令仍可串联。默认安全探测NVENC H.264 CQ16/Main，不可用则用软件x264 CRF18；可用 `prepare --video-encoder libx264` 固定软件编码。解码仍为原软件H.264，未采用会放松坏码流拒绝的CUVID候选。最终LeTools复用编码包，不二次有损压缩。Thor全125检查、114通过项转换由9分32秒降至4分29.5秒，代价是视频约3.88倍体积、峰值内存约8.13GB。完整流程、适用范围和质量证据见provider文档。

## 安装一个 provider

先按 [LeTools 的安装文档](https://github.com/XieWeikai/letools#readme) 安装 LeTools，然后将 provider 安装到同一个 Python 环境。Marvin 8095 使用独立 `~/projects/letools-tianji/.venv`，请按 [专用环境说明](tianji_provider/README.md#marvin-的-8095-专用环境) 操作；下面的通用示例会更新 uv tool 管理的另一份 LeTools：

```bash
git clone https://github.com/XieWeikai/data-providers.git
cd data-providers
uv pip install --python "$(uv tool dir)/letools/bin/python" -e ./tianji_provider
letools providers list
```

随后可以在任意目录使用该 source-format：

```bash
letools convert /path/to/recordings /path/to/lerobot_dataset \
  --source-format tianji --task "将线缆插入接口" --fps 50 --to v2.1 \
  --video-codec libx264 --video-pixel-format yuv420p
```

这是直接转换示例，不会自动运行完整 check，也不读取旧检查报告；推荐使用 [check 成功才继续的命令链](tianji_provider/README.md#推荐流程check--prepare--convert--validate)。缓存转换省略图像编码参数，复用已有 H.264 视频。

## 添加其他 provider

在仓库根目录新增一个独立子目录，例如：

```text
data-providers/
├── README.md
├── tianji_provider/
│   ├── pyproject.toml
│   ├── README.md
│   └── src/tianji_provider/
└── another_provider/
    ├── pyproject.toml
    ├── README.md
    └── src/another_provider/
```

每个 provider 使用唯一的包名、Python 模块名和 entry-point 名称，独立声明依赖。README 应说明输入格式、字段语义、时间对齐策略、必需参数和可执行示例。遵循 LeTools 仓库的 [新增数据源指南](https://github.com/XieWeikai/letools/blob/main/skills/letools-add-source/SKILL.md)。

本仓库保存 provider 代码及文档。原始轨迹、转换数据集、视频、缓存、环境目录和本地临时文件不纳入版本控制。
