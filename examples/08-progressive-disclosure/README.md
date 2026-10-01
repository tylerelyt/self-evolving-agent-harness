# Lab 08｜按需调阅关键上下文：一项 Skill 的内容怎样逐层出现？

[打开 Notebook](workshop.ipynb)。这是专栏《自进化 Agent 实战》08 讲《渐进披露：按需调阅关键上下文》的配套练习。事故材料中的日期、实例名、域名、请求编号和数值均为构造值。

默认从 Notebook 在线运行；也提供离线材料验证。执行结果、带输出的阅读页与原始检查材料保存在本地，不随练习发布。

```text
08 讲练习
├─ 阶段一：Context 预算与分层
│  └─ 同一套文件全部装入 / 只装主干，比较字符占用
├─ 阶段二：Agent Loop 推动按需取用
│  └─ 能力索引 → 选中方法 → 字段表 → 模板 → 脚本结果
└─ 阶段三：Harness 接入与描述边界
   └─ 能力发现、选中装入、循环取用；描述太宽误选、太窄漏选
```

第一阶段由代码格直接读取四个文件，拼接“全部写进入口”的对照文本，用字符数直观比较主干和附件的篇幅。随后查看实际请求，追踪这些内容在调查的哪一步进入模型输入。Notebook 为读者展示的文件与模型通过工具取得的文件分开记录。

第二阶段直接调用 `codex.thread_start()` 创建会话：Codex 从 `.agents/skills/legacy-payment-oncall/` 发现能力索引；先进行普通问候，再用 `thread.turn([SkillInput(...), TextInput(...)])` 显式选择方法并简述主干；最后只给调查目标，由 Agent Loop 根据方法中的使用时机读取资料、写报告、执行检查。每次实际调用 `turn.run()` 等待结果。初始索引、选中装入、调查分别留存请求范围；通过正文随机标记与工具回执核对逐层出现的顺序。这里显式指定Skill，把观察集中在选中以后正文怎样装入、附件怎样按需取得。

离线分支真实读取同一套文件，按照构造调查过程写入报告，再通过子进程运行检查脚本。报告故意让第二条推断缺引用，程序应返回该条目。`replay-context.json` 和 `replay-events.json` 是本地材料回放记录；没有创建模型请求或模拟模型回执，不能用它们证明模型主动选择或按需取用。

第三阶段用三种构造描述与两种任务做本地词语匹配对照，展示入口太宽、适中、太窄的差别，理解入口描述怎样影响匹配范围。最后对回能力索引、选中装入、Loop 取用和 litellm 代理层各自负责的动作；读回报告，核对输入与方法未变并自动关闭本次连接。

## 文件与材料

```text
materials/
├─ legacy-payment-oncall/
│  ├─ SKILL.md
│  ├─ references/log-fields.md
│  ├─ assets/incident-report.md
│  └─ scripts/check_report.py
└─ inputs/
   ├─ alert.md
   ├─ legacy.log
   ├─ release.md
   ├─ pool.md
   └─ config.md
```

主干保留调查顺序与附件使用时机；字段表解释旧日志 `rid` 与 `request_id` 的对应关系；模板区分事实、推断和待验证问题；检查程序只验证事实、推断条目是否带有“文件名:行号”的引用格式，不判断来源是否支持结论。准备时复制材料到本次 `.runtime/`，分别为索引、正文、字段表、模板、脚本源码和脚本结果生成不同随机标记，原材料不变。

`examples/notebook08_setup.py` 负责 Python 版本、依赖和密钥准备；`examples/notebook08_story.py` 负责材料、表格、独立记录与结束处理。公共 `examples/notebook_support.py` 准备 Codex SDK、litellm 与 GLM-5.2 连接，`lab.watch()` 保护等待，`lab.record_result()` 保存结果。关键 SDK 调用、文件读取、Context 对照、脚本执行和描述对照写在 Notebook 的七个代码格中；源文件无执行输出。

## 离线运行

使用已装好 `nbformat`、`nbclient`、`nbconvert`、`ipykernel` 与样板固定依赖的 Python 3.12 环境，在仓库根目录执行：

```bash
python examples/08-progressive-disclosure/run_validation.py --mode offline
```

运行编号以 `nb08-v1-` 开头。验证器清除密钥，设置离线运行标识，将模型连接启动替换为报错保护；不安装依赖、不请求模型。整本运行后保存 `validation/runs/<运行编号>/reading.html`、`executed.ipynb` 和 `result.json`，并更新统一报告。原始本地读写、脚本结果、字符对照、描述对照和结束状态保存在对应 `.runtime/<运行编号>/`。

真实调用验证使用 `--mode live`，需要 `GLM_API_KEY`；它通过一次隐藏输入核验准备分支，并检查初始只见索引、选中后正文出现、参考和模板按进度回流、脚本结果返回而源码未常驻、进程与端口释放。离线记录与真实调用结果分别保存，不能相互代替。

完整重跑准备格会先关闭上一轮连接。代码格中途报错也会触发关闭；自动验证始终在结束处理里再关闭一次。沿用样板的 `Sandbox.full_access` 与 `ApprovalMode.deny_all`，实验目录表示任务范围，不能把它当成操作系统隔离。
