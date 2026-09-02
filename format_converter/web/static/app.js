/* FormatConverter single-page UI controller.
 *
 * Hard rules:
 *  - No persistent browser storage and no third-party scripts/styles/fonts/images.
 *  - fetch() only ever targets same-origin relative paths (the local API).
 *  - API keys may be saved only to the project-root .env file; the key is
 *    never stored in the browser, never logged, and the password input is
 *    cleared immediately after any key request completes.
 *  - Jobs are tracked per-id and survive mode switches and page reloads (the
 *    server keeps recent jobs for the life of the process). Switching modes
 *    never cancels an in-flight poll loop.
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

  var JOB_STATUS_LABELS = {
    queued: "排队",
    running: "处理中",
    interrupted: "已中断",
    succeeded: "成功",
    failed: "失败"
  };

  // All UI state lives in memory only; a page refresh resets the file
  // selection, but the job list is recovered from the server via GET /api/jobs.
  var appState = {
    mode: "convert",
    files: [], // { name, size, file, valid, reason }
    jobs: []   // { job_id, job_type, status, message, created_at, updated_at, current, total }
  };

  var busy = false;
  // job_id -> true while that job's poll loop is running. Never cleared on a
  // mode switch, so an in-flight job keeps updating after the user moves on.
  var polling = {};
  // job_id -> true while a user-initiated action (resume/retry/delete) on that
  // job's row is in flight, so the buttons are disabled until it settles and a
  // duplicate POST/DELETE cannot be fired.
  var pendingActions = {};
  // The job whose progress the main status line reflects (null after reload).
  var feedbackJobId = null;

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

  // -- file selection / validation -----------------------------------------

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

  // -- recent jobs -----------------------------------------------------------

  function upsertJob(data) {
    if (!data || !data.job_id) {
      return;
    }
    var found = false;
    appState.jobs = appState.jobs.map(function (job) {
      if (job.job_id === data.job_id) {
        found = true;
        return {
          job_id: data.job_id,
          job_type: data.job_type || job.job_type,
          status: data.status || job.status,
          message: data.message != null ? data.message : job.message,
          created_at: typeof data.created_at === "number" ? data.created_at : job.created_at,
          updated_at: typeof data.updated_at === "number" ? data.updated_at : job.updated_at,
          current: typeof data.current === "number" ? data.current : job.current,
          total: typeof data.total === "number" ? data.total : job.total
        };
      }
      return job;
    });
    if (!found) {
      appState.jobs.push({
        job_id: data.job_id,
        job_type: data.job_type || appState.mode,
        status: data.status || "queued",
        message: data.message || "",
        created_at: typeof data.created_at === "number" ? data.created_at : 0,
        updated_at: typeof data.updated_at === "number" ? data.updated_at : 0,
        current: typeof data.current === "number" ? data.current : 0,
        total: typeof data.total === "number" ? data.total : 0
      });
    }
    renderJobs();
  }

  function formatJobTime(ts) {
    if (!ts) {
      return "";
    }
    var d = new Date(ts * 1000);
    function two(n) {
      return (n < 10 ? "0" : "") + n;
    }
    return two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
  }

  function actionPending(jobId) {
    return Object.prototype.hasOwnProperty.call(pendingActions, jobId);
  }

  function beginAction(jobId) {
    // Guards against a duplicate submit from a double click while the previous
    // request for this job is still in flight.
    if (actionPending(jobId)) {
      return false;
    }
    pendingActions[jobId] = true;
    renderJobs();
    return true;
  }

  function appendAction(container, label, jobId, fn, pending, danger) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "job-action" + (danger ? " job-action-danger" : "");
    if (pending) {
      btn.disabled = true;
      btn.textContent = "处理中…";
    } else {
      btn.textContent = label;
      btn.addEventListener("click", function () { fn(jobId); });
    }
    container.appendChild(btn);
  }

  function renderJobs() {
    var list = els.jobList;
    list.textContent = "";
    var emptyEl = els.jobListEmpty;
    if (!appState.jobs.length) {
      emptyEl.hidden = false;
      return;
    }
    emptyEl.hidden = true;

    var sorted = appState.jobs.slice().sort(function (a, b) {
      return (b.updated_at || 0) - (a.updated_at || 0);
    });

    sorted.forEach(function (job) {
      var meta = MODE_META[job.job_type];
      var li = document.createElement("li");
      li.className = "job-row job-" + (job.status || "queued");
      li.setAttribute("data-job-id", job.job_id);

      var typeEl = document.createElement("span");
      typeEl.className = "job-type";
      typeEl.textContent = meta ? meta.title : job.job_type;

      var statusEl = document.createElement("span");
      statusEl.className = "job-status";
      statusEl.textContent = JOB_STATUS_LABELS[job.status] || job.status;

      var timeEl = document.createElement("span");
      timeEl.className = "job-time";
      timeEl.textContent = formatJobTime(job.updated_at);

      li.appendChild(typeEl);
      li.appendChild(statusEl);
      li.appendChild(timeEl);

      var terminal = job.status === "succeeded" ||
        job.status === "failed" || job.status === "interrupted";
      var pending = actionPending(job.job_id);
      var actions = document.createElement("span");
      actions.className = "job-actions";

      if (job.status === "succeeded") {
        var link = document.createElement("a");
        link.className = "job-download";
        link.href = "/api/jobs/" + job.job_id + "/download";
        link.textContent = "下载结果";
        actions.appendChild(link);
      }

      if (job.job_type === "ai-clean") {
        // failed/interrupted AI jobs get a continue/retry control; the text
        // distinguishes the state. While a row action is in flight the button
        // is disabled (handled inside appendAction) so it cannot be re-clicked.
        if (job.status === "interrupted") {
          appendAction(actions, "继续处理", job.job_id, resumeJob, pending);
        } else if (job.status === "failed") {
          appendAction(actions, "重试", job.job_id, retryJob, pending);
        }
      }

      if (terminal) {
        appendAction(actions, "删除", job.job_id, deleteJob, pending, true);
      }

      li.appendChild(actions);

      if (job.status === "failed") {
        var msg = document.createElement("span");
        msg.className = "job-msg";
        msg.textContent = job.message || "任务失败。";
        li.appendChild(msg);
      }

      list.appendChild(li);
    });
  }

  function resumeJob(jobId) {
    if (!beginAction(jobId)) {
      return;
    }
    fetch("/api/jobs/" + jobId + "/resume", { method: "POST" })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) {
            throw new Error(data && data.error ? data.error : "继续处理失败（HTTP " + resp.status + "）。");
          }
          return data;
        });
      })
      .then(function () {
        delete pendingActions[jobId];
        feedbackJobId = jobId;
        setStatus("正在继续处理任务…", "running");
        pollJob(jobId);
      })
      .catch(function (err) {
        delete pendingActions[jobId];
        renderJobs();
        setError("继续处理失败：" + err.message);
      });
  }

  function retryJob(jobId) {
    if (!beginAction(jobId)) {
      return;
    }
    fetch("/api/jobs/" + jobId + "/retry", { method: "POST" })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) {
            throw new Error(data && data.error ? data.error : "重试失败（HTTP " + resp.status + "）。");
          }
          return data;
        });
      })
      .then(function () {
        delete pendingActions[jobId];
        feedbackJobId = jobId;
        setStatus("正在重新处理…", "running");
        pollJob(jobId);
      })
      .catch(function (err) {
        delete pendingActions[jobId];
        renderJobs();
        setError("重试失败：" + err.message);
      });
  }

  function deleteJob(jobId) {
    if (actionPending(jobId)) {
      return;
    }
    if (!window.confirm("删除后该任务的输出与检查点将被清除且无法恢复。确定删除？")) {
      return;
    }
    if (!beginAction(jobId)) {
      return;
    }
    fetch("/api/jobs/" + jobId, { method: "DELETE" })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) {
            throw new Error(data && data.error ? data.error : "删除失败（HTTP " + resp.status + "）。");
          }
          return data;
        });
      })
      .then(function () {
        delete pendingActions[jobId];
        appState.jobs = appState.jobs.filter(function (job) {
          return job.job_id !== jobId;
        });
        delete polling[jobId];
        if (feedbackJobId === jobId) {
          feedbackJobId = null;
          setStatus("");
        }
        renderJobs();
      })
      .catch(function (err) {
        delete pendingActions[jobId];
        renderJobs();
        setError("删除失败：" + err.message);
      });
  }

  function loadRecentJobs() {
    fetch("/api/jobs")
      .then(function (resp) {
        return readJson(resp);
      })
      .then(function (data) {
        if (!data || !Array.isArray(data.jobs)) {
          return;
        }
        data.jobs.forEach(upsertJob);
        data.jobs.forEach(function (job) {
          if (job.status === "queued" || job.status === "running") {
            pollJob(job.job_id);
          }
        });
      })
      .catch(function () {
        // Server not reachable yet; leave the recent-jobs area empty.
      });
  }

  // -- modes / panel ---------------------------------------------------------

  function setMode(mode) {
    appState.mode = mode;
    feedbackJobId = null;
    setStatus("");
    setError("");
    // Note: appState.jobs is deliberately NOT cleared here. Recent jobs stay
    // visible across mode switches; only the transient status line resets.

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
    renderJobs();
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
    els.refreshJobsBtn.addEventListener("click", loadRecentJobs);
  }

  // -- submit / polling ------------------------------------------------------

  function startJob() {
    setError("");
    setStatus("");

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
          rememberModel(params.model);
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
        var now = Date.now() / 1000;
        upsertJob({
          job_id: data.job_id,
          job_type: appState.mode,
          status: data.status || "queued",
          message: "任务已提交。",
          created_at: now,
          updated_at: now
        });
        feedbackJobId = data.job_id;
        var validCount = appState.files.filter(function (item) {
          return item.valid;
        }).length;
        setStatus("正在处理 " + validCount + " 个文件…", "running");
        pollJob(data.job_id);
      })
      .catch(function (err) {
        setBusy(false);
        setStatus("");
        setError("提交失败：" + err.message);
      });
  }

  function pollJob(jobId) {
    if (polling[jobId]) {
      return;
    }
    polling[jobId] = true;

    function tick() {
      if (!polling[jobId]) {
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
          if (!polling[jobId]) {
            return;
          }
          upsertJob(data);
          var status = data.status;
          if (jobId === feedbackJobId && status === "running") {
            if (data.job_type === "ai-clean" && typeof data.total === "number" && data.total > 0) {
              setStatus("AI 校对中 · " + data.current + " / " + data.total, "running");
            }
          }
          if (status === "queued" || status === "running") {
            setTimeout(tick, POLL_INTERVAL_MS);
            return;
          }
          // Terminal state: stop polling this job.
          delete polling[jobId];
          if (jobId === feedbackJobId) {
            if (status === "succeeded") {
              setBusy(false);
              setStatus("任务完成", "success");
            } else if (status === "failed") {
              setBusy(false);
              setStatus("任务失败", "failed");
              setError("任务失败：" + (data.message || "无详细消息。"));
            }
          }
        })
        .catch(function () {
          // The job may have been cleaned up or the server restarted; stop
          // polling rather than spin forever on 404s.
          delete polling[jobId];
          if (jobId === feedbackJobId) {
            setBusy(false);
          }
        });
    }

    setTimeout(tick, 0);
  }

  // -- model memory + connection test (ai-clean mode only) -------------------

  function loadModels() {
    fetch("/api/ai/models")
      .then(function (resp) {
        return readJson(resp);
      })
      .then(function (data) {
        if (data && Array.isArray(data.models)) {
          renderModelOptions(data.models);
        }
      })
      .catch(function () {});
  }

  function renderModelOptions(models) {
    els.modelOptions.textContent = "";
    models.forEach(function (model) {
      var option = document.createElement("option");
      option.value = model;
      els.modelOptions.appendChild(option);
    });
  }

  function setModelMessage(text, isError) {
    els.modelMessage.textContent = text;
    els.modelMessage.hidden = !text;
    els.modelMessage.className = "config-hint" + (isError ? " model-error" : " model-ok");
  }

  function setTestStatus(text, isError) {
    els.testConnectionStatus.textContent = text;
    els.testConnectionStatus.hidden = !text;
    els.testConnectionStatus.className = "config-hint" + (isError ? " model-error" : " model-ok");
  }

  function authHeaders() {
    var headers = { "Content-Type": "application/json" };
    if (sessionToken) {
      headers["X-FC-Session-Token"] = sessionToken;
    }
    return headers;
  }

  function saveModel() {
    var model = (els.modelInput.value || "").trim();
    if (!model) {
      setModelMessage("请先填写模型名。", true);
      return;
    }
    setModelMessage("正在保存…", false);
    fetch("/api/ai/models", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ model: model })
    })
      .then(function (resp) {
        return readJson(resp).then(function (data) {
          if (!resp.ok) {
            if (resp.status === 403) {
              throw new Error("会话已失效，请刷新页面后重试。");
            }
            throw new Error(
              data && data.error ? data.error : "保存模型失败（HTTP " + resp.status + "）。"
            );
          }
          return data;
        });
      })
      .then(function (data) {
        renderModelOptions(data.models);
        setModelMessage("已保存模型。", false);
      })
      .catch(function (err) {
        setModelMessage(err.message, true);
      });
  }

  function deleteModel() {
    var model = (els.modelInput.value || "").trim();
    if (!model) {
      setModelMessage("请先填写要删除的模型名。", true);
      return;
    }
    setModelMessage("正在删除…", false);
    fetch("/api/ai/models", {
      method: "DELETE",
      headers: authHeaders(),
      body: JSON.stringify({ model: model })
    })
      .then(function (resp) {
        return readJson(resp).then(function (data) {
          if (!resp.ok) {
            if (resp.status === 403) {
              throw new Error("会话已失效，请刷新页面后重试。");
            }
            throw new Error(
              data && data.error ? data.error : "删除模型失败（HTTP " + resp.status + "）。"
            );
          }
          return data;
        });
      })
      .then(function (data) {
        renderModelOptions(data.models);
        setModelMessage("已删除模型。", false);
      })
      .catch(function (err) {
        setModelMessage(err.message, true);
      });
  }

  function testConnection() {
    var model = (els.modelInput.value || "").trim();
    if (!model) {
      setModelMessage("请先填写模型名。", true);
      return;
    }
    setTestStatus("正在测试连接…（将向 OrcaRouter 发起真实网络请求）", false);
    fetch("/api/ai/connection-test", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ provider: "orcarouter", model: model })
    })
      .then(function (resp) {
        return readJson(resp).then(function (data) {
          if (!resp.ok) {
            if (resp.status === 403) {
              throw new Error("会话已失效，请刷新页面后重试。");
            }
            throw new Error(
              data && data.error ? data.error : "测试失败（HTTP " + resp.status + "）。"
            );
          }
          return data;
        });
      })
      .then(function (data) {
        if (data && data.ok) {
          setTestStatus("连接正常", false);
        } else {
          setTestStatus((data && data.error) ? data.error : "连接测试失败。", true);
        }
      })
      .catch(function (err) {
        setTestStatus(err.message, true);
      });
  }

  function rememberModel(model) {
    // Fire-and-forget: submitting an AI job remembers the model name so it is
    // offered again next time. Never touches the API key.
    if (!model) {
      return;
    }
    fetch("/api/ai/models", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ model: model })
    })
      .then(function (resp) {
        return readJson(resp);
      })
      .then(function (data) {
        if (data && Array.isArray(data.models)) {
          renderModelOptions(data.models);
        }
      })
      .catch(function () {});
  }

  function initModelControls() {
    els.modelSave.addEventListener("click", saveModel);
    els.modelDelete.addEventListener("click", deleteModel);
    els.testConnection.addEventListener("click", testConnection);
    loadModels();
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
    els.aiKeySection = document.querySelector("[data-key-section]");
    els.modelField = document.getElementById("model-field");
    els.modelInput = document.getElementById("ai-model");
    els.modelOptions = document.getElementById("model-options");
    els.modelSave = document.querySelector("[data-model-save]");
    els.modelDelete = document.querySelector("[data-model-delete]");
    els.testConnection = document.querySelector("[data-connection-test]");
    els.modelMessage = document.getElementById("model-message");
    els.testConnectionStatus = document.getElementById("test-connection-status");
    els.jobList = document.getElementById("job-list");
    els.jobListEmpty = document.getElementById("job-list-empty");
    els.refreshJobsBtn = document.getElementById("refresh-jobs-btn");
    els.keyStatus = document.querySelector("[data-key-config-status]");
    els.apiKeyInput = document.getElementById("ai-api-key");
    els.keySave = document.querySelector("[data-key-save]");
    els.keyClear = document.querySelector("[data-key-clear]");
    els.keyDetect = document.querySelector("[data-key-detect]");
    els.keyHint = document.querySelector("[data-key-hint]");
    els.keyError = document.querySelector("[data-key-error]");

    initPanel();
    initKeyConfig();
    initModelControls();
    setMode("convert");
    loadRecentJobs();
  });
})();
