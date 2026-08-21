# 生产镜像（仓库根构建上下文，docker-compose 用）。与 server/Dockerfile（Render 用，
# 上下文为 server/）保持同构：改一处请同步另一处。Python 版本与 CI/pyproject 四处同版
# （tests/test_python_version_alignment.py 钉住），升级要一起改。
FROM python:3.12-slim

WORKDIR /app
# A7：优先用全钉版 lockfile 安装，保证镜像可复现；requirements.txt 仍是区间真源
COPY server/requirements.lock server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock
COPY server/ .

# A8：非 root 运行。/data/uploads 是 compose 挂载点（命名卷首次挂载会继承镜像内
# 目录属主）；/app 需可写（开发档 SQLite 落在工作目录）。
RUN useradd --create-home --uid 10001 medplat \
    && mkdir -p /data/uploads /app/uploads \
    && chown -R medplat:medplat /app /data
USER medplat

# A8：演示种子**不再烧进镜像默认值**。此前 `ENV MEDPLAT_SEED_DEMO=1` 意味着
# 任何拿这个镜像的部署，不显式关就会往生产库灌演示数据。现在默认不设（start.sh
# 只认 "1"），演示档（render.yaml / compose 覆盖）需要时再显式开。
EXPOSE 8000
# 健康探针走应用自身依赖（httpx），不装 curl；PORT 可被运行环境覆盖
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,httpx;httpx.get('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8000'),timeout=4).raise_for_status()"
CMD ["sh", "start.sh"]
