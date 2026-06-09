# ris-mcp

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-server-6E40C9)
![Austrian law](https://img.shields.io/badge/data-RIS%20OGD%20API-EF3340)
![License: MIT](https://img.shields.io/badge/License-MIT-2EA44F)

A local **MCP server for Austria's RIS Open-Government API**
([data.bka.gv.at/ris/api](https://data.bka.gv.at/ris/api/v2.6/)). It gives
Claude, Msty, LM Studio and other MCP clients tools for **federal law**
(consolidated), **state law** (consolidated) and **case law** (OGH, VfGH,
VwGH, BVwG, …). Every response comes back as clean Markdown — ready for a
language model and readable by a human.

The server is built for **local models**: it bundles the typical research
chain into single calls so even small models don't have to orchestrate many
tool calls, and it is tuned to make hallucinating legal content hard — every
case number, date, document ID and headnote is taken verbatim from the API.

In practice, you ask in natural language and the assistant picks the right
tool:

> *„Was sagt § 1295 ABGB und welche OGH-Judikatur gibt es dazu?"*
> *„OGH-Entscheidungen zu Hund Schmerzengeld."*
> *„Volltext von § 19 Wiener Bauordnung."*

> No API key required — the RIS-OGD interface is open. The server caches
> locally and rate-limits its requests to be a good citizen.

## Scope

The focus is **Austrian civil law and the OGH** — that's where the workflow
is most polished. The same tools also reach the other courts (VfGH, VwGH,
BVwG, LVwG, DSB, plus OLG/LG via the *Justiz* application) and the
**consolidated state law** of all nine federal states.

## Tools

| Tool | Purpose |
|------|---------|
| `ris_recherche_norm` | **One-call research:** full text of a provision **plus** the most relevant OGH headnotes, in one document. The easiest entry point for "case law on § X". |
| `bundesrecht_search` | Search the consolidated federal collection (BrKons). |
| `bundesrecht_get_norm` | Full text of a single section / article / annex. |
| `bundesrecht_get_gesamte_vorschrift` | A whole statute (e.g. the complete ABGB) as HTML→Markdown. |
| `landesrecht_search` | Search consolidated **state law** (LrKons); use the `bundesland` filter. |
| `landesrecht_get_norm` | Full text of a single state-law provision. |
| `ogh_search` | OGH search, optimised for the civil-law workflow. Searches headnotes and decision texts automatically and falls back to Austrian spelling (e.g. *Schmerzengeld*). |
| `judikatur_search` | Generic search for every other court (`gericht` enum). Same auto two-mode behaviour as `ogh_search`. |
| `judikatur_get_entscheidung` | Full text of a headnote or decision document. |
| `ris_local_search` | FTS5 search over locally cached documents (no RIS call). |
| `ris_health` | Server and cache status. |

## Works with

Any MCP-compatible client:

| Client | Model | Privacy |
|--------|-------|---------|
| Claude Desktop (Anthropic) | Cloud | Query text goes to Anthropic |
| ChatGPT Desktop (OpenAI) | Cloud | Query text goes to OpenAI |
| Msty Studio | Cloud or local | Fully local possible |
| LM Studio | Local | Stays on your machine |
| Mistral Vibe | Cloud or local | Depends on configuration |
| Any other MCP-compatible client | — | — |

> RIS-OGD is **public open data** — no client documents touch this server.
> The privacy column refers only to what the LLM (cloud or local) sees of
> the query text you type.

**Note on local models:** the model must support reliable tool calling.
**Qwen3 6B+** and **Gemma 4** work well; smaller models (under 4B) may
struggle.

## Installation

```bash
git clone <repo-url> ris-mcp
cd ris-mcp
uv sync
```

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

## MCP client configuration

In your client's MCP config (e.g. Msty: Settings → MCP, or an `mcp.json`):

```jsonc
{
  "mcpServers": {
    "ris": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--directory", "/path/to/ris-mcp", "ris-mcp"]
    }
  }
}
```

Adjust the path and restart the client — all tools then appear under the
server `ris`.

> **macOS tip:** use the **absolute path** to `uv` (find it with `which uv`).
> Apps launched from the Dock get a minimal `PATH`, so a bare `"command":
> "uv"` may fail to start the server intermittently.

## Examples

**Provision + case law in one call:**

```
ris_recherche_norm(gesetz="ABGB", paragraph=1295)
```

returns the full text of § 1295 ABGB together with the most relevant OGH
headnotes (each with its headnote text and `doc_id`). For the full reasoning
of a decision, follow up with `judikatur_get_entscheidung(doc_id="…")`.

**Topic / fact-pattern search:**

```
ogh_search(suchworte="Hund Schmerzengeld")
```

returns recent OGH decisions on the topic, newest first. Keep search terms
sparse — they are AND-combined, and the concrete facts ("Hund", "gebissen")
live in the decision text, not in the abstract headnote.

## Research system prompt

A matching system prompt — [`recherche-systemprompt.md`](recherche-systemprompt.md) —
ships with the server, for use with a local model. It enforces a strict
anti-hallucination discipline: no legal content from the model's own
knowledge, only verbatim from RIS.

## Cache

A single SQLite file (`~/Library/Caches/ris-mcp/cache.db` on macOS,
`~/.cache/ris-mcp/cache.db` on Linux) with three areas: `search_cache`
(search results, 24 h TTL), `document_cache` (full texts, 30-day TTL,
historical versions kept permanently) and `meta` (HTTP ETag / Last-Modified
per URL for conditional requests). An FTS5 index over the cached texts backs
`ris_local_search`. The cache lives outside the repo; delete the file to
reset.

Settings can be overridden via `RIS_MCP_*` environment variables
(see [`src/ris_mcp/config.py`](src/ris_mcp/config.py)).

## Note

This is a private research-support tool, not a substitute for legal review.
The authoritative text is always the official RIS original. Assessment and
subsumption remain the task of the lawyer.

## License

[MIT](LICENSE).
