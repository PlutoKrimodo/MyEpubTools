const modeButtons = Array.from(document.querySelectorAll("#modeSwitch button[data-mode]"));
const modeSections = Array.from(document.querySelectorAll("[data-mode]"))
  .filter((element) => !element.closest("#modeSwitch"));

const inputFile = document.querySelector("#inputFile");
const epubFile = document.querySelector("#epubFile");
const templateFile = document.querySelector("#templateFile");
const previewBtn = document.querySelector("#previewBtn");
const buildBtn = document.querySelector("#buildBtn");
const extractPreviewBtn = document.querySelector("#extractPreviewBtn");
const extractBtn = document.querySelector("#extractBtn");
const outputDirectory = document.querySelector("#outputDirectory");
const selectOutputDirectoryBtn = document.querySelector("#selectOutputDirectoryBtn");
const resetOutputDirectoryBtn = document.querySelector("#resetOutputDirectoryBtn");
const message = document.querySelector("#message");
const previewSummary = document.querySelector("#previewSummary");
const chapterList = document.querySelector("#chapterList");
const textSummary = document.querySelector("#textSummary");
const textPreview = document.querySelector("#textPreview");
const downloadArea = document.querySelector("#downloadArea");
const logs = document.querySelector("#logs");
const chapterSamples = document.querySelector("#chapterSamples");
const generateRegexBtn = document.querySelector("#generateRegexBtn");
let outputDirectoryToken = "";

function showMessage(text, type = "error") {
  message.textContent = text;
  message.className = `status ${type}`;
}

function clearMessage() {
  message.textContent = "";
  message.className = "status hidden";
}

function setBusy(busy) {
  previewBtn.disabled = busy;
  buildBtn.disabled = busy;
  extractPreviewBtn.disabled = busy;
  extractBtn.disabled = busy;
  selectOutputDirectoryBtn.disabled = busy;
  resetOutputDirectoryBtn.disabled = busy || !outputDirectoryToken;
  if (generateRegexBtn) {
    generateRegexBtn.disabled = busy;
  }
}

function resetOutputDirectory() {
  outputDirectoryToken = "";
  outputDirectory.value = "outputs";
  resetOutputDirectoryBtn.disabled = true;
}

/* ---------------- 转换方向 ---------------- */

function applyMode(mode) {
  modeButtons.forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  modeSections.forEach((section) => {
    section.classList.toggle("hidden", section.dataset.mode !== mode);
  });
  clearMessage();
}

modeButtons.forEach((button) => {
  button.addEventListener("click", () => applyMode(button.dataset.mode));
});

/* ---------------- 公共 ---------------- */

function appendOutputDirectory(data) {
  const value = outputDirectory.value.trim();
  if (value) {
    data.append("output_dir", value);
  }
  if (outputDirectoryToken) {
    data.append("output_dir_token", outputDirectoryToken);
  }
}

function renderDownload(filename, url) {
  const link = document.createElement("a");
  link.href = url;
  link.textContent = `下载 ${filename}`;
  link.className = "download-link";
  downloadArea.innerHTML = "";
  downloadArea.appendChild(link);
}

async function requestJson(url, data = new FormData()) {
  const response = await fetch(url, { method: "POST", body: data });
  const json = await response.json();
  if (!response.ok || !json.ok) {
    throw new Error(json.error || "请求失败");
  }
  return json;
}

/* ---------------- TXT → EPUB ---------------- */

generateRegexBtn.addEventListener("click", async () => {
  clearMessage();
  const samples = chapterSamples.value;
  if (!samples.trim()) {
    showMessage("请先粘贴至少一行章节标题示例。");
    return;
  }

  try {
    setBusy(true);
    const data = new FormData();
    data.append("samples", samples);
    const result = await requestJson("/txt2epub/api/generate-chapter-regex", data);
    document.querySelector("#chapterRegex").value = result.regex;
    showMessage(
      `已生成正则并填入（匹配示例 ${result.matched}/${result.total} 行）。可点「预览章节」验证。`,
      "ok",
    );
  } catch (error) {
    showMessage(error.message);
  } finally {
    setBusy(false);
  }
});

function baseFormData() {
  const data = new FormData();
  data.append("encoding", document.querySelector("#encoding").value);
  data.append("chapter_mode", document.querySelector("#chapterMode").value);
  data.append("chapter_regex", document.querySelector("#chapterRegex").value.trim());
  return data;
}

function renderChapters(result) {
  previewSummary.textContent = `识别到 ${result.section_count} 个章节。`;
  chapterList.innerHTML = "";

  result.sections.forEach((section) => {
    const item = document.createElement("li");
    item.textContent = `${section.index}. ${section.title}（${section.paragraph_count} 段）`;
    chapterList.appendChild(item);
  });
}

selectOutputDirectoryBtn.addEventListener("click", async () => {
  clearMessage();

  try {
    setBusy(true);
    const result = await requestJson("/txt2epub/api/select-output-directory");
    if (result.cancelled) {
      return;
    }
    outputDirectoryToken = result.token;
    outputDirectory.value = result.path;
    resetOutputDirectoryBtn.disabled = false;
  } catch (error) {
    showMessage(error.message);
  } finally {
    setBusy(false);
  }
});

resetOutputDirectoryBtn.addEventListener("click", () => {
  resetOutputDirectory();
  clearMessage();
});

previewBtn.addEventListener("click", async () => {
  clearMessage();
  downloadArea.innerHTML = "";

  if (!inputFile.files.length) {
    showMessage("请先选择 TXT 文件。");
    return;
  }

  const data = baseFormData();
  data.append("input_file", inputFile.files[0]);

  try {
    setBusy(true);
    previewSummary.textContent = "正在识别章节……";
    chapterList.innerHTML = "";
    const result = await requestJson("/txt2epub/api/preview", data);
    renderChapters(result);
    showMessage("章节预览完成。", "ok");
  } catch (error) {
    previewSummary.textContent = "预览失败。";
    showMessage(error.message);
  } finally {
    setBusy(false);
  }
});

buildBtn.addEventListener("click", async () => {
  clearMessage();
  downloadArea.innerHTML = "";

  if (!inputFile.files.length) {
    showMessage("请先选择 TXT 文件。");
    return;
  }
  const data = baseFormData();
  data.append("input_file", inputFile.files[0]);
  if (templateFile.files.length) {
    data.append("template_file", templateFile.files[0]);
  }
  data.append("title", document.querySelector("#title").value.trim());
  data.append("author", document.querySelector("#author").value.trim());
  data.append("subtitle", document.querySelector("#subtitle").value.trim());
  data.append("lang", document.querySelector("#lang").value.trim());

  appendOutputDirectory(data);

  if (document.querySelector("#tocPage").checked) {
    data.append("toc_page", "1");
  }

  try {
    setBusy(true);
    logs.textContent = "正在生成 EPUB……";
    const result = await requestJson("/txt2epub/api/build", data);
    logs.textContent = result.logs.length ? result.logs.join("\n") : "生成完成。";
    renderDownload(result.filename, result.download_url);
    showMessage("EPUB 生成完成。", "ok");
  } catch (error) {
    logs.textContent = "生成失败。";
    showMessage(error.message);
  } finally {
    setBusy(false);
  }
});

/* ---------------- EPUB → TXT ---------------- */

function extractFormData() {
  const data = new FormData();
  data.append("line_ending", document.querySelector("#lineEnding").value);
  data.append("encoding", document.querySelector("#outputEncoding").value);
  if (document.querySelector("#keepTitles").checked) {
    data.append("keep_titles", "1");
  }
  if (document.querySelector("#blankLine").checked) {
    data.append("blank_line", "1");
  }
  return data;
}

function summarize(result) {
  const stats = result.stats;
  let text = `正文文档 ${stats.documents} 个、标题 ${stats.headings} 条、段落 ${stats.paragraphs} 段，共 ${stats.characters} 字。`;
  if (result.truncated) {
    text += `仅显示前 ${result.lines.length} 行。`;
  }
  return text;
}

extractPreviewBtn.addEventListener("click", async () => {
  clearMessage();
  downloadArea.innerHTML = "";

  if (!epubFile.files.length) {
    showMessage("请先选择 EPUB 文件。");
    return;
  }

  const data = extractFormData();
  data.append("input_file", epubFile.files[0]);

  try {
    setBusy(true);
    textSummary.textContent = "正在提取正文……";
    textPreview.textContent = "";
    const result = await requestJson("/txt2epub/api/to-txt/preview", data);
    textSummary.textContent = summarize(result);
    textPreview.textContent = result.lines.join("\n") || "（没有提取到文字）";
    showMessage("文本预览完成。", "ok");
  } catch (error) {
    textSummary.textContent = "预览失败。";
    showMessage(error.message);
  } finally {
    setBusy(false);
  }
});

extractBtn.addEventListener("click", async () => {
  clearMessage();
  downloadArea.innerHTML = "";

  if (!epubFile.files.length) {
    showMessage("请先选择 EPUB 文件。");
    return;
  }

  const data = extractFormData();
  data.append("input_file", epubFile.files[0]);
  appendOutputDirectory(data);

  try {
    setBusy(true);
    logs.textContent = "正在提取正文……";
    const result = await requestJson("/txt2epub/api/to-txt", data);
    logs.textContent = result.logs.length ? result.logs.join("\n") : "导出完成。";
    renderDownload(result.filename, result.download_url);
    showMessage("TXT 导出完成。", "ok");
  } catch (error) {
    logs.textContent = "导出失败。";
    showMessage(error.message);
  } finally {
    setBusy(false);
  }
});

/* ---------------- 初始化 ---------------- */

const initialMode = modeButtons.find((button) => button.classList.contains("active"));
applyMode(initialMode ? initialMode.dataset.mode : "to-epub");
