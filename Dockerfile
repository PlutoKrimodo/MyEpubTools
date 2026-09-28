# EPUB 工具箱：CNB 仅预览模式使用的业务镜像
FROM python:3.12-slim

WORKDIR /app

# 先装依赖再拷代码：只改业务代码时，这一层能命中缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 关键：应用默认监听 127.0.0.1，容器里必须改成 0.0.0.0，否则预览连不上。
# 端口只在这里定义一处，launch 命令不再重复传，避免多处置不一致。
ENV EPUB_TOOLS_HOST=0.0.0.0 \
    EPUB_TOOLS_PORT=8686 \
    PYTHONUNBUFFERED=1

EXPOSE 8686

# 不用 启动.sh：它会创建 venv 并装依赖，在镜像里是多余的。
CMD ["python", "app.py"]
