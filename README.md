# data-providers

[LeTools](https://github.com/XieWeikai/letools) 的独立数据源 provider 集合。每个 provider 单独打包、按需安装，通过 `letools.source_providers` entry point 注册，无需修改 LeTools 源码。

## 已有 provider

| 目录 | source-format | 输入 | 文档 |
|---|---|---|---|
| `tianji_provider/` | `tianji` | Tianji / Marvin Pro Thor 的 ROS 2 MCAP 轨迹与四宫格 H.264 视频 | [使用说明](tianji_provider/README.md) · [验证记录](tianji_provider/VALIDATION.md) |

Tianji provider 支持目标 FPS（默认 50）、必填任务文本、所有信号的时间交集，以及四路相机拆分。每条录制保持为一个 episode，命令暂停时用同帧 state 补 action，默认裁剪首尾静止部分并保留中途暂停。输出由 LeTools 的 LeRobot v2.1 / v3.0 后端负责。

## 安装一个 provider

先按 [LeTools 的安装文档](https://github.com/XieWeikai/letools#readme) 安装 LeTools，然后将所需 provider 安装到同一个 Python 环境。对于 `uv tool` 安装的 LeTools：

```bash
git clone https://github.com/XieWeikai/data-providers.git
cd data-providers
uv pip install --python "$(uv tool dir)/letools/bin/python" -e ./tianji_provider
letools providers list
```

随后可以在任意目录使用该 source-format：

```bash
letools convert /path/to/recordings /path/to/lerobot_dataset \
  --source-format tianji --task "将线缆插入接口" --fps 50 --to v2.1
```

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
