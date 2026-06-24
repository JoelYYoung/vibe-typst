# Vibe Typst — workspace image (one per user). Build with rootless Podman on amd64 Linux.
# Pre-build these artifacts BEFORE running podman build (they are COPY'd in, not built here):
#
# Resolver — build on the target host to match CPU/toolchain:
#   cd resolver
#   RUSTFLAGS='-C target-cpu=x86-64' cargo build --release
#   # binary → resolver/target/release/tcb-resolver
#
# Frontend:
#   cd frontend && npm ci && npm run build
#   # output → frontend/dist/
#
# Python venv (must match container OS: Debian bookworm, Python 3.11):
#   cd backend && uv sync
#   tar czf .venv.tar.gz .venv
#   # re-run after any changes to pyproject.toml
#
# Then:  podman build -t tcb-workspace:latest .

# ---- single runtime stage -----------------------------------------------------------------------
FROM docker.io/library/debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
# Rootless Podman with single-uid mapping: APT's _apt sandbox user (uid 42) and
# nobody (gid 65534) don't exist in the mapped namespace — disable the sandbox.
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/50podman-rootless.conf && \
    apt-get update && apt-get install -y --no-install-recommends \
      python3 ca-certificates curl git bash \
      procps lsof xz-utils libssl3 \
    && rm -rf /var/lib/apt/lists/*
# CJK fonts (Noto Sans/Serif CJK) so Chinese/Japanese/Korean text renders instead of tofu.
# Typst scans /usr/share/fonts directly and does NOT use fontconfig — whose post-install
# (fc-cache/chown) fails under rootless Podman's single-uid mapping — so we install the font
# package and tolerate that failure (`|| true`); the .otf files land regardless.
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/50podman-rootless.conf && \
    apt-get update && (apt-get install -y --no-install-recommends fonts-noto-cjk || true) && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# typst CLI 0.15.0 (must match the resolver crates), static musl build.
# --no-same-owner: the tar archive contains uid 1001 entries; rootless Podman with
# single-uid mapping can't chown to 1001. --no-same-owner tells tar to keep uid=0.
RUN curl -fsSL https://github.com/typst/typst/releases/download/v0.15.0/typst-x86_64-unknown-linux-musl.tar.xz \
      -o /tmp/typst.tar.xz && \
    tar -xJf /tmp/typst.tar.xz -C /tmp --no-same-owner && \
    install -m755 /tmp/typst-x86_64-unknown-linux-musl/typst /usr/local/bin/typst && \
    rm -rf /tmp/typst* && typst --version

WORKDIR /app
# backend code (without .venv dir — excluded in .containerignore; copied as tar instead)
COPY backend/ /app/backend/
# Pre-built Python venv (built on O3 with Debian bookworm Python 3.11.2 — same as this
# container). Single tar file avoids Podman spawning parallel goroutines per file.
# Extracting with tar is single-threaded: no rayon/tokio thread pool, no pthread_create.
# Stored at repo root to avoid the **/.venv ignore pattern in .containerignore.
COPY .venv.tar.gz /tmp/venv.tar.gz
RUN tar xzf /tmp/venv.tar.gz -C /app/backend/ --no-same-owner && \
    rm /tmp/venv.tar.gz && \
    /app/backend/.venv/bin/python --version

# Pre-compiled resolver binary (built natively on O3 to avoid Docker/Podman resource limits)
COPY resolver/target/release/tcb-resolver ./resolver/target/release/tcb-resolver
COPY frontend/dist ./frontend/dist
# a default sample deck so the container is usable standalone
COPY backend/sample ./sample

# Pre-built Claude CLI binary (self-contained Node.js SEA).
# Build on O3 with: npm install @anthropic-ai/claude-code then copy claude.exe
# Single-file extraction keeps Podman goroutine count low (avoids pids.max limit).
COPY node-claude.tar.gz /tmp/node-claude.tar.gz
RUN tar xzf /tmp/node-claude.tar.gz -C /usr/local/bin --strip-components=1 --no-same-owner && \
    rm /tmp/node-claude.tar.gz && \
    # Claude Code expects itself at ~/.local/bin/claude — create the canonical symlink
    mkdir -p /root/.local/bin && \
    ln -s /usr/local/bin/claude /root/.local/bin/claude && \
    # Restrict terminal cd to /workspace (server-mode security)
    printf '%s\n' \
      'cd() {' \
      '  local t="${1:-/workspace}"' \
      '  local abs; abs="$(realpath -m -- "$t" 2>/dev/null)" || abs="$t"' \
      '  if [[ "$abs" != /workspace && "$abs" != /workspace/* ]]; then' \
      '    printf "cd: restricted to /workspace\n" >&2; return 1' \
      '  fi' \
      '  builtin cd -- "$t"' \
      '}' \
    >> /root/.bash_profile && \
    claude --version

COPY docker-entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

# Per-user persistent data is bind-mounted here at runtime (projects, ~/.tcb, ~/.claude, caches)
ENV PORT=8080 \
    TCB_BROWSE_ROOT=/workspace \
    RENDER_DIR=/tmp/tcb-render \
    TCB_STATE_PATH=/workspace/.tcb/state.json \
    TYPST_FILE=/app/sample/welcome.typ \
    HOME=/root
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint"]
