"""Extract error signals from streaming process output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ErrorSignal:
    """A single error event extracted from log output."""

    raw_line: str
    source: str  # "stdout" | "stderr"
    error_type: str = "unknown"
    context_lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return self.raw_line[:200]


# ── Error indicator patterns ──────────────────────────────────────────────────

_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Dependency errors
    # File-path form first (absolute or ./ relative) → wrong entry point, not a missing package
    (re.compile(r"Cannot find module ['\"]([./][^'\"]*)['\"]", re.I), "wrong_entry_point"),
    # Bare module name → missing npm / pip package
    (re.compile(r"Cannot find module ['\"]([a-zA-Z@][^'\"]*)['\"]", re.I), "missing_dependency"),
    (re.compile(r"Cannot find package ['\"]([a-zA-Z@][^'\"]*)['\"]", re.I), "missing_dependency"),
    (re.compile(r"Module not found: Error: Can't resolve ['\"](.+?)['\"]", re.I), "missing_dependency"),
    (re.compile(r"No module named ['\"]?(.+?)['\"]?$", re.I), "missing_dependency"),
    (re.compile(r"ModuleNotFoundError", re.I), "missing_dependency"),
    (re.compile(r"ImportError: cannot import name", re.I), "missing_dependency"),
    (re.compile(r"Could not find a version that satisfies the requirement (\S+)", re.I), "missing_dependency"),
    (re.compile(r"npm ERR! 404 Not Found.*'(.+?)'", re.I), "missing_dependency"),
    (re.compile(r"error: package `(.+?)` not found", re.I), "missing_dependency"),

    # Port conflict
    (re.compile(r"EADDRINUSE", re.I), "port_conflict"),
    (re.compile(r"address already in use", re.I), "port_conflict"),
    (re.compile(r"bind: address already in use", re.I), "port_conflict"),
    (re.compile(r"listen tcp.*: bind: address already in use", re.I), "port_conflict"),

    # Missing env var
    (re.compile(r"KeyError:\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"), "missing_env_var"),
    (re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)\s+is\s+(undefined|not defined)", re.I), "missing_env_var"),
    (re.compile(r"Missing required env(?:ironment)? var(?:iable)?[s:]?\s*['\"]?([A-Z_][A-Z0-9_]*)", re.I), "missing_env_var"),
    (re.compile(r"Environment variable ['\"]([A-Z_][A-Z0-9_]*)['\"] is not set", re.I), "missing_env_var"),

    # Build failures
    (re.compile(r"Build failed", re.I), "build_failure"),
    (re.compile(r"SyntaxError:", re.I), "build_failure"),
    (re.compile(r"error TS\d+:", re.I), "build_failure"),
    (re.compile(r"Failed to compile", re.I), "build_failure"),
    (re.compile(r"Compilation failed", re.I), "build_failure"),
    (re.compile(r"error\[E\d+\]:", re.I), "build_failure"),  # Rust
    (re.compile(r"\[ERROR\] BUILD FAILURE", re.I), "build_failure"),  # Maven

    # Version mismatch
    (re.compile(r"engines.*node.*required.*but", re.I), "version_mismatch"),
    (re.compile(r"requires node(?:\.js)?[. ]?(?:version)?\s*([\d.]+(?:\+)?)", re.I), "version_mismatch"),
    # packageManager / Corepack (Node official; CI + local yarn/pnpm shims)
    (
        re.compile(
            r"corepack must be enabled|please run corepack enable|run [`']corepack enable|"
            r"enable corepack|corepack is disabled|could not find.*corepack|"
            r"packageManager field.*corepack|prepare corepack|"
            r"To use .{2,20}, first enable corepack|Internal Error.*corepack",
            re.I,
        ),
        "corepack_required",
    ),
    # Wrong CLI vs lockfile (npm install in a yarn/pnpm repo)
    (
        re.compile(
            r"Usage Error:.*yarn\.lock|Usage Error:.*pnpm-lock\.yaml|"
            r"The project contains (?:a )?yarn\.lock|The project contains (?:a )?pnpm-lock\.yaml|"
            r"Please use (?:pnpm|yarn) (?:to install|to run)|"
            r"ERR_PNPM_UNSUPPORTED_PM|package manager.*mismatch",
            re.I,
        ),
        "package_manager_wrong",
    ),
    # engines / strict — package.json "engines" rejected (often fixable without changing Node)
    (
        re.compile(
            r"ERR_PNPM_UNSUPPORTED_ENGINE|"
            r"\bUnsupported engine\b|engine \"node\" is incompatible|"
            r"The engine \"node\" is incompatible|engine \[node\]|"
            r"npm ERR! code EBADENGINE|npm error code EBADENGINE|npm warn.*EBADENGINE|"
            r"\bEBADENGINE\b|"
            r"Unsupported package\.json ['\"]engines['\"]",
            re.I,
        ),
        "engines_strict",
    ),
    (re.compile(r"python_requires", re.I), "version_mismatch"),
    (re.compile(r"This package requires Python", re.I), "version_mismatch"),
    # uv / pip resolver Python version constraint errors
    (re.compile(r"current Python version.{0,60}does not satisfy Python", re.I), "version_mismatch"),
    (re.compile(r"does not satisfy Python\s*[><=!]+\s*[\d.]+", re.I), "version_mismatch"),
    (re.compile(r"requires Python\s*[><=!]+\s*[\d.]+", re.I), "version_mismatch"),

    # Permission errors
    (re.compile(r"EACCES", re.I), "permission_error"),
    (re.compile(r"Permission denied", re.I), "permission_error"),
    (re.compile(r"Operation not permitted", re.I), "permission_error"),

    # Missing config
    (re.compile(r"Config file .+ not found", re.I), "missing_config"),
    (re.compile(r"Could not find .+\.config\.(js|ts|json)", re.I), "missing_config"),
    (re.compile(r"No such file or directory.*\.env", re.I), "missing_config"),

    # Docker bind mount: host created a directory where the app opens a file (e.g. ov.conf)
    (re.compile(
        r"IsADirectoryError:\s*(?:\[Errno 21\]\s*)?Is a directory:\s*['\"]([^'\"]+)['\"]",
        re.I,
    ), "bind_mount_is_directory"),

    # CLI tool invoked without required subcommand (Click / Typer / argparse / picocli)
    (re.compile(r"^Missing command\.", re.I), "cli_no_subcommand"),
    (re.compile(r"\[OPTIONS\]\s+COMMAND\s+\[ARGS\]", re.I), "cli_no_subcommand"),
    (re.compile(r"error: the following arguments are required:.*subcommand", re.I), "cli_no_subcommand"),
    # picocli: "Missing required subcommand"
    (re.compile(r"^Missing required subcommand", re.I | re.MULTILINE), "cli_no_subcommand"),
    # Usage line that lists [COMMAND] as the last positional — subcommand-style CLI
    (re.compile(r"^usage:\s+\S.*\[COMMAND\]\s*$", re.I | re.MULTILINE), "cli_no_subcommand"),

    # CLI tool that needs positional arguments / files (printed usage + options, then exited)
    # Matches Apache Commons CLI, picocli, standard Unix getopt, etc.
    # Note: only fires when the usage line does NOT indicate a subcommand-style CLI (checked above).
    (re.compile(r"^usage:\s+\S", re.I), "cli_needs_args"),

    # SSL / TLS certificate errors
    (re.compile(r"SSL(?:Error)?.*CERTIFICATE_VERIFY_FAILED", re.I), "ssl_error"),
    (re.compile(r"certificate verify failed", re.I), "ssl_error"),
    (re.compile(r"SSL handshake failed", re.I), "ssl_error"),
    (re.compile(r"unable to get local issuer certificate", re.I), "ssl_error"),
    (re.compile(r"self[- ]signed certificate", re.I), "ssl_error"),
    (re.compile(r"DEPTH_ZERO_SELF_SIGNED_CERT", re.I), "ssl_error"),
    (re.compile(r"UNABLE_TO_VERIFY_LEAF_SIGNATURE", re.I), "ssl_error"),
    (re.compile(r"npm ERR! code SELF_SIGNED_CERT_IN_CHAIN", re.I), "ssl_error"),

    # Node.js 17+ / OpenSSL 3 — legacy webpack/crypto (very common on SO / old React apps)
    (re.compile(r"ERR_OSSL_EVP_UNSUPPORTED", re.I), "node_openssl_legacy"),
    (re.compile(r"digital envelope routines::unsupported", re.I), "node_openssl_legacy"),
    (re.compile(r"digital envelope routines::initialization error", re.I), "node_openssl_legacy"),
    (re.compile(r"error:0308010C:digital envelope routines", re.I), "node_openssl_legacy"),
    (re.compile(r"error:03000086:digital envelope routines", re.I), "node_openssl_legacy"),
    (re.compile(r"opensslErrorStack:.*digital envelope", re.I), "node_openssl_legacy"),

    # Memory limit / out-of-memory
    (re.compile(r"FATAL ERROR:.*Allocation failed.*JavaScript heap out of memory", re.I), "memory_limit"),
    (re.compile(r"JavaScript heap out of memory", re.I), "memory_limit"),
    (re.compile(r"\bENOMEM\b", re.I), "memory_limit"),
    (re.compile(r"not enough memory", re.I), "memory_limit"),
    (re.compile(r"^MemoryError$", re.I), "memory_limit"),
    (re.compile(r"java\.lang\.OutOfMemoryError", re.I), "memory_limit"),
    (re.compile(r"Cannot allocate memory", re.I), "memory_limit"),
    (re.compile(r"GC overhead limit exceeded", re.I), "memory_limit"),

    # Disk space / inotify watcher limit
    (re.compile(r"\bENOSPC\b", re.I), "disk_space"),
    (re.compile(r"No space left on device", re.I), "disk_space"),
    (re.compile(r"ENOSPC.*inotify|inotify.*limit|max_user_watches", re.I), "disk_space"),
    (re.compile(r"System limit for number of file watchers reached", re.I), "disk_space"),
    (re.compile(r"enospc.*too many open files", re.I), "disk_space"),

    # Network / connection errors
    (re.compile(r"\bECONNREFUSED\b", re.I), "network_error"),
    (re.compile(r"\bECONNRESET\b", re.I), "network_error"),
    (re.compile(r"\bETIMEDOUT\b|\bESOCKETTIMEDOUT\b", re.I), "network_error"),
    (re.compile(r"connection timed? out", re.I), "network_error"),
    (re.compile(r"\bENOTFOUND\b|getaddrinfo.*ENOTFOUND", re.I), "network_error"),
    (re.compile(r"getaddrinfo.*failed|name or service not known", re.I), "network_error"),
    (re.compile(r"dial tcp.*connection refused", re.I), "network_error"),  # Go
    (re.compile(r"requests\.exceptions\.(ConnectionError|Timeout)", re.I), "network_error"),  # Python
    (re.compile(r"net::ERR_CONNECTION_REFUSED", re.I), "network_error"),

    # GPU / CUDA — trending Python ML (PyTorch, LangChain tools, NVIDIA Warp) and CUDA extensions
    (
        re.compile(
            r"No CUDA GPUs are available|CUDA driver version is insufficient|CUDA runtime|CUDA error \d+|"
            r"CUDA unknown error|libcudart\.so|libcuda\.so.*not found|"
            r"NVIDIA-SMI has failed|nvidia-smi: command not found|Found no NVIDIA driver|"
            r"Torch not compiled with CUDA|not compiled with CUDA enabled|AssertionError:.*CUDA|"
            r"CUDA must be installed|requires CUDA|cuDNN|could not load.*cudnn|"
            r"\bwarp.*CUDA",
            re.I,
        ),
        "gpu_cuda_runtime",
    ),

    # Database connection / auth errors
    (re.compile(r"password authentication failed for user", re.I), "database_error"),
    (re.compile(r'(?:database|db|catalog)\s+"?[\w-]+"?\s+does not exist', re.I), "database_error"),
    (re.compile(r'FATAL:\s*role "[\w-]+" does not exist', re.I), "database_error"),
    (re.compile(r"Access denied for user.*MySQL", re.I), "database_error"),
    (re.compile(r"MongoServerError|MongoNetworkError|MongooseServerSelectionError", re.I), "database_error"),
    (re.compile(r"MongoServerSelectionError.*ECONNREFUSED", re.I), "database_error"),
    (re.compile(r"SequelizeConnectionError|SequelizeConnectionRefusedError", re.I), "database_error"),
    (
        re.compile(
            r"PrismaClientInitializationError|PrismaClientKnownRequestError|"
            r"prisma.*Can't reach database server|Can't reach database server|"
            r"\berror:\s*P10\d{2}\b|Error \(P10\d{2}\)|Invalid [`']?prisma",
            re.I,
        ),
        "database_error",
    ),
    (re.compile(r"django\.db\.OperationalError.*could not connect", re.I), "database_error"),
    (re.compile(r"sqlalchemy.*OperationalError.*could not connect", re.I), "database_error"),

    # npm peer dependency conflicts
    (re.compile(r"npm ERR!.*peer dep.*missing", re.I), "peer_dependency"),
    (re.compile(r"Could not resolve dependency.*peer", re.I), "peer_dependency"),
    (re.compile(r"\bERESOLVE\b.*could not resolve", re.I), "peer_dependency"),
    (re.compile(r"Conflicting peer dependency", re.I), "peer_dependency"),
    (re.compile(r"peer dep.*conflict|conflict.*peer dep", re.I), "peer_dependency"),
    (re.compile(r"npm ERR! ERESOLVE", re.I), "peer_dependency"),

    # Ruby Bundler version mismatch
    (re.compile(r"You must use Bundler \d+ or greater", re.I), "bundler_version"),
    (re.compile(r"the running version of Bundler.*older than the version that created the lockfile", re.I), "bundler_version"),
    (re.compile(r"Your Gemfile\.lock was generated with Bundler", re.I), "bundler_version"),
    (re.compile(r"bundler.*requires.*bundler.*version", re.I), "bundler_version"),

    # glibc / libstdc++ / manylinux — binary wheels built for newer distros (SO + GitHub Issues)
    (
        re.compile(
            r"version `?GLIBC_|GLIBC_\d+\.\d+.*not found|/lib(?:64)?/.*libc\.so\.6.*GLIBC_|"
            r"GLIBCXX_\d+\.\d+.*not found|libstdc\+\+\.so\.[0-9]+: version|"
            r"manylinux_\d+_\d+.*not (?:a supported|compatible)|"
            r"not a supported wheel on this platform|"
            r"No matching distribution found for .*manylinux",
            re.I,
        ),
        "glibc_toolchain",
    ),

    # Missing system / native library dependencies
    (re.compile(r"Package .* was not found in the pkg-config search path", re.I), "system_dependency"),
    (re.compile(r"cannot find -l(\w[\w\-]*)", re.I), "system_dependency"),
    (re.compile(r"fatal error: [\w/]+\.h: No such file or directory", re.I), "system_dependency"),
    (re.compile(r"pkg-config.*not found|pkg-config.*No such file", re.I), "system_dependency"),
    (re.compile(r"error: library not found for -l", re.I), "system_dependency"),
    (re.compile(r"checking for [\w_]+\.h\.\.\. no$", re.I), "system_dependency"),
    (re.compile(r"libpq-fe\.h.*not found|libssl.*not found", re.I), "system_dependency"),

    # C/C++ compiler not found or failed
    (re.compile(r"error: command ['\"]?(gcc|cc|g\+\+|clang)['\"]? (?:failed|not found)", re.I), "compiler_error"),
    (re.compile(r"gcc: command not found|g\+\+: command not found|cc: not found", re.I), "compiler_error"),
    (re.compile(r"clang: error: linker command failed", re.I), "compiler_error"),
    (re.compile(r"No such file or directory.*cc1plus|cc1: error", re.I), "compiler_error"),
    (re.compile(r"error: Microsoft Visual C\+\+ \d+ is required", re.I), "compiler_error"),

    # Lock file conflict / out of sync
    (re.compile(r"yarn\.lock.*conflict|conflict.*yarn\.lock", re.I), "lock_file_conflict"),
    (re.compile(r"npm ERR!.*package-lock\.json.*conflict", re.I), "lock_file_conflict"),
    (re.compile(r"Your lock file needs to be updated", re.I), "lock_file_conflict"),
    (re.compile(r"LOCKFILE.*out of date|out of date.*LOCKFILE", re.I), "lock_file_conflict"),
    (re.compile(r"Integrity check failed|integrity mismatch", re.I), "lock_file_conflict"),
    (re.compile(r"ERR_INVALID_THIS.*lockfile|lock file.*invalid", re.I), "lock_file_conflict"),
    # Poetry / uv — lockfile drift (GitHub Actions + local churn)
    (
        re.compile(
            r"pyproject\.toml changed significantly.*poetry\.lock|poetry\.lock.*not compatible|"
            r"poetry\.lock.*out of sync|Run [`']?poetry lock|lock file.*pyproject\.toml.*poetry",
            re.I,
        ),
        "lock_file_conflict",
    ),
    (
        re.compile(
            r"lockfile at [`\"'].*uv\.lock[`\"'] needs to be updated|--locked was provided.*uv lock|"
            r"run [`']?uv lock[`']?\s*$|uv\.lock.*out of sync",
            re.I,
        ),
        "lock_file_conflict",
    ),

    # pip dependency resolver dead-end (PEP 517 / conflicting pins)
    (
        re.compile(
            r"ResolutionImpossible|pip.*dependencies do not satisfy|"
            r"these package versions have conflicting dependencies|"
            r"Cannot install .{0,200}conflicting dependencies",
            re.I,
        ),
        "pip_resolution",
    ),

    # pip metadata-generation-failed
    (re.compile(r"ERROR: metadata-generation-failed", re.I), "metadata_generation"),
    (re.compile(r"error in.*setup command.*egg-info", re.I), "metadata_generation"),
    (re.compile(r"pip.*error.*Failed to build.*\(setup\.py\)", re.I), "metadata_generation"),
    (re.compile(r"subprocess-exited-with-error.*get_requires_for_build_wheel", re.I), "metadata_generation"),

    # npm lifecycle script failure where a script command itself is missing
    # (classic chicken-and-egg, e.g. "husky: not found" during prepare).
    # Must come before the generic missing_tool patterns.
    (re.compile(r"^sh:\s*\d*:?\s*(\S+):\s+not found$", re.I), "npm_lifecycle_failure"),

    # node-gyp / native addon compilation
    (re.compile(r"node-gyp.*failed|gyp ERR! build error", re.I), "node_gyp"),
    (re.compile(r"^gyp ERR!", re.I), "node_gyp"),
    (re.compile(r"prebuild-install.*ERR|node-pre-gyp.*error", re.I), "node_gyp"),
    (re.compile(r"bindings\.gyp.*not found|could not load the bindings file", re.I), "node_gyp"),

    # Java version / JAVA_HOME issues
    (re.compile(r"Unsupported class file major version \d+", re.I), "java_version"),
    (re.compile(r"class file has wrong version \d+", re.I), "java_version"),
    (re.compile(r"JAVA_HOME.*not set|JAVA_HOME.*invalid directory|JAVA_HOME.*is not defined", re.I), "java_version"),
    (re.compile(r"Unable to locate a Java Runtime|Could not find.*java.*JDK|JDK.*not found", re.I), "java_version"),
    (re.compile(r"error: invalid target release: \d+", re.I), "java_version"),

    # Gradle / Maven build-tool errors
    (re.compile(r"org\.gradle\.api\.GradleException", re.I), "gradle_error"),
    (re.compile(r"Could not resolve.*gradle|gradle.*could not resolve", re.I), "gradle_error"),
    (re.compile(r"\[ERROR\] Failed to execute goal.*maven-compiler-plugin", re.I), "gradle_error"),
    (re.compile(r"Could not determine java version from", re.I), "gradle_error"),
    (re.compile(r"Gradle.*build.*failed|BUILD FAILED in \d", re.I), "gradle_error"),
    (re.compile(r"org\.gradle\.internal\.jvm\.JavaHomeException", re.I), "gradle_error"),

    # Docker daemon / network errors
    (re.compile(r"Cannot connect to the Docker daemon", re.I), "docker_error"),
    (re.compile(r"Is the docker daemon running\?", re.I), "docker_error"),
    (re.compile(r"docker: Error response from daemon", re.I), "docker_error"),
    (re.compile(r"Pool overlaps with other one on this address space", re.I), "docker_error"),
    (re.compile(r"network.*already exists.*docker|failed to create network", re.I), "docker_error"),
    (re.compile(r"Error response from daemon: pull access denied", re.I), "docker_error"),
    (re.compile(r"docker.*service.*not found|Cannot start service.*container", re.I), "docker_error"),
    # Docker BuildKit / compose build (trending repos ship Dockerfiles)
    (
        re.compile(
            r"ERROR: failed to solve|failed to solve:.*executor|running.*did not complete successfully|"
            r"executor failed running",
            re.I,
        ),
        "docker_error",
    ),

    # Git LFS — large model weights, PDFs, binaries in hot ML/design repos
    (
        re.compile(
            r"\bgit-lfs\b|Git LFS|git: 'lfs' is not a git command|smudge filter lfs failed|"
            r"LFS (?:fetch|pull|clone) (?:failed|error)|batch response:.*lfs|"
            r"is a Git LFS pointer|pointer file.*LFS",
            re.I,
        ),
        "git_lfs_error",
    ),

    # Playwright / Puppeteer — E2E and scraping in TS/JS trending projects
    (
        re.compile(
            r"Executable doesn't exist|browserType\.launch:|playwright\.exe.*not found|"
            r"Please run the following command to download new browsers:|"
            r"npx playwright install|"
            r"browserType\.executablePath|Failed to launch (?:chromium|chrome|firefox)|"
            r"Could not find Chrome|Chromium revision|chrome revision is not downloaded|"
            r"ENOENT.*(?:chromium|playwright)",
            re.I,
        ),
        "playwright_browsers",
    ),

    # Git submodule not initialized
    (re.compile(r"fatal: No url found for submodule", re.I), "git_submodule"),
    (re.compile(r"Submodule .* registered but no checkout", re.I), "git_submodule"),
    (re.compile(r"No such file or directory.*\.gitmodules", re.I), "git_submodule"),
    (re.compile(r"error: Server does not allow request for unadvertised object.*submodule", re.I), "git_submodule"),
    (re.compile(r"Please make sure you have the correct access rights.*submodule", re.I), "git_submodule"),

    # Git remote / auth (private repo, bad URL, expired token — common GitHub + SO threads)
    (re.compile(r"remote:\s*Repository not found", re.I), "git_remote_auth"),
    (re.compile(r"fatal:\s+repository ['\"](?:https?://|git@)[^'\"]+['\"] not found", re.I), "git_remote_auth"),
    (re.compile(r"fatal:\s+Could not read from remote repository", re.I), "git_remote_auth"),
    (re.compile(r"fatal:\s+Authentication failed for ['\"]https?://", re.I), "git_remote_auth"),
    (re.compile(r"Support for password authentication was removed", re.I), "git_remote_auth"),

    # Rust linker / OpenSSL errors
    (re.compile(r"error: linking with `cc` failed", re.I), "rust_linker"),
    (re.compile(r"ld(?:\.lld)? returned \d+ exit status", re.I), "rust_linker"),
    (re.compile(r"undefined reference to.*(?:ERR_get_error|SSL_|OPENSSL_)", re.I), "rust_linker"),
    (re.compile(r"could not find native static library `ssl`", re.I), "rust_linker"),
    (re.compile(r"the following required packages cannot be found:.*openssl", re.I), "rust_linker"),
    (re.compile(r"pkg-config.*openssl.*not found|openssl.*pkg-config.*not found", re.I), "rust_linker"),

    # Ruby native gem build errors
    (re.compile(r"An error occurred while installing .+ and Bundler cannot continue", re.I), "ruby_gem_error"),
    (re.compile(r"mkmf\.rb can't find header files for ruby", re.I), "ruby_gem_error"),
    (re.compile(r"Gem::Ext::BuildError: ERROR, Failed to build gem native extension", re.I), "ruby_gem_error"),
    (re.compile(r"ERROR.*Failed to build gem native extension", re.I), "ruby_gem_error"),
    (re.compile(r"make.*Error \d+.*extconf\.rb", re.I), "ruby_gem_error"),

    # go.mod: invalid "go" directive (e.g. go 1.22.0 — patch-style rejected; fix file not apt install go)
    (re.compile(r"\.go\.mod:\d+:\s*invalid go version", re.I), "go_mod_bad_version"),

    # Language-specific compiler/toolchain not found (must come before generic patterns)
    (re.compile(r"\bGo compiler not found\b", re.I), "missing_tool"),
    (re.compile(r"\bgo(?:lang)? (?:compiler|toolchain|binary) not found\b", re.I), "missing_tool"),
    (re.compile(r"\bRust(?:c)? (?:compiler|toolchain) not found\b", re.I), "missing_tool"),
    (re.compile(r"\bC(?:\+\+)? compiler not found\b", re.I), "missing_tool"),

    # Missing CLI tool — command not found by make/shell (e.g. "make: uv: No such file or directory")
    (re.compile(r"^make:\s+(\S+): No such file or directory"), "missing_tool"),
    (re.compile(r"bash: (\S+): command not found"), "missing_tool"),
    (re.compile(r"zsh: command not found: (\S+)"), "missing_tool"),
    (re.compile(r"sh: \d*:? ?(\S+): (?:not found|No such file or directory)"), "missing_tool"),
    # Debian/Ubuntu dash/bash: /bin/sh: 1: pnpm: not found
    (
        re.compile(
            r"(?:/bin/)?sh:\s*\d+:\s*(\S+):\s*(?:not found|No such file or directory)"
        ),
        "missing_tool",
    ),
    (re.compile(r"(\S+): command not found"), "missing_tool"),
    # Script-level "Error: <tool> not found" (e.g. bash scripts that do their own checks)
    (re.compile(r"Error:\s+(\w[\w.-]*)\s+not found", re.I), "missing_tool"),
    (re.compile(r"(\w[\w.-]+) (?:is )?not found(?: in PATH)?", re.I), "missing_tool"),

    # Generic fatal errors (lowest priority)
    (re.compile(r"^error:", re.I), "generic_error"),
    (re.compile(r"^fatal:", re.I), "generic_error"),
    (re.compile(r"uncaughtException", re.I), "generic_error"),
    (re.compile(r"Traceback \(most recent call last\)", re.I), "generic_error"),
    (re.compile(r"panic:", re.I), "generic_error"),  # Go
]

_NOISE_PATTERNS = [
    re.compile(r"DeprecationWarning", re.I),
    re.compile(r"warn\s+-", re.I),
    re.compile(r"^npm warn\b", re.I),       # npm warnings (EBADENGINE, deprecated, etc.)
    re.compile(r"^\s*at "),  # JS stack trace lines
    re.compile(r"^\s*\^"),   # pointer lines
]


def detect_errors(lines: list[tuple[str, str]]) -> list[ErrorSignal]:
    """
    Scan a list of (source, line) log entries and extract error signals.
    Returns a deduplicated list sorted by priority.
    """
    signals: list[ErrorSignal] = []
    seen_types: set[str] = set()

    all_texts = [line for _, line in lines]

    for idx, (source, line) in enumerate(lines):
        if _is_noise(line):
            continue

        for pattern, error_type in _ERROR_PATTERNS:
            if pattern.search(line):
                context = all_texts[max(0, idx - 2): idx + 3]
                signal = ErrorSignal(
                    raw_line=line,
                    source=source,
                    error_type=error_type,
                    context_lines=context,
                )
                signals.append(signal)
                break

    # Deduplicate: keep first occurrence of each error_type, but allow
    # multiple signals for types where different instances need separate fixes.
    _MULTI_SIGNAL_TYPES = {"missing_dependency", "system_dependency", "ruby_gem_error", "missing_tool"}
    deduplicated: list[ErrorSignal] = []
    seen: dict[str, int] = {}
    for sig in signals:
        count = seen.get(sig.error_type, 0)
        if sig.error_type in _MULTI_SIGNAL_TYPES or count == 0:
            deduplicated.append(sig)
            seen[sig.error_type] = count + 1

    return deduplicated


def is_fatal_exit(exit_code: int, signals: list[ErrorSignal]) -> bool:
    """Determine whether a process exit represents a real failure."""
    if exit_code == 0:
        return False
    if exit_code in (130, 143):  # SIGINT, SIGTERM — user killed it
        return False
    return True


def _is_noise(line: str) -> bool:
    if "EBADENGINE" in line or "ERR_PNPM_UNSUPPORTED_ENGINE" in line:
        return False
    return any(p.search(line) for p in _NOISE_PATTERNS)


# ── CLI usage/help parser ──────────────────────────────────────────────────────

_USAGE_LINE_RE = re.compile(r"^usage:\s+(.+)", re.I)
_OPTION_LINE_RE = re.compile(r"^\s{1,8}(-\w[\w,]*|--[\w][\w-]*)[\s,]")


def parse_usage_help(full_output: str) -> dict:
    """Extract the usage synopsis and a preview of available options from CLI help text.

    Returns a dict with:
      - ``usage_synopsis``: the positional-arg pattern from the ``usage:`` line (e.g. ``<FILE>...``)
      - ``options_preview``: up to 12 option lines from the help text
    """
    usage_synopsis = ""
    options: list[str] = []

    for line in full_output.splitlines():
        if not usage_synopsis:
            m = _USAGE_LINE_RE.match(line)
            if m:
                usage_synopsis = m.group(1).strip()
        else:
            if _OPTION_LINE_RE.match(line):
                stripped = line.strip()
                if stripped:
                    options.append(stripped[:80])
                    if len(options) >= 12:
                        break

    return {"usage_synopsis": usage_synopsis, "options_preview": options}
