# Audit and observed verification report

## Status

The last offline regression run passed 39 tests on Python 3.13.16. This is a repaired, trusted-code deployment candidate, NOT a claim of complete Telegram/Render end-to-end readiness. The release intentionally does not claim secure public multi-tenant execution. Test doubles are used only in explicitly named UI contract tests. No real Telegram requests used dummy tokens. No exposed original token is shipped.

## Root cause from the actual source

Original run_script_thread, lines 501-561, calls subprocess.Popen with neither env nor cwd. Therefore Python inherits the parent environment, including Render BOT_TOKEN, and the parent working directory. Uploaded code using os.getenv('BOT_TOKEN', a_literal) receives the host's token before considering its fallback. Python-dotenv with its usual non-overriding behavior can likewise leave that inherited value in place. A literal assignment in uploaded source, without getenv, is not replaced by inherited environment variables. No actual uploaded user's source or runtime trace was provided to establish which conditional case produced a particular observed message; the inheritance defect itself is present in the supplied launcher.

The host now accepts HOST_BOT_TOKEN only, and children get a fresh allowlisted environment plus their own explicit .env, cwd and venv. Host account IDs are rejected as project token declarations. Tokens are never discovered by scraping source. This fixes accidental inheritance; it cannot constrain intentionally malicious Python.

## Important source findings and repairs

- Original lines 26-27 embed a real-looking token and default owner ID. Removed both defaults; the exposed token must be revoked, which code cannot do on the user's behalf.
- Original install_requirements and live_install use host sys.executable pip and inherit secrets. All project installs now use a private venv, fixed environment, tracked process group, timeout, return-code checks and pip check. No auto-install during upload.
- Original file_name is joined directly into a shared per-user folder. New filenames are validated and projects use random identifiers in separate directories; callbacks use short opaque IDs and verify ownership.
- Original extractall has no explicit member/path/type/expansion bounds or per-project staging. New extraction is validated, bounded and rolled back on failure, including missing/ambiguous entry points.
- Original start registers a process only inside a later thread. Duplicate callbacks can spawn duplicates; old finally cleanup can remove newer state. Start/registration is synchronous under one lock and watcher cleanup checks process identity.
- Original stop kills only the main PID. New normal descendants share a separate process group and receive TERM then KILL, including dependency installation subprocesses. Intentional setsid/daemon escapes are not sandboxed.
- Original restore starts every uploaded .py, including deliberately stopped scripts, and does not run ZIP entry points. New restore uses persisted desired flags and project entry points, with per-project error containment.
- Original logs and Telegram install output can expose tokens and are not bounded against huge individual lines. New logs are bounded, token/config-value redacted and overlong lines suppressed. Unknown/transformed secrets remain the uploaded code author's responsibility.
- Original state writes/read errors are swallowed, threading is unsynchronized and malformed data resets users silently. New state writes are locked/atomic and invalid state fails closed. Old metadata requires explicit manual migration/reupload.
- Original general callback handler has no acknowledgement on some branches; some long filenames exceed Telegram's 64-byte callback-data limit. New callbacks acknowledge immediately and use short project IDs.
- Original Speed intentionally sleeps 0.1s and labels that artificial delay as speed. New metric measures a real system query and warns container quotas differ from system totals.
- Original Recommended Install button has no text-handler branch, despite helper functions. New per-project menu routes it to isolated installation. Unused automatic GitHub --no-deps behavior is removed/documented.
- Original bot lacks Web Service HTTP binding and signal cleanup. Added 0.0.0.0:$PORT liveness endpoint, startup getMe check, shutdown cleanup and local instance lock.
- An actual audit test found HTTPServer performs reverse DNS on this runner's invalid long container hostname. Replaced only server_bind with TCP binding and fixed server identity; the real HTTP tests then passed.
- Review found a child can keep stdout open after its parent exits. Watcher now waits for the parent separately from the bounded reader and kills the remaining process group.
- One intermediate child-cleanup test failed because /proc disappeared between exists() and read(). The test was corrected to treat ProcessLookupError/FileNotFoundError as observed process exit; final full run passed.

## Mandatory result table

PASS means the named check was actually executed and observed, and does not extend beyond its stated scope.

| Component or feature | Test performed | Result: PASS, FAIL, or NOT TESTED | Error found | Fix applied | Any remaining limitation |
|---|---|---|---|---|---|
| Python syntax | compileall on all delivered .py files | PASS | No final syntax errors | Recompiled after changes | Runtime imports checked separately |
| Static references | Compiler symbol-table scan for unresolved global references and duplicate top-level definitions | PASS | No final unresolved globals or duplicates | Source paths repaired | Not a full third-party linter or formal verification |
| requirements.txt syntax | Parsed each direct dependency with packaging.Requirement | PASS | None | Direct host packages pinned | Transitive dependency resolution not verified |
| Clean host environment | Created new venv successfully | PASS | None | Isolated environment | Does not imply host package install passed |
| Host dependency installation | pip install --no-index in fresh venv | FAIL | No pyTelegramBotAPI distribution available offline | Cannot fetch packages in this runner; online build documented | Run exact build command with network; real host imports and compatibility remain unverified |
| Real SDK dependency compatibility | Online install and pip check with actual Telegram SDK/psutil | NOT TESTED | Required distributions unavailable locally | requirements.txt and build command supplied | Do not infer compatibility from UI doubles |
| Missing/invalid/valid dummy host settings | Settings validation plus bot.py --check-config subprocesses | PASS | Original real-token fallback and owner default | HOST_BOT_TOKEN required; numeric owner/port validation | Token shape only; dummy values never authenticated |
| Token A versus Token B | Real child prints resolved token in controlled test; verifies no inherited host/database/owner/marker variables | PASS | Host environment inheritance | Fresh child env plus project .env | Does not inspect source literals or prevent malicious code |
| Host token rejection | Same token and same numeric account ID with different suffix | PASS | Host account could be reused accidentally | Explicit account-ID rejection | Arbitrary source can ignore declaration |
| Missing project configuration | Start without .env while parent BOT_TOKEN is set | PASS | Original implicit fallback | Clear error; no start | Hard-coded-only projects must declare the intended token too |
| dotenv subset | Quotes, comments, duplicate/invalid/reserved keys and literal interpolation | PASS | Missing project-specific validation | Strict documented parser | Not the entire python-dotenv grammar |
| User ownership/approval | Cross-user read/config/stop attempts; owner-only approval | PASS | Weak separation/shared folders and unchecked flows | Ownership checks, private-only UI and revocation stop | Application checks are not OS isolation |
| Upload limits and filenames | Unsupported/empty/corrupt files, traversal names, normal-user limit, owner exemption | PASS | Direct filename path joins | Validated names and per-project staging | Trusted source may consume runtime resources |
| ZIP checks | Valid wrapper, traversal, absolute/backslash/drive paths, reserved folders, symlink, case duplicate, expansion limit | PASS | No explicit bounded validation | Bounded validated extraction and rollback | Not exhaustive ZIP fuzzing |
| Entry points | Valid root/wrapper and missing/ambiguous entry cases | PASS | ZIPs did not become runnable projects | Explicit deterministic selection | Both bot.py and main.py require user to disambiguate layout |
| Storage/persistence | Initialization, reload, invalid JSON, actual mode bits and injected permission failure | PASS | Silent resets and unsynchronized writes | Fail-closed load and atomic writes | Permission-denial recovery was fault-injected; real Render disk not tested |
| Per-project dependency isolation | Real venv creation, empty requirements install, pip check and sys.prefix/base_prefix check | PASS | Global pip installation | Private interpreter/environment | Real third-party package installation not exercised |
| Missing package / install failure | Real ModuleNotFoundError process and local missing-wheel pip failure | PASS | Errors previously swallowed | Safe failure message and captured logs | Online version-conflict resolver scenarios remain untested |
| Start/stop/restart | Real subprocess lifecycle and distinct restart PID | PASS | Untracked/duplicate processes and main-PID-only stop | Synchronous tracking and process groups | Stubborn escaped/daemonized code not contained |
| Concurrent duplicate launch | Six simultaneous starts of one project | PASS | Threaded registration race | Atomic reservation | Single application instance scope |
| Duplicate Telegram account | Two projects declaring same account on this host | PASS | Local duplicate polling risk | Active account-ID check | Remote pollers/webhooks and deploy overlap not detected |
| Ordinary child cleanup | Spawned child verified gone or zombie after stop | PASS | Child remained alive under original kill() | Process-group TERM/KILL | Kernel reaping is external; setsid escapes not isolated |
| Auto-restore | Relaunch desired project after Manager restart; explicitly stopped remains stopped | PASS | All uploaded scripts restarted indiscriminately | Desired-state persistence | Requires the SAME surviving project data/venv |
| Logs | Real token/custom-secret output, bounded retained lines/text and runtime errors | PASS | Original output exposed credentials | Redaction and limits | Arbitrary transformed/unconfigured secrets cannot be guaranteed |
| HTTP health server | Real socket binding, HTTP 200/503 and address-in-use failure | PASS | Original missing HTTP server; reverse-DNS startup failure found during testing | Minimal threaded server with DNS-independent bind | Liveness only, not Telegram readiness; no public load test |
| Local host instance lock | Two Managers attempt same directory lock | PASS | Duplicate local host startup possible | flock | No cross-machine distributed lease |
| UI menus/callbacks | Start, approve, document/config routing, all top-level menus, ownership and callback size with explicit test doubles | PASS | Broken recommended action and long callback payloads | Repaired handlers and ID-based callback data | Actual SDK serialization, custom emoji entitlement and Telegram rendering NOT tested |
| Combined host lifecycle | main() with real HTTP/lock/cleanup and fake Telegram transport | PASS | Original no combined lifecycle | Coordinated cleanup | Actual signal delivery under Telegram polling not tested |
| OS-level cross-user security | Compared actual parent/child UID | FAIL | Both use same OS UID; no sandbox | Execution disabled by default; trusted-code warning | Genuine hostile multi-user isolation NOT implemented on this architecture |
| Real Telegram operation | Hosting /start, real uploads and second owned bot identity | NOT TESTED | No live credentials/API test | Exact live checklist below | Required before real use |
| Render deploy/restart | Actual Render build, health, resources, suspension and filesystem behavior | NOT TESTED | No Render deployment performed | Blueprint and documented limits supplied | Required; Free plan cannot ensure uptime or persistence |
| SIGTERM with live polling | Actual Render termination while hosting and child are polling | NOT TESTED | No live Render/Telegram run | Signal handling and cleanup implemented | Observe child termination on the actual service |
| Original credential leak | Scanned every final release file for original embedded token | PASS | Token embedded in original source | Removed from release and required replacement | Only BotFather revocation invalidates the exposed credential |

## Live end-to-end acceptance checklist (all currently NOT TESTED)

Use TWO different bots you own and a second owned Telegram user for approval tests. Do not use dummy values for API calls.

1. In a clean network-capable Python 3.13 environment run `python -m pip install -r requirements.txt && python -m pip check`. Verify imports with `python -c "import telebot, psutil"`. Stop here if either fails.
2. Set fresh HOST_BOT_TOKEN, positive OWNER_ID, DATA_DIR and Render's PORT. Run `python bot.py --check-config`; confirm no token appears in output.
3. Start with `python bot.py` or deploy the provided Blueprint. Confirm authentication completes, health returns 200 and no unhandled startup exception occurs. Keep one poller only.
4. Send /start to host bot A and verify menu and premium-emoji fallback behavior. Test pending, approval, rejection and attempted unauthorized approval from another user.
5. Enable execution only for trusted code. Upload a valid Python or ZIP bot B that loads BOT_TOKEN from its environment. Upload B's .env, not A's token.
6. Add minimal B requirements and use Install. Confirm success, inspect logs and test importing the installed package in B. Recheck host functionality to confirm its environment remains intact.
7. Run B and use getMe in B or compare its public username/messages to the bot account shown in BotFather. Send distinct /start commands to A and B; verify replies come from different intended accounts. Never display their tokens.
8. Test Status and Logs, duplicate Run, Stop and Restart; verify the hosted bot stops replying when stopped and resumes only after restart. Verify ordinary children stop as well.
9. Test invalid ZIP/upload, missing .env, A's token supplied for B, invalid-shaped token, valid-shaped but invalid token, missing package and a real requirements version conflict. Confirm clear safe errors without bringing down A.
10. Test the same token being polled on another controlled machine and resolve the expected conflict by stopping that other process. Test webhook interaction only intentionally; do not delete a production webhook blindly.
11. Stop B explicitly, restart the hosting service and observe its documented recovery behavior. On Free expect the local data may be entirely erased. With persistent paid storage separately test desired-state restoration and stopped-state preservation.
12. Send SIGTERM or perform a controlled Render shutdown while a project is running. Observe host and child cleanup and new instance startup. Ensure no overlapping old/new poller remains.
13. Verify user-private menus, configuration ownership, file permissions, secret-free logs and backup handling. Do NOT treat this as evidence of OS sandboxing; that feature fails by design here.
14. Check Render resource metrics while installing/running intended projects. Check cold-start/sleep and service-initiated traffic limits. Do not claim 24/7 hosting or persistent uploads on Free.

## Explicit changes and limitations

Uploads no longer auto-run or auto-install. Existing visible functions are preserved, but their unsafe semantics are replaced. Status exposes global user statistics only to owner; normal users see their own projects. Legacy users.json/uplo556ads is not auto-imported; reapproval and reviewed reupload are required in a fresh DATA_DIR. JS was only advertised, not implemented in the original run handler. The unused global GitHub --no-deps installer is not retained as an automatic action; reviewed project-specific requirements are supported instead. No database service, paid disk, object-store integration, distributed lock, resource quota system or secure container runner is fabricated or claimed.

The online host install, live Telegram checks and Render checks remain open acceptance gates. The offline install failure was environmental, not silently marked PASS. Full production readiness cannot honestly be declared from this environment.
