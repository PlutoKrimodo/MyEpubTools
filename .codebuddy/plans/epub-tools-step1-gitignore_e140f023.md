---
name: epub-tools-step1-gitignore
overview: 为 EPUB 工具集补齐 .gitignore，忽略运行时生成的 uploads/、outputs/、work/ 及临时构建目录 .build_*，并验证忽略规则生效，从而收尾「第一步」改造。
todos:
  - id: update-gitignore
    content: 在 .gitignore 末尾追加项目自定义区块，根锚定忽略 uploads/、outputs/、work/、.build_*/
    status: completed
  - id: verify-ignore-rules
    content: 用 git check-ignore 与 git status 验证规则生效，且未误伤 web/、templates/、modules/、sample_test.epub
    status: completed
    dependencies:
      - update-gitignore
  - id: sync-dev-summary
    content: 更新 开发总结.md 的「待办」，标记 .gitignore 收尾已完成
    status: completed
    dependencies:
      - update-gitignore
---

## Product Overview
承接 `开发总结.md`「待办」中的第一项：为运行时产生的生成物补全 `.gitignore` 忽略规则，使仓库保持干净、不再出现未被追踪的临时目录。这是「第一步结构与公共层改造」的收尾动作，不涉及任何功能开发或业务逻辑变更。

## Core Features
- 在现有 `.gitignore` 末尾追加「项目自定义」区块，忽略运行时生成目录：`uploads/`、`outputs/`、`work/`
- 忽略 `build_epub.py` 在 CLI 模式下可能产生的临时构建目录 `.build_*`
- 采用根目录锚定写法，精确命中项目根下的同名目录，避免误伤其他位置
- 保留现有 VisualStudio 模板规则，不重写、不删除既有条目
- 确保源码与模板资源（`web/`、`templates/`、`modules/`、`app.py`、`build_epub.py`、`sample_test.epub`、`requirements.txt`）仍被正常追踪
- 同步更新 `开发总结.md` 的「待办」条目，标记该项已完成


## Tech Stack Selection
沿用项目现有技术栈，本次无需引入任何新依赖：
- 版本控制：Git（`.gitignore` 规则文件）
- 项目运行时仍为 Python + Flask + BeautifulSoup4（本次不改动任何 Python 代码）

## Implementation Approach
- **策略**：以「追加」方式在 `.gitignore` 末尾新增一个带注释标题的项目自定义区块，而非修改或重排 363 行 VisualStudio 模板内容，最大限度降低回归风险与 diff 噪音。
- **写法选择**：使用根锚定模式 `/uploads/`、`/outputs/`、`/work/`、`/.build_*/`。相比裸写 `uploads/`，根锚定可避免忽略未来可能出现的同名子目录（例如某工具内部资源目录），符合「精确匹配项目根目录」的意图。
- **为什么不加 `.gitkeep`**：`modules/txt2epub.py` 的 `ensure_dirs()` 已对三个目录执行 `mkdir(parents=True, exist_ok=True)`，运行时会自动创建，无需提交占位文件，因此无需为空目录做任何额外处理。
- **为什么不清理既有规则**：`__pycache__/`、`*.pyc` 已在模板中覆盖；`.builds`（第 96 行）与实际的 `.build_<stem>` 命名不同，属于不同用途，保留不动，仅补充真正缺失的规则。
- **不自动化提交**：用户尚未决定是否提交，`git add/commit` 不在本方案范围内，仅保证工作区状态可通过 `git status` 验证。

## Implementation Notes
- **`.gitignore` 追加内容（示意）**：
  ```gitignore
  # --- 项目自定义：运行时生成物 ---
  /uploads/
  /outputs/
  /work/
  /.build_*/
  ```
  说明：`/uploads/` 为上传暂存（按 job 创建，处理完即清理）；`/outputs/` 为默认 EPUB 输出目录（`build_epub.py` CLI 默认输出亦指向此处）；`/work/` 为构建工作目录。
- **验证要点（不修改任何代码）**：
  - `git status` 应保持干净，且不出现 `uploads/`、`outputs/`、`work/`、`.build_*` 未跟踪项；
  - 若目录已存在，`git check-ignore -v uploads outputs work .build_demo` 应命中新增规则；
  - 反向确认未被误伤：`git check-ignore web templates modules sample_test.epub` 应**无输出**（即未被忽略）。
- **Blast radius 控制**：只追加文件末尾内容，不触碰既有规则与任何源码；即使规则写错也仅影响忽略行为，可立即回退。
- **边界情形**：CLI 模式若以根目录之外的 txt 为输入，`.build_<stem>` 可能生成在别处；本方案按约定采用根锚定，若确有必要可另行追加非锚定的 `.build_*/`，但默认不引入以免过度忽略。

## Directory Structure
本次改动范围极小，仅涉及以下文件：

```
workspace/
├── .gitignore          # [MODIFY] 在文件末尾追加「项目自定义：运行时生成物」区块，
│                       #   新增 /uploads/、/outputs/、/work/、/.build_*/ 四条根锚定规则；
│                       #   保留全部既有 VisualStudio 模板规则不变。
└── 开发总结.md          # [MODIFY] 更新「待办」小节：将「.gitignore 尚未忽略 uploads/、outputs/、work/」
                        #   标记为已完成（可补充实际采用的忽略模式），保持文档与仓库状态一致。
```

