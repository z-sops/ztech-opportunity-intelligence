# MCP Contract (v1)

Server name `ztech-opportunity-intelligence`, version `1.0.0`. Built on the official MCP Python SDK v2 (`MCPServer`).

## Running

```bash
ztech-oi-mcp                                   # stdio (default) — spawn as a child process
ztech-oi-mcp --transport streamable-http --port 8765   # remote later; engine unchanged
```

Logs go to **stderr** as JSON. stdout is reserved for the MCP protocol.

## Tools

All tools return **structured content** (a JSON object). Errors come back as `isError: true` with a JSON text body `{"error": "<ErrorCode>", "message": "...", "retryable": bool}`. Pydantic input-validation errors name the offending field.

| Tool | Input | Output |
|---|---|---|
| `analyze_prospect_opportunity` | `prospect: ProspectInput`, `options?: ResearchOptions`, `response?: "summary"\|"full"` (default summary) | summary view, or the full `IntelligenceReport` |
| `research_company` | `prospect`, `response?` | report with no competitors |
| `discover_competitors` | `prospect`, `max_competitors?` (1-10) | `{prospect, status, competitors[], evidence[], limitations[], errors[]}` |
| `research_competitors` | `prospect`, `competitors: CompetitorHint[1..5]`, `response?` | report comparing only those competitors |
| `analyze_opportunity` | `research_id` | re-derived `{comparisons, opportunities, opportunity_score, sales_angles, stale_evidence_count}`; **no network** |
| `get_opportunity_report` | `research_id`, `sections?: string[]` | full canonical report, or the chosen sections plus identity fields |
| `get_opportunity_timeline` | `research_id` **or** `domain` **or** `company_name` | `{prospect_entity_key, research_ids[], score_history[], events[]}` merged across runs |
| `get_engine_info` | — | engine/schema version, configured providers (no secrets), confidence documentation |

`research_id` must match `^[a-z]+_[A-Za-z0-9_]+$`, so path-like strings are rejected. Allowed `sections`: `prospect, competitors, evidence, observations, signals, advertising_intelligence, content_intelligence, social_intelligence, comparisons, changes, opportunities, opportunity_score, sales_angles, timeline, conflicts, limitations, provider_status, telemetry`.

### Example call

```json
{"name": "analyze_prospect_opportunity",
 "arguments": {"prospect": {"company_name": "SweetCrumb Bakery", "domain": "sweetcrumb.com",
   "location": "Chicago", "industry": "Bakery", "products_services": ["wedding cakes"],
   "known_competitors": [{"company_name": "Rival Bakes", "domain": "rivalbakes.com"}]},
   "options": {"max_competitors": 3, "idempotency_key": "ztech-lead-1842"}}}
```

See `examples/sample_output_summary.json` for the response shape.

## What the server will never do

It has no tools for SQL, filesystem access, shell execution, credentials, sending email/WhatsApp, or writing to ZTech. These absences are asserted by `tests/test_mcp.py::test_tool_surface_is_small_and_safe`.

## ZTech (Electron / Node.js) integration — tested

`clients/node/mcp_client_test.mjs` is a runnable contract test that uses the official `@modelcontextprotocol/sdk` (v1.x). It spawns the Python server over stdio, lists tools, runs a research, reads the canonical report, and checks idempotency, error codes and that no secrets leak. The snippet below is the same pattern.

ZTech spawns the server as a child process from the Electron **main** process. It never runs in the renderer.

```js
// main process — @modelcontextprotocol/sdk
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const transport = new StdioClientTransport({
  command: "ztech-oi-mcp",                       // or: python -m ztech_oi.adapters.mcp_server
  env: { ...process.env, ZTECH_OI_DB_PATH: oiDbPath, BRAVE_API_KEY: secrets.brave },
});
const client = new Client({ name: "ztech", version: "1.0.0" });
await client.connect(transport);

const res = await client.callTool({
  name: "analyze_prospect_opportunity",
  arguments: { prospect: { company_name: lead.name, domain: lead.domain, location: lead.city },
               options: { idempotency_key: `lead-${lead.id}` } },
});
if (res.isError) throw new Error(res.content[0].text);
const summary = res.structuredContent;           // schema_version "1.0"
// later: getOpportunityReport → map into ZTech EvidencePacket / Opportunity Timeline / Pitch context
```

Use `idempotency_key = lead id` so ZTech retries are safe. ZTech owns the mapping into its own models, and the engine never writes ZTech tables (spec §8).

## Claude Desktop / other MCP clients

```json
{"mcpServers": {"ztech-oi": {"command": "ztech-oi-mcp",
  "env": {"ZTECH_OI_DB_PATH": "/path/oi.sqlite3", "BRAVE_API_KEY": "..."}}}}
```
