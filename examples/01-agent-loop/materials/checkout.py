# 练习代码；函数、字段名与对应日志数值均为构造值，不代表真实服务。
def create_order(payload, repository):
    order = {
        "amount": payload["amount"],
        "currency": payload["currency"],
    }
    return repository.save(order)
