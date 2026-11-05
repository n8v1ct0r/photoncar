# Photoncar task log

Read this and `git log` first after a compaction or in a new session.

## Objective

Phase 2: build the MVP on recorded data (comma2k19 plus synthetic), per `docs/phase-1-plan.md` and `docs/contracts.md`. Stop at the end of Phase 2 and report test results.

## Decisions (owner, 2026-09-23)

- Car: BMW M3 (E90) first profile, core car-agnostic. Hardware option A. No in-car screen.
- Lap schema: Photoncar lap v0, mirroring the AI Race Engineer draft field names.
- Visual direction: Roadbook (impeccable seed 14e368b3). Overpass + DSEG7, 6 named colors.
- E90 wiring: OBD port only for the MVP; PT-CAN tap later, as its own approved step.
- Privacy radius R = 0.5 km (`config/photoncar.toml`).
- Plan, dependency list, and unsourced defaults approved. GitHub n8v1ct0r, MIT.
- Portfolio interactive embed approved (2026-09-23): static read-only replay of 1 public-dataset drive, built from the same web app through a DataSource seam (API vs static JSON). Spec: docs/contracts.md "Portfolio embed". Budget numbers still proposed.

## Phase 2 checklist

- [x] Foundation: branch `phase-2`, pyproject + uv (Python 3.13.5), web toolchain (Vite, Preact, TS 6.0.3 strict, ESLint strict, Prettier), fonts, config, records, signals, schema, contracts, Makefile, CI, scan scripts
- [x] Device: OBD PIDs, TX guard, listen-only check, OBD poller (single TX owner), SocketCAN source, DBC decoding (opendbc 28015820), bench emulator, provisioning files
- [x] Data: fetch script (pinned, SHA-256), comma2k19 source, synthetic OBD + synthetic drives, recorded source, dataset notes, full 900 s route fetch
- [x] Analytics: alignment, Kalman, performance, laps, behavior, rules, baseline (known-answer tests)
- [x] Store: writer, reader, rollup, index, privacy zones + export filter, lap v0 schema + exporter, report, merge
- [x] Server: summary stats, model client (Anthropic + fake), validator, cache, limits, FastAPI app per contract
- [x] Safety fixes from the TX-path review (4 major, 3 minor): one-shot check, structural audit via open_live_can_session, 10 s rate window, untimed logs fail, hard 10 Hz ceiling, AST scanner holes, kernel timestamps
- [x] Web: frame + safety line, stage list, review (roadbook, map, strips), report, live tripmeter, legend; loading/empty/error states; DataSource seam
- [x] Portfolio embed: build:embed mode (hash routing, relative base), export-embed command, budget checks, no-network test
- [x] Integration: pipeline runner, CLI (replay, serve, seed-demo), end-to-end replay on comma2k19, realtime factor (117.7x on the 900 s route, docs/measurements.md)
- [x] Review round 2 leftovers: live speed without IMU, dataset root vs PHOTONCAR_DATA_DIR, replay-stream and serve moving lock, rested_h, 4 safety minors
- [x] Live sensors: GPS (UBX NAV-PVT) and IMU (LSM6DSOX) readers, stdlib only
- [ ] Verification: full lint/typecheck/test suites, dataset tests, e2e safety-line test, scans, adversarial review
- [ ] Phase 2 report

## Ship (branch `ship`, 2026-11-05)

- [x] Honesty strip in the embed only (portfolio shared/STRIP.md): RECORDED comma2k19 drive, SIMULATED throttle and coolant. IBM Plex Mono self-hosted (OFL, latin 400).
- [x] Replay deck on the review: play, pause, scrub, 1x or 10x. A plain visit opens 6 s before the default box, already playing (paused under reduced motion, and paused on a `?box=N` link). Strips, map, and readouts follow 1 playhead; a plain line reads the car at the playhead.
- [x] Live embed at 390 keeps 1 viewport under the 32px strip (padding and gaps, never glance sizes).
- [ ] Deploy the embed to Vercel (photoncar.vercel.app).
- [ ] Public snapshot repo: blocked until PHOTONCAR_STEALTH_TERMS_FILE exists (stealth scan cannot run without it).

## Blockers

None.

## Last check results

- Foundation: ruff 0 issues, mypy strict 0 issues (18 files); web typecheck, lint, format, build all green.
- Core (commit aa27dfa): 679 unit tests + 8 dataset tests pass; mypy strict 95 files clean; ruff clean; copy lint 0; privacy scan 0; gitleaks dir 0 leaks.
- 2026-09-24: run wf_9a7a6e5c-145 stopped at an account session limit; its green partial state is commit d24f6a4 (817 unit + 21 dataset tests). Resumed as wf_b4d80e9f-2bb, with builders auditing existing files and finishing only the gaps.
- Figure 1.4 source: the default route holds a real standing start, 24.396 s (commuter merge, 0.110 g).

- 2026-09-25: commits 30377b4 (review-round fixes, 837 unit + 21 dataset green) and 04c6f87 (contract additions). Final Phase 2 wave running as wf_e29b7161-c92: web + embed, integration leftovers, safety minors, sensors; then web QA review, backend adversarial review, fix round.
- 2026-09-26: commit 0b7a3f7 green (1127 unit, 22 dataset, 15 e2e). Final fix round wf_677f890d-004: gear bounds + dataset vehicle profile, shifts out of roadbook numbering, miles in displays, embed trim before decimation, link recheck before first request, web minors. Then verify and write the Phase 2 report.
