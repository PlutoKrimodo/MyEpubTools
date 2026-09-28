import os

from flask import Flask, jsonify, render_template

from modules.registry import discover

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# 扫描 modules/ 下的工具模块并自动挂载，新增工具无需改动本文件。
TOOLS, TOOL_WARNINGS = discover()
for warning in TOOL_WARNINGS:
    print(f"[tools] {warning}")
for tool in TOOLS:
    app.register_blueprint(tool.blueprint, url_prefix=tool.url_prefix)


@app.route("/")
def index():
    return render_template("base.html", tools=TOOLS)


@app.route("/api/tools")
def api_tools():
    """已注册工具清单，供前端（或调试）使用。"""
    return jsonify({"ok": True, "tools": [tool.as_dict() for tool in TOOLS]})


if __name__ == "__main__":
    # 默认只监听回环地址（与 启动.bat 的本地使用场景一致）。
    # 云开发环境 / 容器里需要监听 0.0.0.0，端口转发才能连进来：
    #   EPUB_TOOLS_HOST=0.0.0.0 EPUB_TOOLS_PORT=5000 python app.py
    host = os.environ.get("EPUB_TOOLS_HOST", "127.0.0.1")
    port = int(os.environ.get("EPUB_TOOLS_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
