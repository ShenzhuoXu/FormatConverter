/* FormatConverter single-page UI controller.
 *
 * Hard rules:
 *  - No local persistent storage and no third-party scripts/styles/fonts/images.
 *  - fetch() only ever targets same-origin relative paths (the local API).
 *  - No API-key input of any kind; AI key state is inferred from job results.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 1000;

  var KEY_WARNING = "⚠ 未检测到可用 Key（请检查服务器环境变量 ORCAROUTER_API_KEY）";
  var KEY_OK = "✓ Key 可用";

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  function delay(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
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

  function isKeyFailure(message) {
    return /Missing API key|ORCAROUTER_API_KEY|rejected the API key/i.test(message);
  }

  onReady(function () {
    document.querySelectorAll(".card[data-job-type]").forEach(function (card) {
      initCard(card);
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
    var keyStatusEl = card.querySelector("[data-key-status]");

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
      link.textContent = "下载 ZIP";
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
              if (keyStatusEl) {
                keyStatusEl.textContent = KEY_OK;
                keyStatusEl.className = "key-status ok";
              }
              showDownloadLink();
              return;
            }
            if (status === "failed") {
              done = true;
              setBusy(false);
              var message = data && data.message ? data.message : "任务失败，无详细消息。";
              setStatus("失败", "failed");
              setError("任务失败：" + message);
              if (keyStatusEl && isKeyFailure(message)) {
                keyStatusEl.textContent = KEY_WARNING;
                keyStatusEl.className = "key-status warn";
              }
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
})();
