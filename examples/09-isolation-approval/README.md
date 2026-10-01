# Lab 09｜令行禁止：怎样用隔离与审批守住授权边界？

[打开 Notebook](workshop.ipynb)。这是专栏《自进化 Agent 实战》09 讲的配套练习，以专栏正文为叙事依据。实例 `legacy-payment-demo-09`、三处调用、30 秒和 5 秒均为构造值；连接池只返回参数，不模拟生产等待。

默认从 Notebook 在线运行，也提供不需要密钥的离线材料回放。执行阅读页与核验结果保存在本地，不随练习发布。

```text
09 讲练习
├─ 阶段一：默认拒绝与显式可写根
│  └─ cwd 适配层 + 一个测试根；共享一行改法被拒，回到三个调用点
├─ 阶段二：计划与执行环境各自负责什么
│  └─ 只读写入被拒；扩大配置被拒；参数回归独立执行
└─ 阶段三：生成配置、人审与一次性例外
   └─ 同组权限临时文件自检；独立审批拒绝 → 批准一次 → 再次拒绝
```

第一阶段先只读形成计划，再生成配置。教学计划已由题设确认，独立代码逐项核对预先确定的范围后才应用配置；这只是教学批准的回放，生产中必须由人或独立审批系统审核。`cwd` 指向适配层，`writableRoots` 只添加对应测试，其他位置默认只读；`networkAccess=false`，`approvalPolicy=never`，临时目录例外同时关闭。Agent 可以提出配置，不能自行启用或添加共享目录。

第二阶段分别检查只读规划、执行边界和代码正确性。规划阶段使用 `readOnly` 权限；执行阶段可以临场调整方案，但权限范围保持不变。参数回归通过子进程执行，验证 authorize、capture、cancel 都显式传入五秒，以及共享接口仍默认三十秒。

第三阶段回看已生成的配置，以同组权限做临时文件自检：适配层成功，共享目录失败，再撤回适配层探针。正式任务应在应用配置后、业务修改前完成此项检查；这里为讲清机制，在第三阶段单独重做自检。另一个独立任务演示一次性客户端审批，匹配完整命令、cwd 与脚本哈希，只消费一次批准，不给主 Hotfix 扩权。若重复需要同一例外，应重新审视计划和默认边界。回调不能防止核对后发生的并发替换，也不代替远端系统权限。

## 三层材料与执行位置

```text
materials/
├─ services/payment/legacy_adapter/adapter.py
├─ tests/payment/legacy_adapter/test_adapter.py
├─ lib/database/pool.py
├─ PLAN.md
├─ permission-proposal.json
└─ hotfix-replay.json
```

准备格将三层代码复制到本次 `.runtime/<运行编号>/work/`，原材料不变。离线从 `hotfix-replay.json` 取得补丁，真实写入适配层和测试；共享写入由教学回放函数检查并拒绝。该函数只约束本例的回放写入，不能隔离任意 Shell 或子进程，不能据此宣布原生 Sandbox 已生效。离线审批直接调用同一核对回调，批准后真实执行构造报告脚本；没有伪造模型回答、模型请求或 SDK 工具回执。

Notebook 直接展示固定版本 SDK 的 `CodexClient.thread_start()` 与 `turn_start(thread_id, input, params)`，把已核对的 `sandboxPolicy` 放在第三个参数中，再用 `TurnHandle.run()` 消费实际结果。只读形成计划与配置、允许和拒绝探针、补丁执行、自检均由真实 Codex 调用完成。共享拒绝必须有匹配命令、退出码、目标路径和权限错误；沙箱启动失败、缺路径或未执行均不算通过。网络关闭检查的是配置及实际提交给执行端的参数，没有做外网连通性实测。

审批部分单独展示 `review_command(method, params)` 的完整判断、`CodexClient(approval_handler=review_command)`、两个独立 Thread 的实际调用以及一次性批准的消费。第一次原生请求返回 `decline`，第二次有一次教学批准时返回 `accept`；最后直接调用回调检查其再次返回 `decline`，这是回调自检，不是第三次模型执行。审批客户端与 Hotfix 客户端分别创建和关闭。

`examples/notebook09_setup.py` 负责准备，`examples/notebook09_story.py` 负责材料、表格、配置核对、离线回放与结束处理；关键 SDK 调用和审批回调均在 Notebook。公共 `examples/notebook_support.py` 准备模型连接，`lab.watch()` 保护等待，`lab.record_result()` 保存结果。litellm 只适配模型协议，不批准权限。重跑准备格会关闭旧连接，代码格出错及最后一格都自动关闭本次连接。

## 离线核验

使用已有的 Python 3.12 环境及 `nbformat`、`nbclient`、`nbconvert`、`ipykernel` 和样板固定依赖：

```bash
python examples/09-isolation-approval/run_validation.py --mode offline
```

编号以 `nb09-v1-` 开头。验证器移除密钥，阻止模型连接、外部网络和非教学子进程，不安装依赖。保存 `reading.html`、`executed.ipynb`、`result.json` 与 `REPORT.md`，原始回放及文件检查留在对应 `.runtime/`。源 Notebook 无执行输出；十九格中有十个代码格，按权限参数、只读规划、受限执行、自检、审批回调和审批对照分开阅读。

真实调用验证使用 `--mode live`，需要 `GLM_API_KEY`，准备过程隐藏输入一次。live 检查原生适配层与测试可写、共享写入被拒、规划只读、配置不扩大、网络关闭参数、独立回归、临时文件自检、一次性审批以及全部客户端进程和端口释放。离线与在线结果分别记录。
