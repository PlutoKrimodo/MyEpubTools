/* 通用工具页面框架：按声明式配置渲染控件，统一处理「JSON 响应 / 文件下载」，
   并支持读取现有值预填、多文件上传（可调顺序）与可勾选的文件列表。 */
(() => {
    "use strict";

    const configEl = document.getElementById("tkPageConfig");
    if (!configEl) return;

    const page = JSON.parse(configEl.textContent || "{}");
    const modes = Array.isArray(page.modes) ? page.modes : [];
    const fields = Array.isArray(page.fields) ? page.fields : [];
    const canLoad = Boolean(page.load_endpoint);

    const els = {
        modeCard: document.getElementById("tkModeCard"),
        mode: document.getElementById("tkMode"),
        uploadTitle: document.getElementById("tkUploadTitle"),
        file: document.getElementById("tkFile"),
        fileOrder: document.getElementById("tkFileOrder"),
        uploadHint: document.getElementById("tkUploadHint"),
        fieldsTitle: document.getElementById("tkFieldsTitle"),
        fields: document.getElementById("tkFields"),
        fieldsHint: document.getElementById("tkFieldsHint"),
        submit: document.getElementById("tkSubmit"),
        preview: document.getElementById("tkPreview"),
        reload: document.getElementById("tkReload"),
        status: document.getElementById("tkStatus"),
        memory: document.getElementById("tkMemory"),
        memoryText: document.getElementById("tkMemoryText"),
        memoryClear: document.getElementById("tkMemoryClear"),
        resultTitle: document.getElementById("tkResultTitle"),
        resultHint: document.getElementById("tkResultHint"),
        result: document.getElementById("tkResult"),
    };

    let currentMode = modes.length ? modes[0] : null;
    let busy = false;
    let lastObjectUrl = "";
    let selectedFiles = [];
    const controls = new Map();

    /* ---------------------------------------------------------------- 状态 */
    function setStatus(text, kind) {
        els.status.textContent = text || "";
        els.status.className = "tk-status" + (kind ? " tk-" + kind : "");
        els.status.classList.toggle("tk-hidden", !text);
    }

    function clearStatus() {
        setStatus("", "");
    }

    function clearResult() {
        els.result.textContent = "";
    }

    function updateSubmitLabel() {
        if (busy) {
            els.submit.textContent = "正在处理……";
            return;
        }
        els.submit.textContent = (currentMode && currentMode.submit_label) || page.submit_label || "开始处理";
    }

    function setBusy(next) {
        busy = next;
        els.submit.disabled = next;
        els.file.disabled = next;
        if (els.reload) els.reload.disabled = next;
        if (els.preview) els.preview.disabled = next;
        controls.forEach(({ field, input }) => {
            if (field && field.type === "filelist") {
                input.querySelectorAll("input, button").forEach((node) => { node.disabled = next; });
                return;
            }
            input.disabled = next;
        });
        updateSubmitLabel();
    }

    function uploadLabel() {
        return els.uploadTitle.textContent || "文件";
    }

    function currentFiles() {
        if (selectedFiles.length) return selectedFiles;
        return Array.from(els.file.files || []);
    }

    /* --------------------------------------- 跨板块共享的「当前书籍」记忆 */
    /* 板块在 iframe 里整页加载，切走时本页 JS 状态就没了；外层页面不会跳转，
       所以把 File 对象交给它保管，本页加载时再取回来放进文件框。
       只存在于内存：刷新或关闭页面即失效，不往磁盘写任何东西。
       直接打开某个板块页（不在 iframe 里）时没有这个保管者，功能自动退化为不做记忆。 */
    function memoryStore() {
        try {
            if (window.parent && window.parent !== window && window.parent.EPUB_MEMORY) {
                return window.parent.EPUB_MEMORY;
            }
        } catch (_error) {
            /* 跨域受限时退化成不做记忆，不影响页面其它功能 */
        }
        return null;
    }

    function currentAccept() {
        return (currentMode && currentMode.accept) || page.upload.accept || "";
    }

    function acceptsFile(file, accept) {
        const rules = String(accept || "")
            .split(",")
            .map((rule) => rule.trim().toLowerCase())
            .filter((rule) => rule.startsWith(".") || rule.includes("/"));
        if (!rules.length) return true;
        const name = String(file.name || "").toLowerCase();
        const type = String(file.type || "").toLowerCase();
        return rules.some((rule) => (rule.startsWith(".") ? name.endsWith(rule) : type === rule));
    }

    function applyFilesToInput(files) {
        let list = files.filter((file) => acceptsFile(file, currentAccept()));
        if (!isMultiple()) list = list.slice(0, 1);
        try {
            const transfer = new DataTransfer();
            list.forEach((file) => transfer.items.add(file));
            els.file.files = transfer.files;
        } catch (_error) {
            /* 个别浏览器不允许给 input.files 赋值：selectedFiles 仍然生效，
               预览与提交照常，只是原生控件里不显示文件名 */
        }
        selectedFiles = list;
        return list.length;
    }

    function renderMemoryBar() {
        if (!els.memory) return;
        const store = memoryStore();
        const name = (store && store.fileName) || "";
        els.memory.classList.toggle("tk-hidden", !name);
        if (!name) return;
        els.memoryText.textContent = currentFiles().length
            ? "已记住「" + name + "」，切换到其它板块后仍然可用"
            : "已记住「" + name + "」，但与当前模式的文件类型不符，未自动载入";
    }

    function autoLoad() {
        if (!canLoad || !page.autoload || !currentFiles().length) return;
        clearStatus();
        clearResult();
        postExtras(page.load_endpoint, "values", "正在读取现有值……", { silent: true });
    }

    function restoreRemembered() {
        const store = memoryStore();
        const files = store && store.get();
        if (!files || !files.length) return 0;
        const applied = applyFilesToInput(files);
        renderFileOrder();
        renderMemoryBar();
        /* 恢复文件等同于「重新选中」，必须走预填：否则表单是空的，
           而元数据编辑这类工具用空表单提交会把原有元数据清空。 */
        if (applied) autoLoad();
        return applied;
    }

    /* ------------------------------------------------------------ 多文件顺序 */
    function orderButton(label, title, disabled, onClick) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tk-order-btn";
        button.textContent = label;
        button.title = title;
        button.disabled = disabled;
        if (!disabled) button.addEventListener("click", onClick);
        return button;
    }

    function moveFile(index, delta) {
        const target = index + delta;
        if (target < 0 || target >= selectedFiles.length) return;
        const moved = selectedFiles.splice(index, 1)[0];
        selectedFiles.splice(target, 0, moved);
        renderFileOrder();
    }

    function isMultiple() {
        if (currentMode && currentMode.multiple !== null && currentMode.multiple !== undefined) {
            return Boolean(currentMode.multiple);
        }
        return Boolean(page.upload.multiple);
    }

    function renderFileOrder() {
        if (!els.fileOrder) return;
        els.fileOrder.textContent = "";
        const show = isMultiple() && selectedFiles.length > 0;
        els.fileOrder.classList.toggle("tk-hidden", !show);
        if (!show) return;

        const head = document.createElement("p");
        head.className = "tk-order-head";
        head.textContent = page.upload.order_hint || "顺序即处理顺序，可用按钮上下调整：";
        els.fileOrder.appendChild(head);

        selectedFiles.forEach((file, index) => {
            const row = document.createElement("div");
            row.className = "tk-order-item";

            const order = document.createElement("span");
            order.className = "tk-order-index";
            order.textContent = String(index + 1);

            const name = document.createElement("span");
            name.className = "tk-order-name";
            name.textContent = file.name;
            name.title = file.name;

            row.appendChild(order);
            row.appendChild(name);
            row.appendChild(orderButton("↑", "上移", index === 0, () => moveFile(index, -1)));
            row.appendChild(
                orderButton("↓", "下移", index === selectedFiles.length - 1, () => moveFile(index, 1))
            );
            els.fileOrder.appendChild(row);
        });
    }

    /* ------------------------------------------------------------ 模式切换 */
    function applyMode(mode) {
        currentMode = mode;
        els.mode.querySelectorAll("button").forEach((button) => {
            const active = button.dataset.value === mode.value;
            button.classList.toggle("tk-active", active);
            button.setAttribute("aria-selected", active ? "true" : "false");
        });

        els.uploadTitle.textContent = mode.upload_label || page.upload.label;
        els.uploadHint.textContent = mode.hint || page.upload.hint || "";
        if (mode.accept) {
            els.file.setAttribute("accept", mode.accept);
        } else {
            els.file.removeAttribute("accept");
        }
        els.file.multiple = isMultiple();
        els.file.value = "";
        selectedFiles = [];
        renderFileOrder();
        updateSubmitLabel();
    }

    function renderModes() {
        if (!modes.length) {
            els.modeCard.classList.add("tk-hidden");
            els.file.multiple = isMultiple();
            updateSubmitLabel();
            return;
        }
        els.modeCard.classList.remove("tk-hidden");
        modes.forEach((mode) => {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = mode.label;
            button.dataset.value = mode.value;
            button.setAttribute("role", "tab");
            button.addEventListener("click", () => {
                if (busy || mode.value === currentMode.value) return;
                clearStatus();
                clearResult();
                applyMode(mode);
                // applyMode 会清空文件框，这里把记住的书带回来；
                // 文件类型与新模式的 accept 不符时不会硬塞（如解包 EPUB / 重打包 ZIP）。
                restoreRemembered();
                renderMemoryBar();
            });
            els.mode.appendChild(button);
        });
        applyMode(modes[0]);
    }

    /* ------------------------------------------------------ 可勾选文件列表 */
    function renderFileList(entry) {
        const { input, state } = entry;
        input.textContent = "";

        const tools = document.createElement("div");
        tools.className = "tk-filelist-tools";

        const selectAll = document.createElement("button");
        selectAll.type = "button";
        selectAll.className = "tk-link-btn";
        selectAll.textContent = "全选";
        selectAll.addEventListener("click", () => {
            state.items.forEach((item) => state.checked.add(item.value));
            renderFileList(entry);
        });

        const clearAll = document.createElement("button");
        clearAll.type = "button";
        clearAll.className = "tk-link-btn";
        clearAll.textContent = "清空";
        clearAll.addEventListener("click", () => {
            state.checked.clear();
            renderFileList(entry);
        });

        const count = document.createElement("span");
        count.className = "tk-filelist-count";
        count.textContent = "已选 " + state.checked.size + " / " + state.items.length;

        tools.appendChild(selectAll);
        tools.appendChild(clearAll);
        tools.appendChild(count);
        input.appendChild(tools);

        if (!state.items.length) {
            const empty = document.createElement("p");
            empty.className = "tk-filelist-empty";
            empty.textContent = state.emptyText || "选中文件后，这里会列出可勾选的条目。";
            input.appendChild(empty);
            return;
        }

        const list = document.createElement("div");
        list.className = "tk-filelist-items";
        state.items.forEach((item) => {
            const row = document.createElement("label");
            row.className = "tk-filelist-row";

            const box = document.createElement("input");
            box.type = "checkbox";
            box.value = item.value;
            box.checked = state.checked.has(item.value);
            box.addEventListener("change", () => {
                if (box.checked) {
                    state.checked.add(item.value);
                } else {
                    state.checked.delete(item.value);
                }
                count.textContent = "已选 " + state.checked.size + " / " + state.items.length;
            });

            const label = document.createElement("span");
            label.className = "tk-filelist-label";
            label.textContent = item.label || item.value;

            const path = document.createElement("span");
            path.className = "tk-filelist-path";
            path.textContent = item.value;
            path.title = item.value;

            row.appendChild(box);
            row.appendChild(label);
            row.appendChild(path);
            list.appendChild(row);
        });
        input.appendChild(list);
    }

    /* -------------------------------------------------------------- 配置区 */
    function fieldElement(field) {
        const wrapper = document.createElement("div");
        wrapper.className = "tk-field";
        const id = "tk-f-" + String(field.name).replace(/[^A-Za-z0-9_-]/g, "-");
        const hint = document.createElement("p");
        hint.className = "tk-hint";
        hint.textContent = field.hint || "";
        hint.classList.toggle("tk-hidden", !field.hint);

        let input;

        if (field.type === "filelist") {
            wrapper.classList.add("tk-span-2");
            input = document.createElement("div");
            input.className = "tk-filelist";
            input.id = id;

            const label = document.createElement("label");
            label.htmlFor = id;
            label.textContent = field.label;
            wrapper.appendChild(label);

            const entry = { field, input, state: { items: [], checked: new Set() } };
            controls.set(field.name, entry);
            renderFileList(entry);

            wrapper.appendChild(input);
            wrapper.appendChild(hint);
            return wrapper;
        }

        if (field.type === "checkbox") {
            const row = document.createElement("div");
            row.className = "tk-checkbox";
            input = document.createElement("input");
            input.type = "checkbox";
            input.id = id;
            input.checked = Boolean(field.default);
            const label = document.createElement("label");
            label.htmlFor = id;
            label.textContent = field.label;
            row.appendChild(input);
            row.appendChild(label);
            wrapper.appendChild(row);
        } else {
            if (field.type === "textarea") {
                input = document.createElement("textarea");
                input.rows = field.rows || 6;
                wrapper.classList.add("tk-span-2");
            } else if (field.type === "select") {
                input = document.createElement("select");
                (field.options || []).forEach((option) => {
                    const item = document.createElement("option");
                    item.value = option.value;
                    item.textContent = option.label;
                    input.appendChild(item);
                });
            } else {
                input = document.createElement("input");
                input.type = field.type === "number" ? "number" : field.type === "file" ? "file" : "text";
            }
            input.id = id;
            if (field.placeholder && field.type !== "file") input.placeholder = field.placeholder;

            const label = document.createElement("label");
            label.htmlFor = id;
            label.textContent = field.label;
            wrapper.appendChild(label);
            wrapper.appendChild(input);
        }

        if (input.type !== "checkbox" && input.type !== "file") {
            input.value = field.default == null ? "" : String(field.default);
        }
        if (field.required) input.required = true;

        controls.set(field.name, { field, input });
        wrapper.appendChild(hint);
        return wrapper;
    }

    function renderFields() {
        els.fieldsTitle.textContent = page.fields_title || "处理设置";
        els.fieldsHint.textContent = page.fields_hint || "";
        els.fieldsHint.classList.toggle("tk-hidden", !page.fields_hint);

        if (!fields.length) {
            const placeholder = document.createElement("p");
            placeholder.className = "tk-hint";
            placeholder.style.margin = "0";
            placeholder.textContent = "本工具暂无额外设置，直接上传文件并开始处理即可。";
            els.fields.appendChild(placeholder);
            return;
        }
        fields.forEach((field) => els.fields.appendChild(fieldElement(field)));
    }

    /* -------------------------------------------------------------- 预填 */
    function setSelectValue(select, value) {
        const exists = Array.prototype.some.call(
            select.options,
            (option) => option.value === value
        );
        if (!exists) {
            // 现有值不在预设选项里（如小众语言代码）时动态补一个，避免被静默丢掉。
            const option = document.createElement("option");
            option.value = value;
            option.textContent = value;
            select.appendChild(option);
        }
        select.value = value;
    }

    function applyValues(values) {
        if (!values || typeof values !== "object") return 0;
        let applied = 0;
        controls.forEach((entry, name) => {
            if (!Object.prototype.hasOwnProperty.call(values, name)) return;
            const { field, input } = entry;
            const value = values[name];

            if (field.type === "filelist") {
                const list = Array.isArray(value) ? value : [];
                entry.state.items = list.map((item) => (
                    typeof item === "string"
                        ? { value: item, label: item, checked: false }
                        : {
                            value: item.value,
                            label: item.label || item.value,
                            checked: Boolean(item.checked),
                        }
                ));
                entry.state.checked = new Set(
                    entry.state.items.filter((item) => item.checked).map((item) => item.value)
                );
                renderFileList(entry);
                applied += 1;
                return;
            }
            if (field.type === "checkbox") {
                input.checked = Boolean(value);
            } else if (field.type === "file") {
                return;
            } else if (field.type === "select") {
                setSelectValue(input, value == null ? "" : String(value));
            } else {
                input.value = value == null ? "" : String(value);
            }
            applied += 1;
        });
        return applied;
    }

    async function postExtras(endpoint, kind, busyText, options) {
        const settings = options || {};
        if (!endpoint || busy) return;
        if (!currentFiles().length) {
            if (!settings.silent) setStatus("请先选择" + uploadLabel() + "。", "error");
            return;
        }

        setBusy(true);
        setStatus(busyText || "正在处理……", "");
        try {
            const data = collectFormData();
            const response = await fetch(endpoint, { method: "POST", body: data });
            if (!response.ok) {
                setStatus(await readErrorMessage(response), "error");
                return;
            }
            const payload = await response.json().catch(() => null);
            if (!payload) {
                setStatus("服务返回的内容无法解析。", "error");
                return;
            }

            let fallback = "完成。";
            if (kind === "html" && payload.html) {
                renderPreview(payload.html);
                fallback = "预览已生成。";
            } else if (kind === "values") {
                const count = applyValues(payload.values);
                fallback = "已读取现有值，共 " + count + " 项。";
            }
            setStatus(payload.summary || fallback, "ok");
        } catch (error) {
            setStatus((error && error.message) || "请求失败，请重试。", "error");
        } finally {
            setBusy(false);
        }
    }

    /* ---------------------------------------------------------------- 提交 */
    function collectFormData() {
        const data = new FormData();
        if (currentMode) data.append("mode", currentMode.value);

        const files = currentFiles();
        if (isMultiple()) {
            files.forEach((file) => data.append("file", file));
        } else if (files.length) {
            data.append("file", files[0]);
        }

        controls.forEach((entry, name) => {
            const { field, input } = entry;
            if (field.type === "filelist") {
                entry.state.checked.forEach((value) => data.append(name, value));
            } else if (field.type === "checkbox") {
                data.append(name, input.checked ? "1" : "0");
            } else if (field.type === "file") {
                if (input.files.length) data.append(name, input.files[0]);
            } else {
                data.append(name, input.value);
            }
        });
        return data;
    }

    function filenameFromHeader(header) {
        if (!header) return "";
        const utf8 = header.match(/filename\*=UTF-8''([^;]+)/i);
        if (utf8) {
            try {
                return decodeURIComponent(utf8[1].trim());
            } catch (_error) {
                return utf8[1].trim();
            }
        }
        const plain = header.match(/filename="?([^";]+)"?/i);
        return plain ? plain[1].trim() : "";
    }

    function messageFromHeader(response) {
        const raw = response.headers.get("X-Toolkit-Message");
        if (!raw) return "";
        try {
            return decodeURIComponent(raw);
        } catch (_error) {
            return raw;
        }
    }

    function renderDownload(url, filename, autoStart) {
        if (lastObjectUrl && lastObjectUrl !== url) {
            URL.revokeObjectURL(lastObjectUrl);
            lastObjectUrl = "";
        }
        if (url.startsWith("blob:")) lastObjectUrl = url;

        clearResult();
        const link = document.createElement("a");
        link.className = "tk-download";
        link.href = url;
        link.download = filename;
        link.textContent = "下载 " + filename;

        const detail = document.createElement("span");
        detail.className = "tk-result-detail";
        detail.textContent = "浏览器会将其保存到默认下载目录。";

        els.result.appendChild(link);
        els.result.appendChild(detail);
        if (autoStart) link.click();
    }

    function renderPreview(html) {
        clearResult();
        const frame = document.createElement("iframe");
        frame.title = "处理结果预览";
        frame.srcdoc = html;
        els.result.appendChild(frame);
    }

    function renderText(message) {
        clearResult();
        const item = document.createElement("p");
        item.className = "tk-result-detail";
        item.style.margin = "0";
        item.textContent = message;
        els.result.appendChild(item);
    }

    async function readErrorMessage(response) {
        try {
            const data = await response.json();
            if (data && data.error) return data.error;
        } catch (_error) {
            /* 非 JSON 响应，回落到状态码提示 */
        }
        return "请求失败（HTTP " + response.status + "）。";
    }

    async function handleSuccess(response) {
        const contentType = (response.headers.get("Content-Type") || "").toLowerCase();
        if (contentType.includes("application/json")) {
            const data = await response.json().catch(() => null);
            if (page.result_kind === "html" && data && data.html) {
                renderPreview(data.html);
            } else if (data && data.download_url) {
                renderDownload(data.download_url, data.filename || "下载文件");
            } else {
                renderText((data && data.message) || "处理完成。");
            }
            setStatus((data && data.message) || "处理完成。", "ok");
            return;
        }

        const blob = await response.blob();
        const filename = filenameFromHeader(response.headers.get("Content-Disposition")) || "download";
        const url = URL.createObjectURL(blob);
        renderDownload(url, filename, true);
        setStatus(
            messageFromHeader(response) || ("处理完成，已开始下载 " + filename + "。"),
            "ok"
        );
    }

    async function submit() {
        if (busy) return;
        clearStatus();
        clearResult();

        if (!currentFiles().length) {
            setStatus("请先选择" + uploadLabel() + "。", "error");
            els.file.focus();
            return;
        }

        setBusy(true);
        setStatus("正在处理……", "");
        try {
            const response = await fetch(page.endpoint, { method: "POST", body: collectFormData() });
            if (!response.ok) {
                setStatus(await readErrorMessage(response), "error");
                return;
            }
            await handleSuccess(response);
        } catch (error) {
            setStatus((error && error.message) || "请求失败，请重试。", "error");
        } finally {
            setBusy(false);
        }
    }

    /* -------------------------------------------------------------- 初始化 */
    els.submit.addEventListener("click", submit);

    els.file.addEventListener("change", () => {
        selectedFiles = Array.from(els.file.files || []);
        renderFileOrder();
        /* 只有真的选中了文件才更新记忆；在文件对话框里点「取消」不算重新上传 */
        if (selectedFiles.length) {
            const store = memoryStore();
            if (store) store.set(selectedFiles);
        }
        renderMemoryBar();
        autoLoad();
    });

    if (canLoad && els.reload) {
        els.reload.textContent = page.load_label || "读取现有值";
        els.reload.classList.remove("tk-hidden");
        els.reload.addEventListener("click", () => {
            clearStatus();
            clearResult();
            postExtras(page.load_endpoint, "values", "正在读取现有值……", {});
        });
    }

    if (page.preview_endpoint && els.preview) {
        els.preview.textContent = page.preview_label || "预览效果";
        els.preview.classList.remove("tk-hidden");
        els.preview.addEventListener("click", () => {
            clearStatus();
            clearResult();
            postExtras(page.preview_endpoint, "html", "正在生成预览……", {});
        });
    }

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" || event.isComposing || busy) return;
        const target = event.target;
        if (!target || target.tagName !== "INPUT") return;
        const type = (target.getAttribute("type") || "text").toLowerCase();
        if (type !== "text" && type !== "number") return;
        event.preventDefault();
        submit();
    });

    if (els.memoryClear) {
        els.memoryClear.addEventListener("click", () => {
            const store = memoryStore();
            if (store) store.clear();
            els.file.value = "";
            selectedFiles = [];
            renderFileOrder();
            renderMemoryBar();
            setStatus("已清除记住的文件；切换板块时不再自动带上它。", "");
        });
    }

    els.resultTitle.textContent = page.result_title || "处理结果";
    if (page.result_hint) {
        els.resultHint.textContent = page.result_hint;
        els.resultHint.classList.remove("tk-hidden");
    }

    renderModes();
    renderFields();
    renderFileOrder();
    // 必须放在 renderModes 之后：那里会按首个模式的 multiple / accept 清空文件框。
    restoreRemembered();
})();
