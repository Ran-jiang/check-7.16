"""从项目根目录的 .env 加载产品运行配置。"""

from __future__ import annotations

import os
from .paths import PROJECT_ROOT


def load_project_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
