"""
hannah/agent.py  —  LiveKit Agents 1.x
Hannah AI Research Assistant

Architecture note:
  LiveKit spawns the entrypoint in a CHILD process (separate PID from the
  main worker process). Writing to a global dict in the child does NOT
  affect the main process where the HTTP server runs.

  Fix: write state to a temp JSON file on disk. HTTP server reads that file.
  Both processes share the filesystem, so this works reliably.
"""

import json, logging, os, tempfile, threading, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, Agent, JobContext, WorkerOptions, function_tool
from livekit.plugins import deepgram, silero, google

load_dotenv()
logger = logging.getLogger("hannah")

# ── State file — shared between main process and child processes ────
STATE_FILE = Path(tempfile.gettempdir()) / "hannah_state.json"
ROOT       = Path(__file__).parent.parent

def _write(payload: dict):
    """Write state to the shared temp file."""
    try:
        STATE_FILE.write_text(json.dumps(payload))
    except Exception:
        pass

def _read() -> dict:
    """Read state from the shared temp file."""
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

# Initialise the file
_write({
    "agent_state": "idle",
    "transcript":  "All systems ready.",
    "user":        "",
    "tool":        "",
    "source":      "",
    "results":     "",
    "queries":     0,
    "tools":       0,
    "connected":   False,
})

# ── HTTP server (runs in main process) ──────────────────────────────
class _H(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        if self.path.startswith("/state"):
            body = STATE_FILE.read_bytes() if STATE_FILE.exists() else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            f = ROOT / "hannah_ui.html"
            if f.exists():
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404, "hannah_ui.html not found")

def _start_server(port: int = 8765):
    srv = HTTPServer(("localhost", port), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    logger.info("Hannah UI → http://localhost:%d", port)
    webbrowser.open(f"http://localhost:{port}")

# ── Personality ──────────────────────────────────────────────────────
INSTRUCTIONS = """
You are Hannah, a sharp and warm AI research assistant specialising in
biotech, pharma, life sciences, bioinformatics, and AI/ML in medicine.

Personality: like a trusted colleague — direct, precise, warm, never stiff.
Natural conversation.

Before every tool call say one of these (vary them, don't repeat):
  "One sec, let me look into that…"
  "Give me a moment to check…"
  "Let me pull that up for you…"
  "One moment while I dig into that…"
  "Hold on, checking now…"
  "Let me see what I can find…"
  "Just a second, grabbing the latest…"

After results, give a concise spoken summary — 2-4 sentences.
Always cite the source and date. Never invent findings or approvals.
""".strip()

# ── Agent ─────────────────────────────────────────────────────────────
class Hannah(Agent):
    def __init__(self): super().__init__(instructions=INSTRUCTIONS)

    def _push(self, **kw):
        """Merge kw into the current state file."""
        state = _read()
        state.update(kw)
        _write(state)

    @function_tool()
    async def get_pubmed_papers(
        self,
        query: Annotated[str, "Search terms"],
        max_results: Annotated[int, "1-10"] = 5,
    ) -> str:
        """Search PubMed for recent biomedical papers."""
        s = _read(); s.update(tool="get_pubmed_papers", source="PubMed",
            results="fetching…", tools=s.get("tools",0)+1); _write(s)
        from tools import get_pubmed_papers as f
        return await f(query=query, max_results=max_results)

    @function_tool()
    async def get_biorxiv_preprints(
        self,
        query: Annotated[str, "Topic"],
        server: Annotated[str, "biorxiv or medrxiv"] = "biorxiv",
        days_back: Annotated[int, "Days back (max 90)"] = 30,
        max_results: Annotated[int, "1-10"] = 5,
    ) -> str:
        """Fetch preprints from bioRxiv or medRxiv."""
        s = _read(); s.update(tool="get_biorxiv_preprints",
            source=server.capitalize(), results="fetching…",
            tools=s.get("tools",0)+1); _write(s)
        from tools import get_biorxiv_preprints as f
        return await f(query=query, server=server, days_back=days_back,
                       max_results=max_results)

    @function_tool()
    async def get_arxiv_papers(
        self,
        query: Annotated[str, "Search terms"],
        category: Annotated[str, "q-bio, cs.LG, cs.AI, stat.ML"] = "q-bio",
        max_results: Annotated[int, "1-10"] = 5,
    ) -> str:
        """Search arXiv for AI/ML and biology papers."""
        s = _read(); s.update(tool="get_arxiv_papers", source="arXiv",
            results="fetching…", tools=s.get("tools",0)+1); _write(s)
        from tools import get_arxiv_papers as f
        return await f(query=query, category=category, max_results=max_results)

    @function_tool()
    async def get_fda_updates(
        self,
        search_type: Annotated[str, "drug_approvals, drug_events, or news"] = "drug_approvals",
        query: Annotated[str, "Optional drug or topic filter"] = "",
        max_results: Annotated[int, "1-10"] = 5,
    ) -> str:
        """Fetch FDA regulatory updates."""
        s = _read(); s.update(tool="get_fda_updates", source="FDA openFDA",
            results="fetching…", tools=s.get("tools",0)+1); _write(s)
        from tools import get_fda_updates as f
        return await f(search_type=search_type, query=query or None,
                       max_results=max_results)

    @function_tool()
    async def get_ema_updates(
        self,
        max_results: Annotated[int, "1-10"] = 5,
    ) -> str:
        """Fetch EMA medicine updates."""
        s = _read(); s.update(tool="get_ema_updates", source="EMA RSS",
            results="fetching…", tools=s.get("tools",0)+1); _write(s)
        from tools import get_ema_updates as f
        return await f(max_results=max_results)

    @function_tool()
    async def get_biotech_news(
        self,
        source: Annotated[str, "fierce, biopharma, biospace, nature, or all"] = "all",
        max_results: Annotated[int, "1-10"] = 5,
    ) -> str:
        """Fetch biotech and pharma industry news."""
        s = _read(); s.update(tool="get_biotech_news", source="Biotech News",
            results="fetching…", tools=s.get("tools",0)+1); _write(s)
        from tools import get_biotech_news as f
        return await f(source=source, max_results=max_results)

    @function_tool()
    async def get_topic_summary(
        self,
        topic: Annotated[str, "Topic to summarise"],
        include_papers: Annotated[bool, "Include PubMed"] = True,
        include_preprints: Annotated[bool, "Include bioRxiv"] = True,
        include_news: Annotated[bool, "Include news"] = True,
    ) -> str:
        """Get a combined snapshot of literature and news."""
        s = _read(); s.update(tool="get_topic_summary", source="All Sources",
            results="fetching…", tools=s.get("tools",0)+1); _write(s)
        from tools import get_topic_summary as f
        return await f(topic=topic, include_papers=include_papers,
                       include_preprints=include_preprints,
                       include_news=include_news)

# ── Entrypoint (runs in child process) ──────────────────────────────
async def entrypoint(ctx: JobContext):
    logger.info("Hannah → room: %s", ctx.room.name)
    await ctx.connect()

    session = AgentSession(
        stt=deepgram.STT(model="nova-2"),
        llm=google.LLM(model="gemini-2.5-flash-lite"),
        tts=deepgram.TTS(model="aura-2-thalia-en"),
        vad=silero.VAD.load(),
    )

    @session.on("agent_state_changed")
    def on_state(ev):
        try:
            raw   = str(getattr(ev, "new_state", getattr(ev, "state", "")))
            lower = raw.lower()
            if   "speak"  in lower: mapped = "speaking"
            elif "think"  in lower: mapped = "thinking"
            elif "listen" in lower: mapped = "listening"
            else:                   mapped = "idle"
            s = _read(); s.update(agent_state=mapped, connected=True); _write(s)
        except Exception as exc:
            logger.debug("on_state error: %s", exc)

    @session.on("user_input_transcribed")
    def on_user(ev):
        try:
            if getattr(ev, "is_final", True):
                text = getattr(ev, "transcript", "")
                if text:
                    s = _read()
                    s.update(user=text, queries=s.get("queries",0)+1,
                             agent_state="thinking", connected=True)
                    _write(s)
        except Exception as exc:
            logger.debug("on_user error: %s", exc)

    @session.on("conversation_item_added")
    def on_item(ev):
        try:
            item = getattr(ev, "item", None)
            if item and getattr(item, "role", "") == "assistant":
                text = getattr(item, "text_content", "") or ""
                if text:
                    s = _read(); s.update(transcript=text, connected=True); _write(s)
        except Exception as exc:
            logger.debug("on_item error: %s", exc)

    # Mark connected immediately so overlay disappears
    s = _read()
    s.update(connected=True, agent_state="speaking",
             transcript="Hi, I'm Hannah. What can I help you with today?")
    _write(s)

    await session.start(room=ctx.room, agent=Hannah())

    await session.generate_reply(
        instructions=(
            "Say exactly: 'Hi, I'm Hannah. What can I help you with today?' "
            "Nothing more. Keep it exactly that short."
        )
    )

# ── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _start_server(8765)
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
