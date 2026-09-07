# syntax=docker/dockerfile:1

# 多阶段构建：编译阶段。固定多架构索引，确保相同提交使用相同基础镜像。
FROM python:3.14-alpine@sha256:c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc AS builder

# 设置构建参数
ARG BUILDKIT_INLINE_CACHE=1

# 安装编译依赖（包括 Rust 编译器）
RUN apk add --no-cache \
    gcc \
    g++ \
    make \
    libffi-dev \
    libsodium-dev \
    musl-dev \
    python3-dev \
    rust \
    cargo \
    openssl-dev \
    pkgconfig

ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
ENV CARGO_BUILD_JOBS=4
ENV OPENSSL_DIR=/usr
ENV OPENSSL_LIBDIR=/usr/lib
ENV PKG_CONFIG_PATH=/usr/lib/pkgconfig
ENV PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig

# 复制依赖文件
COPY requirements.txt .

# Upgrade packaging tools first, then build a deterministic wheelhouse.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# 运行阶段：使用与构建阶段相同的最小化镜像
FROM python:3.14-alpine@sha256:c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc

# 设置运行时环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1
ENV TZ=Asia/Shanghai

# 固定的 Python Alpine 基础镜像已包含 CA 证书与时区数据，仅配置运行时区。
RUN cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

WORKDIR /app

# 使用非 root 用户运行（安全考虑）
RUN addgroup -g 1000 appuser && \
    adduser -D -s /bin/sh -u 1000 -G appuser appuser && \
    mkdir -p /app/data && \
    chown appuser:appuser /app/data

# 只读挂载构建阶段的 wheel，不把临时 wheelhouse 写入运行镜像层。
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-compile /wheels/*

# 复制应用代码
COPY --chown=appuser:appuser src/ ./src/

USER appuser

# Documentation only; listeners remain disabled unless explicitly configured.
EXPOSE 8765 7654

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.main"]
