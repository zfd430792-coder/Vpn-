"""Проверяем, что каждому хендлеру хватит того, что даёт aiogram + мидлварь."""
import inspect, sys, os, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from anibot import config, handlers
from anibot.db import Database
from anibot.middlewares import Deps
from anibot.userbot import Userbot

# что реально лежит в data к моменту вызова хендлера
PROVIDED = {
    # от aiogram
    "bot", "event_from_user", "event_chat", "event_update", "event_router",
    "dispatcher", "state", "raw_state", "fsm_storage", "event_context",
    "callback_data", "command", "handler",
    # от нашей мидлвари
    "db", "cfg", "userbot", "is_admin",
}

dp = Dispatcher(storage=MemoryStorage())
cfg = config.Config(bot_token="1:x", admins={1}, data_dir=Path(os.environ["DATA_DIR"]))
dp.update.outer_middleware(Deps(Database(cfg.db_path), cfg, Userbot(0, "", "")))
handlers.setup(dp)

problems, total = [], 0
for router in dp.sub_routers:
    for event_name, observer in router.observers.items():
        for h in observer.handlers:
            fn = h.callback
            if not inspect.isfunction(fn):
                continue
            total += 1
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            for i, p in enumerate(params):
                if i == 0:
                    continue  # первый — сам event
                if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
                    continue
                if p.default is not p.empty:
                    continue  # есть значение по умолчанию — не страшно
                if p.name not in PROVIDED:
                    problems.append(
                        f"{router.name}.{fn.__name__} ({event_name}): "
                        f"обязательный аргумент '{p.name}' никто не передаёт"
                    )

print(f"Проверено хендлеров: {total}")
if problems:
    print(f"\n❌ Проблем: {len(problems)}")
    for p in problems:
        print("   -", p)
    sys.exit(1)
print("✅ Все подписи совпадают с тем, что прокидывается")
