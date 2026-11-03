"""为独立教学实例创建随机凭据；不覆盖已有配置。"""

from pathlib import Path
import os
import secrets


def main():
    path = Path(__file__).resolve().parent / ".env"
    values = {
        "WEB_PORT": "13019",
        "MEDIA_PORT": "19019",
        "LANGFUSE_BASE_URL": "http://localhost:13019",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(16),
        "LANGFUSE_SECRET_KEY": "sk-lf-" + secrets.token_hex(32),
    }
    for name in (
        "POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_AUTH",
        "MINIO_ROOT_PASSWORD", "SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET",
        "ADMIN_PASSWORD",
    ):
        values[name] = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(".env 已存在；沿用原配置，不重新生成或覆盖。")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print("已创建 selfhost/.env，文件权限 0600。请勿提交或公开其中的凭据。")


if __name__ == "__main__":
    main()
