# AI Task Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve FormatConverter AI proofreading so long Markdown jobs have retry, visible chunk progress, durable checkpoints, and resumable execution after transient failure or service restart.

**Architecture:** Keep FormatConverter as a deterministic document pipeline, not a full agent loop. Add reliability first inside the AI client/orchestration boundary, then introduce a disk-backed AI job store that persists input chunks and per-chunk outputs using atomic writes. The Web API remains localhost-only and must continue to avoid exposing output paths or API keys.

**Tech Stack:** Python standard library, OpenAI-compatible SDK already wrapped by `format_converter.llm_client`, static HTML/CSS/JavaScript, pytest offline fake clients.

**Spec:** Referenced ChatGPT conversation `增强AI任务可靠性` plus current project status in `docs/implementation-status.md`.

## Global Constraints

- Do not save API keys in manifests, checkpoints, browser storage, logs, responses, or test fixtures.
- Keep Web service bound to `127.0.0.1` only; do not add CORS headers.
- Keep `ai-clean` optional and non-default; no real network calls in tests.
- Preserve existing CLI contracts unless a task explicitly changes and tests the CLI behavior.
- Keep chunk splitting stable for a created durable job by writing chunk text to disk at job creation time.
- Use atomic writes for every checkpoint artifact that may be read during resume.
- Treat `ConnectionFailedError`, `RateLimitError`, and HTTP 5xx-style `ServerError` as retryable; treat authentication, permission, malformed input, unsupported provider/model, overwrite, encoding, and chunk-too-large failures as non-retryable.

---

## Current Project Map

- Modify `format_converter/llm_client.py`: expose retry classification without importing SDK types outside this module.
- Modify `format_converter/ai_cleaner.py`: add chunk progress callback and retry wrapper around each `client.complete()` call.
- Modify `format_converter/jobs.py`: add progress fields, richer AI task states, and later route `ai-clean` through the durable job runner.
- Create `format_converter/ai_jobs.py`: durable AI job manifest, chunk/result/final paths, atomic JSON/text writes, resume planner.
- Modify `format_converter/web_server.py`: include progress metadata in job status/list responses; add resume/retry endpoints after durable jobs exist.
- Modify `format_converter/web/static/app.js`: render `AI 校对中 · N / M`, `interrupted`, `merging`, and resume/retry actions.
- Modify `format_converter/web/static/index.html` and `styles.css`: add compact controls for continue/retry/delete only when needed.
- Add/modify tests in `tests/test_llm_client.py`, `tests/test_ai_cleaner.py`, `tests/test_jobs.py`, `tests/test_web_server.py`, and `tests/test_web_ui.py`.
- Update `README.md`, `CHANGELOG.md`, `docs/implementation-status.md`, and `docs/verification-checklist.md` after each accepted step.

---

### Task 1: Step 4.2 Retry Classification And Chunk Progress

**Files:**
- Modify: `format_converter/llm_client.py`
- Modify: `format_converter/ai_cleaner.py`
- Modify: `format_converter/jobs.py`
- Modify: `format_converter/web_server.py`
- Modify: `format_converter/web/static/app.js`
- Test: `tests/test_llm_client.py`
- Test: `tests/test_ai_cleaner.py`
- Test: `tests/test_jobs.py`
- Test: `tests/test_web_server.py`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Produces: `is_retryable_llm_error(exc: BaseException) -> bool`
- Produces: `ChunkProgress = Callable[[int, int], None]`
- Produces: `clean_markdown_with_ai(..., max_attempts: int = 4, backoff_seconds: Sequence[float] = (1.0, 2.0, 4.0), progress: ChunkProgress | None = None, sleep: Callable[[float], None] = time.sleep) -> str`
- Produces: `JobResult.current: int = 0`, `JobResult.total: int = 0`

- [ ] **Step 1: Write failing retry classification tests**

```python
from format_converter.llm_client import (
    AuthenticationError,
    ConnectionFailedError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    is_retryable_llm_error,
)

def test_retryable_llm_errors_are_classified():
    assert is_retryable_llm_error(ConnectionFailedError("network"))
    assert is_retryable_llm_error(RateLimitError("429"))
    assert is_retryable_llm_error(ServerError("500"))

def test_permanent_llm_errors_are_not_retryable():
    assert not is_retryable_llm_error(AuthenticationError("401"))
    assert not is_retryable_llm_error(PermissionDeniedError("403"))
```

- [ ] **Step 2: Write failing AI cleaner retry/progress tests**

```python
def test_retries_retryable_chunk_failure_then_continues():
    client = FlakyClient([ConnectionFailedError("network"), "fixed"])
    sleeps = []
    result = clean_markdown_with_ai(
        "Alpha.",
        client,
        model="m1",
        max_attempts=2,
        backoff_seconds=(0.25,),
        sleep=sleeps.append,
    )
    assert result == "fixed"
    assert sleeps == [0.25]
    assert len(client.calls) == 2

def test_reports_progress_after_each_successful_chunk():
    progress = []
    client = EchoClient()
    clean_markdown_with_ai(
        "One.\n\nTwo.",
        client,
        model="m1",
        max_chars=6,
        progress=lambda current, total: progress.append((current, total)),
    )
    assert progress == [(1, 2), (2, 2)]
```

- [ ] **Step 3: Implement `is_retryable_llm_error`**

```python
def is_retryable_llm_error(exc: BaseException) -> bool:
    return isinstance(exc, (ConnectionFailedError, RateLimitError, ServerError))
```

- [ ] **Step 4: Implement retry in `clean_markdown_with_ai`**

```python
for index, chunk in enumerate(chunks, start=1):
    attempts = 0
    while True:
        attempts += 1
        try:
            revised_chunk = client.complete(system=system_prompt, user=chunk, model=model)
            revised.append(revised_chunk)
            if progress is not None:
                progress(index, len(chunks))
            break
        except Exception as exc:
            if attempts >= max_attempts or not is_retryable_llm_error(exc):
                raise
            sleep(backoff_seconds[min(attempts - 1, len(backoff_seconds) - 1)])
```

- [ ] **Step 5: Add job progress storage**

```python
@dataclass(frozen=True)
class JobResult:
    job_id: str
    status: JobStatus
    message: str
    output_paths: tuple[Path, ...]
    job_type: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    current: int = 0
    total: int = 0
```

- [ ] **Step 6: Wire AI progress callback in `_handle_ai_clean`**

```python
def progress(current: int, total: int) -> None:
    update_progress(current, total, f"AI 校对中 · {current} / {total}")
```

Pass the callback only for `ai-clean` jobs. Existing convert/clean/pipeline jobs should keep `current=0,total=0`.

- [ ] **Step 7: Include progress in Web responses**

```python
{
    "job_id": result.job_id,
    "status": result.status.value,
    "message": message,
    "current": result.current,
    "total": result.total,
}
```

- [ ] **Step 8: Render chunk progress in the frontend**

```javascript
if (data.job_type === "ai-clean" && data.total > 0 && data.status === "running") {
  setStatus("AI 校对中 · " + data.current + " / " + data.total, "running");
}
```

- [ ] **Step 9: Run focused verification**

Run: `pytest tests/test_llm_client.py tests/test_ai_cleaner.py tests/test_jobs.py tests/test_web_server.py tests/test_web_ui.py`

Expected: all tests pass offline, with no real network calls.

- [ ] **Step 10: Run full verification**

Run: `pytest`

Expected: all tests pass.

---

### Task 2: Step 4.3 Durable Chunk Checkpoint

**Files:**
- Create: `format_converter/ai_jobs.py`
- Modify: `format_converter/jobs.py`
- Modify: `format_converter/web_server.py`
- Modify: `.gitignore`
- Test: `tests/test_ai_jobs.py`
- Test: `tests/test_jobs.py`
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: `split_into_chunks(text, max_chars) -> tuple[list[str], list[str]]`
- Produces: `AIJobStore(root: Path)`
- Produces: `AIJobStore.create_job(input_path: Path, text: str, provider: str, model: str, max_chars: int) -> AIJobManifest`
- Produces: `AIJobStore.load(job_id: str) -> AIJobManifest`
- Produces: `AIJobStore.next_unfinished(job_id: str) -> int | None`
- Produces: `AIJobStore.save_result(job_id: str, index: int, text: str) -> None`
- Produces: `AIJobStore.merge(job_id: str) -> Path`

- [ ] **Step 1: Write manifest creation tests**

```python
def test_create_job_writes_manifest_input_chunks_and_separators(tmp_path):
    store = AIJobStore(tmp_path / ".formatconverter-jobs")
    manifest = store.create_job(
        input_path=tmp_path / "doc.md",
        text="One.\n\nTwo.",
        provider="orcarouter",
        model="m1",
        max_chars=6,
    )
    job_dir = store.job_dir(manifest.job_id)
    assert (job_dir / "manifest.json").is_file()
    assert (job_dir / "input.md").read_text(encoding="utf-8", newline="") == "One.\n\nTwo."
    assert (job_dir / "chunks" / "0001.txt").is_file()
    assert (job_dir / "chunks" / "0002.txt").is_file()
    assert "api_key" not in (job_dir / "manifest.json").read_text(encoding="utf-8").lower()
```

- [ ] **Step 2: Write resume tests that distrust stale `completed_chunks`**

```python
def test_next_unfinished_uses_result_files_not_only_manifest(tmp_path):
    store = AIJobStore(tmp_path / ".formatconverter-jobs")
    manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
    store.save_result(manifest.job_id, 1, "One fixed.")
    store.mark_completed_for_test(manifest.job_id, [1, 2])
    assert store.next_unfinished(manifest.job_id) == 2
```

- [ ] **Step 3: Implement atomic write helpers**

```python
def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
```

Use the same pattern for JSON with `json.dumps(..., ensure_ascii=False, indent=2)`.

- [ ] **Step 4: Implement job directory layout**

```text
.formatconverter-jobs/
  <job_id>/
    manifest.json
    input.md
    separators.json
    chunks/
      0001.txt
    results/
      0001.md
    final.md
```

- [ ] **Step 5: Implement manifest schema**

```json
{
  "job_id": "32 hex chars",
  "type": "ai-clean",
  "status": "running",
  "provider": "orcarouter",
  "model": "m1",
  "max_chars": 12000,
  "total_chunks": 8,
  "current_chunk": 4,
  "chunks": [
    {"index": 1, "chars": 11328, "status": "completed"}
  ],
  "created_at": 1788283560.0,
  "updated_at": 1788283985.0
}
```

- [ ] **Step 6: Route Web `ai-clean` through durable jobs**

For Web `ai-clean`, use `.formatconverter-jobs` under the project root for checkpoint state, but continue writing downloadable final outputs inside the existing private Web job directory. The final output path remains the only path exposed through `JobResult.output_paths`.

- [ ] **Step 7: Preserve CLI behavior for now**

Keep CLI `ai-clean` on the existing direct function unless the implementation deliberately adds `--resume-job` later. This avoids changing single-file CLI semantics during the Web reliability step.

- [ ] **Step 8: Run durable job verification**

Run: `pytest tests/test_ai_jobs.py tests/test_jobs.py tests/test_web_server.py`

Expected: durable job creation, per-chunk writes, stale manifest recovery, and final merge pass offline.

---

### Task 3: Step 4.3 Service Restart Recovery

**Files:**
- Modify: `format_converter/ai_jobs.py`
- Modify: `format_converter/jobs.py`
- Modify: `format_converter/web_server.py`
- Modify: `format_converter/web/static/app.js`
- Test: `tests/test_ai_jobs.py`
- Test: `tests/test_jobs.py`
- Test: `tests/test_web_server.py`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: `AIJobStore.load(job_id)`
- Produces: `AIJobStore.scan_recent(limit: int = 20) -> list[AIJobManifest]`
- Produces: `AIJobStore.mark_stale_running_interrupted() -> None`
- Produces: `POST /api/jobs/{id}/resume`

- [ ] **Step 1: Write restart recovery tests**

```python
def test_startup_turns_stale_running_jobs_into_interrupted(tmp_path):
    store = AIJobStore(tmp_path / ".formatconverter-jobs")
    manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
    store.update_status(manifest.job_id, "running")
    store.mark_stale_running_interrupted()
    assert store.load(manifest.job_id).status == "interrupted"
```

- [ ] **Step 2: Hydrate recent durable AI jobs at `JobManager` startup**

```python
class JobManager:
    def __init__(self, ai_job_store: AIJobStore | None = None) -> None:
        self._ai_job_store = ai_job_store
        if self._ai_job_store is not None:
            self._ai_job_store.mark_stale_running_interrupted()
            self._load_ai_job_snapshots()
```

- [ ] **Step 3: Implement resume endpoint**

```python
elif len(parts) == 2 and parts[0] and parts[1] == "resume":
    self._handle_resume_job(handler, parts[0])
```

Resume should require a known durable `ai-clean` job in `interrupted` state and should not accept arbitrary paths from the client.

- [ ] **Step 4: Make resume skip completed result files**

```python
index = store.next_unfinished(job_id)
while index is not None:
    chunk = store.read_chunk(job_id, index)
    result = client.complete(system=SYSTEM_PROMPT, user=chunk, model=model)
    store.save_result(job_id, index, result)
    index = store.next_unfinished(job_id)
final = store.merge(job_id)
```

- [ ] **Step 5: Add frontend `继续处理` for interrupted jobs**

```javascript
if (job.status === "interrupted") {
  var resumeBtn = document.createElement("button");
  resumeBtn.type = "button";
  resumeBtn.className = "job-action";
  resumeBtn.textContent = "继续处理";
  resumeBtn.addEventListener("click", function () { resumeJob(job.job_id); });
  li.appendChild(resumeBtn);
}
```

- [ ] **Step 6: Verify restart scenario**

Run: `pytest tests/test_ai_jobs.py tests/test_jobs.py tests/test_web_server.py tests/test_web_ui.py`

Expected: stale running durable jobs appear as `interrupted` after a new manager/server is created, and resume continues from the first missing result file.

---

### Task 4: Step 4.4 Job Management Polish

**Files:**
- Modify: `format_converter/ai_jobs.py`
- Modify: `format_converter/jobs.py`
- Modify: `format_converter/web_server.py`
- Modify: `format_converter/web/static/app.js`
- Modify: `format_converter/web/static/styles.css`
- Test: `tests/test_ai_jobs.py`
- Test: `tests/test_jobs.py`
- Test: `tests/test_web_server.py`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Produces: `JobStatus.cancelled`
- Produces: `DELETE /api/jobs/{id}` for cleanup
- Produces: `POST /api/jobs/{id}/retry` for a fresh rerun, not overwrite-in-place

- [ ] **Step 1: Add `cancelled` only for durable jobs**

```python
class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    interrupted = "interrupted"
    merging = "merging"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
```

- [ ] **Step 2: Define terminal status behavior**

```python
_TERMINAL_STATUSES = frozenset({
    JobStatus.succeeded,
    JobStatus.failed,
    JobStatus.interrupted,
    JobStatus.cancelled,
})
```

- [ ] **Step 3: Add retry as a new job**

`POST /api/jobs/{id}/retry` should create a new Web job using the original durable job input and current provider/model settings. It must not overwrite the old job directory or old manifest.

- [ ] **Step 4: Add delete cleanup**

`DELETE /api/jobs/{id}` should delete only a validated durable AI job directory and remove the in-memory snapshot. Keep the existing `cleanup_job()` path-safety checks for Web temp directories.

- [ ] **Step 5: Render action matrix**

```text
completed: 下载结果
interrupted: 继续处理, 重新处理
failed: 重新处理
cancelled: 重新处理, 删除
```

- [ ] **Step 6: Verify management behavior**

Run: `pytest tests/test_ai_jobs.py tests/test_jobs.py tests/test_web_server.py tests/test_web_ui.py`

Expected: retry creates a new job id, delete cannot remove paths outside `.formatconverter-jobs`, and frontend static checks cover action labels.

---

## Recommended Execution Order

1. Ship Task 1 as Step 4.2. This is the smallest useful reliability improvement and should fix most transient `APIConnectionError` failures without changing storage.
2. Ship Task 2 as the first half of Step 4.3. This creates durable artifacts but does not yet require full UX polish.
3. Ship Task 3 as the second half of Step 4.3. This makes service restart recovery real.
4. Ship Task 4 only after Step 4.3 is stable. Cancel/delete/retry are useful, but they should not be mixed into the first durable checkpoint implementation.

## Self-Review

- Spec coverage: retry/backoff, retryable vs non-retryable errors, progress, durable manifest, atomic chunk/result writes, resume by checking result files, startup recovery, `interrupted` vs `failed`, `continue` vs `retry`, stable chunk numbering, and separate merge stage are all mapped to tasks.
- Placeholder scan: no open-ended placeholder steps remain; each task has concrete files, interfaces, test commands, and expected behavior.
- Type consistency: task interfaces consistently use `AIJobStore`, `AIJobManifest`, `current/total`, `interrupted`, `merging`, `succeeded`, `failed`, and `cancelled`.
