# 课程与运行条件

每讲的文章主题和独立练习入口见[首页逐讲目录](README.md#逐讲练习)。第 23 讲有自己的总装练习；[独立综合实战](workshop/capstone/README.md)另列，不替代任何一讲。

## 环境准备

| 范围 | 运行环境 | 准备说明 |
| --- | --- | --- |
| 第 01—09 讲 | Codex SDK、Notebook、本地模型适配器 | [Notebook 依赖与运行方式](examples/README.md#前置条件与运行) |
| 第 10—18 讲 | Hermes | 各讲 README；通用依赖由 `bash scripts/setup_deps.sh` 安装 |
| 第 19—20 讲 | Hermes、数据集与评测工具；第20讲使用 GEPA | 各讲分别准备题包、运行环境和验收目录 |
| 第 21—23 讲 | Hermes、SkillClaw | 先运行 `bash scripts/setup_deps.sh` 和 `bash scripts/setup_skillclaw.sh`，再按本讲说明运行 |
| 独立综合实战 | 双实例 HTTP 环境、Hermes、SkillClaw | [综合实战启动说明](workshop/capstone/README.md) |

## 平台与模型

- Python 使用 3.12；安装脚本也支持通过 `uv` 准备环境。
- 第 03、08、09 讲需要宿主支持 Codex 原生 Sandbox。
- 第 18—20 讲的隔离评测需要 macOS `sandbox-exec`；其他平台可先运行各讲提供的离线自检。第 19 讲的 Skill Up 校验与 Hermes 接线预检也需要 macOS。
- 真实模型按本讲说明配置 `GLM_API_KEY`，凭据不写入 Notebook 或提交到仓库。
- 脚本模型用于复现实验流程，其结果与真实模型效果分别记录。

跨讲可以复用经过核对的材料，也可使用本讲提供的教学输入。各讲自己的准备、任务、运行步骤与验收条件以对应 README 为准。
