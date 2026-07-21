# ADR-0003: Persisted events drive real-time interfaces

Status: accepted

Ego exposes typed lifecycle events for runs, participant probes, phases,
participant turns, and decision creation. An optional in-process asynchronous
queue receives each event only after its SQLite transaction commits.

Interactive interfaces consume the queue for live updates and replay SQLite
events when reconstructing a run. They must not infer progress from elapsed time,
poll for the most recent run, or treat the queue as durable state. Raw provider
output and private reasoning are not part of the event contract.
