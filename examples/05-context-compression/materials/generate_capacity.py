"""生成练习日志；服务、时间、编号和全部数值均为构造值。"""
import secrets


def capacity_lines():
    samples = [
        f"sample={i:04d} service=payment-api cpu_pct=24 memory_pct=41 "
        f"queue_wait_ms=12 elapsed_us={1000 + secrets.randbelow(9000)} state=healthy"
        for i in range(180)
    ]
    return ["# 构造材料：服务、时间、编号和全部数值均为构造值，不对应生产环境。", *samples,
            "01:30 error=payment_timeout；01:40 application_release=v3.8.2",
            "02:07 pool_wait_ms=3600 active_connections=60 pool_limit=60 error_rate_pct=8.4",
            "任务编号：CASE-527；下一步：核对连接池配置变更；发布是否有影响仍待验证。"]


def write_capacity(path):
    path.write_text("\n".join(capacity_lines()) + "\n", encoding="utf-8")
    return path
