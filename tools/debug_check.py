"""Run one bot check in dry-run mode (no Discord messages, separate state file) and save the output."""
import io
import os
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

bot.STATE_FILE = Path("/tmp/debug_state.json")
if Path("state.json").exists():
    bot.STATE_FILE.write_text(Path("state.json").read_text())
buf = io.StringIO()
code = 0
with redirect_stdout(buf), redirect_stderr(buf):
    try:
        cfg = bot.load_config()
        bot.check(cfg, dry_run=True, now=bot.pd.Timestamp.now(tz="UTC"))
    except SystemExit as e:
        code = e.code
        print("SystemExit:", e.code)
    except BaseException:
        traceback.print_exc()
print(buf.getvalue())
Path("research_results.md").write_text(f"exit: {code}\n\n```\n{buf.getvalue()[-6000:]}\n```")
