"""Central configuration — reads from environment variables only."""
import os
from dotenv import load_dotenv

load_dotenv()

# ── SurrealDB ────────────────────────────────────────────────
SURREALDB_URL  = os.environ.get("SURREALDB_URL",  "ws://localhost:8000/rpc")
SURREALDB_NS   = os.environ.get("SURREALDB_NS",   "hackathon")
SURREALDB_DB   = os.environ.get("SURREALDB_DB",   "journey_agent")
SURREALDB_USER = os.environ.get("SURREALDB_USER", "root")
SURREALDB_PASS = os.environ.get("SURREALDB_PASS", "root")

# ── Azure OpenAI ──────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT   = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY    = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT", "text-embedding-3-small")

# ── LangSmith ────────────────────────────────────────────────
LANGCHAIN_TRACING_V2 = os.environ.get("LANGCHAIN_TRACING_V2", "false")
LANGCHAIN_API_KEY    = os.environ.get("LANGCHAIN_API_KEY", "")
LANGCHAIN_PROJECT    = os.environ.get("LANGCHAIN_PROJECT", "journey-orchestration-agent")

# ── Agent tuning ─────────────────────────────────────────────
CONFIDENCE_THRESHOLD      = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.55"))
KYC_FRESHNESS_MONTHS      = int(os.environ.get("KYC_FRESHNESS_MONTHS", "12"))
HIGH_VALUE_FEE_THRESHOLD  = float(os.environ.get("HIGH_VALUE_FEE_THRESHOLD", "500.0"))
