以下是构造的长期会话背景，不对应真实平台。
过去几个月的任务一直围绕新支付平台：通过灰度链路发布，有 Canary 实例，流量经服务网格和 Sidecar。
发布入口为 platform_release_history，流量入口为 platform_mesh_metrics；在项目目录使用 Python 执行 query.py，后接入口名称。
此前一次 Sidecar 升级出现流量异常，先核对 Canary 版本和服务网格采集，再对齐灰度批次。实例 pay-new-canary-01、版本 4.2、灰度比例 10% 均为构造值。
这段记录属于新支付平台任务。
