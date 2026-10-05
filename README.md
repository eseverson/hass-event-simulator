# Event Simulator

A Home Assistant custom integration that adds an admin-only sidebar panel for testing automations against real history: pick an automation, pull the recorded history of the entities it depends on onto an editable timeline, and replay it in a sandboxed child process running on virtual time, so nothing touches the live instance. The result shows whether the automation fired, when, which trigger, which condition stopped it, and which service calls it would have made.

**Work in progress** — the panel is a placeholder for now. See `docs/superpowers/specs/` for the design.
