# 自己部署 Langfuse

第19讲既可以连接 Langfuse Cloud，也可以连接自部署实例。Trace、Observations、数据集和版本导出使用同一套 SDK，不需要先注册云端账号。

本目录按 [Langfuse v4.72.0 官方 Compose](https://github.com/langfuse/langfuse/blob/v4.72.0/docker-compose.yml)准备单机教学环境。Web、Worker、PostgreSQL、ClickHouse、Redis 和 MinIO 都运行在本项目的独立容器中；没有依赖宿主机已经运行的数据库。默认只把 Web 和对象存储绑定到本机回环地址，不向局域网公开。

## 启动

需要 Docker Engine 和 Docker Compose。下面的命令在本目录执行。容器的内存上限合计约11.5 GiB，不代表实际常驻用量；镜像和数据需要另外准备磁盘空间。不要在磁盘接近写满的机器上运行。

```bash
python3 init.py
docker compose -p oncall-course-lf config --quiet
docker compose -p oncall-course-lf up -d
docker compose -p oncall-course-lf ps
curl --fail http://localhost:13019/api/public/health
```

`init.py` 为本次部署生成随机凭据，写入权限为0600的 `.env`，不会覆盖已有文件。不要把 `.env`、容器数据和带凭据的 `docker compose config` 输出提交到 Git。

首次启动需要进行数据库迁移。健康接口返回正常以后，访问 `http://localhost:13019`。启动配置会建立一个教学组织、项目和账户，登录名是 `learner@example.test`，密码为 `.env` 中的 `ADMIN_PASSWORD`。API 使用同一文件中的 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`。

如果部署在自己有权使用的 Linux 服务器上，可用 SSH 转发两个端口：

```bash
ssh -N -L 13019:127.0.0.1:13019 -L 19019:127.0.0.1:19019 your-server
```

浏览器和SDK仍连接本机地址。两边的端口必须空闲；如需修改，连同 `.env` 中的端口、`LANGFUSE_BASE_URL` 和转发命令一起调整。

## 接入第19讲代码

回到第19讲目录，准备固定 SDK，并加载刚才自己生成的连接配置：

```bash
python3 setup.py --langfuse
set -a
. selfhost/.env
set +a
.deps/sdk/bin/python -B langfuse_io.py collect \
  --trace-id REAL_TRACE_ID --out runtime/collected.json
```

其余 `publish`、`export` 和 `restore` 命令与本讲 README 相同。`collect` 不会自行生成事故记录，必须先由真实运行写入 Trace；数据集的标准答案仍来自独立标注，不能从 Agent 的旧回答自动抄出。

## 停止与删除

暂时停止并保留数据：

```bash
docker compose -p oncall-course-lf down
```

确定不再需要本次测试数据时，再删除这个项目的数据卷：

```bash
docker compose -p oncall-course-lf down --volumes
```

`--volumes` 会永久删除该实例中的 Trace、数据集和账户，执行前应导出需要保留的记录。它不是备份方案。不要执行全局 `docker system prune`，也不要清理其他项目的卷。关闭 SSH 转发后，远端部署不会继续占用本地端口。

本配置用于单机教学验证。企业长期部署需要另行安排备份、访问管理和容量监测，不把一次样例验证当成生产环境验收。
