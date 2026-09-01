/* FormatConverter single-page UI controller.
 *
 * Hard rules:
 *  - No persistent browser storage and no third-party scripts/styles/fonts/images.
 *  - fetch() only ever targets same-origin relative paths (the local API).
 *  - API keys may be saved only to the project-root .env file; the key is
 *    never stored in the browser, never logged, and the password input is
 *    cleared immediately after any key request completes.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 1000;

  // Memory-only session token injected by the server into the index meta tag.
  var sessionToken = (function () {
    var meta = document.querySelector('meta[name="fc-session-token"]');
    return meta ? meta.getAttribute("content") : "";
  })();

  // Per-mode metadata: accepted extension and the strings shown in the panel.
  var MODE_META = {
    convert: {
      ext: ".pdf",
      title: "PDF 转 Markdown",
      desc: "支持 .pdf 文件，可一次选择多个；单个输出直接下载，多个输出打包为 ZIP。",
      drop: "支持 .pdf 文件，可一次选择多个"
    },
    clean: {
      ext: ".md",
      title: "Markdown 清理",
      desc: "支持 .md 文件，可一次选择多个；清理重复段落、合并被折行的段落并保留列表。",
      drop: "支持 .md 文件，可一次选择多个"
    },
    pipeline: {
      ext: ".pdf",
      title: "转换后清理流水线",
      desc: "支持 .pdf 文件，可一次选择多个；先转换为 Markdown，再执行清理。",
      drop: "支持 .pdf 文件，可一次选择多个"
    },
    "ai-clean": {
      ext: ".md",
      title: "AI 校对",
      desc: "支持 .md 文件，可一次选择多个；会发送到 OrcaRouter 做校对（真实网络请求）。",
      drop: "支持 .md 文件，可一次选择多个"
    }
  };

  // All UI state lives in memory only; a page refresh resets everything.
  var appState = {
    mode: "convert",
    files: [], // { name, size, file, valid, reason }
    currentJobId: null
  };

  var busy = false;

  // DOM element handles, bound once the document is ready.
  var els = {};

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  function readJson(resp) {
    return resp.text().then(function (text) {
      try {
        return JSON.parse(text);
      } catch (err) {
        return null;
      }
    });
  }

  function formatBytes(size) {
    if (typeof size !== "number" || !isFinite(size) || size < 0) {
      return "0 B";
    }
    var units = ["B", "KB", "MB", "GB"];
    var index = 0;
    var value = size;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    if (index === 0) {
      return String(Math.round(value)) + " B";
    }
    return value.toFixed(1) + " " + units[index];
  }

  function currentMeta() {
    return MODE_META[appState.mode];
  }

  function isAccepted(name) {
    return name.toLowerCase().endsWith(currentMeta().ext);
  }

  function validateFiles(files) {
    // Returns the first problem as a message, or "" when every file is valid
    // for the current mode. Checks extension, duplicate names (case
    // insensitive) and empty (zero-byte) files.
    var seen = {};
    for (var i = 0; i < files.length; i += 1) {
      var name = String(files[i].name || "");
      var lower = name.toLowerCase();
      if (!isAccepted(name)) {
        return "“" + name + "”扩展名不符合该任务要求，仅支持 " + currentMeta().ext + "。";
      }
      if (Object.prototype.hasOwnProperty.call(seen, lower)) {
        return "文件名重复（不区分大小写）：“" + name + "”。";
      }
      seen[lower] = true;
      if (typeof files[i].size === "number" && files[i].size === 0) {
        return "“" + name + "”是空文件，无法处理。";
      }
    }
    return "";
  }

  function getSelectedFiles(fileInput) {
    var input = fileInput || els.fileInput;
    return input && input.files ? Array.from(input.files) : [];
  }

  function readFileAsDataUrl(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        resolve(String(reader.result));
      };
      reader.onerror = function () {
        reject(new Error("无法读取文件：" + file.name));
      };
      reader.readAsDataURL(file);
    });
  }

  function readUploads(files) {
    return Promise.all(files.map(function (file) {
      return readFileAsDataUrl(file).then(function (dataUrl) {
        var comma = dataUrl.indexOf(",");
        return {
          filename: file.name,
          data_b64: comma >= 0 ? dataUrl.slice(comma + 1) : ""
        };
      });
    }));
  }

  function revalidateFiles() {
    // Re-check every selected file against the current mode's rules (used on
    // mode switch so files that no longer match are flagged, not dropped).
    var seen = {};
    appState.files.forEach(function (item) {
      var lower = String(item.name || "").toLowerCase();
      var dup = Object.prototype.hasOwnProperty.call(seen, lower);
      seen[lower] = true;
      var okExt = isAccepted(item.name);
      item.valid = okExt && !dup && item.size > 0;
      if (item.valid) {
        item.reason = "";
      } else if (!okExt) {
        item.reason = "扩展名不符合要求";
      } else if (dup) {
        item.reason = "文件名重复";
      } else {
        item.reason = "空文件";
      }
    });
  }

  function addFiles(fileList) {
    var incoming = Array.from(fileList || []);
    if (incoming.length === 0) {
      return;
    }
    incoming.forEach(function (file) {
      appState.files.push({
        name: file.name,
        size: file.size || 0,
        file: file,
        valid: true,
        reason: ""
      });
    });
    revalidateFiles();
    var problem = validateFiles(appState.files.map(function (item) {
      return item.file;
    }));
    if (problem) {
      setError(problem);
    } else {
      setError("");
    }
    els.fileInput.value = "";
    renderFileList();
  }

  function removeFile(item) {
    appState.files = appState.files.filter(function (x) {
      return x !== item;
    });
    var problem = validateFiles(appState.files.map(function (x) {
      return x.file;
    }));
    if (problem) {
      setError(problem);
    } else {
      setError("");
    }
    renderFileList();
  }

  function clearFiles() {
    appState.files = [];
    els.fileInput.value = "";
    setError("");
    renderFileList();
  }

  function renderFileList() {
    var list = els.fileList;
    list.textContent = "";

    var validItems = appState.files.filter(function (item) {
      return item.valid;
    });
    var totalBytes = 0;
    validItems.forEach(function (item) {
      totalBytes += item.size || 0;
    });

    appState.files.forEach(function (item) {
      var li = document.createElement("li");
      li.className = "file-row" + (item.valid ? "" : " file-row-invalid");

      var icon = document.createElement("span");
      icon.className = "file-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "📄";

      var nameEl = document.createElement("span");
      nameEl.className = "file-name";
      nameEl.textContent = item.name;
      nameEl.title = item.name;

      var sizeEl = document.createElement("span");
      sizeEl.className = "file-size";
      sizeEl.textContent = formatBytes(item.size);

      var stateEl = document.createElement("span");
      stateEl.className = "file-state";
      stateEl.textContent = item.valid ? "待处理" : item.reason;

      var removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "file-remove";
      removeBtn.textContent = "×";
      removeBtn.setAttribute("aria-label", "移除 " + item.name);
      removeBtn.addEventListener("click", function () {
        removeFile(item);
      });

      li.appendChild(icon);
      li.appendChild(nameEl);
      li.appendChild(sizeEl);
      li.appendChild(stateEl);
      li.appendChild(removeBtn);
      list.appendChild(li);
    });

    if (appState.files.length === 0) {
      els.fileSummary.textContent = "尚未选择文件";
      els.clearBtn.hidden = true;
    } else {
      els.fileSummary.textContent =
        "共 " + validItems.length + " 个文件，总计 " + formatBytes(totalBytes);
      els.clearBtn.hidden = false;
    }
    syncStartButton();
  }

  function syncStartButton() {
    var hasValid = appState.files.some(function (item) {
      return item.valid;
    });
    els.startBtn.disabled = busy || !hasValid;
    els.startBtn.textContent = busy ? "处理中…" : "开始处理";
  }

  function setMode(mode) {
    appState.mode = mode;
    appState.currentJobId = null;
    setStatus("");
    setError("");
    clearDownload();

    var meta = MODE_META[mode];
    els.panel.setAttribute("data-job-type", mode);
    els.panelTitle.textContent = meta.title;
    els.panelDesc.textContent = meta.desc;
    els.fileInput.accept = meta.ext;
    els.dropSub.textContent = meta.drop;

    var isAi = mode === "ai-clean";
    els.aiKeySection.hidden = !isAi;
    els.modelField.hidden = !isAi;

    els.modeButtons.forEach(function (btn) {
      var active = btn.getAttribute("data-mode") === mode;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });

    revalidateFiles();
    var problem = validateFiles(appState.files.map(function (item) {
      return item.file;
    }));
    if (problem) {
      setError(problem);
    } else {
      setError("");
    }
    renderFileList();
  }

  function initPanel() {
    els.modeButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode(btn.getAttribute("data-mode"));
      });
    });

    els.dropZone.addEventListener("click", function () {
      els.fileInput.click();
    });
    els.dropZone.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        els.fileInput.click();
      }
    });
    els.dropZone.addEventListener("dragover", function (event) {
      event.preventDefault();
      els.dropZone.classList.add("drag-active");
    });
    els.dropZone.addEventListener("dragleave", function () {
      els.dropZone.classList.remove("drag-active");
    });
    els.dropZone.addEventListener("drop", function (event) {
      event.preventDefault();
      els.dropZone.classList.remove("drag-active");
      addFiles(event.dataTransfer ? Array.from(event.dataTransfer.files) : []);
    });
    els.fileInput.addEventListener("change", function () {
      addFiles(getSelectedFiles(els.fileInput));
    });
    els.clearBtn.addEventListener("click", clearFiles);
    els.startBtn.addEventListener("click", startJob);
  }

  function startJob() {
    setError("");
    setStatus("");
    clearDownload();
    appState.currentJobId = null;

    var files = appState.files.map(function (item) {
      return item.file;
    });
    if (files.length === 0) {
      setError("请先选择文件。");
      els.dropZone.focus();
      return;
    }

    var problem = validateFiles(files);
    if (problem) {
      setError(problem);
      return;
    }

    if (appState.mode === "ai-clean") {
      var model = (els.modelInput.value || "").trim();
      if (!model) {
        setError("请填写模型名。");
        els.modelInput.focus();
        return;
      }
    }

    setBusy(true);
    setStatus("读取文件…");

    readUploads(files)
      .then(function (uploads) {
        var params = {};
        if (appState.mode === "ai-clean") {
          params.provider = "orcarouter";
          params.model = (els.modelInput.value || "").trim();
        }
        var payload = {
          job_type: appState.mode,
          params: params,
          uploads: uploads
        };
        postJob(payload);
      })
      .catch(function (err) {
        setBusy(false);
        setStatus("");
        setError(err.message);
      });
  }

  function setBusy(value) {
    busy = value;
    syncStartButton();
  }

  function setStatus(text, cls) {
    els.status.textContent = text;
    els.status.className = "status";
    if (cls) {
      els.status.className += " " + cls;
    }
  }

  function setError(text) {
    els.error.textContent = text;
  }

  function clearDownload() {
    els.downloadArea.textContent = "";
  }

  function showDownloadLink() {
    clearDownload();
    var link = document.createElement("a");
    link.href = "/api/jobs/" + appState.currentJobId + "/download";
    link.className = "download-btn";
    link.textContent = "下载结果";
    els.downloadArea.appendChild(link);
  }

  function postJob(payload) {
    fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (resp) {
        return readJson(resp).then(function (data) {
          if (!resp.ok) {
            throw new Error(
              data && data.error ? data.error : "提交任务失败（HTTP " + resp.status + "）。"
            );
          }
          return data;
        });
      })
      .then(function (data) {
        appState.currentJobId = data.job_id;
        var validCount = appState.files.filter(function (item) {
          return item.valid;
        }).length;
        setStatus("正在处理 " + validCount + " 个文件…", "running");
        pollUntilDone(data.job_id);
      })
      .catch(function (err) {
        setBusy(false);
        setStatus("");
        setError("提交失败：" + err.message);
      });
  }

  function pollUntilDone(jobId) {
    var done = false;

    function tick() {
      if (done || appState.currentJobId !== jobId) {
        return;
      }
      fetch("/api/jobs/" + jobId)
        .then(function (resp) {
          return readJson(resp).then(function (data) {
            if (!resp.ok) {
              throw new Error(
                data && data.error ? data.error : "查询状态失败（HTTP " + resp.status + "）。"
              );
            }
            return data;
          });
        })
        .then(function (data) {
          if (appState.currentJobId !== jobId) {
            return;
          }
          var status = data.status;
          if (status === "queued" || status === "running") {
            setStatus("正在处理…", "running");
            setTimeout(tick, POLL_INTERVAL_MS);
            return;
          }
          if (status === "succeeded") {
            done = true;
            setBusy(false);
            setStatus("成功", "success");
            showDownloadLink();
            return;
          }
          if (status === "failed") {
            done = true;
            setBusy(false);
            var message = data && data.message ? data.message : "任务失败，无详细消息。";
            setStatus("失败", "failed");
            setError("任务失败：" + message);
            return;
          }
          // Unknown status value: keep polling, keep the user informed.
          setStatus("正在处理…", "running");
          setTimeout(tick, POLL_INTERVAL_MS);
        })
        .catch(function (err) {
          if (appState.currentJobId !== jobId) {
            return;
          }
          done = true;
          setBusy(false);
          setStatus("");
          setError("查询失败：" + err.message);
        });
    }

    setTimeout(tick, 0);
  }

  // -----------------------------------------------------------------------
  // OrcaRouter API key configuration (ai-clean mode only)
  // -----------------------------------------------------------------------

  function initKeyConfig() {
    var statusEl = els.keyStatus;
    var input = els.apiKeyInput;
    var saveBtn = els.keySave;
    var clearBtn = els.keyClear;
    var detectBtn = els.keyDetect;
    var hintEl = els.keyHint;
    var errorEl = els.keyError;

    function setKeyError(text) {
      if (text) {
        errorEl.textContent = text;
        errorEl.hidden = false;
      } else {
        errorEl.textContent = "";
        errorEl.hidden = true;
      }
    }

    function renderStatus(stateText, sourceText, source) {
      statusEl.textContent = "状态：" + stateText + "；来源：" + sourceText;
      statusEl.className = "key-status";
      if (source === "environment" || source === "dot_env") {
        statusEl.className += " ok";
      } else {
        statusEl.className += " warn";
      }
    }

    function loadStatus() {
      setKeyError("");
      fetch("/api/ai/key-status")
        .then(function (resp) {
          return readJson(resp);
        })
        .then(function (data) {
          if (!data || typeof data.configured !== "boolean") {
            renderStatus("未配置", "未配置", "none");
            hintEl.hidden = true;
            clearBtn.hidden = true;
            return;
          }
          var configured = data.configured;
          var source = data.source;
          if (configured && source === "environment") {
            renderStatus("已配置", "系统环境变量", source);
            hintEl.textContent = "当前仍优先使用系统环境变量；保存的 .env Key 会在环境变量未设置时生效。";
            hintEl.hidden = false;
            // The env var itself can never be touched; clearing only ever
            // removes the local .env backup key (per the product spec).
            clearBtn.hidden = false;
          } else if (configured && source === "dot_env") {
            renderStatus("已配置", "本地 .env", source);
            hintEl.hidden = true;
            clearBtn.hidden = false;
          } else {
            renderStatus("未配置", "未配置", "none");
            hintEl.hidden = true;
            clearBtn.hidden = true;
          }
        })
        .catch(function () {
          renderStatus("未配置", "未配置", "none");
          hintEl.hidden = true;
          clearBtn.hidden = true;
        });
    }

    function saveKey() {
      setKeyError("");
      var value = input.value || "";
      if (!value.trim()) {
        setKeyError("请先填写 API Key。");
        input.focus();
        return;
      }
      var headers = { "Content-Type": "application/json" };
      if (sessionToken) {
        headers["X-FC-Session-Token"] = sessionToken;
      }
      fetch("/api/ai/key", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ api_key: value })
      })
        .then(function (resp) {
          return readJson(resp).then(function (data) {
            if (!resp.ok) {
              if (resp.status === 403) {
                throw new Error("会话已失效，请刷新页面后重试。");
              }
              throw new Error(
                data && data.error ? data.error : "保存失败（HTTP " + resp.status + "）。"
              );
            }
            return data;
          });
        })
        .then(function () {
          loadStatus();
        })
        .catch(function (err) {
          setKeyError(err.message);
        })
        .then(function () {
          input.value = "";
        });
    }

    function clearKey() {
      setKeyError("");
      var headers = {};
      if (sessionToken) {
        headers["X-FC-Session-Token"] = sessionToken;
      }
      fetch("/api/ai/key", {
        method: "DELETE",
        headers: headers
      })
        .then(function (resp) {
          return readJson(resp).then(function (data) {
            if (!resp.ok) {
              if (resp.status === 403) {
                throw new Error("会话已失效，请刷新页面后重试。");
              }
              throw new Error(
                data && data.error ? data.error : "清除失败（HTTP " + resp.status + "）。"
              );
            }
            return data;
          });
        })
        .then(function () {
          loadStatus();
        })
        .catch(function (err) {
          setKeyError(err.message);
        })
        .then(function () {
          // Hygiene: drop any unsaved draft typed into the password field.
          input.value = "";
        });
    }

    saveBtn.addEventListener("click", saveKey);
    clearBtn.addEventListener("click", clearKey);
    detectBtn.addEventListener("click", loadStatus);

    loadStatus();
  }

  onReady(function () {
    els.panel = document.getElementById("work-panel");
    els.panelTitle = document.getElementById("panel-title");
    els.panelDesc = document.getElementById("panel-desc");
    els.modeButtons = Array.from(document.querySelectorAll("#mode-selector .mode-btn"));
    els.fileInput = document.getElementById("file-input");
    els.dropZone = document.getElementById("drop-zone");
    els.dropSub = document.getElementById("drop-sub");
    els.fileList = document.getElementById("file-list");
    els.fileSummary = document.getElementById("file-summary");
    els.clearBtn = document.getElementById("clear-files-btn");
    els.startBtn = document.getElementById("start-btn");
    els.status = document.getElementById("status");
    els.error = document.getElementById("error");
    els.downloadArea = document.getElementById("download-area");
    els.aiKeySection = document.querySelector("[data-key-section]");
    els.modelField = document.getElementById("model-field");
    els.modelInput = document.getElementById("ai-model");
    els.keyStatus = document.querySelector("[data-key-config-status]");
    els.apiKeyInput = document.getElementById("ai-api-key");
    els.keySave = document.querySelector("[data-key-save]");
    els.keyClear = document.querySelector("[data-key-clear]");
    els.keyDetect = document.querySelector("[data-key-detect]");
    els.keyHint = document.querySelector("[data-key-hint]");
    els.keyError = document.querySelector("[data-key-error]");

    initPanel();
    initKeyConfig();
    setMode("convert");
  });
})();
