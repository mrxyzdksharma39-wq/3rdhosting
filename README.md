# DK HOSTING - audited trusted-code edition

This is the repaired version of the supplied hosting bot, with the original branding, premium emoji definitions, owner approval/rejection, two-project normal-user limit, My Files, Upload, Restart, Stop, Speed, Status, Recommended Install, and owner notifications retained. Execution, uploads, persistence and callbacks were repaired rather than leaving the original unsafe implementations in place. Core hosting logic is separated into hosting.py for offline tests. No real token is included.

## Before anything else

Revoke the token embedded in the original source with BotFather and generate a replacement. Assume it is compromised. Do not reuse it for any account. Never commit tokens, project .env files, data directories or backups to Git.

**This is NOT an OS sandbox.** All subprocesses run as the hosting service's OS user. Application checks prevent ordinary cross-user menu/configuration access; malicious Python or dependency build code can still read files, inspect processes, access host credentials and control other processes with that UID. Venvs separate dependencies, not security. Environment allowlisting prevents accidental secret inheritance, not deliberate host compromise. Execution is off by default and must only be enabled for code and dependencies you have reviewed and trust. Do not run a public arbitrary-code hosting service with this architecture. Genuine multi-tenant protection needs separately isolated containers/VMs per project, separate credentials, restricted filesystem/network access and CPU/memory/PID/disk quotas on infrastructure that supports those controls. Those controls are not implemented here.

## Files and exact commands

- bot.py: Telegram interface, menus, HTTP health lifecycle and shutdown.
- hosting.py: configuration, project storage, ZIP checks, isolated installs and process management.
- requirements.txt: pinned direct host dependencies, pyTelegramBotAPI and psutil.
- render.yaml: Free Web Service trial configuration.
- .python-version: Python 3.13 series. Render selects an available patch; the offline test runner was Python 3.13.16. Exact Render patch/build is not verified.
- test_hosting.py: runnable offline regression tests. It uses real processes/venvs for core tests and explicit Telegram/psutil test doubles only for UI contract tests.
- TEST_REPORT.md and test-output.txt: observed results and remaining deployment checks.

Build command:

```bash
python -m pip install -r requirements.txt && python -m pip check
```

Start command (one process, not Gunicorn, not multiple workers):

```bash
python bot.py
```

Offline configuration/storage validation, with real environment variables already set:

```bash
python bot.py --check-config
```

This command validates token shape, owner/port settings, storage creation and single-directory instance locking without importing Telegram or making an API call. It does not authenticate the token or test the HTTP socket. A valid-shaped dummy is NOT a valid Telegram credential.

Regression tests, run from the extracted project directory:

```bash
python -m unittest discover -s . -p 'test_*.py' -v
```

## Render configuration

Deploy the archive contents at repository root, including the hidden .python-version file. Do not commit the archive itself as your source code. Either create a Blueprint from render.yaml or configure a Python Web Service manually with Free plan, the exact build/start commands above, and health-check path `/healthz`.

| Variable | Required | Value / meaning |
|---|---|---|
| HOST_BOT_TOKEN | Yes | Newly regenerated token of the hosting bot only. No BOT_TOKEN fallback. |
| OWNER_ID | Yes | Positive decimal Telegram user ID of the owner; do not use a username. |
| ALLOW_UNSANDBOXED_EXECUTION | For execution | Defaults to false. Set true only after accepting the trusted-code-only restriction. |
| DATA_DIR | Recommended | Blueprint uses user_files_v2, a local private directory. Free storage is ephemeral. |
| PORT | Render provides it | Bind to this integer on 0.0.0.0. Local default is 10000. Do not set it in a hosted project's .env. |

The hosting process does not load a host .env automatically. Use Render environment variables or exported shell variables. HOST_BOT_TOKEN is the only accepted hosting-token key. Remove the old deployment's BOT_TOKEN variable to avoid operator confusion, even though it is neither used for host configuration nor inherited by hosted projects.

1. Rotate the exposed token, and stop the old bot deployment before starting this one.
2. Back up the legacy data outside Render. Choose a NEW DATA_DIR such as user_files_v2; see migration notes below.
3. Push the corrected project files to a private Git repository. Keep data and secrets out of it.
4. Create the Render Python Web Service or Blueprint. Enter HOST_BOT_TOKEN and OWNER_ID in Render's environment settings. Keep execution false initially.
5. Deploy. Verify that dependency installation AND pip check succeed. A network-capable clean host install could not be performed in the offline audit environment.
6. Verify `/healthz` returns HTTP 200 and service logs say the hosting bot authenticated. The endpoint is a liveness check for the process/HTTP thread, NOT proof that Telegram polling or any hosted bot is currently healthy.
7. Send /start to the hosting account. Check the menu. Send /start from a second owned Telegram user account and test owner approval/rejection.
8. If you accept the risk and use only trusted projects, set ALLOW_UNSANDBOXED_EXECUTION=true and redeploy. Stop the old polling instance first. Environment edits/redeploys can erase Free-plan local data.
9. Follow the project workflow below using a SECOND bot account you own. Check both bot identities and logs before trusting the deployment.
10. Run the live verification checklist in TEST_REPORT.md. This deployment is not externally certified or fully end-to-end tested.

The service is Linux/POSIX-specific: process groups and flock are intentional. Do not use this on Windows. No JavaScript runtime support is implemented; the original welcome message advertised JS but its run handler actually only allowed Python. That misleading advertisement is removed.

## Uploading and running a separate bot

Uploads do not execute immediately. This deliberate safety change prevents unconfigured projects from starting under the wrong account and prevents dependency installation during upload.

1. Get approval from the owner (the owner is always approved).
2. Upload one .py file, or a .zip project. Maximum compressed upload: 10 MiB; maximum expanded ZIP: 40 MiB and 1000 entries. ZIPs reject unsafe paths, special-file modes, reserved runtime folders, encrypted entries, duplicate paths and excessive compression ratios. Each upload creates a separate project; normal users may hold two projects.
3. ZIP layout: put bot.py OR main.py at the project root, optionally inside one single enclosing folder. Both names together are ambiguous and rejected. If neither exists, exactly one root-level .py file is accepted. Other files may be submodules. Include assets and requirements in this same project; do not upload support .py files separately, because those are separate projects.
4. Select My Files > project > Token / .env. Upload a UTF-8 file named exactly `.env`, containing its OWN token under BOT_TOKEN. Or include this file at the ZIP project's root. Never commit it to Git. The bot attempts to delete standalone credential upload messages, but Telegram delivery is not end-to-end secret storage and deletion is best effort.
5. If needed, select Requirements and upload a UTF-8 `requirements.txt` for that project. A ZIP can already contain it. Ordinary arbitrary .txt uploads are not auto-installed.
6. Select Install. It creates this project's own venv and installs requirements using that venv's Python, a clean environment and pip's isolated mode. One installation at a time across this host, with a 300-second timeout per subprocess. No requirements file means an empty project venv is created. Pip failures and pip check failures are reported. No hosted dependencies are installed globally.
7. Select Run. It passes the project's configuration only, with a fixed minimal runtime environment, sets cwd to the project directory and executes its entry point using its venv interpreter. The service never scrapes Python source for tokens.
8. Select Status and Logs. A successful process launch is NOT proof of successful Telegram authentication. Missing packages usually appear as ModuleNotFoundError, and a project may exit immediately.
9. Select Stop to terminate the process group and clear the auto-restore flag. Select Restart to stop and launch that same configured project. The main Stop/Restart menu operates on all your projects. Stopped projects stay stopped after host restart if the same data survives.
10. Stop a project before changing .env or requirements. Configuration selection expires after 10 minutes; /cancel cancels it. This prevents a credential file from accidentally configuring a different project.

Example project .env syntax (substitute your own real owned-bot token; the label below is not a usable token):

```dotenv
BOT_TOKEN=your_uploaded_bot_token
APP_MODE=production
```

The dotenv subset supports comments on their own lines, blank lines, KEY=value and matching single/double quotes. It does NOT expand `${...}`, process escapes, accept `export`, merge host variables or support multiline values. Inline comment characters are literal. Reserved host/runtime variables and Python/pip/loader overrides are rejected. Additional application-specific settings can be included. Do not use .env interpolation in uploaded projects. Python-dotenv is not a host dependency; if your project uses it, add it to your project's requirements.txt and keep it pointed at this project's .env.

If your source uses `os.getenv('BOT_TOKEN', 'hardcoded-token')`, its explicitly configured project BOT_TOKEN takes precedence, not the hosting token. If your source uses a literal hard-coded token without getenv, an environment variable cannot change that source literal. For safe validation, declare the SAME intended token in the project's .env too, or preferably refactor your source to load BOT_TOKEN only from the environment. The host cannot prove that arbitrary source code uses the declared token; it cannot prevent a malicious project from intentionally using another account. Projects with no declared valid-shaped BOT_TOKEN are rejected even if their source contains a literal. Token shape validation does not prove authenticity; live getMe/polling requires a valid token you own.

The host rejects the hosting bot's numeric account ID in project configuration and rejects simultaneous local projects declaring the same Telegram account. This prevents ordinary duplicate polling here. It cannot detect another machine polling the same token, a literal hidden inside source, webhooks, or a deployment overlap on another Render instance. If Telegram returns conflict/409, stop every other instance and resolve any webhook intentionally; this service never deletes webhooks automatically.

Recommended Install is preserved as an optional PER-PROJECT action. It is large and may exhaust Free resources; use a minimal requirements.txt instead. The old unused automatic GitHub pytgcalls --no-deps install is not run: uncontrolled Git installs and skipping dependencies are unreliable. If your trusted project genuinely needs that library, declare an appropriate reviewed version/source and its dependencies in that project's requirements. Installing arbitrary packages executes arbitrary build code and has the same trusted-code risk.

## Persistence, recovery and legacy data

New metadata is DATA_DIR/state.json. Project sources, .env and per-project venvs are under DATA_DIR/projects. Writes use a private temporary file, fsync and atomic replacement under a lock. Invalid metadata causes startup to fail instead of silently wiping approvals/projects. The application uses JSON, not SQLite/PostgreSQL; no DATABASE_URL is read and no SQL database is required. There is no backup/encryption service built in. Runtime logs are bounded in memory, not on disk, and are lost on host restart. Known configured values and token-shaped strings are redacted. Secrets produced or transformed by arbitrary uploaded code are not guaranteed to be detectable; never print secrets from project code.

The original users.json/uplo556ads format is intentionally NOT automatically converted or executed. Keep it backed up and read-only. Use a fresh DATA_DIR, reapprove trusted users, then reupload their reviewed project files/ZIPs and explicit per-project configuration. Original .py files may contain compromised credentials: rotate and edit them before reuploading. No old file is deleted or modified by this edition. Legacy processes and global package installs are not adopted. This is a compatibility limitation, not silent data migration.

If the SAME data directory survives a process restart, projects marked desired/running at shutdown are relaunched using their existing venv/configuration. Projects explicitly stopped do not relaunch. A crashed process is not endlessly restarted during the current lifetime, but its saved desired flag permits one restore attempt after a later host restart. Restore failures are left stopped with a safe log; the host continues. Dependencies are not silently reinstalled during restore. Partial/invalid venvs can be removed manually while the host is stopped, then Install can recreate them. Do not restore state.json alone without the associated projects and secrets.

Render Free loses local changes on restart, redeploy AND spin-down. Therefore uploaded source, credentials, approvals, metadata, venvs and project runtime output can all be lost. Only committed source/configuration and Render's separately managed environment settings are expected to remain. Free cannot attach a persistent disk. Auto-recovery cannot recover erased files. A persistent paid service with a disk at DATA_DIR is the practical minimum for reliable trusted-code recovery. A database alone does not persist uploaded source, venvs or assets.

Free web services spin down after 15 minutes without inbound HTTP/WebSocket traffic; outbound Telegram long polling is not inbound web traffic. Telegram messages will not wake this long-polling web service. Free has a shared 750-hour workspace allowance, limited CPU and 512 MiB RAM, and may be restarted or suspended for excessive service-initiated traffic. Host plus installs plus projects share all these resources. Health checks are not a promise of continuous uptime. Do not add artificial keepalive traffic to pretend this is reliable 24/7 hosting.

A paid web service with a persistent disk can retain the existing health endpoint and process model for trusted projects. A paid background worker is a better fit for polling-only workloads but needs a worker-specific launch mode if you remove this HTTP component. For public multi-user hosting, use a separate isolation/orchestration design, not merely a paid upgrade.

Run exactly one poller per token. Local file locking prevents two hosts sharing one local DATA_DIR, not instances on different machines or ephemeral filesystems. Render's normal zero-downtime deployments can overlap instances; before replacing a Free deployment, suspend/stop the old service first and accept downtime. A paid disk disables zero-downtime deploys and helps avoid overlap, but operational verification is still required. No distributed lease is implemented.

## What is and is not verified

39 offline tests passed after fixes. Real subprocesses proved token A/B separation, isolated venv behavior, missing-dependency errors, installation failure handling, process stop/restart, ordinary-child cleanup, saved-state recovery, concurrent duplicate-start blocking and HTTP serving. UI tests use test doubles, not the installed Telegram SDK. A clean host environment was created, but host dependency installation could not succeed without an online package index. Real package resolution, actual Telegram menu rendering/custom emoji entitlement, long polling, network failures, Render deploy behavior and live restarts remain unverified. See TEST_REPORT.md before production decisions.

Sources checked for deployment guidance:
- Render free limitations: https://render.com/docs/free
- Python version selection: https://render.com/docs/python-version
- Web-service binding: https://render.com/docs/web-services
- Deployment overlap, signals and persistent disks: https://render.com/docs/deploys
- Blueprint fields: https://render.com/docs/blueprint-spec
