"""Job Hunter 应用包。

在任何子模块导入 Playwright 之前，先把浏览器内核目录指向项目内自带的
`.ms-playwright`（若存在）。这样无论用 `python -m app.main`、systemd、
`run_server.sh` 还是 `nohup` 启动，都能自动找到 Chromium，无需手动 export
环境变量；同时让整个项目自包含、可整体迁移。
"""

import os as _os
from pathlib import Path as _Path

_BUNDLED_BROWSERS = _Path(__file__).resolve().parent.parent / ".ms-playwright"
if _BUNDLED_BROWSERS.is_dir():
    # 用户已显式指定则尊重其设置，否则用项目内置目录。
    _os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_BUNDLED_BROWSERS))
    _os.environ.setdefault("REBROWSER_BROWSERS_PATH", str(_BUNDLED_BROWSERS))
