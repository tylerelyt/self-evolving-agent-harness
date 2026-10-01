"""构造的历史支付适配层；三个调用点沿用共享默认值。"""
from lib.database.pool import acquire


def authorize():
    return acquire()


def capture():
    return acquire()


def cancel():
    return acquire()
