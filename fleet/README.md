# fleet/

Coordination kit for running several machines on this repository in parallel. Each machine owns a disjoint set of
paths (a "sector"), audits it with a multi-agent workflow, and fixes only what the owner approves.

- `kit/sectors.json`, `kit/resolve_sectors.py`: the partition (24 sectors) and the ownership resolver (`--check`, `--owner`, `--diff`, `--sector`).
- `kit/keepers.json`, `kit/check_keepers.py`: the single list of single-writer and frozen paths, and its gate.
- `kit/RULES.md`: the rules of engagement every machine follows.
- `kit/TOOLCHAIN.md`, `kit/test-env.sh`, `kit/t`: session launch, toolchain, the isolated test stack and the wrapper
  that runs every test against it only, local checks before any hand-over.
- `kit/sector-deep-work.workflow.js`: the per-machine workflow (Claude Code Workflow tool).
- `kit/integration-rehearsal.sh`: merges a batch of branches on `origin/main` and runs the checks before a window.
- `kit/fleet_board.sh`: the coordinator's status board, a plain shell loop.
- `kit/kit_id.sh`: the kit fingerprint every launch prompt names.

This repository is public. The per-sector missions, the reconnaissance dossiers, the findings and the owner's handoff
report are distributed privately and never committed here; machines keep their reports in their own output directory.

Nothing in this directory is imported by an application or shipped in an image.
