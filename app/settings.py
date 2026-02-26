from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
WEB_DIR = BASE_DIR / "web"
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DB_PATH = DATA_DIR / "app.db"
DEFAULT_LLM_CONFIG_PATH = CONFIG_DIR / "llm.json"

MAX_UPLOAD_MB = 200
MAX_PROMPT_CHARS = 120_000
DEFAULT_TOP_K_CHUNKS = 12

