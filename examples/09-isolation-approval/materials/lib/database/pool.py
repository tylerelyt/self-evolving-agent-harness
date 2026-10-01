"""构造的共享连接池接口；30 秒是教学构造值，不产生真实等待。"""


def acquire(wait_seconds=30):
    return {"wait_seconds": wait_seconds}
