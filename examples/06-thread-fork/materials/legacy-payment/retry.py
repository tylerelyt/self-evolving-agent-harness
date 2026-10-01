"""构造的新版本重试路径：校验失败时没有归还已取得的连接。"""


def retry_payment(pool, headers):
    connection = pool.acquire()
    if "Idempotency-Key" not in headers:
        raise ValueError("缺少幂等请求头")
    pool.release(connection)
    return "已处理"
