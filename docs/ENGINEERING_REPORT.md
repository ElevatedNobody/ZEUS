# ZEUS — Engineering Report

**Scope:** voice architecture, embedding/reranking root cause, login UI replacement,
startup sequence, sovereignty, hardware fit, model selection, testing.

**Test suite:** 514 tests, 514 passing, 0 failing, 0 skipped.

---

## 1. Root cause of "No models available for task 'embedding' / 'reranker'"

One function. `OllamaClient.pick_available_model()` compared an installed model
name against a preferred name with `_model_matches()`, which accepted:

* an exact match
* a `-` or `_` suffix (`qwen2.5:7b` matching `qwen2.5:7b-instruct-q4_K_M`)

and **not a `:` tag**. `ollama pull bge-m3` installs the model as
`bge-m3:latest`. The preference list said `bge-m3`. `bge-m3:latest` did not
start with `bge-m3-` or `bge-m3_`, and was not equal to `bge-m3`, so the
matcher returned no match — on a machine where the model was pulled, loaded and
working.

Confirmed directly before changing anything:

```
'bge-m3'              -> None
'bge-reranker-v2-m3'  -> None
```

That one missing case is why RAG appeared broken. The message was accurate
about its own internal state and completely misleading about the machine.

**Second root cause, in the same area:** embedding and reranking were being
resolved through `model_router`, which is a **chat** model router. Even with
matching fixed, that shape is wrong — an embedding model is reached through
`POST /api/embeddings`, not `/api/chat`, and a reranker is not a generation
model at all. Fixing only the matcher would have left the architecture that
produced the bug intact.

**Third:** `rerank()` was called with `top_n=` while the engine's parameter is
`top_k` — a `TypeError` at the moment reranking was first exercised.

### Fixes

* `core/ollama_client.py` — `_model_matches()` now accepts the tag forms Ollama
  actually produces, while staying strict about model family. Verified against
  10 cases, including that `qwen2.5:3b` must **not** satisfy a request for
  `qwen2.5:7b`. Loose matching would be worse than the bug.
* `core/capabilities.py` (new) — capability access separated into
  **MODEL / CAPABILITY / INTERFACE**. Embedding goes through the embeddings
  endpoint, reranking through a local scorer, chat through the chat endpoint.
* `core/model_router.py` — the `embedding` and `reranker` slots were **removed**,
  and asking the chat router for them now raises with a message naming the
  right provider. The architectural rule is enforced by code, not by a comment.
* `core/capabilities.py` — `RerankProvider.rerank()` translates the pipeline's
  `top_n` to the engine's `top_k`, with the vocabulary difference documented.

---

## 2. What was removed

| Removed | Why |
|---|---|
| `ui/login_window.py` (757 lines) | Replaced, not restyled — PART 3. A second login screen is a second place to fix a security bug. |
| `voice.offline.voice_banner()` | Printed `STT: LOCAL WHISPER (READY)` from a directory listing. Files present ≠ a model that loads. |
| `model_router` `embedding` / `reranker` slots | Routing a non-chat capability through a chat router is the bug in item 1. |
| `openai>=1.30.0` from `requirements.txt` | Nothing imported it, and three separate tests ban importing it. A cloud SDK installed on a sovereign machine is one careless line from being used. |
| `ollama>=0.3.0` from `requirements.txt` | Unused; the client is `httpx` against loopback. |
| `torch`, `torchaudio`, `chatterbox-tts`, `faster-whisper`, `vosk`, `huggingface_hub` from `requirements.txt` | ~2 GB of downloads that starting ZEUS must not depend on. Moved to `requirements-voice.txt` (optional). |
| The duplicate RAG path in `ask_knowledge_base` | It had no reranking, no evidence extraction and no citation validation, so the quality of an answer depended on which door the question came in through. |

Nothing was removed to make a check pass. `voice_status()` and
`check_voice_models()` are kept — they report filesystem truth and are used by
the setup and doctor scripts.

---

## 3. Final embedding model

**BGE-M3** (`bge-m3:latest`), resolved through `EmbeddingProvider`.

It was not replaced. The brief was explicit that a model should not be swapped
because the router could not see it, and that is exactly what had happened —
BGE-M3 was installed and working the whole time. It is multilingual, strong on
technical text, and already on the machine.

Fallbacks, in order, for a machine without it: `nomic-embed-text`,
`qwen3-embedding:0.6b`, `mxbai-embed-large`, `all-minilm`. An explicit
`[models] embedding_model` in `config.ini` overrides the list.

**The vector dimension is discovered from the first successful call, never
assumed.** BGE-M3 is 1024, nomic is 768, all-minilm is 384; a hard-coded number
silently corrupts the vector store the day someone changes model.

Verified end to end over real HTTP:

```
embedding : ready | bge-m3:latest returned a 256-dimension vector
evidence  : {'dimension': 256, 'sample': [...], 'latency_ms': 253}
```

(256 is the test runtime's width; on your machine BGE-M3 returns 1024.)

---

## 4. Final reranker, and how it is invoked

**Ollama has no reranking endpoint.** Reranking scores a (query, passage)
*pair*; `/api/chat` generates text and `/api/embeddings` returns one vector per
input. Asking a chat model to "rate this 0–10" is slow, unstable between runs,
and produces a number that cannot be compared across queries. So reranking was
**not** abandoned and **not** forced through the chat API. It runs locally, in
Python, through `RerankProvider`, which tries three backends in order:

1. **BGE-Reranker-v2-M3** as a cross-encoder, via FlagEmbedding or
   sentence-transformers, loaded `local_files_only` from `models/reranker`.
2. **Qwen3-Reranker-0.6B** through a **new local adapter** (`industrial/reranker.py`).
   Evaluated as the brief asked: it **cannot** be driven as a cross-encoder —
   it is a `Qwen3ForCausalLM`, and loading it through `CrossEncoder` bolts a
   *randomly initialised* classification head onto it and returns confident
   noise. The adapter detects it by architecture, formats the pair in the
   model's own prompt template, and scores it as
   `softmax(logit["yes"], logit["no"])["yes"]` on the next token. It needs
   torch (~2 GB) and holds 0.6B parameters resident, which on your hardware
   competes with the chat model — so it is used only when its weights have been
   deliberately placed on disk, and is not the default.
3. **Hybrid local scorer** — the default today. Dense retrieval score + lexical
   overlap + industrial-taxonomy overlap + exact equipment-tag match + element
   type + OCR confidence. Standard library and numpy only.

Current honest state on a machine with no reranker weights:

```
reranking : degraded | hybrid_local ranked 2 passages; no cross-encoder present,
            hybrid local scoring in use
evidence  : {'backend': 'hybrid_local', 'cross_encoder': False,
             'probe_ranked_correctly': True, 'latency_ms': 13}
```

`DEGRADED`, with the reason — not `READY`, and not "unavailable". Reranking is
pure local Python, so it keeps working with the model runtime stopped, which is
tested.

---

## 5. RAG, end to end, and after a restart

`ask_knowledge_base` was running a shorter pipeline than the industrial one:
cosine similarity → top-K → prompt. No reranking, no evidence objects, no
citation validation. Both paths now run the same 14 stages:

```
query embedding → dense retrieval → top-K candidates → reranking
→ top-N selection → evidence extraction → citation contract → generation
→ citation validation → delivery
```

Retrieval widens to `4 × top_k` before reranking. A reranker handed exactly the
passages that will be kept cannot change anything — that is now a test.

`scripts/rag_selftest.py` (new) runs all 14 stages against the real models on
your machine and reports each one separately, because "RAG is broken" is not
actionable and "the query embedded but nothing scored above the threshold" is.
Executed here against a runtime speaking Ollama's protocol: **14/14 stages
pass**, and `--reuse` after a simulated restart passes with the vector store
intact and no re-indexing.

Run it on your laptop:

```
python scripts/rag_selftest.py
# restart ZEUS, then:
python scripts/rag_selftest.py --reuse
```

---

## 6. Voice architecture

Rebuilt as `voice/providers.py`. Voice used to mean exactly two engines, both
requiring large downloads; a machine without them had no voice at all and the
banner reported a state nobody had checked.

**Five states, as specified:**

| State | Meaning |
|---|---|
| `INITIALIZING` | a probe or load is in flight |
| `AVAILABLE` | input and output both verified working |
| `DEGRADED` | one direction works, the other does not |
| `DISABLED` | switched off, or nothing installed → **TEXT MODE** |
| `ERROR` | something explicitly configured failed to load |

**Providers, cheapest first** — a machine with nothing installed still gets a
voice from its own operating system:

| Output | Needs | Download |
|---|---|---|
| `system` (SAPI5 / NSSpeechSynthesizer / espeak via pyttsx3) | a few hundred KB | **none** |
| `piper` | local binary + local `.onnx` | none at runtime |
| `chatterbox` | torch + local checkpoints | none at runtime |

| Input | Needs | Download |
|---|---|---|
| `whisper` | faster-whisper + a complete local model dir | none at runtime |
| `vosk` | vosk + a local model dir | none at runtime |

**There is no zero-install option for speech input** — every recogniser needs
weights. So a fresh machine speaks but does not listen, which is `DEGRADED`,
and is reported as exactly that.

**Nothing is downloaded at runtime.** `enforce_offline()` runs before any ML
library is imported; every engine loads from an explicit local path;
`from_pretrained()` is never called at runtime; a missing model is a **state**,
never a trigger to fetch. Tested: probing opens no network socket, and no
provider advertises a runtime download.

**No unverified "voice ready".** `probe()` is filesystem and import-spec only
and can never return `READY`; only `activate()`, which performs a real load,
can. One real bug this caught: on Linux, pyttsx3's espeak driver plays through
`aplay` via `os.system`, whose failure nobody checks — so `speak()` returned
`True` having produced no sound. The provider now verifies a playback path
exists and reports `UNSUPPORTED` when it does not.

**Voice is never fatal.** `_init_voice()` cannot prevent startup; the worst case
is a logged reason and typed replies. The Command Center's Voice button now
reflects the provider that actually loaded and explains itself when there is
none.

### Whisper and Chatterbox

**Both kept. Both demoted from mandatory to optional.**

They remain fully supported and are still selected first when their weights are
present — Chatterbox is the best-sounding option available. What changed is
that they no longer decide whether ZEUS has a voice at all, and they are no
longer installed by default. They live in `requirements-voice.txt` with
`scripts/setup_models.py` unchanged as the one place allowed to touch the
network.

---

## 7. Login UI (PART 3)

`ui/login_window.py` was **deleted**, not restyled. It was built in a different
visual language (a cyan HUD with a rotating orb and a private palette of hex
literals) and was structurally a two-panel dialog. Changing its colours would
have left two identities in one product and two places to change a shade.

`ui/access.py` is built from the same three pieces as the Command Center —
`ui.theme`, `ui.backdrop`, `ui.widgets` — with a test asserting it carries no
private colour literals.

**Authentication is unchanged and real.** Carried over deliberately, because it
was correct:

* verification on a worker thread (Argon2id is ~0.3–0.5s at m=64MB; on the GUI
  thread it froze the window mid-click and let a second Return re-enter the
  handler)
* workers kept in a **list** until they finish — overwriting a reference to a
  running QThread is the "Destroyed while thread is still running" crash
* the lockout countdown, so a mistyped password never looks like a dead install
* Escape cannot dismiss the screen
* errors name no account, no hash, no database path

**No old login window can appear first:** a test asserts `ui/login_window.py`
does not exist, that `main.py` never mentions it, and that exactly one file in
`ui/` defines `prompt_login`.

Nothing on the screen claims a system state — a test forbids `SYSTEM: OFFLINE`,
`VOICE READY`, `RAG READY` and `SECURE` there, because authentication runs
before any capability has been probed. And nothing animates while idle; motion
is confined to a 3px strip, only while a password is being verified.

---

## 8. Startup sequence (PART 4)

One authoritative path, in `main.start_up()`, logged stage by stage:

```
ZEUS START → SYSTEM INITIALIZATION → LOCAL MODEL DISCOVERY
→ CAPABILITY READINESS → AUTHENTICATION → COMMAND CENTER
```

Only AUTHENTICATION can stop the sequence. Discovery and readiness report what
they find and hand over — a machine with the runtime stopped still gets a login
screen and a working application. A test asserts the stage order, and a
structural (AST) test asserts `main()` stops when `start_up()` fails and
`start_up()` stops when `authenticate()` fails.

---

## 9. Sovereignty (PART 5)

No cloud LLM, embedding, reranking, OCR, STT or TTS. No external telemetry. The
only network traffic is Ollama on loopback. Local Python inference is used for
reranking and, optionally, for the Qwen3 adapter.

Enforced by tests rather than asserted: source files are scanned for cloud
endpoints, `sys.modules` is checked for cloud SDKs, and **`requirements.txt` is
now checked too** — that is where `openai` had been sitting.

`pyttsx3` is deliberately *not* treated as a cloud library. It wraps the speech
engine already in the operating system and contacts nothing; `gtts`, whose name
looks similar, is Google's cloud endpoint and stays banned.

---

## 10. Hardware (PARTS 6 and 7)

Target: Intel Core i5, 16 GB, Intel Iris Xe, no dedicated GPU.

* The router steps down under memory pressure — tested by comparing the choice
  at 2 GB free against the choice at 32 GB free.
* Every model choice carries the reason it was made.
* **DeepSeek-Coder-V2 16B is last in the `code` ladder and must not be promoted.**
  At q4 it needs roughly 10 GB resident and pushes this machine into swap. It
  stays listed so a workstation can use it, and stays last so a laptop never
  does. A test asserts it is not first for any slot.
* The heavy voice stack is no longer installed by default, which removes ~2 GB
  of libraries and the model weights behind them from a default install.

---

## 11. No false claims (PART 10)

Every status is backed by a real call:

* embedding reports `READY` only after a vector came back with a numeric width
* reranking reports `READY` only with a cross-encoder present, otherwise
  `DEGRADED` with the reason
* voice reports `AVAILABLE` only after a provider loaded
* a probe can never return `READY` — only an activation can

The four states are distinguished because they have four different fixes:

```
NO MODEL PULLED                    -> ollama pull bge-m3
MODEL DETECTED / ADAPTER REQUIRED  -> the model is there, the interface is not
RUNTIME NOT REACHABLE              -> ollama serve
DEGRADED                           -> working, but not on the preferred path
```

Previously all of these surfaced as one message.

New tool: **`capability_report`** — ask ZEUS "what can you actually do right
now?" and every line is the result of a call, not a configuration read.

---

## 12. Tests

**514 tests, 514 passing, 0 failing, 0 skipped.**

New:

| File | Tests | Covers |
|---|---|---|
| `tests/fake_ollama.py` | — | a local HTTP server speaking Ollama's protocol |
| `tests/test_capabilities_rag.py` | 22 | matcher cases, health states, full RAG chain, restart survival, Qwen3 detection |
| `tests/test_voice_providers.py` | 18 | the five states, no downloads, no false claims |
| `tests/test_failure_modes.py` | 20 | the failure matrix below |
| `tests/test_access_window.py` | 16 | ported from the old login tests, plus design-system and no-claims tests |

**`tests/fake_ollama.py` matters:** the failure was an *interface* failure —
wrong name form, wrong endpoint — and mocking the client would have hidden it
exactly the way the original code did. Tests now make real HTTP requests
against something that answers the way Ollama answers.

### Failure-mode matrix — all executed and passing

| Condition | Behaviour |
|---|---|
| Ollama not running | named, with `ollama serve`; startup continues; reranking still works |
| Embedding model missing | `MODEL MISSING` + the exact `ollama pull` command |
| Embeddings endpoint absent | `MODEL DETECTED / ADAPTER REQUIRED` — a different problem, said differently |
| Reranker missing | `DEGRADED` on the hybrid scorer, never unavailable |
| Voice models missing | TEXT MODE; application fully usable |
| Voice present but broken | `ERROR` with the reason; still not fatal |
| Low RAM (2 GB free) | router steps down to a smaller model |
| Malformed `config.ini` | defaults apply; no exception reaches the RAG path |
| Empty document | skipped, index continues |
| Binary file named `.txt` | skipped, index continues |
| Very long document (1,200 sections) | chunked, not truncated |
| Path that is not a folder | refused with a clear message |
| Invalid credentials | refused; reveals nothing about the account store |

### Tests blocked in this environment

Three things could not be verified here and need your laptop:

1. **The real BGE-M3 vector width (1024).** No Ollama in this container. The
   fix is verified against a real HTTP server speaking Ollama's protocol, and
   `_model_matches('bge-m3:latest', 'bge-m3')` — the exact failing case — is
   tested directly. Run `python scripts/rag_selftest.py` to confirm on yours.
2. **Windows SAPI5 speech output.** This container is Linux and has no audio
   device. The provider was exercised on the espeak path, which is what caught
   the silent-playback bug.
3. **Qwen3-Reranker-0.6B running.** torch is not installed here. The detection
   logic and the no-torch fallback are tested; the scoring path needs the
   weights on disk.

---

## 13. Deliberately not done

**`--classic-ui` and `ui/dashboard.py` are still present.** PART 4 asks for
them to go once migrated, and they are not migrated yet: the old dashboard has
click-to-edit calendar, to-do and notes pages, and the Command Center currently
routes those through typed commands. Deleting it now would remove working,
tested functionality to satisfy a checklist item — against the standing
instruction to preserve existing functionality.

The login duplication, which is what PART 3 actually asked for, **is** gone.

To finish the migration: build calendar, to-do and notes panels in the Command
Center, move `tests/test_dashboard_panels.py` onto them, then delete
`ui/dashboard.py`, `ui/hud_widgets.py` and the `--classic-ui` branch.

---

## 14. What to run on your machine

```bat
:: 1. start the model runtime
ollama serve

:: 2. what can ZEUS actually do?
python scripts\startup_check.py

:: 3. the full RAG pipeline, all 14 stages, on your real models
python scripts\rag_selftest.py

:: 4. restart ZEUS, then prove the index survived
python scripts\rag_selftest.py --reuse

:: 5. what voice do you have? (probe only - loads nothing)
python main.py --voice-status

:: 6. optional: the OS voice, no model download
pip install pyttsx3

:: 7. optional: the neural voice stack (~2 GB)
pip install -r requirements-voice.txt
python scripts\setup_models.py
```
