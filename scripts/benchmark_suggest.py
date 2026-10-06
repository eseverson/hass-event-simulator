#!/usr/bin/env python3
"""Real-data benchmark for Suggest mode (issue #26): do suggestions reproduce what the user's own automations did?

For every automation that fired in the window, its recorded fire times (`automation_triggered` events) are the ground truth. The first N fires (N = 1, 3, all) become should-fire labels (anchor `start`, tight periods), the automation's referenced entities plus a few unrelated distractor entities are the tracks, and `event_simulator/suggest/subscribe` runs over 7- and 30-day windows. Per run it records whether a suggestion in the top 1 / top 3 reproduces the real fire times (recall and precision of 90 % or more over the whole window; a suggested fire counts only while the real automation was enabled), the recall of the top suggestion on the held-out fires, its extra fires per day, the time taken, and the top suggestion next to the real automation's triggers and conditions.

How to run it, against a copy (it never writes to the source config dir):

    python scripts/benchmark_suggest.py --source ~/.config/homeassistant --out bench.json --md bench.md

with a Python environment that has the same `homeassistant` release as the live instance (and `home-assistant-frontend`, which the integration's `frontend` dependency needs). It:

1. Copies into `--workdir` (a fresh temp dir unless given): the recorder through SQLite's online backup from a read-only connection (`mode=ro`, so the live database and its WAL are only read), `automations.yaml` (every automation set `initial_state: false`, so nothing acts), the automations of any `homeassistant: packages:` file, `blueprints/`, the core config and registries from `.storage`, and the config entries with their data and options left out, every one disabled (the device registry drops the devices of missing entries, and device triggers then fail to validate). Nothing else from the source is read, and the script refuses a workdir inside the source.
2. Boots a disposable Home Assistant in this process on that copy with only `recorder` (auto purge off), the automations and `event_simulator` (plus their manifest dependencies; `http` on 127.0.0.1 only), mints an admin token and talks to it over the real websocket.
3. Runs the benchmark, writes the JSON (every run, each suggestion's triggers, conditions and metrics, the real automations' triggers and conditions) and the Markdown summary, stops Home Assistant, and deletes the workdir unless `--keep` (pass `--workdir` of a kept copy to rerun without copying again).

A kept copy remembers its window end (the disposable instance records into it, so its own latest event moves on). `--only automation.x` limits the automations; `--windows 7 30`, `--labels 1 3 all`, `--distractors 3` and `--jobs 2` change the sweep.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import re
import shutil
import socket
import sqlite3
import statistics
import sys
import tempfile
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
INTEGRATION = REPO / "custom_components" / "event_simulator"
STORAGE_FILES = (
    "core.config",
    "core.area_registry",
    "core.device_registry",
    "core.entity_registry",
    "core.floor_registry",
    "core.label_registry",
)
DISTRACTOR_DOMAINS = ("binary_sensor", "sensor", "light", "switch", "media_player", "lock", "input_boolean", "person")
# Matching a suggested fire to a real one: the miner's default tolerance (a fire may land 2 min before to 10 min after).
TOL_BEFORE, TOL_AFTER = 120.0, 600.0
REPRODUCE = 0.9  # recall and precision over the whole window
MAX_LABELS = 500


# Copying, read-only


class _Loader(yaml.SafeLoader):
    """Reads the source's YAML without resolving anything: tags become placeholders."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: f"<{suffix} {getattr(node, 'value', '')}>")


def prepare(source: Path, work: Path, port: int) -> Path:
    """The disposable config dir under `work`, copied from `source` (only read)."""
    source, work = source.resolve(), work.resolve()
    if work == source or source in work.parents:
        raise SystemExit(f"refusing to work inside the source config dir: {work}")
    config = work / "config"
    db = config / "home-assistant_v2.db"
    if not db.exists():
        (config / ".storage").mkdir(parents=True, exist_ok=True)
        print(f"backing up {source / 'home-assistant_v2.db'} (read-only) ...", file=sys.stderr)
        live = sqlite3.connect(f"file:{source / 'home-assistant_v2.db'}?mode=ro", uri=True)
        copy = sqlite3.connect(db)
        try:
            live.backup(copy)
        finally:
            copy.close()
            live.close()
        # The disposable instance records into the copy, so a rerun on a kept copy would see later events: the default window end is the copy's, taken now.
        (work / "latest_event").write_text(str(latest_event(db)))
    # Home Assistant 2026.9 moves `http:` from YAML into `.storage/http` on first start and prefers the stored copy after; start from the YAML every time.
    (config / ".storage" / "http").unlink(missing_ok=True)
    for name in STORAGE_FILES:
        if (source / ".storage" / name).exists():
            shutil.copyfile(source / ".storage" / name, config / ".storage" / name)
    # The device registry drops devices whose config entries are gone, and device triggers then fail to validate: keep every entry, disabled (so none sets up) and without its data or options (credentials stay behind).
    entries = json.loads((source / ".storage" / "core.config_entries").read_text())
    entries["data"]["entries"] = [e for e in entries["data"]["entries"] if e["domain"] != "event_simulator"]
    for entry in entries["data"]["entries"]:
        entry.update(data={}, options={}, disabled_by="user")
    (config / ".storage" / "core.config_entries").write_text(json.dumps(entries))
    if (source / "blueprints").is_dir():
        shutil.copytree(source / "blueprints", config / "blueprints", dirs_exist_ok=True)
    automations = yaml.load((source / "automations.yaml").read_text(), _Loader) or []
    packaged = []
    main = yaml.load((source / "configuration.yaml").read_text(), _Loader) or {}
    packages = ((main.get("homeassistant") or {}).get("packages")) or {}
    for include in packages.values() if isinstance(packages, dict) else ():
        match = re.fullmatch(r"<include (\S+)>", str(include))
        if not match or not (source / match.group(1)).is_file():
            continue
        package = yaml.load((source / match.group(1)).read_text(), _Loader) or {}
        found = package.get("automation") or []
        packaged += found if isinstance(found, list) else [found]
    for automation in (*automations, *packaged):
        automation["initial_state"] = False
    (config / "automations.yaml").write_text(yaml.safe_dump(automations, sort_keys=False))
    (config / "package_automations.yaml").write_text(yaml.safe_dump(packaged, sort_keys=False))
    (config / "configuration.yaml").write_text(
        "automation: !include automations.yaml\n"
        "automation packages: !include package_automations.yaml\n"
        "recorder:\n"
        f"  db_url: sqlite:///{db}\n"
        "  auto_purge: false\n"
        "  auto_repack: false\n"
        "http:\n"
        "  server_host: 127.0.0.1\n"
        f"  server_port: {port}\n"
        "logger:\n"
        "  default: warning\n"
    )
    custom = config / "custom_components"
    custom.mkdir(exist_ok=True)
    link = custom / "event_simulator"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(INTEGRATION)
    return config


# Ground truth, from the copy


def real_fires(db: Path, start: float, end: float) -> dict[str, list[tuple[float, str]]]:
    """Each automation's fires in `[start, end)` as (time, source); manual runs (no source) are left out."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        """select json_extract(d.shared_data, '$.entity_id'), ev.time_fired_ts, json_extract(d.shared_data, '$.source')
           from events ev join event_types t using (event_type_id) join event_data d using (data_id)
           where t.event_type = 'automation_triggered' and ev.time_fired_ts >= ? and ev.time_fired_ts < ?
           order by ev.time_fired_ts""",
        (start, end),
    ).fetchall()
    con.close()
    fires: dict[str, list[tuple[float, str]]] = {}
    for entity_id, at, source in rows:
        if entity_id and source:
            fires.setdefault(entity_id, []).append((at, source))
    return fires


def active_entities(db: Path, start: float, end: float) -> list[str]:
    """Entities with a moderate amount of recorded history in the window: the distractor pool."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        """select m.entity_id, count(*) from states s join states_meta m using (metadata_id)
           where s.last_updated_ts >= ? and s.last_updated_ts < ? group by m.entity_id""",
        (start, end),
    ).fetchall()
    con.close()
    return sorted(e for e, n in rows if e.split(".")[0] in DISTRACTOR_DOMAINS and 10 <= n <= 3000)


def enabled_intervals(db: Path, automation: str, end: float) -> list[tuple[float, float]]:
    """When the real automation was on (enabled), from its recorded states; before its first recorded state it did not exist yet."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        """select s.last_updated_ts, s.state from states s join states_meta m using (metadata_id)
           where m.entity_id = ? and s.last_updated_ts < ? order by s.last_updated_ts""",
        (automation, end),
    ).fetchall()
    con.close()
    intervals: list[tuple[float, float]] = []
    for (at, state), following in zip(rows, [*rows[1:], (end, None)], strict=True):
        if state == "on":
            intervals.append((at, following[0]))
    return intervals


def latest_event(db: Path) -> float:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    (value,) = con.execute("select max(time_fired_ts) from events").fetchone()
    con.close()
    return value


# Metrics


def coverage(fires: list[float], reals: list[float]) -> tuple[int, int]:
    """(real fires matched by some fire, fires matching some real fire), by binary search: a month of a chatty trigger is 30 000 of each."""
    fires, reals = sorted(fires), sorted(reals)
    hit = sum(_any_in(fires, r - TOL_BEFORE, r + TOL_AFTER) for r in reals)
    good = sum(_any_in(reals, f - TOL_AFTER, f + TOL_BEFORE) for f in fires)
    return hit, good


def _any_in(values: list[float], lo: float, hi: float) -> bool:
    """Whether sorted `values` has one in [lo, hi]."""
    i = bisect.bisect_left(values, lo)
    return i < len(values) and values[i] <= hi


def _enabled(t: float, enabled: list[tuple[float, float]], starts: list[float]) -> bool:
    i = bisect.bisect_right(starts, t) - 1
    return i >= 0 and t < enabled[i][1]


def evaluate(suggestion: dict[str, Any], reals: list[float], held_out: list[float], days: float, enabled: list[tuple[float, float]]) -> dict[str, Any]:
    """Only fires while the real automation was enabled count: before it existed, or while it was off, it could not fire."""
    starts = [lo for lo, _ in enabled]
    fires = [t for t in map(_ts, suggestion["stats"].get("fire_times", [])) if _enabled(t, enabled, starts)]
    hit, good = coverage(fires, reals)
    held_hit, _ = coverage(fires, held_out)
    recall = hit / len(reals) if reals else 1.0
    precision = good / len(fires) if fires else 0.0
    return {
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "reproduces": recall >= REPRODUCE and precision >= REPRODUCE,
        "held_out_recall": round(held_hit / len(held_out), 3) if held_out else None,
        "extra_per_day": round((len(fires) - good) / days, 2),
        "fires": len(fires),
    }


def _ts(value: str | float) -> float:
    return value if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


# Talking to the disposable instance


class Client:
    def __init__(self, session: Any, url: str, token: str) -> None:
        self.session, self.url, self.token = session, url, token
        self.ws: Any = None
        self.next_id = 1

    async def connect(self) -> Client:
        self.ws = await self.session.ws_connect(self.url, max_msg_size=0)
        await self.ws.receive_json()  # auth_required
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        reply = await self.ws.receive_json()
        if reply.get("type") != "auth_ok":
            raise RuntimeError(f"websocket auth failed: {reply}")
        return self

    async def call(self, msg: dict[str, Any]) -> Any:
        msg_id = self.next_id
        self.next_id += 1
        await self.ws.send_json({**msg, "id": msg_id})
        while True:
            reply = await self.ws.receive_json()
            if reply.get("id") == msg_id and reply.get("type") == "result":
                if not reply.get("success"):
                    raise RuntimeError(f"{msg['type']}: {reply.get('error')}")
                return reply.get("result")

    async def subscribe_final(self, msg: dict[str, Any], timeout: float = 1800) -> dict[str, Any]:
        """A subscription's final event: the one carrying `suggestions` or `error`."""
        msg_id = self.next_id
        self.next_id += 1
        await self.ws.send_json({**msg, "id": msg_id})
        async with asyncio.timeout(timeout):
            while True:
                reply = await self.ws.receive_json()
                if reply.get("id") != msg_id:
                    continue
                if reply.get("type") == "result" and not reply.get("success"):
                    return {"error": reply.get("error")}
                event = reply.get("event") or {}
                if "suggestions" in event or "error" in event:
                    await self.ws.send_json({"id": self.next_id, "type": "unsubscribe_events", "subscription": msg_id})
                    self.next_id += 1
                    return event


async def boot(config: Path) -> Any:
    from homeassistant import bootstrap
    from homeassistant.core import CoreState
    from homeassistant.runner import RuntimeConfig

    hass = await bootstrap.async_setup_hass(RuntimeConfig(config_dir=str(config), skip_pip=True))
    if hass is None:
        raise SystemExit("Home Assistant did not set up")
    run = asyncio.create_task(hass.async_run())
    for _ in range(600):
        if hass.state is CoreState.running:
            break
        await asyncio.sleep(0.5)
    if not hass.config_entries.async_entries("event_simulator"):
        flow = await hass.config_entries.flow.async_init("event_simulator", context={"source": "user"})
        await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    for _ in range(120):  # the http server starts after the core reports running
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", hass.http.server_port)
        except OSError:
            await asyncio.sleep(0.5)
        else:
            writer.close()
            break
    try:  # Home Assistant 2026.9 tries an `http:` config moved out of YAML as pending, and restarts (dropping every websocket) unless it is confirmed within 5 minutes
        from homeassistant.components.http.config import async_get_and_load_store
    except ImportError:
        pass
    else:
        store = await async_get_and_load_store(hass)
        if store.pending is not None:
            await store.async_promote_pending()
    user = await hass.auth.async_create_user("benchmark", group_ids=["system-admin"])
    refresh = await hass.auth.async_create_refresh_token(user, client_id="http://127.0.0.1/")
    token = hass.auth.async_create_access_token(refresh)
    return hass, run, token


# The sweep


async def run_one(
    client: Client,
    automation: str,
    alias: str,
    tracks: list[str],
    window: tuple[float, float],
    fires: list[float],
    n_labels: int,
    enabled: list[tuple[float, float]],
) -> dict[str, Any]:
    start, end = window
    labeled = fires[:n_labels]
    held_out = fires[n_labels:]
    labels = [{"start": _iso(t), "end": _iso(t + 1), "kind": "fire", "anchor": "start"} for t in labeled]
    began = time.monotonic()
    result = await client.subscribe_final(
        {
            "type": "event_simulator/suggest/subscribe",
            "entity_ids": tracks,
            "start": _iso(start),
            "end": _iso(end),
            "labels": labels,
            "options": {"top_k": 8},
        }
    )
    took = round(time.monotonic() - began, 1)
    days = (end - start) / 86400
    out: dict[str, Any] = {
        "automation": automation,
        "alias": alias,
        "days": round(days),
        "labels": len(labeled),
        "fires": len(fires),
        "tracks": tracks,
        "seconds": took,
    }
    if "error" in result:
        out["error"] = result["error"]
        return out
    suggestions = result.get("suggestions") or []
    # In a thread: the instance under test shares this event loop, and other runs' replays are waiting on it.
    scored = await asyncio.to_thread(lambda: [evaluate(s, fires, held_out, days, enabled) for s in suggestions])
    out.update(
        {
            "warning": result.get("warning"),
            "evaluated": result.get("evaluated"),
            "top1": bool(scored and scored[0]["reproduces"]),
            "top3": any(s["reproduces"] for s in scored[:3]),
            "best": scored[0] if scored else None,
            "suggestions": [
                {"config": {k: s["config"].get(k) for k in ("triggers", "conditions")}, "explanation": s["explanation"], "exact": s.get("exact"), "diverged": s.get("diverged", False), **m}
                for s, m in zip(suggestions, scored, strict=True)
            ],
        }
    )
    return out


async def sweep(args: argparse.Namespace, config: Path, port: int) -> dict[str, Any]:
    import aiohttp

    db = config / "home-assistant_v2.db"
    stamp = config.parent / "latest_event"
    latest = float(stamp.read_text()) if stamp.exists() else latest_event(db)
    end = args.end.timestamp() if args.end else latest // 3600 * 3600
    hass, run, token = await boot(config)
    port = hass.http.server_port
    results: list[dict[str, Any]] = []
    try:
        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/api/websocket"
            meta = await Client(session, url, token).connect()
            listed = {a["entity_id"]: a for a in await meta.call({"type": "event_simulator/automations"})}
            jobs: list[tuple[Any, ...]] = []
            configs: dict[str, Any] = {}
            for days in args.windows:
                start = end - days * 86400
                truth = real_fires(db, start, end)
                pool = active_entities(db, start, end)
                for automation in sorted(truth):
                    if args.only and automation not in args.only:
                        continue
                    if automation not in listed:
                        continue
                    fires = [t for t, _ in truth[automation]]
                    if len(fires) < args.min_fires:
                        continue
                    info = await meta.call({"type": "event_simulator/automation", "entity_id": automation})
                    cfg = info["config"]
                    configs[automation] = {k: cfg.get(k) or cfg.get(k.rstrip("s")) for k in ("triggers", "conditions")}
                    refs = [e for e in info["referenced_entities"] if e != automation]
                    rng = zlib.crc32(automation.encode())
                    others = [e for e in pool if e not in refs and e != automation]
                    distractors = [others[(rng + k * 7919) % len(others)] for k in range(args.distractors)] if others else []
                    tracks = list(dict.fromkeys([*refs, *distractors]))[:50] or ["sun.sun"]
                    counts = sorted({min(len(fires), MAX_LABELS if n == "all" else int(n)) for n in args.labels})
                    for n in counts:
                        jobs.append((automation, listed[automation]["alias"], tracks, (start, end), fires, n, enabled_intervals(db, automation, end)))
            print(f"{len(jobs)} runs", file=sys.stderr)
            semaphore = asyncio.Semaphore(args.jobs)

            async def worker(job: tuple[Any, ...]) -> None:
                async with semaphore:
                    client = await Client(session, url, token).connect()
                    try:
                        found = await run_one(client, *job)
                    finally:
                        await client.ws.close()
                    results.append(found)
                    status = found.get("error") or f"top1={found['top1']} top3={found['top3']}"
                    print(f"{found['automation']} {found['days']}d N={found['labels']}: {status} ({found['seconds']} s)", file=sys.stderr)

            await meta.ws.close()
            await asyncio.gather(*(worker(job) for job in jobs))
    finally:
        await hass.async_stop()
        await run
    results.sort(key=lambda r: (r["automation"], r["days"], r["labels"]))
    return {"end": _iso(end), "runs": results, "real_configs": configs}


# Report


def markdown(report: dict[str, Any]) -> str:
    runs = report["runs"]
    lines = [f"Window end {report['end']}; tolerance {TOL_BEFORE:g} s before / {TOL_AFTER:g} s after; reproduces = recall and precision >= {REPRODUCE:.0%} over all real fires.", ""]
    for days in sorted({r["days"] for r in runs}):
        for n_kind in ("1", "3", "all"):
            subset = [r for r in runs if r["days"] == days and _n_kind(r) == n_kind]
            if not subset:
                continue
            ok = [r for r in subset if "error" not in r]
            held = [r["best"]["held_out_recall"] for r in ok if r.get("best") and r["best"]["held_out_recall"] is not None]
            extra = [r["best"]["extra_per_day"] for r in ok if r.get("best")]
            lines.append(
                f"- {days} d, N={n_kind}: top-1 {sum(r['top1'] for r in ok)}/{len(subset)}, top-3 {sum(r['top3'] for r in ok)}/{len(subset)}"
                + (f", held-out recall (top 1) median {statistics.median(held):.2f}" if held else "")
                + (f", extra fires/day (top 1) median {statistics.median(extra):.1f}" if extra else "")
                + f", median {statistics.median(r['seconds'] for r in subset):.1f} s"
                + (f", {len(subset) - len(ok)} errors" if len(ok) < len(subset) else "")
            )
    lines += ["", "| automation | window | N | fires | top 1 | top 3 | held-out recall | extra/day | s | top suggestion |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        best = r.get("best") or {}
        top = (r.get("suggestions") or [{}])[0].get("explanation", r.get("error", {}).get("message", "") if isinstance(r.get("error"), dict) else str(r.get("error", "")))
        lines.append(
            f"| {r['automation']} | {r['days']} d | {r['labels']} | {r['fires']} | {'yes' if r.get('top1') else 'no'} | {'yes' if r.get('top3') else 'no'} | "
            f"{best.get('held_out_recall', '')} | {best.get('extra_per_day', '')} | {r['seconds']} | {str(top)[:140].replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def _n_kind(run: dict[str, Any]) -> str:
    if run["labels"] == run["fires"] or run["labels"] == MAX_LABELS:
        return "all"
    return str(run["labels"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=Path.home() / ".config" / "homeassistant", help="live config dir (only read)")
    parser.add_argument("--workdir", type=Path, help="where the copy goes (default: a new temp dir); reuses a copy already there")
    parser.add_argument("--keep", action="store_true", help="keep the workdir")
    parser.add_argument("--out", type=Path, required=True, help="JSON results")
    parser.add_argument("--md", type=Path, help="Markdown summary")
    parser.add_argument("--windows", type=int, nargs="+", default=[7, 30])
    parser.add_argument("--labels", nargs="+", default=["1", "3", "all"])
    parser.add_argument("--distractors", type=int, default=3)
    parser.add_argument("--min-fires", type=int, default=2)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--end", type=datetime.fromisoformat, help="window end (default: the copy's last event, on the hour)")
    args = parser.parse_args()

    created = args.workdir is None
    work = Path(tempfile.mkdtemp(prefix="suggest-bench-")) if created else args.workdir
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    try:
        config = prepare(args.source, work, port)
        report = asyncio.run(sweep(args, config, port))
    finally:
        if not args.keep and created:
            shutil.rmtree(work, ignore_errors=True)
    args.out.write_text(json.dumps(report, indent=1, default=str))
    if args.md:
        args.md.write_text(markdown(report))
    print(markdown(report).split("\n\n")[0])


if __name__ == "__main__":
    main()
