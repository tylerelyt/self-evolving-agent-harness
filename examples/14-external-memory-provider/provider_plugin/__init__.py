"""教学Home中的原生提供者注册入口。"""
from oncall_memory import OncallMemoryProvider


def register(ctx):
    ctx.register_memory_provider(OncallMemoryProvider())
