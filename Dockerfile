# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

# git：requirements 里的 dewu-client 是私有 GitHub 仓库，pip 要 clone
# curl：HEALTHCHECK 用
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 私有仓库凭证走 BuildKit secret：只在这一层可见，不进镜像、不进历史。
#   本地：docker build --secret id=gh_token,src=<含 token 的文件> .
#   CI：  见 .github/workflows/deploy.yml
# token 用 GitHub fine-grained PAT，只给 yqin7/dewu-monitor 的 Contents: Read。
RUN --mount=type=secret,id=gh_token \
    if [ -s /run/secrets/gh_token ]; then \
      git config --global url."https://x-access-token:$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/"; \
    fi \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -f /root/.gitconfig

COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
