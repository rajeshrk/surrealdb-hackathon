"""
Streamlit entry point — Adaptive Customer Journey Orchestration Agent.

Run with:
    streamlit run app.py
"""
import streamlit as st

from db.client import SurrealClient, run_sync

st.set_page_config(
    page_title="Journey Orchestration Agent",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Bootstrap SurrealDB on first load ─────────────────────────────────────

if "db_bootstrapped" not in st.session_state:
    with st.spinner("Connecting to SurrealDB and bootstrapping schema…"):
        async def _bootstrap() -> None:
            db = await SurrealClient.connect()
            await db.bootstrap()
            await db.close()

        try:
            run_sync(_bootstrap())
            st.session_state.db_bootstrapped = True
        except Exception as exc:
            st.error(
                f"❌ Could not connect to SurrealDB: {exc}\n\n"
                "Make sure SurrealDB is running:\n"
                "```\n"
                "docker run --rm -p 8000:8000 surrealdb/surrealdb:latest start "
                "--user root --pass root memory\n"
                "```"
            )
            st.stop()

# ── Seed document embeddings once ─────────────────────────────────────────

if "embeddings_seeded" not in st.session_state:
    import config

    if config.AZURE_OPENAI_API_KEY:
        with st.spinner("Generating document embeddings…"):
            async def _seed_embeddings() -> None:
                from langchain_openai import AzureOpenAIEmbeddings
                import db.queries as Q

                db = await SurrealClient.connect()
                emb_model = AzureOpenAIEmbeddings(
                    azure_deployment=config.AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT,
                    azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                    api_key=config.AZURE_OPENAI_API_KEY,
                    api_version=config.AZURE_OPENAI_API_VERSION,
                )
                docs = await db.query(
                    "SELECT id, content FROM document WHERE embedding = []"
                )
                for doc in docs:
                    doc_id = str(doc.get("id", "")).split(":")[-1]
                    content = doc.get("content", "")
                    if content and doc_id:
                        emb = await emb_model.aembed_query(content)
                        await Q.update_document_embedding(db, doc_id, emb)
                await db.close()

            try:
                run_sync(_seed_embeddings())
            except Exception:
                pass  # Non-fatal — vector search just returns no results

    st.session_state.embeddings_seeded = True

# ── Main page ──────────────────────────────────────────────────────────────

st.title("🏦 Adaptive Customer Journey Orchestration Agent")
st.markdown(
    """
    A real-time agentic system that dynamically orchestrates customer journeys in financial
    services — powered by **SurrealDB** (graph + relational + vector) and **LangGraph**.

    ---

    ### Navigate using the sidebar:

    | Page | Description |
    |------|-------------|
    | 💬 **Customer Portal** | Chat interface — trigger agent recommendations |
    | 📊 **Advisor Dashboard** | Knowledge graph, journey timeline, approval queue |
    | 🛡️ **Compliance Ops** | Toggle rules, audit trail, real-time graph evolution |

    ---

    ### Demo Personas
    | Customer | Scenario |
    |----------|----------|
    | **Sarah Chen** | New-to-bank millennial — chat "I just had a baby" |
    | **James Morrison** | Wealth client with expired KYC — investment blocked |
    | **Maria Santos** | At-risk retention — negative sentiment pattern |
    """
)

col1, col2, col3 = st.columns(3)
with col1:
    st.metric("SurrealDB", "Graph + Vector + Relational", "Single engine")
with col2:
    st.metric("LangGraph", "6-node StateGraph", "Checkpointed")
with col3:
    st.metric("Compliance", "3-tier enforcement", "Real-time")
