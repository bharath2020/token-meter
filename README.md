# Token Meter

A read-only live monitor of token burn and API-equivalent cost per Firstmate worker
(task workers, second mates and the main Firstmate session), built from Claude Code's
session logs. It shows which worker is consuming tokens right now, as a web page
and as a terminal table.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/bharath2020/token-meter/main/install.sh | sh
```

It needs only `python3` (3.11 or newer; standard library only) plus `curl` or `wget` and
`tar`; no git and no sudo. It installs the program into `~/.local/share/token-meter` and a
`token-meter` launcher into `~/.local/bin`, and warns if `~/.local/bin` is not on your
`PATH`. Run the same command again to upgrade. To install elsewhere, set `PREFIX` (uses
`$PREFIX/share/token-meter` and `$PREFIX/bin`), or `TOKEN_METER_INSTALL_DIR` and
`TOKEN_METER_BIN_DIR`. To uninstall, delete those two paths.

From a clone, `./token-meter` in the repository works the same way without installing.

## Quick start

```sh
cd ~/path/to/your/firstmate/home   # or pass --home, or set TOKEN_METER_FIRSTMATE_HOME
token-meter web                    # then open http://127.0.0.1:4791/
token-meter tui                    # live terminal table (Ctrl-C to quit)
```

## Start a view

The Firstmate home is taken from `--home`, else `TOKEN_METER_FIRSTMATE_HOME`, else the
Firstmate home that holds the current directory (the nearest directory at or above it
with `bin/`, `data/` and `state/` subdirectories). If none is found it exits with an
error saying how to name one.

The web server listens on `127.0.0.1:4791` only. To reach it from another device, add
that machine's address with `--host` (or `TOKEN_METER_HOSTS`), for example
`--host 127.0.0.1,192.0.2.10`. If an address is not available, it says so and keeps
serving on the others.

The page updates over server-sent events (`/events`) and falls back to polling
`/api/snapshot`; its timeline chart reads `/api/timeline`. All of them, and the page
itself, are GET-only: no actions, and only worker
names, backlog titles, working-directory names, model names, token counts and costs are
shown - never message content or secrets.

Useful options (`token-meter --help` lists all):

| Option | Default | Meaning |
| --- | --- | --- |
| `--port` | `4791` (`TOKEN_METER_PORT`) | web port |
| `--host` | `127.0.0.1` (`TOKEN_METER_HOSTS`) | comma-separated addresses to listen on |
| `--interval` | `2` | seconds between updates |
| `--window` | `1h` | tui: window for the token split columns (`5m`, `1h`, `today`, `48h`) |
| `--once` | | tui: print one frame and exit |
| `--projects` | `~/.claude/projects` (`TOKEN_METER_PROJECTS`) | session logs |
| `--home` | `TOKEN_METER_FIRSTMATE_HOME`, else the Firstmate home holding the current directory | Firstmate home |

## What it shows

For the fleet and for each worker: tokens and cost in the last 5 minutes, last hour,
today (since local midnight) and last 48 hours, each split into cache read, cache write,
output and input; the current burn rate in $/hour and tokens/minute (measured over the
last 5 minutes); the time of the last API call; and a "burning" marker when that call was
in the last 2 minutes. Workers are sorted by current burn rate, then by most recent
activity.

The web page also has a timeline chart of when the burn happened: cost or tokens per
15-minute bucket over the last 48 hours, or per 5-minute bucket over the last 6 hours,
stacked by worker. The six workers that burned most in the range are shown separately
and the rest fold into "others"; hovering or tapping a bucket shows each worker's
cache read / cache write / output / input split. The terminal view shows a per-hour
sparkline of fleet cost over the last 48 hours.

## How it reads usage

Every worker runs Claude Code, which logs to `~/.claude/projects/**/*.jsonl`. Each
`assistant` line carries `message.usage` (`input_tokens`, `output_tokens`,
`cache_creation_input_tokens` with `cache_creation.ephemeral_5m_input_tokens` /
`ephemeral_1h_input_tokens`, `cache_read_input_tokens`), `message.model`, `requestId`
and `timestamp`. One API call can be logged on several lines, so each
`(message.id, requestId)` pair is counted once. When a line has no `cache_creation`
breakdown, its `cache_creation_input_tokens` count as 5-minute writes.

On start it reads every log modified in the last 48 hours (a couple of seconds for a few
hundred MB), then tails: it remembers each file's byte offset, reads only complete lines
appended since the last poll, picks up new files, and starts over if a file shrinks or is
replaced. Logs older than 48 hours are skipped to their end and only read if they grow
again.

Polls are kept cheap so the monitor stays near idle (about 0.5% of a core with a page
open). The whole log tree is walked only every 30 seconds; the 2-second polls in between
check only logs that grew in the last 15 minutes, the project directories (a new session
log changes its directory) and the `subagents` directories of active sessions. A log
that was quiet for longer than that is picked up by the next 30-second walk. A
session's first lines are re-read for attribution only when its log has grown. The
timeline is built only when the page asks for it and is reused until new calls arrive
or the minute changes; every connected page shares the same snapshot.
Sub-agent logs under `<session>/subagents/` count toward their parent session.

It never writes anything under the Firstmate home or `~/.claude`.

## Model and effort

Each call's model is `message.model` on its usage line. Its effort level comes from the
request: Claude Code logs every API request as an `api-request` line whose
`params.output_config.effort` holds the effort and whose `id` the answering assistant
lines carry as `requestRef`, so calls are joined to requests by that id (not by order;
the request line comes first in the same log). If a call has no matching request, the
`effort` field on its own line is used, then the `effort=` value in the worker's
`state/*.meta` record; otherwise it shows "unknown". (`api-request-blob` lines also
carry `output_config.effort` but have no request id, so they are not used.)

Both views show each worker's current model and effort (of its latest call); the web
page also lists the 48-hour model/effort mix with costs, can colour the timeline by
worker, model or effort, and shows each worker's models and efforts with their cost in
a bucket's breakdown.

## How sessions are attributed to workers

Each session is labelled from its working directory and the first lines of its log
(re-checked every 30 seconds until known); the fleet layout and backlog are re-read every
30 seconds.

1. Working directory inside a second mate's home → that second mate. Second-mate homes
   come from `<firstmate home>/state/*.meta` files with `kind=secondmate` and `home=`;
   the file name is the second mate's id.
2. Working directory inside the Firstmate home → "firstmate main session".
3. A task worker's first message points to an operational-inbox `.msg` file
   (`read '<home>/state/operational-inbox/<id>.msg'`, also looked for under `handled/`);
   its `Your steering inbox is `<home>/state/<task-id>.inbox`` line names the task. If
   `<home>` is a second mate's home, the worker is shown as `<second mate> > <task-id>`.
   A `<home>/data/<task-id>/launch-brief.md` path in the first lines is a fallback.
4. Otherwise a `worktree=` line in a `state/*.meta` file (main home or a second mate's
   home) names the task.
5. Otherwise a directory under `~/.treehouse/homes/<mate>/` is shown as
   `<mate> > (unidentified worker)`, and any other `~/.treehouse/` directory as an
   unidentified worker.
6. Anything else is "not fleet work: <directory>".

Task workers get the title of their `- [ ] <task-id> - <title> (repo: ...)` line in
`data/backlog.md` of the home that owns them.

## Pricing

Costs are API-equivalent at list prices per million tokens (not what a subscription
bills):

| Model | Input | Output | Cache write 5 min | Cache write 1 h | Cache read |
| --- | --- | --- | --- | --- | --- |
| claude-opus-5-5 | $4.00 | $20.00 | $5.00 (1.25x input) | $8.00 (2x input) | $0.20 |
| claude-sonnet-5-5 | $2.00 | $10.00 | $2.50 | $4.00 | $0.20 |
| claude-haiku-5-5 | $0.10 | $0.50 | $0.125 | $0.20 | $0.01 |
| claude-haiku-5-5, prompt over 100K tokens | $0.50 | $2.50 | $0.625 | $1.00 | $0.05 |

A prompt is input + cache read + cache write tokens. Model names with a suffix
(`claude-opus-5-5[1m]`, a date) use the base model's price. Any other model's tokens are
counted at $0 and flagged as unpriced in both views.

## Tests

```sh
python3 -m unittest discover -s tests
```
