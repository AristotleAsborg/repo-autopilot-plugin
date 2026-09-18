"""测试共用夹具。

## 为什么不用 pytest 的 tmp_path / tempfile.mkdtemp

本机沙箱给新建目录套的 ACL 很特殊，踩了三个坑才能跑通：

  1. `tmp_path` 默认落在系统临时目录（`C:\\Users\\...\\Temp\\dsh-XXXX\\pytest-of-ASUS`），
     沙箱对该目录禁写 → `PermissionError: WinError 5`
  2. 改用 `--basetemp` 指到仓库内，pytest 又把 basetemp 建成自己和后续运行都读不了的目录
  3. 改用标准库 `tempfile.mkdtemp` 仍未逃掉：**mkdtemp 建的目录连 `os.scandir`
     都被拒**。因为 mkdtemp 强制 0700 模式，而目录所有者是沙箱的 AppContainer SID
     （见 state/capabilities.yaml 的 security_constraints），进程反而进不去。

可用的做法：**普通 `mkdir`**。它不指定模式，目录 ACL 继承自父目录，
因而对自己可读写。因此下面用手工拼 uuid 的唯一目录名。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

# 放在仓库内、且在 .gitignore 覆盖范围内。
# conftest.py 位于 <repo>/tests/，仓库根是 parents[1]（不是 parents[2]——那是工作区根）。
SCRATCH_ROOT = Path(__file__).resolve().parents[1] / ".cache" / "test-scratch"


@pytest.fixture()
def scratch() -> Path:
    """一个可读写的临时目录，位置在工作区内。每个测试独立，用完即删。"""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_ROOT / f"t-{uuid.uuid4().hex[:12]}"
    path.mkdir()   # 普通 mkdir：继承父目录 ACL，不要用 mkdtemp
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def state_dir(scratch: Path) -> Path:
    """队列测试用的 state 目录。"""
    d = scratch / "state"
    d.mkdir()
    return d
