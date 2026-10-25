# siftwise with MCP clients

How to connect `siftwise mcp` to each coding agent, and how to check it works. Every config
format below comes from the client's official docs (linked in a comment above each snippet,
checked 2026-10-25).

Tested on Windows with Claude Code, GitHub Copilot in VS Code, Cursor, Codex CLI and Gemini
CLI. Windsurf and Cline configs follow their docs but are untested.

The tests (2026-10-25, siftwise built from this repository, a local server, the
`mcp-smoke/shop` demo repo): `select_tests`, `triage_failure` and `explain` worked in each
tested client, and fail-open with the server down was checked in Cursor. Not tested: Windsurf,
Cline, the Codex VS Code extension, the Gemini Code Assist VS Code extension (Google sign-in
failed with "The caller does not have permission", unrelated to siftwise), and macOS or Linux.

| Client | Status | Config file | How siftwise finds your repository |
|---|---|---|---|
| [Claude Code](#claude-code) | tested (Windows) | `~/.claude.json` (`claude mcp add`) or `.mcp.json` | `CLAUDE_PROJECT_DIR` (Claude Code sets it) |
| [GitHub Copilot in VS Code](#github-copilot-in-vs-code) | tested (Windows) | `.vscode/mcp.json` | MCP roots, else the workspace folder it starts in |
| [Cursor](#cursor) | tested (Windows) | `.cursor/mcp.json` or `~/.cursor/mcp.json` | MCP roots: no path to configure |
| [Codex CLI](#codex-cli) | tested (Windows) | `~/.codex/config.toml` | the working directory: its `cwd` setting |
| [Gemini CLI](#gemini-cli) | tested (Windows) | `.gemini/settings.json` or `~/.gemini/settings.json` | the working directory: its `cwd` setting |
| [Windsurf](#windsurf-devin-desktop) | untested | `mcp_config.json` (global) | `SIFTWISE_REPO_PATH`, or the `repo_path` argument |
| [Cline](#cline) | untested | `cline_mcp_settings.json` (global) | `SIFTWISE_REPO_PATH`, or the `repo_path` argument |

## What every client needs

**The command:** `siftwise mcp`, from `uv tool install 'siftwise[mcp]'` (or
`pip install 'siftwise[mcp]'`). Clients start it without a shell, so `siftwise` must be on the
PATH the client sees, or give the full path from `uv tool dir --bin`.

**The environment.** Set these in the client's config, not only in your shell. The official
MCP TypeScript SDK starts stdio servers with a short list of inherited variables (`HOME`,
`PATH`, `USER` and a few more; `APPDATA`, `USERPROFILE` and the like on Windows) plus what the
config sets
([`stdio.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/main/packages/client/src/client/stdio.ts));
which clients build on it is unverified, so don't count on your shell's variables reaching
siftwise. Gemini CLI's docs say it removes inherited variables whose names contain `TOKEN`.

| Variable | | |
|---|---|---|
| `SIFTWISE_SERVER` | required unless it's `http://localhost:8000` | your siftwise API |
| `SIFTWISE_API_TOKEN` | required | the server's token: keep it out of committed files |
| `SIFTWISE_REPO` | optional | `owner/name` in siftwise; default from the `origin` remote |
| `SIFTWISE_BASE_REF` | optional | the base branch; default `origin/HEAD`, else `main` |
| `SIFTWISE_REPO_PATH` | optional | the repository, when the client starts the server elsewhere |

**The repository.** Each tool works on a git checkout, the first of these that applies:

1. the tool's `repo_path` argument (an absolute path; the agent can pass it);
2. `SIFTWISE_REPO_PATH`;
3. `CLAUDE_PROJECT_DIR`, which Claude Code sets for the servers it starts;
4. the client's workspace folders, as
   [MCP roots](https://modelcontextprotocol.io/specification/2025-06-18/client/roots): the
   first one inside a git repository. siftwise asks only clients that declare the roots
   capability, and gives up after 5 s;
5. the server's working directory.

A subdirectory of a repository works too (siftwise uses its top level). The client's own
folders (3 and 4) are forgiving: one that isn't in a git repository but holds exactly one
directly inside (a workspace one level above the checkout) means that one, and the result
says so in `repository_note`; anything else moves on to the next step. The folders you set
(1 and 2) are taken as meant: outside a git repository they're an error.

Clients start stdio servers in different places. When nothing gives a repository, every tool
answers with a message like
`the server's working directory, C:\Users\you, is not inside a git repository: call again with
repo_path set to the repository's absolute path, or set SIFTWISE_REPO_PATH ... Git
repositories one level down: ...`, and
`select_tests` says to run the full suite. Whether a given agent then retries with `repo_path`
on its own is untested.

**Fail open.** If the server is down, the token is missing or anything errors,
`select_tests` says to run the full suite and `triage_failure` says verdicts are unavailable.
The token never appears in a tool result.

**Timeouts.** Each API call gives up after 30 s, inside Codex's 60 s default tool timeout, so
the agent gets the fail-open answer instead of a client error. Startup (to `initialize`) took
2-4 s on Windows in the smoke test; Codex's default startup timeout is 10 s.

## Test it

### 1. The smoke test (any machine, no client)

[`scripts/mcp_smoke.py`](../scripts/mcp_smoke.py) starts `siftwise mcp` over stdio the way
clients do (the MCP SDK's client: a minimal environment, the `initialize` handshake) and
checks every tool and the fail-open cases against a real server:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d   # the API on :8000
uv run --no-sync python scripts/mcp_smoke.py                    # this checkout's code
uv run --no-sync python scripts/mcp_smoke.py --command siftwise mcp   # an installed siftwise
uv run --no-sync python scripts/mcp_smoke.py --keep             # keep the demo repo for step 2
```

It reads the token from `SIFTWISE_API_TOKEN` or the repo's `.env` and never prints it. Each
check prints PASS or FAIL; the exit code is the number of failures. The demo repo is the
Quickstart's example project, recorded as `mcp-smoke/shop`: `main` passed, a `feature`
branch changes `shop/cart.py`, and `junit-failing.xml` has one failing cart test.

### 2. By hand, in each client

Run the smoke test with `--keep`, open the kept `shop` folder in the client (it prints the
path), and configure siftwise as in the client's section with `SIFTWISE_REPO=mcp-smoke/shop`
(the demo has no `origin` remote) and `SIFTWISE_BASE_REF=main`. Then type:

| Prompt | Expected |
|---|---|
| `Which tests should I run for my change? Use siftwise.` | `select_tests`: mode `selective`, command `pytest tests/test_cart.py`, 2 of 4 known tests |
| `The tests failed and wrote junit-failing.xml. Ask siftwise before fixing anything.` | `triage_failure` with `junit_paths: ["junit-failing.xml"]`: `tests.test_cart::test_total` is `new_regression`; next step "Fix the 1 new_regression failure(s) first." |
| `Why does siftwise select tests.test_cart::test_total?` | `explain`: known, selected, linked through `shop/cart.py`, `file_path` `tests/test_cart.py` |
| Stop the API (`docker compose stop api`), then the first prompt again | `select_tests`: mode `full`, "could not reach siftwise ...": run the full suite |

Also check: the client lists three tools (`select_tests`, `triage_failure`, `explain`) and
shows the server as `siftwise 0.1.0` or later. Record what you see per client, including the client version,
the OS and any server output (each section says where to find it).

## Claude Code

Status: **tested** on Windows (the agent called `select_tests` on its own after a change).

```sh
# https://code.claude.com/docs/en/mcp: options before the name, the server command after --
claude mcp add --env SIFTWISE_SERVER=https://siftwise.example.com \
    --env SIFTWISE_API_TOKEN=your-token --transport stdio siftwise -- siftwise mcp
```

That's the default `local` scope (stored in `~/.claude.json`, this project only); add
`--scope user` for every project. For a team, a project `.mcp.json` expands `${VAR}` from your
environment:

```json
{
  "mcpServers": {
    "siftwise": {
      "command": "siftwise",
      "args": ["mcp"],
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "${SIFTWISE_API_TOKEN}"
      }
    }
  }
}
```

- **Repository:** `CLAUDE_PROJECT_DIR`, which Claude Code sets to the project root for the
  servers it starts ([its MCP docs](https://code.claude.com/docs/en/mcp)); no setting needed.
  The server also starts in the project, so the working directory gives the same answer.
- **Check it:** `claude mcp list`, and `/mcp` in a session.
- **Windows:** if `siftwise` isn't found, use the full path
  (`"C:\\Users\\you\\.local\\bin\\siftwise.exe"` in JSON).

## GitHub Copilot in VS Code

Status: **tested** on Windows. Use Copilot Chat in agent mode.

`.vscode/mcp.json` in the workspace:

```json
// https://code.visualstudio.com/docs/agents/reference/mcp-configuration:
// "servers", type stdio, inputs with promptString/password, cwd defaults to the workspace
{
  "inputs": [
    {
      "type": "promptString",
      "id": "siftwise-token",
      "description": "siftwise API token",
      "password": true
    }
  ],
  "servers": {
    "siftwise": {
      "type": "stdio",
      "command": "siftwise",
      "args": ["mcp"],
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "${input:siftwise-token}"
      }
    }
  }
}
```

VS Code asks for the token on first start and stores it securely, so the file can be
committed. (Leave the `//` comment lines out of the real file.)

- **Repository:** MCP roots, else the workspace folder it starts in (both worked in testing).
  VS Code "provides the MCP server with the user's workspace root folder information"
  ([its MCP developer guide](https://code.visualstudio.com/api/extension-guides/ai/mcp)), so
  siftwise uses the first workspace folder in a git repository (or the only repository
  directly inside one); otherwise the server's working directory is the workspace folder by
  default (`cwd`). No setting needed.
- **Newer config locations:** VS Code's guided flow now prefers a portable format
  (`.mcp.json` with `mcpServers`, or `~/.copilot/mcp-config.json`) and labels
  `.vscode/mcp.json` as kept for compatibility. The portable format has no `${input:...}`
  variables, so the token has to come from `${SIFTWISE_API_TOKEN}` in the environment.
- **Trust:** workspace configs need Workspace Trust; other sources prompt on first start.
- **Check it / logs:** Command Palette, **MCP: List Servers**, select siftwise, **Show
  Output**. **MCP: Reset Trust** resets MCP trust decisions.
- **Windows:** the sandbox options (`sandboxEnabled`) are macOS and Linux only.

## Cursor

Status: **tested** on Windows, fail-open with the server down included.

`.cursor/mcp.json` in the project (or `~/.cursor/mcp.json` for every project):

```json
// https://cursor.com/docs/context/mcp: mcpServers, type stdio, ${workspaceFolder}, ${env:NAME}
{
  "mcpServers": {
    "siftwise": {
      "type": "stdio",
      "command": "siftwise",
      "args": ["mcp"],
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "${env:SIFTWISE_API_TOKEN}"
      }
    }
  }
}
```

(Leave the `//` comment line out of the real file.)

- **Repository:** MCP roots, with no path configured (that's what testing used). Cursor's
  docs list Roots as supported, so siftwise asks Cursor for its workspace folders and uses
  the first one in a git repository (or the only repository directly inside one). Cursor's
  docs don't say where stdio servers start. If roots don't arrive (the
  tools say "not inside a git repository"), add `"SIFTWISE_REPO_PATH": "${workspaceFolder}"`
  to `env` in the project file (`${workspaceFolder}` is the folder that holds
  `.cursor/mcp.json`; in the global file it may not be your project).
- **The token:** `${env:SIFTWISE_API_TOKEN}` reads Cursor's own environment. An app started
  from the dock or Start menu may not have your shell's variables, so start Cursor from a
  shell that has it, or use `"envFile"` with a file outside the repository.
- **Check it:** Cursor's MCP settings list the server and its tools; tool calls need your
  approval by default.
- **Windows:** `command` must be on the PATH Cursor sees, or a full path.

## Codex CLI

Status: **tested** on Windows (the CLI; the Codex VS Code extension is untested).

`~/.codex/config.toml` (or `.codex/config.toml` in a trusted project):

```toml
# https://learn.chatgpt.com/docs/extend/mcp?surface=cli (was developers.openai.com/codex/mcp):
# [mcp_servers.<name>], cwd, env, env_vars,
# startup_timeout_sec (default 10), tool_timeout_sec (default 60)
[mcp_servers.siftwise]
command = "siftwise"
args = ["mcp"]
cwd = "/path/to/your/repo"            # Windows: 'C:\Users\you\code\repo' (single quotes)
env_vars = ["SIFTWISE_API_TOKEN"]     # forwarded from the environment you start codex in

[mcp_servers.siftwise.env]
SIFTWISE_SERVER = "https://siftwise.example.com"
```

Or `codex mcp add siftwise --env SIFTWISE_SERVER=https://siftwise.example.com
--env SIFTWISE_API_TOKEN=your-token -- siftwise mcp` (that writes the token into the file).

- **Repository:** the working directory, from `cwd` in `config.toml` (that's what testing
  used). Codex's MCP docs don't mention roots (if Codex does declare them, they're used before
  the working directory), and don't say what the default directory is. In the global file one
  fixed `cwd` means one repository; for several, leave it out and run `codex` from the
  repository (untested), or let the agent pass `repo_path`.
- **"Model not supported":** if Codex says the model isn't supported with a ChatGPT account,
  pick another model with `/model`. That's Codex's account check, not siftwise.
- **Environment:** list the token in `env_vars` (or set it in `env`). Don't rely on Codex
  passing your whole environment through.
- **Timeouts:** startup has 10 s by default; raise `startup_timeout_sec` if a cold start on
  Windows is slower.
- **Check it:** `codex mcp list`, and `/mcp` in a session.
- **Windows:** TOML single-quoted strings keep backslashes as they are.

## Gemini CLI

Status: **tested** on Windows (the CLI; the Gemini Code Assist VS Code extension is untested).

`.gemini/settings.json` in the project (or `~/.gemini/settings.json`):

```json
// https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md:
// mcpServers, cwd, env ($VAR, ${VAR}, %VAR% on Windows), timeout in ms (default 600000)
{
  "mcpServers": {
    "siftwise": {
      "command": "siftwise",
      "args": ["mcp"],
      "cwd": "/path/to/your/repo",
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "$SIFTWISE_API_TOKEN"
      },
      "timeout": 60000
    }
  }
}
```

(Leave the `//` comment lines out of the real file.) Or:
`gemini mcp add -s project -e SIFTWISE_SERVER=https://siftwise.example.com
-e SIFTWISE_API_TOKEN=your-token siftwise siftwise mcp`.

- **The token must be in `env`.** Gemini CLI removes inherited variables whose names match
  `*TOKEN*` (and `*KEY*`, `*SECRET*` ...) before starting a server; variables listed in `env`
  are kept. `$SIFTWISE_API_TOKEN` expands from the environment you start `gemini` in.
- **Repository:** the working directory, from `cwd` in `settings.json` (that's what testing
  used). Gemini CLI's MCP docs don't mention roots (if it does declare them, they're used
  first) and don't say what the default directory is. Without `cwd`, starting `gemini` in the
  repository probably works (untested); the agent can also pass `repo_path`.
- **Schemas:** Gemini CLI strips `additionalProperties` and `$schema`; siftwise's schemas
  don't depend on either.
- **Check it:** `/mcp` lists servers, their status and tools.
- **Windows:** `%SIFTWISE_API_TOKEN%` works there too.

## Windsurf (Devin Desktop)

Status: **untested**. Windsurf's MCP docs now redirect to docs.devin.ai and call the app Devin
Desktop; the config file moved with it.

Open the config from the Cascade panel: the `...` (Actions) menu, then **Open MCP config file**
(macOS/Linux `~/.config/devin/mcp_config.json`, Windows `%APPDATA%\devin\mcp_config.json`):

```json
// https://docs.devin.ai/desktop/cascade/mcp: mcpServers, ${env:NAME}, ${file:/path}
{
  "mcpServers": {
    "siftwise": {
      "command": "siftwise",
      "args": ["mcp"],
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "${file:~/.config/siftwise/token}",
        "SIFTWISE_REPO_PATH": "/path/to/your/repo"
      }
    }
  }
}
```

(Leave the `//` comment line out of the real file.)

- **Repository:** `SIFTWISE_REPO_PATH`, or the `repo_path` argument. The docs say "We
  currently support an MCP server's tools, resources, and prompts", so no roots; the working
  directory isn't documented, and there is no `cwd` or workspace variable. The config is
  global: set `SIFTWISE_REPO_PATH` for one repository, or leave it out and let the agent pass
  `repo_path` (siftwise's message tells it to; untested whether Cascade does).
- **The token:** `${file:...}` reads a file's trimmed contents (keep it out of any repository);
  `${env:SIFTWISE_API_TOKEN}` reads the app's environment.
- **Limits:** Cascade sees at most 100 tools in total across servers.
- **Check it:** the MCPs section of the `...` menu shows each server's enabled tool count.
- **Windows:** use forward slashes or doubled backslashes in JSON paths.

## Cline

Status: **untested**.

In the Cline panel: the **MCP Servers** icon, **Configure**, **Configure MCP Servers** opens
`cline_mcp_settings.json` (the Cline CLI uses `~/.cline/data/settings/cline_mcp_settings.json`):

```json
// https://docs.cline.bot/mcp/configuring-mcp-servers: mcpServers, command, args, env,
// disabled, autoApprove
{
  "mcpServers": {
    "siftwise": {
      "command": "siftwise",
      "args": ["mcp"],
      "env": {
        "SIFTWISE_SERVER": "https://siftwise.example.com",
        "SIFTWISE_API_TOKEN": "your-token",
        "SIFTWISE_REPO_PATH": "/path/to/your/repo"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

(Leave the `//` comment lines out of the real file.)

- **Repository:** `SIFTWISE_REPO_PATH`, or the `repo_path` argument. Cline's MCP docs don't
  mention roots (if Cline does declare them, they're used before the working directory), and
  neither the working directory nor a `cwd` field is documented. The settings are global: set
  `SIFTWISE_REPO_PATH` for one repository, or leave it out and let the agent pass `repo_path`
  (untested whether Cline's agent does).
- **The token:** the docs show literal values only; this file is outside your repository.
- **Timeouts:** Cline has a per-server request timeout in its MCP settings; keep it above
  30 s.
- **Windows:** doubled backslashes in JSON paths.

## Reporting results

Open an issue with the client, its version, the OS, which checks passed, and the server output
if something failed (with the token removed). The README lists a client as supported only
after these checks pass.
