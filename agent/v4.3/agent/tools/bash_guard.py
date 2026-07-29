"""bash 命令 guard：白名单 + 黑名单兜底 + 路径限制 + python 内容扫描。

移植自 dft-bench-solver-v1-stable，并加入 QE / ALAMODE 专用程序。

设计：
- 程序白名单 ALLOWED_PROGRAMS：不在列表 → reject（覆盖 transcript 99%）
- 显式黑名单 DENIED_PROGRAMS：双保险，即使白名单错放也拦
- 写操作目标 path：resolve 后必须在 cwd 子树内；含未展开变量直接 reject
- 读操作：宽松，但 ~/.ssh/ ~/.aws/ /etc/shadow .env id_rsa 等敏感路径/文件名 deny
- python 命令：内容扫描 os.system / subprocess / socket / urllib 等危险 import/调用
- 命令替换 $(...) / `...`：递归走相同检查
- env SOLVER_BASH_GUARD=off 整体放行（debug）

非目标：
- 不抗主动恶意（白名单仍可能漏，比如某个 ALLOWED 程序自身有 shell escape 能力）
- 不沙盒进程资源（mem/cpu/network），那是容器层的事
- 不解析 shell 的全部语法（heredoc 内容当字符串看，不展开变量）
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

# ==================== 配置 ====================

ALLOWED_PROGRAMS: frozenset[str] = frozenset({
    # coreutils 读
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep",
    "awk", "sed", "find", "which", "file", "strings",
    "diff", "sort", "uniq", "tr", "cut", "tee", "stat", "readlink",
    # 文件系统写（path 检查）
    "mkdir", "cp", "mv", "rm", "touch", "ln",
    # process / shell control
    "cd", "pwd", "echo", "printf", "sleep", "ps", "pgrep", "pkill",
    "kill", "wait", "nohup", "timeout", "time",
    "true", "false", "yes", "disown", "setsid", "type",
    # shell brace group { cmd; cmd; }
    "{", "}",
    # shell 控制结构关键字
    "while", "for", "do", "done", "if", "then", "else", "elif", "fi",
    "until", "case", "esac", "in", "select", "break", "continue", "return",
    # 数字 / 序列
    "seq",
    # python（内容会被扫描）
    "python", "python3",
    # MPI + QE executables
    "mpirun", "mpiexec",
    "pw.x", "bands.x", "dos.x", "ph.x", "projwfc.x", "pp.x", "ld1.x",
    "matdyn.x", "q2r.x", "dynmat.x", "hp.x",
    # ALAMODE executables
    "alm", "anphon",
    # 系统信息只读
    "nvidia-smi", "nproc", "uname", "hostname", "date", "id", "groups",
    "free", "df", "du",
    # archive
    "tar", "gzip", "gunzip", "zcat", "bzip2", "bunzip2",
    # shell 内置（test、true 等已含）
    "[", "test",
    # QE GPU wrapper（如果存在）
    "qe-gpu-wrap", "qe-gpu",
})

QE_EXECUTABLES: frozenset[str] = frozenset({
    # 当 agent 用绝对路径调这些时，会绕过 PATH 上的 $QE_GPU_WRAPPER_DIR/<bin>
    # 引到一个 CPU build（10× slowdown，烧掉 harness budget）。bench 的 PATH
    # 已经把 wrapper 暴露成同名命令，agent 应该只用 bare name。
    # 触发案例：WO3 vc_relax 2026-05-19 16:48 烧掉 2 小时 wall。
    "pw.x", "bands.x", "dos.x", "ph.x", "projwfc.x", "pp.x", "ld1.x",
    "matdyn.x", "q2r.x", "dynmat.x", "hp.x",
    "average.x", "fs.x", "molecularpdos.x", "plotband.x",
    "epsilon.x", "fermi_velocity.x",
    # ALAMODE
    "alm", "anphon",
})

DENIED_PROGRAMS: frozenset[str] = frozenset({
    # 网络
    "curl", "wget", "nc", "ncat", "telnet", "ssh", "scp", "rsync",
    "ftp", "sftp", "rclone", "lftp", "socat",
    # 提权 / 权限
    "sudo", "su", "doas", "chmod", "chown", "chgrp",
    "mount", "umount", "useradd", "usermod", "userdel", "passwd",
    "setcap", "setpriv", "capsh",
    # 破坏 / 设备
    "dd", "mkfs", "fdisk", "parted", "shred", "wipefs", "blkid",
    # shell escape（递归 shell 防 white-bypass）
    "bash", "sh", "zsh", "fish", "csh", "tcsh", "ksh", "ash", "dash",
    "exec", "eval", "source", ".",
    # 安装类（环境我们准备好）
    "apt", "apt-get", "dpkg", "yum", "rpm", "snap", "flatpak", "brew",
    "pip", "pip3", "conda", "mamba", "pipx", "npm", "yarn", "cargo",
    # 调试 / 提取
    "strace", "ltrace", "gdb", "ptrace",
    # env dump：会泄漏 ANTHROPIC_API_KEY 等 solver subprocess 继承的 secret
    "env", "printenv",
})

# 写操作（target path 必须 in workdir）
WRITE_PROGRAMS: frozenset[str] = frozenset({
    "mkdir", "cp", "mv", "rm", "touch", "ln", "tee",
})

# 读敏感路径 deny
READ_DENY_DIR_PREFIXES: tuple[str, ...] = (
    "~/.ssh/", "~/.aws/", "~/.gcp/", "~/.azure/", "~/.kube/", "~/.gnupg/",
    "~/.docker/", "~/.config/gcloud/", "~/.config/rclone/",
    "/etc/sudoers.d/",
)

READ_DENY_EXACT_FILES: tuple[str, ...] = (
    "~/.netrc", "~/.pgpass", "~/.git-credentials",
    "/etc/shadow", "/etc/sudoers", "/etc/gshadow",
)

READ_DENY_FILENAME_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r'^\.env$'),
    re.compile(r'^\.envrc$'),
    re.compile(r'^\.env\.'),
    re.compile(r'^id_(?:rsa|ed25519|ecdsa|dsa)\b'),
    re.compile(r'\.pem$'),
    re.compile(r'\.key$'),
    re.compile(r'^environ$'),
)

# python 内容黑名单
PYTHON_DENY_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in [
    r'\bos\.system\b', r'\bos\.popen\b', r'\bos\.exec\w*\b',
    r'\bos\.spawn\w*\b', r'\bos\.fork\b',
    r'\bsubprocess\b', r'\bcommands\.',
    r'\b__import__\b', r'\bimportlib\b',
    r'\beval\s*\(', r'\bexec\s*\(', r'\bcompile\s*\(',
    r'\bos\.environ\b', r'\bos\.getenv\b',
    r'\bfrom\s+os\s+import\b',
    r'\bsocket\b', r'\burllib\b', r'\brequests\b', r'\bhttp\.client\b',
    r'\bhttpx\b', r'\baiohttp\b', r'\btornado\b',
    r'\bparamiko\b', r'\btelnetlib\b', r'\bsmtplib\b', r'\bftplib\b',
    r'\bxmlrpc\b',
    r'\bpty\b', r'\bctypes\b', r'\bcffi\b',
])

# 标准 device sink，写到这里无副作用，放行
DEVICE_PATHS_OK: frozenset[str] = frozenset({
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero", "/dev/tty",
    "/dev/random", "/dev/urandom",
})

# ==================== 内部正则 ====================

_CONNECTORS: frozenset[str] = frozenset({";", "&&", "||", "|", "&"})
_VAR_REF_RE = re.compile(r'\$[A-Za-z_]|\$\{')
_CMD_SUBST_RE = re.compile(r'\$\((?!\()([^)]*)\)|`([^`]*)`')
_REDIRECT_RE = re.compile(r'^([12&]?>>?)(.*)$')
_ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_HEREDOC_RE = re.compile(
    r'<<-?\s*[\'"]?(\w+)[\'"]?[^\n]*\n.*?\n\1\s*(?=\n|$)',
    re.DOTALL,
)


# ==================== 公共 API ====================

def check_command(cmd: str, cwd: Path) -> str | None:
    """返回 reason 表示拒绝；None 表示放行。env SOLVER_BASH_GUARD=off 整体放行。"""
    if os.environ.get("SOLVER_BASH_GUARD", "on").lower() == "off":
        return None

    for m in _CMD_SUBST_RE.finditer(cmd):
        inner = (m.group(1) or m.group(2) or '').strip()
        if inner:
            inner_reason = check_command(inner, cwd)
            if inner_reason is not None:
                return f"命令替换内容被拒（{inner!r}）：{inner_reason}"

    cmd_stripped = _CMD_SUBST_RE.sub("CMDSUBST_PLACEHOLDER", cmd)
    cmd_stripped = _HEREDOC_RE.sub('', cmd_stripped)

    try:
        toks = _tokenize(cmd_stripped)
    except ValueError as e:
        return f"命令解析失败：{e}"

    # 在 _split_segments 把绝对路径剥成 basename 前，先扫一遍 toks：
    # 任何 `/.../pw.x` 之类的 token（含 `/` 且 basename 是 QE binary）一律拒。
    # bench 的 PATH 已经暴露 $QE_GPU_WRAPPER_DIR/pw.x 同名命令；走绝对路径
    # 只会引到 CPU build（10× slowdown），WO3 2026-05-19 烧掉 2 小时即此。
    for t in toks:
        if "/" not in t:
            continue
        base = t.rsplit("/", 1)[-1]
        if base in QE_EXECUTABLES:
            return (
                f"QE 可执行文件禁用绝对路径：{t}。"
                f"请用 bare 命令名（如 `pw.x`）以走 PATH 上的 GPU wrapper。"
            )

    for prog, args, redirects in _split_segments(toks):
        if prog is None:
            continue
        if prog in DENIED_PROGRAMS:
            return f"程序在显式拒绝列表：{prog}"
        if prog not in ALLOWED_PROGRAMS:
            return f"程序不在白名单：{prog}"
        if prog in WRITE_PROGRAMS:
            r = _check_write_paths(prog, args, cwd)
            if r is not None:
                return r
        for tgt in redirects:
            if tgt in DEVICE_PATHS_OK:
                continue
            r = _check_path_in_workdir(tgt, cwd, "重定向写")
            if r is not None:
                return r
        for a in args:
            r = _check_read_path_deny(a)
            if r is not None:
                return r
        if prog in ("python", "python3"):
            r = _check_python_content(cmd)
            if r is not None:
                return r

    return None


def _tokenize(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";|&")
    lex.whitespace_split = True
    toks = list(lex)
    return _merge_fd_redirects(toks)


_FD_DIGIT_RE = re.compile(r'^\d+$')


def _merge_fd_redirects(toks: list[str]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if (_REDIRECT_RE.match(t)
                and i + 2 < n
                and toks[i + 1] == "&"
                and _FD_DIGIT_RE.match(toks[i + 2])):
            out.append(t + "&" + toks[i + 2])
            i += 3
            continue
        out.append(t)
        i += 1
    return out


# ==================== 内部 ====================

def _split_segments(
    toks: list[str],
) -> list[tuple[str | None, list[str], list[str]]]:
    segs: list[tuple[str | None, list[str], list[str]]] = []
    cur_prog: str | None = None
    cur_args: list[str] = []
    cur_redirects: list[str] = []
    expect_program = True

    def flush() -> None:
        nonlocal cur_prog, cur_args, cur_redirects, expect_program
        segs.append((cur_prog, cur_args, cur_redirects))
        cur_prog, cur_args, cur_redirects = None, [], []
        expect_program = True

    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t in _CONNECTORS:
            flush()
            i += 1
            continue
        m = _REDIRECT_RE.match(t)
        if m and m.group(1):
            rest = m.group(2)
            if rest == "":
                if i + 1 < n and toks[i + 1] not in _CONNECTORS:
                    cur_redirects.append(toks[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if rest.startswith("&"):
                i += 1
                continue
            cur_redirects.append(rest)
            i += 1
            continue
        if expect_program:
            if _ENV_ASSIGN_RE.match(t):
                i += 1
                continue
            cur_prog = t.split("/")[-1]
            expect_program = False
            i += 1
            continue
        cur_args.append(t)
        i += 1
    flush()
    return segs


def _check_path_in_workdir(
    path_str: str, cwd: Path, reason_prefix: str,
) -> str | None:
    if _VAR_REF_RE.search(path_str):
        return f"{reason_prefix} 路径含未展开变量（要求字面）：{path_str}"
    expanded = os.path.expanduser(path_str)
    p = Path(expanded)
    if not p.is_absolute():
        p = cwd / p
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError):
        return f"{reason_prefix} 路径无法解析：{path_str}"
    cwd_resolved = cwd.resolve()
    try:
        resolved.relative_to(cwd_resolved)
    except ValueError:
        return f"{reason_prefix} 路径不在工作目录：{path_str} → {resolved}"
    return None


def _check_write_paths(prog: str, args: list[str], cwd: Path) -> str | None:
    targets = _extract_write_targets(prog, args)
    for t in targets:
        r = _check_path_in_workdir(t, cwd, f"{prog} 写")
        if r is not None:
            return r
    return None


def _extract_write_targets(prog: str, args: list[str]) -> list[str]:
    non_flag = [a for a in args if not a.startswith("-")]
    if prog in ("mkdir", "touch", "rm", "tee"):
        return non_flag
    if prog in ("cp", "mv"):
        return non_flag[-1:] if non_flag else []
    if prog == "ln":
        return non_flag[-1:] if non_flag else []
    return []


def _check_read_path_deny(path_str: str) -> str | None:
    if not path_str:
        return None
    if _VAR_REF_RE.search(path_str):
        return None
    if path_str.startswith("-"):
        return None
    expanded = os.path.expanduser(path_str)
    basename = os.path.basename(expanded.rstrip("/"))
    for pat in READ_DENY_FILENAME_PATTERNS:
        if pat.search(basename):
            return f"敏感文件被拒（文件名匹配）：{path_str}"
    for f in READ_DENY_EXACT_FILES:
        if expanded == os.path.expanduser(f):
            return f"敏感文件被拒：{path_str}"
    for d in READ_DENY_DIR_PREFIXES:
        d_expanded = os.path.expanduser(d)
        if expanded == d_expanded.rstrip("/"):
            return f"敏感目录被拒：{path_str}"
        if expanded.startswith(d_expanded):
            return f"敏感路径被拒：{path_str}"
    return None


def _check_python_content(cmd: str) -> str | None:
    for pat in PYTHON_DENY_PATTERNS:
        m = pat.search(cmd)
        if m:
            return f"python 内容含危险模式：{m.group(0)}"
    return None
