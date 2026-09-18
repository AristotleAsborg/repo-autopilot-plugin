"""沙箱（路线 1.5）。

## 本机的现实（先看清，再决定做什么）

* 无 Docker —— `state/capabilities.yaml` 已实测 `docker_available: false`；
* 无管理员权限 —— 装不了防火墙规则，也没有 Windows 沙箱可用；
* `resource.setrlimit` 是 **Unix-only**，Windows 上 `import resource` 直接失败。

所以我们走路线自己写明的降级路径，`sandbox_strength: weak`。
路线同时写明这一步"红队演练的逃逸项相应跳过"，因此本模块的原则是：
**把拦住了什么、没拦住什么，一律如实报出来，绝不假装。**

## 真正拦得住的三件事

1. **文件系统**：不把源仓库交给被测代码。每个任务先把仓库**复制**到
   `state/sandbox/<task>/repo`，补丁与测试只看到副本；跑完再对源仓库做一次
   全量哈希比对，确认它一个字节都没变。
2. **资源**：Windows 作业对象 —— 单进程与整作业的内存上限、活动进程数上限、
   随作业关闭杀掉整棵进程树（孙子进程也跑不掉）。CPU 用进程亲和性近似 2 核。
3. **超时**：硬超时到点即 `TerminateJobObject`，不是杀一个进程了事。

## 明确没拦住的（红队演练里相应跳过）

* **网络**：没有 Docker、没有管理员权限，就没有可靠的断网手段。
  环境里塞假代理是自欺欺人（一行就绕过），所以不做。
* **磁盘配额**：Windows 上免管理员的磁盘配额不存在。
* **任意绝对路径读**：作业对象管资源不管权限。被测代码若硬编码
  `D:\\...` 仍能读到当前用户可读的文件。真正的文件隔离要等有 Docker 的部署。

## 顺带做对的一件小事

子进程**不继承我们的环境变量**，只给白名单。理由很直接：被测的是不可信代码，
而我们的环境里可能有 `GH_READ_TOKEN` / `GH_WRITE_TOKEN`。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import jobobject

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"
SANDBOX_ROOT = STATE_DIR / "sandbox"

IGNORED_DIRS = frozenset(
    {".git", "__pycache__", ".cache", ".pytest_cache", ".ruff_cache", ".mypy_cache",
     ".venv", "venv", "node_modules", ".pip-tmp"}
)

# 只有这些环境变量会传给孩子。不可信代码不该看见我们的 token。
ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS")


@dataclass
class Limits:
    """路线 1.5 的资源条款。数值就是路线写的那些。"""

    timeout_seconds: float = 900.0          # 15 分钟硬超时
    memory_bytes: int = 4 * 1024**3         # 内存 4GB
    active_processes: int = 64              # 进程炸弹上限（路线没写，防逃逸加的）
    cpu_cores: int = 2                      # CPU 2 核
    disk_bytes: int | None = 2 * 1024**3    # 磁盘 2GB（Windows 上无法强制，见 enforced）


@dataclass
class SandboxResult:
    passed: bool
    log: str
    exit_code: int | None
    timed_out: bool
    duration_s: float
    workdir: Path
    repo_dir: Path
    source_unchanged: bool
    patch_applied: bool
    enforced: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "workdir": str(self.workdir),
            "repo_dir": str(self.repo_dir),
            "source_unchanged": self.source_unchanged,
            "patch_applied": self.patch_applied,
            "enforced": dict(self.enforced),
            "notes": list(self.notes),
            "log": self.log,
        }


# ------------------------------------------------------------------ 能力表

def capabilities() -> dict[str, str]:
    """
    如实列出每一项隔离能力的现状。**这个函数是给人看的诚实度声明。**

    它有存在的必要：没有它，"弱的沙箱"和"强的沙箱"在调用方眼里一模一样，
    于是"跑过沙箱"会被当成"安全"，而实际上网络是通的。
    """
    job_ok, job_detail = jobobject.can_assign()
    job_state = (
        "job-object（单进程 + 整作业双上限，且关作业杀全树）"
        if job_ok
        else f"not-enforced（拿不到进程接管权限：{job_detail}）"
    )
    return {
        "sandbox_strength": "weak",
        "filesystem": "copy-based：源仓库不暴露给被测代码，跑完全量哈希比对",
        "memory": job_state,
        "processes": job_state,
        "cpu": "affinity（前 2 个核）",
        "timeout": "hard（到点 TerminateJobObject，整棵树）",
        "disk": "not-enforced：Windows 上没有免管理员的磁盘配额手段",
        "network": "not-enforced：无 Docker、无管理员权限；按 1.5 降级方案跳过该项",
        "env": "白名单：不继承宿主环境变量（宿主 env 里可能有 token）",
        "reason": "docker_available=false；resource.setrlimit 在 Windows 上不存在",
    }


# ------------------------------------------------------------------ 工具

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: Path) -> dict[str, str]:
    """
    全量哈希。用于证明"跑完沙箱后源仓库一个字节都没变"。

    读不了的文件直接跳过并**不静默**：跳过会让比对结果看起来一致，
    所以跳过的路径也记进结果（以 `!unreadable:` 前缀），两侧都会带上。
    """
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            manifest[relative] = _sha256(path)
        except OSError:
            manifest[f"!unreadable:{relative}"] = ""
    return manifest


def _ignored(name: str) -> bool:
    """
    复制时跳过哪些条目。

    `pytest-cache-files-*` 是 pytest 在缓存目录写不进去时留下的残骸
    （本机沙箱特有），它由沙箱 SID 拥有，**连读都读不了** ——
    `shutil.copytree` 碰到它会直接抛 Error 把整个复制打断。
    """
    return name in IGNORED_DIRS or name.startswith("pytest-cache-files-")


def _copy_tree(source: Path, target: Path) -> list[str]:
    """
    把源仓库复制进工作目录，返回**跳过**的条目（不静默）。

    自己走一遍而不是用 `shutil.copytree` 的原因：copytree 遇到任何一个读不了的
    条目就整棵树失败。而"某个残骸目录读不了"不该让整个任务跑不起来 ——
    该记下来，然后继续。
    """
    skipped: list[str] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(_ignored(part) for part in relative.parts):
            continue
        destination = target / relative
        try:
            if path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        except OSError as exc:
            skipped.append(f"{relative.as_posix()}: {exc}")
    return skipped


def _child_env(work: Path) -> dict[str, str]:
    home = work / "home"
    temporary = work / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.update(
        {
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMP": str(temporary),
            "TEMP": str(temporary),
        }
    )
    return env


def _normalize(cmd: str | list[str] | tuple[str, ...] | None) -> list[str] | None:
    if cmd is None:
        return None
    if isinstance(cmd, str):
        return ["cmd", "/c", cmd]
    return list(cmd)


def _fingerprint(root: Path) -> str:
    """
    轻量目录指纹：路径 + 大小 + mtime。**不能用 `hash_tree`** ——
    它按设计会跳过点开头的目录，而测试用的副本就放在 `.cache/` 下面，
    于是"前后指纹"永远是两个空字典，验证形同虚设（实测踩过：补丁明明打上了，
    验证却说没变，然后重试时报 "patch does not apply"）。
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if parts and parts[0] == ".git":
            continue
        stat = path.stat()
        digest.update(f"{path.relative_to(root).as_posix()}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def apply_patch(repo_dir: Path, patch_file: Path, log_path: Path) -> tuple[bool, str]:
    """
    在**副本**里应用补丁。补丁本身不执行，执行它的是 git apply，安全。

    ## 必须验证"真的改了"

    只看返回码是不够的：`git apply` 在**不是 git 工作树**、或补丁与文件对不上时，
    行为可能是"返回 0 却没改任何东西"，也可能直接报错。而 4.2 的修复循环一旦
    拿"没打补丁的代码"去跑测试，表现就是"模型改了 8 轮还是同一个断言失败" ——
    看起来像模型不行，实际是补丁没落上。所以这里对每次尝试都**比对目录指纹**，
    返回 0 但内容没变 = 失败；两次都无效就先把副本变成一个 git 工作树再重试。
    """
    before = _fingerprint(repo_dir)

    # 副本一般**不是 git 工作树**，而 git apply 在非工作树里可能"返回 0 却不改任何东西"。
    # 与其先失败一次再补救，不如先把副本初始化成一个空工作树 —— 补丁只有真的落上，
    # 后面的测试才是在测"打完补丁的代码"。
    if not (repo_dir / ".git").exists():
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(repo_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        before = _fingerprint(repo_dir)   # 刚建的 .git 不该算作"补丁生效"

    def try_apply(extras: list[str], note: str) -> tuple[bool, str] | None:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"\n$ git apply {' '.join(extras)} {patch_file.name}  # {note}\n")
            handle.flush()
            outcome = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", *extras, str(patch_file)],
                cwd=str(repo_dir),
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
        if outcome.returncode == 0 and _fingerprint(repo_dir) != before:
            return True, "ok" if not note else f"ok（{note}）"
        return None

    for extra in (["-p1"], ["-p0"]):
        result = try_apply(extra, "")
        if result is not None:
            return result
    return False, "git apply 失败（-p1 与 -p0 都试过；返回 0 但文件未变的情况已按失败处理）"


# ------------------------------------------------------------------ 主入口

def run(
    repo_path: str | Path,
    patch_file: str | Path | None = None,
    test_cmd: str | list[str] | None = None,
    *,
    limits: Limits | None = None,
    root: Path | None = None,
    task_id: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> SandboxResult:
    """
    路线 1.5 的 `run(repo_path, patch_file, test_cmd)`。

    顺序刻意是"先复制、再打补丁、最后跑测试"：补丁和测试都只接触副本，
    源仓库从进程启动那一刻起就不在它们的视野里（除了绝对路径，见模块头）。
    """
    limits = limits or Limits()
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise NotADirectoryError(f"不是目录：{repo_path}")

    sandbox_root = root or SANDBOX_ROOT
    work = sandbox_root / (task_id or uuid.uuid4().hex[:12])
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    repo_dir = work / "repo"
    log_path = work / "run.log"
    log_path.write_text("", encoding="utf-8")

    notes: list[str] = []
    enforced = capabilities()

    # 1) 记下源仓库的指纹，然后复制
    before = hash_tree(repo_path)
    copy_skipped = _copy_tree(repo_path, repo_dir)
    if copy_skipped:
        notes.append(f"复制时跳过 {len(copy_skipped)} 个读不了的条目：{copy_skipped[:3]}")

    # 2) 在副本里打补丁
    patch_applied = False
    if patch_file is not None:
        patch_applied, detail = apply_patch(repo_dir, Path(patch_file), log_path)
        notes.append(f"apply_patch: {detail}")

    # 3) 跑测试
    command = _normalize(test_cmd)
    exit_code: int | None = None
    timed_out = False
    duration = 0.0
    job_ok, job_detail = jobobject.can_assign()

    if command is None:
        notes.append("test_cmd 为空：只打了补丁，没有执行任何命令")
        passed = patch_applied or patch_file is None
    else:
        env = _child_env(work)
        if env_extra:
            env.update(env_extra)

        job = None
        if job_ok:
            try:
                job = jobobject.Job(
                    jobobject.JobLimits(
                        memory_bytes=limits.memory_bytes,
                        active_processes=limits.active_processes,
                    )
                )
            except OSError as exc:
                job = None
                notes.append(f"作业对象创建失败，资源上限未生效：{exc}")
        else:
            notes.append(f"资源上限未生效（拿不到进程接管权限）：{job_detail}")

        started = time.perf_counter()
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"\n$ {' '.join(command)}\n")
            handle.flush()
            process = subprocess.Popen(
                command,
                cwd=str(repo_dir),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,   # 合并进同一个**文件**句柄，不建管道
                stdin=subprocess.DEVNULL,
            )
            if job is not None:
                assigned, why = job.assign_pid(process.pid)
                if not assigned:
                    notes.append(f"进程未进入作业，资源上限未生效：{why}")
                else:
                    affinity_ok, affinity_detail = jobobject.set_affinity(
                        process.pid, limits.cpu_cores
                    )
                    notes.append(
                        f"cpu 亲和性 {limits.cpu_cores} 核："
                        f"{affinity_detail if affinity_ok else '未生效 ' + affinity_detail}"
                    )
            try:
                exit_code = process.wait(timeout=limits.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                # 杀的顺序很要紧：**先动手，再等**。
                # 反过来的话，会先在"作业对象是空的、根本杀不掉它"的情况下白等 30 秒，
                # 然后才轮到 taskkill —— 而那时早就没在等了。
                if job is not None:
                    job.terminate()
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                try:
                    exit_code = process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        exit_code = process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        exit_code = None
        duration = time.perf_counter() - started
        if job is not None:
            job.close()

        passed = (exit_code == 0) and not timed_out
        if timed_out:
            notes.append(f"超时熔断：{limits.timeout_seconds}s 到点，已杀整棵作业树")

    # 4) 证明源仓库没被动过
    after = hash_tree(repo_path)
    source_unchanged = before == after
    if not source_unchanged:
        changed = sorted(set(before) ^ set(after))[:10]
        notes.append(f"源仓库发生变化！差异样本：{changed}")

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    return SandboxResult(
        passed=passed,
        log=log_text[-20000:],
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=duration,
        workdir=work,
        repo_dir=repo_dir,
        source_unchanged=source_unchanged,
        patch_applied=patch_applied,
        enforced=enforced,
        notes=notes,
    )
