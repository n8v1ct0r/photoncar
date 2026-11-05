# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Delegated: proposed in the Phase 1 plan (Python device service with python-can and cantools, a FastAPI server, and a static TypeScript front end served by the device). Playwright (Python) for screenshots. Final choice pending the owner's Phase 1 approval.

## Users

- **The owner-driver.** One enthusiast who logs their own car. They never touch Photoncar while driving. They open the dashboard after the drive, on a laptop or phone connected to the device, to see what the car did, how they drove, and whether anything looked wrong.
- **Portfolio readers.** Engineers and hiring or investing readers who meet Photoncar through screenshots, a demo video, a case study, and the public repo. They judge whether the system is real, careful, and well built.

## Product Purpose

Photoncar turns raw engine, sensor, and CAN bus telemetry into a read of vehicle state, performance, anomalies, and driver behavior. It runs on a small computer in the car with no internet. It fuses CAN, GPS, and IMU data into 1 timeline, explains what the car did, and flags what looks wrong with the signals that prove it. Success: after a drive, the owner can trust each number and each alert because each one points to its evidence.

## Positioning

Most car apps show raw gauges. Photoncar shows one fused timeline where every alert cites its signals and time window, and every sentence of the drive summary cites a computed stat. It is read-only by construction and keeps all data on the device.

## Operating Context

- Hardware: Raspberry Pi 5 with a CAN HAT on the OBD-II port, a USB GPS, and an IMU (owner confirmed option A). Exact parts pending Phase 1 approval.
- First vehicle profile: BMW M3 (E90). The core stays car-agnostic: OBD-II values are the baseline, and DBC decoding is added per car where a DBC exists.
- No in-car screen (owner decision). The device records during the drive. The live view exists for replay mode and screenshots, and must still read at a glance from 1 meter.
- Review happens parked, over the device's local network. Export happens only when the owner starts it.
- Development and tests run on recorded public data first (replay mode), so most work needs no car.

## Capabilities and Constraints

- Read-only. The capture interface is listen-only. The only frames Photoncar may send are standard OBD-II service 01 read requests, 1 PID per frame, at or below a capped rate. It never sends service 09 (reading the VIN would need ISO-TP flow control frames). It never clears trouble codes and never writes to any vehicle module.
- Not a safety system. Every screen shows: "Not a safety system. Do not use while driving."
- While the car moves, the screen shows a glanceable view only and never asks for input. No charts while moving.
- Every alert cites the signals and the time window that triggered it.
- Every public export and every screenshot trims GPS inside a privacy zone around home. The radius lives in 1 config file. Home coordinates never enter the repo.
- Drive logs never enter the repo.
- The model-written drive summary runs only when the device is online, from computed stats only, through a server route with a per-IP rate limit, a daily spend cap, and an output cache.
- Lap export uses a Photoncar-defined, versioned lap schema (v0), since the AI Race Engineer schema does not exist yet (owner decision).
- Portfolio embed (owner decision 2026-09-23): a static, read-only replay of 1 public-dataset drive, built from the same web app, with no server, no API key, and no personal data.
- Open decisions: OBD request rate cap, privacy zone radius, anomaly thresholds, GPS and IMU rates. All are proposed in the Phase 1 plan and need owner confirmation.

## Brand Commitments

- Name: Photoncar.
- The safety line above appears on every screen and in every screenshot.
- Copy: no em dashes or en dashes. Never use the words leverage (as a verb), delve, landscape, showcase, pivotal, seamless, robust, empower, unlock, or transform. Every claim carries a number or the flag "[needs a number]". Copy numbers exactly. Short sentences, active voice, plain words, sentence case.
- Visual bans: a cream background with a serif display and a terracotta accent, identical rounded cards with soft shadows, gradient washes as decoration, arrows added to the end of link text, fade-in animations on every section.

## Evidence on Hand

- Public dataset replay only (dataset and license to be confirmed in the Phase 1 plan).
- No real drives, no measured results, no users yet. Do not fabricate any metric, result, or testimonial.
- A photo of the hardware installed in the car will come from the owner (figure 1.5).

## Product Principles

1. Read-only by construction: safety is a property of the architecture, then proven by a bench test.
2. Every claim shows its evidence: alerts cite signals and windows, and summaries cite stats.
3. Private by default: data stays on the device, and exports trim home.
4. Glance while moving, study when parked.
5. Car-agnostic core, car profiles as add-ons.

## Accessibility & Inclusion

WCAG AA text contrast. Every control works by keyboard with visible focus. prefers-reduced-motion stops non-essential motion. Every page works at 1440px and 390px wide. The live view reads at a glance from 1 meter.
