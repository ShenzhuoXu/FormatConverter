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

  onReady(function () {
    document.querySelectorAll(".card[data-job-type]").forEach(function (card) {
      initCard(card);
      if (card.getAttribute("data-job-type") === "ai-clean") {
        initKeyConfig(card);
      }
    });
  });

  function initCard(card) {
    var jobType = card.getAttribute("data-job-type");
    var form = card.querySelector(".job-form");
    var fileInput = form.querySelector('input[type="file"]');
    var modelInput = form.querySelector('input[name="model"]');
    var submitBtn = form.querySelector(".submit-btn");
    var statusEl = card.querySelector(".status");
    var errorEl = card.querySelector(".error");
    var downloadArea = card.querySelector(".download-area");

    var currentJobId = null;

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      startJob();
    });

    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "status";
      if (cls) {
        statusEl.className += " " + cls;
      }
    }

    function setError(text) {
      errorEl.textContent = text;
    }

    function setBusy(busy) {
      submitBtn.disabled = busy;
      submitBtn.textContent = busy ? "处理中…" : "提交任务";
    }

    function clearDownload() {
      downloadArea.textContent = "";
    }

    function showDownloadLink() {
      clearDownload();
      var link = document.createElement("a");
      link.href = "/api/jobs/" + currentJobId + "/download";
      link.className = "download-btn";
      link.textContent = "下载结果";
      downloadArea.appendChild(link);
    }

    function startJob() {
      setError("");
      setStatus("");
      clearDownload();

      var file = fileInput.files && fileInput.files[0];
      if (!file) {
        setError("请先选择一个文件。");
        fileInput.focus();
        return;
      }

      var expectedExt = jobType === "convert" || jobType === "pipeline" ? ".pdf" : ".md";
      var lowerName = file.name.toLowerCase();
      if (!lowerName.endsWith(expectedExt)) {
        setError("文件扩展名不符合该任务要求，应为 " + expectedExt + "。");
        return;
      }

      var params = {};
      if (jobType === "ai-clean") {
        var model = (modelInput.value || "").trim();
        if (!model) {
          setError("请填写模型名。");
          modelInput.focus();
          return;
        }
        params.provider = "orcarouter";
        params.model = model;
      }

      setBusy(true);
      setStatus("读取文件…");

      var reader = new FileReader();
      reader.onload = function () {
        var dataUrl = String(reader.result);
        var comma = dataUrl.indexOf(",");
        var dataB64 = comma >= 0 ? dataUrl.slice(comma + 1) : "";
        var payload = {
          job_type: jobType,
          params: params,
          upload: { filename: file.name, data_b64: dataB64 }
        };
        postJob(payload);
      };
      reader.onerror = function () {
        setBusy(false);
        setStatus("");
        setError("无法读取该文件，请重试。");
      };
      reader.readAsDataURL(file);
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
          currentJobId = data.job_id;
          setStatus("运行中…", "running");
          pollUntilDone(currentJobId);
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
        if (done || currentJobId !== jobId) {
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
            if (currentJobId !== jobId) {
              return;
            }
            var status = data.status;
            if (status === "queued" || status === "running") {
              setStatus("运行中…", "running");
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
            setStatus("运行中…", "running");
            setTimeout(tick, POLL_INTERVAL_MS);
          })
          .catch(function (err) {
            if (currentJobId !== jobId) {
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
  }

  // -----------------------------------------------------------------------
  // OrcaRouter API key configuration (ai-clean card only)
  // -----------------------------------------------------------------------

  function initKeyConfig(card) {
    var statusEl = card.querySelector("[data-key-config-status]");
    var input = card.querySelector("#ai-api-key");
    var saveBtn = card.querySelector("[data-key-save]");
    var clearBtn = card.querySelector("[data-key-clear]");
    var detectBtn = card.querySelector("[data-key-detect]");
    var hintEl = card.querySelector("[data-key-hint]");
    var errorEl = card.querySelector("[data-key-error]");

    function setError(text) {
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
      setError("");
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
      setError("");
      var value = input.value || "";
      if (!value.trim()) {
        setError("请先填写 API Key。");
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
          setError(err.message);
        })
        .then(function () {
          input.value = "";
        });
    }

    function clearKey() {
      setError("");
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
          setError(err.message);
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
})();
