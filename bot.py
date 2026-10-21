# Original credits: @Mrdk_Sharma_07
# Original menus and premium emoji retained; unsafe execution paths repaired.
import contextlib
import logging
import os
import signal
import sys
import threading
import time
import html
from functools import wraps
from hosting import Settings, Manager, HostingError, MAX_UPLOAD, health_server, redact

# Validate config before importing network dependencies or creating Telegram objects.
try:
    settings = Settings.from_env()
except HostingError as exc:
    if __name__ == "__main__":
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    raise

if __name__ == "__main__" and sys.argv[1:] == ["--check-config"]:
    try:
        check_manager = Manager(settings)
        check_manager.acquire_instance_lock()
        check_manager.close()
    except HostingError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    print("Configuration shape and storage initialization passed; no Telegram API call made.")
    raise SystemExit(0)

try:
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
    import psutil
except ImportError:
    print("Missing host dependencies. Run python -m pip install -r requirements.txt.", file=sys.stderr)
    raise SystemExit(2) from None

# Do not let library exception/HTTP logs disclose tokens or request URLs.
logging.getLogger("TeleBot").disabled = True
telebot.logger.disabled = True
try:
    manager = Manager(settings)
except HostingError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2) from None
OWNER_ID = settings.owner
bot = telebot.TeleBot(settings.token, parse_mode=None, threaded=False)
started = time.monotonic()
stopping = threading.Event()
pending = {}  # one explicit project/config-file selection per private user
pending_lock = threading.Lock()

RECOMMENDED_PACKAGES = [
    "pip",
    "setuptools",
    "wheel",
    "requests",
    "numpy",
    "pandas",
    "flask",
    "aiohttp",
    "pyrogram",
    "python-dotenv",
    "beautifulsoup4",
    "lxml",
    "pillow",
    "matplotlib",
    "scipy",
    "scikit-learn",
    "pytest"
]

PREMIUM_EMOJI_IDS = [

    # Existing Premium Emojis
    "5289722755871162900",
    "5372849966689566579",
    "5359664288241829619",
    "5445033158456145975",
    "5445092669522996408",
    "5445358884480916784",
    "5445307250384075361",
    "5447602197439218445",
    "6147460667281511517",
    "6235459831302460476",
    "6244492465353529537",
    "6318817991102305543",

    # New Premium Emojis
    "5231200819986047254",
    "5944970130554359187",
    "6120548140211770562",
    "4987977590812705943",
    "5416081784641168838",
    "6321171873698681876",
    "5915556996215476302",
    "5877443460725739250",
    "4990298741463319592",
    "5462956611033117422",
    "6231214251835397322",
    "6282846669335702032",
    "5210952531676504517",
    "6129639980387015660",
]

PE_FIRE = PREMIUM_EMOJI_IDS[0]

PE_STAR = PREMIUM_EMOJI_IDS[1]

PE_ROCKET = PREMIUM_EMOJI_IDS[2]

PE_CROWN = PREMIUM_EMOJI_IDS[3]

PE_SHIELD = PREMIUM_EMOJI_IDS[4]

PE_MONEY = PREMIUM_EMOJI_IDS[5]

PE_PHONE = PREMIUM_EMOJI_IDS[6]

PE_CHECK = PREMIUM_EMOJI_IDS[7]

PE_CROSS = PREMIUM_EMOJI_IDS[8]

PE_WARNING = PREMIUM_EMOJI_IDS[9]

PE_LOCK = PREMIUM_EMOJI_IDS[10]

PE_GIFT = PREMIUM_EMOJI_IDS[11]

PE_APPROVE = PREMIUM_EMOJI_IDS[12]

PE_USER = PREMIUM_EMOJI_IDS[13]

PE_ADMIN = PREMIUM_EMOJI_IDS[14]

PE_UPLOAD = PREMIUM_EMOJI_IDS[15]

PE_FOLDER = PREMIUM_EMOJI_IDS[16]

PE_SETTINGS = PREMIUM_EMOJI_IDS[17]

PE_SERVER = PREMIUM_EMOJI_IDS[18]

PE_DIAMOND = PREMIUM_EMOJI_IDS[19]

PE_BOT = PREMIUM_EMOJI_IDS[20]

def pe(emoji_id, fallback="💎"):
    """
    Telegram custom premium emoji HTML.
    """
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def safe_html(value):
    return html.escape(str(value), quote=False)

def premium_header(title, emoji_id=PE_CROWN, fallback="👑"):
    return (
        f"{pe(emoji_id, fallback)} "
        f"<b>{safe_html(title)}</b>"
    )

def approval_buttons(uid):

    markup = InlineKeyboardMarkup()

    markup.row(
        InlineKeyboardButton(
            "✅ APPROVE",
            callback_data=f"approve_{uid}"
        ),
        InlineKeyboardButton(
            "❌ REJECT",
            callback_data=f"reject_{uid}"
        )
    )

    return markup

def control_buttons():

    # IMPORTANT:
    # Do NOT pass a list as the first argument.
    # This fixes:
    # ReplyKeyboardMarkup.__init__()
    # got multiple values for resize_keyboard

    markup = ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    markup.row(
        KeyboardButton("🌏 Upload"),
        KeyboardButton("📁 𝐌𝐲 𝐅𝐢𝐥𝐞𝐬")
    )

    markup.row(
        KeyboardButton("🔄 𝐑𝐞𝐬𝐭𝐚𝐫𝐭"),
        KeyboardButton("⏹ 𝐒𝐭𝐨𝐩")
    )

    markup.row(
        KeyboardButton("⚡ 𝐒𝐩𝐞𝐞𝐝"),
        KeyboardButton("🚀 𝐒𝐭𝐚𝐭ᴜѕ")
    )

    markup.row(
        KeyboardButton(
            "⚙️ Recommended Install"
        )
    )

    return markup

def send(chat, text, **kwargs):
    # Messages contain only safe errors / redacted logs. Keep within Telegram limits.
    return bot.send_message(chat, text[:3900], **kwargs)


def guarded(callback=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(item):
            uid = item.from_user.id
            msg = item.message if callback else item
            if not msg or msg.chat.type != "private" or msg.chat.id != uid:
                if callback:
                    with contextlib.suppress(Exception):
                        bot.answer_callback_query(item.id, "Use this bot in a private chat.")
                return
            if callback:
                # Always acknowledge immediately, before installations or stop operations.
                with contextlib.suppress(Exception):
                    bot.answer_callback_query(item.id)
            try:
                return fn(item)
            except HostingError as exc:
                with contextlib.suppress(Exception):
                    send(uid, str(exc))
            except Exception:
                # Never echo Telegram request exceptions, upload contents, or raw paths.
                with contextlib.suppress(Exception):
                    send(uid, "Operation failed. Check configuration, storage permissions, and service availability, then retry.")
                print("Operation failed (details withheld to protect credentials).", file=sys.stderr)
        return wrapped
    return decorate


def notify_owner(text):
    with contextlib.suppress(Exception):
        send(OWNER_ID, text)


def require_approval(uid):
    manager.ensure_user(uid)
    if not manager.approved(uid):
        raise HostingError("Owner approval required. Send /start to request access.")


def files_menu(uid):
    user = manager.ensure_user(uid)
    rows = [[InlineKeyboardButton(manager.projects[p]["name"][:60], callback_data="file:" + p)]
            for p in user["files"]]
    return InlineKeyboardMarkup(rows)


def project_menu(uid, pid):
    p = manager.project(uid, pid)
    rows = []
    for choices in (("Run", "Stop", "Restart"), ("Status", "Logs"), ("Token / .env", "Requirements"),
                    ("Install", "Recommended Install")):
        actions = {"Run": "run", "Stop": "stop", "Restart": "restart", "Status": "status", "Logs": "logs",
                   "Token / .env": "env", "Requirements": "req", "Install": "install", "Recommended Install": "recommended"}
        rows.append([InlineKeyboardButton(label, callback_data=actions[label] + ":" + pid) for label in choices])
    rows.append([InlineKeyboardButton("Back", callback_data="myfiles")])
    send(uid, f"{p['name']}\nStatus: {manager.status(uid, pid)}", reply_markup=InlineKeyboardMarkup(rows))


@bot.message_handler(commands=["start"])
@guarded()
def start_command(message):
    uid = message.from_user.id
    is_new = str(uid) not in manager.users
    user = manager.ensure_user(uid)
    if is_new and uid != OWNER_ID:
        with contextlib.suppress(Exception):
            send(OWNER_ID, f"NEW USER APPROVAL\nName: {message.from_user.first_name or 'User'}\nUser ID: {uid}",
                 reply_markup=approval_buttons(uid))
    if not manager.approved(uid):
        send(uid, "Access rejected by owner." if user["rejected"] else "Access requested. Waiting for owner approval.")
        return
    welcome = (f"{pe(PE_ROCKET, '🚀')} <b>DK HOSTING</b>\n\n"
               f"Welcome, {safe_html(message.from_user.first_name or 'User')}!\n"
               f"User ID: <code>{uid}</code>\nProjects: {len(user['files'])}\n\n"
               "Python / ZIP projects, private project environments, logs, and saved running-state recovery.\n"
               "Use Upload to add a project; My Files to configure its own token and dependencies.\n"
               "Trusted code only: this service is not a security sandbox.")
    send(uid, welcome, parse_mode="HTML", reply_markup=control_buttons())
    notify_owner(f"User started DK HOSTING: {uid}")


def async_install(uid, pid, recommended=False):
    # Return promptly so polling continues. Manager reserves each actual job atomically.
    def work():
        try:
            result = manager.install(uid, pid, tuple(RECOMMENDED_PACKAGES) if recommended else ())
        except HostingError as exc:
            result = str(exc)
        except Exception:
            result = "Installation failed. Inspect project logs; check storage, package compatibility, and network access."
        with contextlib.suppress(Exception):
            send(uid, result)
    threading.Thread(target=work, daemon=True).start()
    send(uid, "Installation requested for this project only. Result will be sent here; Logs shows bounded, redacted output.")


@bot.message_handler(func=lambda message: True, content_types=["text"])
@guarded()
def control_handler(message):
    uid = message.from_user.id
    require_approval(uid)
    text = (message.text or "").strip()
    ids = list(manager.ensure_user(uid)["files"])
    if text == "/cancel":
        with pending_lock:
            pending.pop(uid, None)
        send(uid, "Configuration upload selection cancelled.")
    elif text.startswith("📁") or text == "/files":
        send(uid, "Your files:" if ids else "No projects. Upload a Python file or ZIP.", reply_markup=files_menu(uid))
    elif text.startswith("🌏") or text == "/help":
        send(uid, "Upload .py or .zip (up to 10 MiB). Nothing runs automatically.\n"
             "Then select My Files > project > Token / .env. Upload .env containing BOT_TOKEN=your_uploaded_bot_token.\n"
             "Use Requirements to upload requirements.txt; Install creates its isolated virtual environment.\n"
             "Use Run, Status, Logs, Stop or Restart. /cancel cancels a configuration upload.\n"
             "Only approved, trusted users may execute code. Do not upload credentials into group chats.")
    elif text.startswith("🔄"):
        results = []
        for pid in ids:
            try:
                manager.restart(uid, pid)
                results.append(manager.projects[pid]["name"] + ": launched (check logs)")
            except HostingError as exc:
                results.append(manager.projects[pid]["name"] + ": " + str(exc))
        send(uid, "\n".join(results) or "No projects.")
    elif text.startswith("⏹"):
        for pid in ids:
            manager.stop(uid, pid)
        send(uid, "Stopped your projects and cleared their auto-restore flags.")
    elif text.startswith("⚡"):
        start = time.monotonic()
        ram = psutil.virtual_memory()
        elapsed = time.monotonic() - start
        send(uid, f"System query: {elapsed:.4f}s\nCPU load: {psutil.cpu_percent()}%\n"
             f"System RAM available: {ram.available // (1024 * 1024)} MiB\n"
             "System metrics may exceed the container's actual plan limits; check Render metrics for quotas.")
    elif text.startswith("🚀"):
        with manager.lock:
            running = sum(item[0].poll() is None for item in manager.running.values())
            if uid == OWNER_ID:
                counts = f"Users: {len(manager.users)}\nApproved: {sum(u['approved'] for u in manager.users.values())}\nProjects: {len(manager.projects)}\nRunning: {running}"
            else:
                counts = f"Your projects: {len(ids)}\n" + "\n".join(manager.projects[p]["name"] + ": " + manager.status(uid, p) for p in ids)
        send(uid, f"{counts}\nUptime: {int(time.monotonic() - started)}s\nExecution enabled: {settings.execution}")
    elif text.startswith("⚙"):
        send(uid, "Select a project below, then Recommended Install. This optional large package set may exceed Free-plan resources.", reply_markup=files_menu(uid))
    else:
        send(uid, "Use the menu or /help. Secrets must be uploaded as a project .env file, not pasted into commands.")


@bot.message_handler(content_types=["document"])
@guarded()
def document_handler(message):
    uid = message.from_user.id
    require_approval(uid)
    doc = message.document
    name = doc.file_name or ""
    with pending_lock:
        choice = pending.get(uid)
    if choice and time.monotonic() - choice[2] > 600:
        with pending_lock:
            pending.pop(uid, None)
        raise HostingError("Project configuration selection expired. Select the project again.")
    limit = 65536 if choice else MAX_UPLOAD
    if not doc.file_size or doc.file_size > limit:
        raise HostingError(f"Upload must be nonempty and at most {limit} bytes.")
    if choice and name != choice[1]:
        raise HostingError(f"Waiting for {choice[1]} for the selected project. Use /cancel to upload a new project.")
    if not choice:
        from hosting import filename
        filename(name)
        if not name.lower().endswith((".py", ".zip")):
            raise HostingError("Upload .py or .zip, or select a project's Token / .env or Requirements button first.")
    info = bot.get_file(doc.file_id)
    data = bot.download_file(info.file_path)
    if len(data) > limit:
        raise HostingError("Downloaded file exceeds the size limit.")
    if choice:
        pid, expected, _ = choice
        manager.configure(uid, pid, expected, data)
        with pending_lock:
            pending.pop(uid, None)
        # Best-effort remove the credential document from the Telegram chat. Telegram retains copies independently.
        if expected == ".env":
            with contextlib.suppress(Exception):
                bot.delete_message(uid, message.message_id)
        send(uid, "Project configuration saved. Run Install for dependencies, then Run. No token was displayed.")
    else:
        pid = manager.upload(uid, name, data)
        notify_owner(f"Project uploaded by user {uid}: {name}")
        project_menu(uid, pid)


@bot.callback_query_handler(func=lambda call: True)
@guarded(callback=True)
def callback_query(call):
    uid, data = call.from_user.id, call.data or ""
    if data.startswith(("approve_", "reject_")):
        action, raw = data.split("_", 1)
        if not raw.isdigit():
            raise HostingError("Invalid user ID.")
        manager.authorize(uid, int(raw), action == "approve")
        send(uid, "User approved." if action == "approve" else "User rejected; their projects were stopped.")
        with contextlib.suppress(Exception):
            send(int(raw), "Access approved. Send /start." if action == "approve" else "Access rejected by owner.")
        return
    require_approval(uid)
    if data == "myfiles":
        send(uid, "Your files:", reply_markup=files_menu(uid))
        return
    action, sep, pid = data.partition(":")
    if not sep:
        raise HostingError("Old or invalid button. Open My Files again.")
    manager.project(uid, pid)
    if action == "file":
        project_menu(uid, pid)
    elif action in {"run", "stop", "restart"}:
        result = {"run": manager.start, "stop": manager.stop, "restart": manager.restart}[action](uid, pid)
        send(uid, result)
        notify_owner(f"Project {action} requested by user {uid}.")
    elif action == "status":
        send(uid, manager.status(uid, pid))
    elif action == "logs":
        send(uid, manager.log_text(uid, pid))
    elif action in {"env", "req"}:
        expected = ".env" if action == "env" else "requirements.txt"
        with pending_lock:
            pending[uid] = (pid, expected, time.monotonic())
        send(uid, f"Upload {expected} for this project within 10 minutes. Stop it first. /cancel cancels.\n"
             + ("Use BOT_TOKEN=the_uploaded_bot_token on its own line. Never use the hosting token." if action == "env" else "Use standard pip requirements, one dependency per line. Install runs build code: trusted dependencies only."))
    elif action in {"install", "recommended"}:
        async_install(uid, pid, action == "recommended")
    else:
        raise HostingError("Unknown button. Open My Files again.")


def main():
    server = None
    def shutdown(signum, frame):
        stopping.set()
        bot.stop_polling()
        # Signal handler raises to prevent infinity_polling from restarting after shutdown.
        raise KeyboardInterrupt
    try:
        manager.acquire_instance_lock()
        server = health_server(settings.port, stopping)
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        # Explicitly verify credentials/network before declaring Telegram ready.
        bot.get_me()
        print("Hosting bot authenticated; health endpoint active. Restoring saved running state.")
        for pid, outcome in manager.restore():
            print("Project restore: " + outcome)
        bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Startup/polling failed. Verify HOST_BOT_TOKEN, outbound HTTPS, one polling instance, writable DATA_DIR, and PORT availability.", file=sys.stderr)
        return 1
    finally:
        stopping.set()
        manager.close()
        if server:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
