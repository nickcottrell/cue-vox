# Security red-team report

**Scope:** `cue-vox` at commit `21db630` (`work` branch), reviewed 2026-08-08.
**Mode:** report-only static review; no production code was changed and no live exploit was run.

## Executive summary

The web service should be treated as a **local unauthenticated remote-control endpoint for the
Claude CLI**, not merely as a microphone UI. It binds to loopback, which limits direct network
exposure, but every Socket.IO action is unauthenticated and Socket.IO explicitly accepts every
origin. A hostile web page opened by the same user can therefore attempt a cross-site WebSocket /
long-polling connection to the loopback service and submit prompts. Browser Private Network Access
behavior is not a security boundary.

The reviewed revision does **not** invoke `claude -p --allowedTools`; every call site invokes plain
`claude`. Consequently, the proposed `--allowedTools` boundary does not exist in this tree. More
generally, Claude CLI's `--allowedTools` option must not be interpreted as a deny-list or sandbox:
it identifies tools allowed without an approval prompt. It does not revoke ambient filesystem,
process, credential, configuration, plugin/MCP, or inherited-environment access, and behavior for
other tools is controlled by the CLI's permission mode and settings. A crafted transcript cannot
rewrite the already-parsed process arguments, but it can prompt the agent to use every capability
that the launched CLI already has. Only OS/container isolation plus an explicit deny policy is a
hard boundary.

The structured-card syntax is also presentation, not authorization. Arbitrary assistant output is
parsed for `[APPROVAL:]`, `[INPUT:]`, and `[YES_NO:]` markers without provenance, nonce, or
server-side pending-action state. A user prompt, prior log entry, compromised model/tool output, or
prompt-injected document can make a convincing approval card. Clicking it sends ordinary text back
to Claude; it does not approve a server-held action. Worse, Claude is launched before the card is
rendered and may already possess permission to perform the represented action.

## Findings and recommended fixes

| ID | Severity | Area | Finding / attack path | Impact | Recommended fix |
|---|---|---|---|---|---|
| CV-01 | **Critical** | Socket.IO authentication | `/` and all Socket.IO handlers accept unauthenticated clients. `text_message`, `audio_data`, `button_response`, `approval_response`, and `input_response` can each reach a Claude subprocess; `interrupt` can kill speech. There is no user/session binding or authorization check. | Any process that can reach the port can spend model quota, drive the agent, alter files if Claude is permitted, poison logs/tokens, and obtain Claude output. | Generate a high-entropy per-launch secret, require it during the Socket.IO handshake and on HTTP requests, reject before handlers run, bind each pending operation to the authenticated session, rate-limit, and cap concurrent Claude jobs. Prefer a Unix socket or authenticated native bridge for a personal local tool. |
| CV-02 | **High** | Cross-origin access | `SocketIO(..., cors_allowed_origins="*")` permits hostile origins. Loopback binding prevents ordinary remote TCP access but does not prevent a malicious web origin, local malware, another local user, browser extension, or forwarded/proxied port from connecting. | Cross-site WebSocket hijacking can turn visiting a page into prompt execution against localhost. | Replace `*` with an exact loopback origin/port allowlist, validate `Origin` (including rejecting `null`), require the launch secret independently of Origin, disable unused polling transports, and set restrictive CSP/frame headers. Do not rely on CORS alone. |
| CV-03 | **Critical** | Claude tool boundary | The current command is `['claude']`, not `claude -p --allowedTools ...`. No tool allow/deny boundary is established by this application. Even if `--allowedTools` is added, it is an auto-approval allowlist, **not a hard deny** for unlisted tools. A transcript cannot inject argv because `shell=False` and argv is fixed, but prompt injection can request any ambiently available tool. | Depending on user/project Claude settings, an unauthenticated prompt may read secrets, execute commands, write files, or use configured MCP/plugin capabilities. Interactive/plain `claude` over piped stdin is also an ambiguous automation contract compared with a deliberately configured print-mode invocation. | Run a dedicated non-privileged OS user in a sandbox/container with a minimal read-only mount, empty home, no SSH/cloud/API credentials, controlled network, and resource/time limits. Use a reviewed fixed CLI configuration with explicit deny rules and non-interactive failure on permission requests. Treat `--allowedTools` only as convenience after verifying the exact installed CLI version; regression-test forbidden Read/Write/Bash/MCP actions. |
| CV-04 | **High** | Environment and working directory | `Popen` supplies no `env`, so Claude inherits the complete server environment (including credentials, proxies, `PATH`, and Claude configuration discovery variables). The working directory is the repo, or its parent when that parent contains `cuesheets`; the latter broadens project/settings discovery and likely filesystem context. The executable is resolved through inherited `PATH`. | Prompt injection can target ambient secrets/configuration; a malicious executable earlier in `PATH` can replace `claude`; parent-directory settings/instructions may silently expand permissions. | Use an absolute, pinned CLI path; construct a minimal environment allowlist; set an isolated `HOME`/config directory; remove credential-agent sockets and proxy variables; use a fixed dedicated working directory; prevent parent/project configuration discovery; document and test the resulting boundary. |
| CV-05 | **Critical** | Approval provenance / confused deputy | Assistant text is regex-parsed into actionable-looking cards. There is no trusted message envelope, server signature, action ID, nonce, expiry, authenticated session association, or server-side record of a pending action. The client returns attacker-controlled approval metadata, while current UI code actually routes an approval click through `text_message`. | Any content that influences model output can spoof an official approval, relabel the target/preview, replay it, or induce a user to click. The click is not authorization for a particular operation. | Never infer control messages from display text. Return structured data on a separate authenticated channel; have the server create an immutable pending action (`id`, normalized operation/arguments, digest, session, expiry); render only that object; accept a one-shot decision containing only its ID; execute exactly the stored action after approval; reject replay/mismatch. Clearly label untrusted model prose. |
| CV-06 | **Critical** | Approval timing | Claude is run with its ambient permissions before its output becomes an approval card. No server-side executor pauses a specific operation. An “Approve” response is merely another prompt asking Claude to proceed. | If Claude already has tool permission, prompt-injected work can occur before the user sees or clicks the gate. A denial cannot undo it. | Remove dangerous tool authority from the planning/model phase. Split planning from execution, make the server the policy-enforcing executor, and grant a narrowly scoped, single-use capability only after a verified decision. Default-deny when UI or client disconnects. |
| CV-07 | **High** | Stored prompt injection and cross-session isolation | Conversation logs, `session_variables`, `input_history`, speech state, and the TTS process are global rather than per Socket.IO session. Recent assistant/user content is inserted into later prompts as instructions. An unauthenticated client can poison context for the legitimate client, and concurrent handlers race over shared state. | Cross-client data leakage, confused approvals, prompt persistence, wrong-session interruption, and token/log corruption. | Partition all state by authenticated session/user; serialize or isolate jobs; label recalled content as untrusted data; do not concatenate it into instruction-bearing prompt sections; lock file writes and use atomic persistence. |
| CV-08 | **High** | Indirect token/file disclosure | There is no direct HTTP “list tokens/logs” route in this revision. However, an attacker who can submit a prompt can ask Claude to read accessible files, environment-derived material, logs, or `.claude/tokens`, and the response is emitted to that attacker's socket. Token files are written outside the repo at the parent `.claude/tokens` path. | Indirect exfiltration of conversation history, stored inputs, repository files, and credentials, governed only by Claude's ambient permissions. | Apply CV-01–CV-04; store sensitive state in a permission-restricted app directory rather than a shared Claude directory; minimize recorded content; encrypt or avoid persistence; never expose secrets to the model runtime unless essential. |
| CV-09 | **High** | UI injection | Most displayed values use `textContent`, but approval `action` and `description` are interpolated into `innerHTML`. Both originate in model-controlled JSON. A crafted marker can therefore inject HTML/event-capable markup into the page (subject to browser parsing/CSP, for which no restrictive policy is configured). | Same-origin script/HTML injection can spoof controls, steal the launch secret if one is later added insecurely, or drive sockets as the user. | Build the title from DOM nodes using `textContent`; never interpolate model data into HTML; add a restrictive CSP with no inline script; add malicious-marker rendering tests. |
| CV-10 | **High** | Audio upload / Whisper | `audio_data` trusts a caller-provided data URL, base64-decodes it without validation or size limit, writes arbitrary bytes to disk, and sends the file to Whisper's media stack. There is no duration/rate/concurrency limit. Cleanup occurs only on the success path. | Memory/CPU/disk exhaustion, orphaned temp files, model-download amplification, parser/decoder attack surface, and unauthenticated quota consumption. | Enforce authenticated sessions; cap encoded and decoded bytes before allocation; validate the data-URL media type and decoded audio container; decode/transcode in a resource-constrained sandbox; impose duration, timeout, rate, disk, and concurrency limits; always delete with `finally`; pre-provision the model. |
| CV-11 | **Medium** | TTS handling | TTS uses argv rather than a shell, so shell metacharacters in model output do not cause shell injection. Nevertheless, unsanitized model output becomes a `say` argument, option-like leading text is not separated with `--`, marker sanitization is incomplete, calls can overlap, and `killall say` terminates every `say` process owned/visible to the server rather than only this request's child. | Availability impact, speaking attacker-controlled or misleading content, unintended option interpretation depending on `say`, and interference with unrelated local processes. | Own one per-session child handle; terminate only that PID/process group; pass an option terminator or stdin supported by the TTS engine; cap text length/time; strip control characters; queue jobs; treat TTS as untrusted output and disable it for security prompts/secrets. |
| CV-12 | **Medium** | Input validation / error disclosure | Socket payloads are assumed to have specific shapes and ranges. `approval_response` accepts arbitrary decisions and caller-supplied action data; scalar/HSL structures and text lengths are not schema-validated. Exception strings are returned to the requesting client. | DoS via malformed/huge values, forged state transitions/tokens, log injection, and leakage of local paths or implementation details. | Define strict schemas, type/range/length limits, allowlisted enum values, and generic client errors with server-side correlation IDs. Reject unknown fields. |
| CV-13 | **Medium** | Process/resource management | Each eligible event synchronously starts Claude with no subprocess timeout, output cap, global concurrency bound, cancellation linkage, or guaranteed reaping/cleanup. Global interruption does not terminate Claude. | An unauthenticated client can create long-lived processes and exhaust CPU, memory, descriptors, quota, and disk. | Use a bounded job queue, one job per authenticated session, hard wall-clock/CPU/memory/output limits, process groups, disconnect cancellation, and `finally` cleanup. |
| CV-14 | **Low** | Flask secret | The Flask secret key is a hard-coded public string. No session cookie is currently used for authorization, so this is not the primary exploit, but any future signed session/CSRF feature would be forgeable. | Future authentication/session mechanisms can fail open if built on this key. | Generate a random per-install or per-launch secret outside source control and rotate it; fail startup if production-like deployment uses the default. |

## Attack answers

### 1. `claude -p --allowedTools`, prompt escalation, environment, and CWD

* **This commit does not use those flags.** All five web call sites and the desktop path execute
  plain `claude` using a fixed argv and `shell=False`.
* A transcript cannot add command-line flags or directly replace the executable through shell
  metacharacters. That is a clean property of the subprocess construction.
* The transcript can still prompt Claude to invoke whatever tools/configuration the CLI exposes.
  `--allowedTools` is not a denial boundary; it pre-approves matching tools. Do not conclude that
  omitted `Read`, `Write`, or `Bash` is forbidden without an explicit deny policy and an isolated
  runtime. Tool aliases, MCP tools, plugins/hooks, nested agents, project settings, and future CLI
  behavior must also be considered.
* The web process passes the entire environment implicitly and selects either this repo or its
  parent as `cwd`. Claude configuration/instructions and sensitive files discoverable from those
  locations therefore remain in scope. `PATH` also controls which `claude` binary runs.

**Conclusion:** crafted content cannot mutate argv, but it can escalate *use of ambient authority*.
The present code supplies no application-enforced tool sandbox.

### 2. Structured markers and spoofed approvals

Yes. The UI scans every assistant response for marker-shaped substrings and turns them into controls.
There is no provenance distinction between a marker deliberately emitted by the trusted orchestration
layer and one copied from user input, a repository file, a web page/tool result, conversation history,
or injected model output. The browser never receives a signed server-created action object.

The risk is more than visual spoofing:

1. `action` and `description` are inserted with `innerHTML`, creating an XSS sink.
2. The apparent approval does not bind the preview to an operation.
3. Approval clicks currently emit `text_message`, so the dedicated server approval handler is not
   the authority even nominally.
4. The model may perform an already-permitted action before producing its response/card.

### 3. Socket.IO and web routes

There is one HTTP route (`GET /`) and no direct token-download route. The Socket.IO surface has no
authentication or authorization, accepts all origins, and does not validate a session identity.
Any reachable client can trigger model work through multiple event types. It can read the responses
to its own emitted events and can request that Claude disclose accessible material. `emit()` without
broadcasting generally replies to the current Socket.IO client, which limits passive cross-client
response broadcast; this does **not** mitigate active unauthenticated prompting or shared global-state
leakage.

Binding to `127.0.0.1` is useful defense-in-depth and avoids direct LAN exposure in the default
launcher. It is not sufficient against hostile websites, local users/processes, port forwarding,
containers sharing the host network, or deployment behind a proxy.

### 4. Whisper and TTS

Audio is arbitrary unauthenticated, unbounded base64 decoded into a temporary file and parsed by
Whisper's dependent media stack. This is a straightforward DoS surface and unnecessarily exposes
native/decoder parsing to attacker bytes. Temp files leak on exceptions.

TTS avoids shell injection because it invokes `say` with a list and no shell. This is a clean area.
However, output remains attacker/model-controlled data passed to a command-line parser, the marker
sanitizer is not a security sanitizer, and global `killall say` is unsafe process ownership.

## Clean / lower-risk areas observed

* The default server bind is `127.0.0.1`, not `0.0.0.0`; this materially reduces direct network
  exposure, while not solving the browser/local attack paths above.
* Claude, `say`, and `killall` are invoked with argv arrays and `shell=False`; transcript shell
  metacharacters do not become shell syntax.
* Ordinary conversation text, input questions, targets, previews, and submitted values are mostly
  assigned through `textContent`, reducing DOM XSS exposure outside the identified approval-title
  `innerHTML` sink.
* The repository exposes no HTTP route that directly enumerates conversation logs or token files.
* Audio temporary files use unpredictable OS-created names rather than caller-selected paths.
* The server runs with Flask debug mode disabled by default.

These properties are defense-in-depth only and do not offset the unauthenticated agent-control path.

## Remediation order

1. **Stop the confused-deputy path:** authenticate every connection, enforce exact origins, and
   disable Claude-capable events until authentication succeeds.
2. **Remove ambient authority:** isolate the Claude process at the OS/container level, scrub its
   environment, pin executable/CWD/config, and enforce explicit denies.
3. **Replace marker approvals:** use server-created, session-bound, expiring, one-shot action objects
   and a policy-enforcing executor; never parse authorization out of prose.
4. **Eliminate model-to-HTML injection:** replace `innerHTML`, then deploy CSP and rendering tests.
5. **Bound all resources:** validate schemas and audio, cap sizes/rates/concurrency/output/time, and
   guarantee process/temp cleanup.
6. **Partition state:** isolate logs, tokens, speech, history, and pending actions per authenticated
   session; treat recalled content as untrusted.

## Review limitations

This was a source-level review of the checked-out commit. The exact installed Claude CLI version,
user/global/project Claude configuration, MCP servers, hooks/plugins, browser Private Network Access
behavior, OS account permissions, and media decoder versions were not available as stable repository
facts. Because those inputs can only increase or alter ambient authority, they must be captured in a
deployment-specific threat model and verified with integration tests in a disposable sandbox.
