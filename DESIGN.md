---
name: Photoncar
description: A drive read as a rally roadbook. Numbered boxes pinned to a time and a distance, each citing the signals that prove it.
colors:
  roll-white: "#F5F7F7"
  tulip-ink: "#111417"
  box-rule: "#5B666D"
  caution-red: "#C4161C"
  marker-yellow: "#FFE600"
  marker-green: "#5EE38C"
  hairline: "#C3C9CC"
  unlit-segment: "#E2E6E7"
typography:
  display:
    fontFamily: "Overpass, Helvetica Neue, Arial, sans-serif"
    fontSize: "2.4375rem"
    fontWeight: 800
    lineHeight: 1.15
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "Overpass, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.9375rem"
    fontWeight: 800
    lineHeight: 1.15
  title:
    fontFamily: "Overpass, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.5625rem"
    fontWeight: 800
    lineHeight: 1
  note:
    fontFamily: "Overpass, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.3
  body:
    fontFamily: "Overpass, Helvetica Neue, Arial, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "\"tnum\" 1, \"zero\" 1"
  label:
    fontFamily: "Overpass, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 800
    lineHeight: 1.5
    letterSpacing: "0.06em"
  live-text:
    fontFamily: "Overpass, Helvetica Neue, Arial, sans-serif"
    fontSize: "max(50.2px, 12.88vw)"
    fontWeight: 600
    lineHeight: 1.05
  live-digits:
    fontFamily: "DSEG7, ui-monospace, monospace"
    fontSize: "max(70.4px, 18.05vw)"
    fontWeight: 700
    lineHeight: 1
rounded:
  none: "0px"
spacing:
  gutter: "24px"
  gutter-narrow: "16px"
  cell: "12px"
  chip-gap: "6px"
components:
  button:
    backgroundColor: "{colors.roll-white}"
    textColor: "{colors.tulip-ink}"
    rounded: "{rounded.none}"
    padding: "7px 14px 5px"
    height: "40px"
  button-hover:
    backgroundColor: "{colors.tulip-ink}"
    textColor: "{colors.roll-white}"
  button-ink:
    backgroundColor: "{colors.tulip-ink}"
    textColor: "{colors.roll-white}"
    rounded: "{rounded.none}"
    padding: "7px 14px 5px"
  button-disabled:
    backgroundColor: "{colors.roll-white}"
    textColor: "{colors.box-rule}"
  tag:
    backgroundColor: "{colors.roll-white}"
    textColor: "{colors.tulip-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 7px 0"
  citation-chip:
    textColor: "{colors.tulip-ink}"
    rounded: "{rounded.none}"
    padding: "2px 7px 1px"
  roadbook-box-selected:
    backgroundColor: "{colors.marker-yellow}"
    textColor: "{colors.tulip-ink}"
  input:
    backgroundColor: "{colors.roll-white}"
    textColor: "{colors.tulip-ink}"
    rounded: "{rounded.none}"
    padding: "8px 10px 6px"
---

# Design System: Photoncar

## Overview

**Creative North Star: "The Roadbook"**

A drive is a rally roadbook: a numbered run of ruled boxes, each pinned to a time and a distance, each printing the signals that prove it. The whole system is paper and ink. The ground is a cool roll of white paper, every structure is a ruled line, and color appears only as a mark someone made on the page: a red caution mark, a yellow highlighter swipe, a green highlighter swipe. Nothing glows, floats, or rounds off.

The review is dense on purpose, the way a navigator's roadbook is dense: the scroll of boxes on the left, a map trace in local meters and stacked channel strips on the right, 1 time cursor across all of them. Selecting a box lands a marker-yellow swipe on its exact cited window, on every strip and on the map. The live view is the same paper at glance size: a rally tripmeter with fixed 7-segment digit banks, 1 caution line with its citation, and the safety line.

It refuses the dark gauge cluster with glowing arcs and the grid of analytics cards. It also refuses the cream-paper, serif, terracotta look: the paper is cool, the type is road-sign lettering.

**Key Characteristics:**
- Cool paper ground, ink structure, color only as marks.
- Ruled boxes and hairlines; no radius, no shadow.
- Overpass with tabular figures everywhere; DSEG7 digits only in the tripmeter.
- Every number points at its evidence: citation chips name source, signal, window, and stat.
- Motion is stepped, never eased.

## Colors

Six named inks on paper, plus 2 tints of the box rule that only draw hairlines and unlit segments.

### Primary
- **Tulip Ink** (#111417): all text, traces, tulip glyphs, full-weight rules (2 to 4px), button borders, the focus ring, and the selected-time cursor. 17.19:1 on roll white.

### Secondary
- **Marker Yellow** (#FFE600): a flat highlighter fill for the selected box, its cited window on every strip, its stretch of the map trace, and text selection. Ink on it reads at 14.58:1.
- **Marker Green** (#5EE38C): a flat highlighter fill for performance runs (0 to 60, 60 to 0, peak lateral) and valid laps. Ink on it reads at 11.30:1.

### Tertiary
- **Caution Red** (#C4161C): caution marks (!, !!, !!!) and anomaly marks only: the alert circle on the map and the dashed threshold line on a strip. 5.61:1 on roll white, 4.76:1 on marker yellow.

### Neutral
- **Roll White** (#F5F7F7): the paper ground of every surface, including the browser chrome color, scrollbar track, and the button face.
- **Box Rule** (#5B666D): secondary text, resolved alerts, context citations, meta labels, map captions, scrollbar thumb. 5.48:1 on roll white.
- **Hairline** (#C3C9CC): the thin rules between boxes, cells, strips, and table rows, and chart grid lines. Never text.
- **Unlit Segment** (#E2E6E7): the unlit segments behind the tripmeter digits. Never text.

### Named Rules
**The Marks Only Rule.** Red, yellow, and green are marks made on the paper, never surfaces, borders of containers, or decoration. Red means caution; yellow means selected or cited; green means a performance run or lap.

**The Ink On Green Rule.** Only tulip ink sits on marker green. Red or gray on green fails AA (3.69:1 and 3.60:1).

**The Resolved Prints Gray Rule.** A cleared alert stays in the roadbook, printed in box rule gray, marks included; when it is the selected box, it prints ink on the yellow swipe.

## Typography

**Display Font:** Overpass (variable, 100 to 900, self-hosted, OFL), with Helvetica Neue and Arial
**Body Font:** Overpass
**Label/Mono Font:** DSEG7 Classic Bold (self-hosted, OFL) for tripmeter digits only

**Character:** Overpass descends from Highway Gothic, the lettering of US road signs, so the dashboard reads like signage and a navigator's notes. Its tabular figures and slashed zero hold every number in a fixed column.

### Hierarchy
- **Display** (800, 39px, 1.15): the title of a sheet (Stages, Legend, a report's drive name).
- **Headline** (800, 31px, 1.15): section heads (Performance, Driver behavior, Laps) and the drive title on the review.
- **Title** (800, 25px, 1.0): roadbook box numbers and stage names. Gear shifts are not roadbook boxes; they appear in the report.
- **Note** (600, 20px, 1.3): the roadbook box note, summary sentences, and plain state lines.
- **Body** (400, 16px, 1.5): prose and table text, 68ch at most.
- **Label** (800, 13px, 0.06em, uppercase): badges and tags ("Replay of public data", "Synthetic", "Resolved"). Meta labels (dt) use 600 at 13px in box rule gray, sentence case.
- **Report number** (800, 49px, 1.05): a performance run's value, swiped green.
- **Live text** (600 and 800, 50.2px at 390 wide, 49px at 1440): every text element of the tripmeter; 20 arcmin of cap height or more at 1 m.
- **Live digits** (DSEG7 700, 70.4px at 390 wide, 153px at 1440 by 900): 40 arcmin or more at 1 m.

### Named Rules
**The Tabular Rule.** Every number uses tabular figures and the slashed zero (`"tnum" 1, "zero" 1`), so columns of times, distances, and values never jitter.

**The Glance Rule.** In the live view no text is smaller than 20 arcmin at 1 m (50.2px at 390 wide, 41.8px at 1440 wide) and digits reach 40 arcmin. A value never moves the layout: each digit bank has fixed positions, padded with blank digits.

**The Units Stay Rule.** A number and its unit never break across lines ("106 °C", "11 s").

## Layout

The frame is shared by every route and state: a masthead with a 3px ink rule under it, the content, and the safety line printed as the footer rule (3px ink over it, 800 weight). The footer is sticky, so the safety line is visible without scrolling at 1440 by 900 and 390 by 844.

At desktop widths (over 900px) the review pins the frame to the viewport: a drive head band (title, source badge, meta line, stat rail, actions), then a 58 to 42 split. The left column is the roadbook scroll; the right column is the map trace over 5 channel strips (44 to 56), with a 2px ink column rule between them. A strip never shrinks below its label cell: on a short window, or with the summary or export output open, the map gives way first down to a 128px floor, and only then does the right column scroll. Boxes are 4 ruled cells: number (72px), time and distance (112px), tulip (64px), note.

At 900px and narrower the review flows as 1 column: the roadbook is full width, each box stacks its note under a row of number, time, and tulip, and the selected box's map and strips follow directly below it. The stat rail becomes 2 ruled columns. Sheets (stages, report, legend) hold at 1180px centered with a 24px gutter (16px when narrow).

The live view fits 1 viewport with no scroll: digit banks, a 4px ink rule, the alert headline and citation, and the safety line. Under 1100px it stacks; at 1100px and wider the banks sit left of a 4px ink column rule with the alert to the right. Tripmeter units use line-height 0.8. In the portfolio embed only, glance text uses line-height 1 (to fit the fullest alert frame at 390 wide), and a bar above the tripmeter carries a "Roadbook" link (40px target) and the credit "Replay of public data (comma2k19, MIT)" at 16px and 13px. That bar sits outside the tripmeter, so it is exempt from the Glance Rule; the device live view has no bar and no navigation.

## Elevation & Depth

Flat. There are no shadows anywhere. Depth comes from rule weight: 3 to 4px ink rules frame the page and the live view, 2px ink rules separate regions and the selected context, 1px hairlines separate boxes, cells, and rows. Selection is shown by a flat fill, never by lift.

### Named Rules
**The Ruled Paper Rule.** Structure is drawn with lines, never with elevation, blur, or tinted panels.

## Shapes

Square corners everywhere (0px): boxes, buttons, tags, chips, inputs, swatches. Tulip glyphs are drawn as SVG from the heading change in local meters: an entry dot, a stem, and an exit arrow turned by the change; a turn back is a hairpin beside the stem, a stop is a crossbar, a missing position is a struck stem. Signal state shows by line form: live solid, stale dashed, missing struck, trimmed dotted.

## Components

### Buttons
- **Shape:** square (0px), 2px tulip ink border, 40px tall.
- **Default:** roll white face, tulip ink label at 800.
- **Hover:** inverts in 1 step (ink face, roll white label); no easing.
- **Focus:** 3px tulip ink outline, 2px offset.
- **Disabled:** box rule label and border.
- **Quiet:** 1px border, 600 weight, for Close and Show all.

### Chips
- **Citation chip:** 13px, 1px border; trigger citations in solid tulip ink, context citations dashed box rule. Parts read source (800), signal, window, and stat with sample count, separated by middle dots.
- **Stat chip:** in a summary, the cited stat's label and exact `display` string in 800.
- **Tag:** uppercase label type in a 1px ink box ("Synthetic", "Resolved" in box rule gray); at live size the tag grows to live text with a 2px border.

### Cards / Containers
- None. Content sits in ruled rows and ruled tables, never in cards.

### Inputs / Fields
- **Style:** square, 2px tulip ink border, roll white field, 20px text. The only input is the parked-only device token field.
- **Focus:** the 3px ink focus ring.
- **Error:** a plain ink sentence (16px, 600) between the field and the button: "Type the token first." for an empty field, or "The device refused that token. Check PHOTONCAR_API_TOKEN on the device." after the device answers 401 to a saved token. A refused token is forgotten, and the caret returns to the field.

### Navigation
- Masthead: the tulip mark and "Photoncar" at 20px 800, then text links at 600. The current page carries a 3px ink underline at a 7px offset; hover shows the same. The live view has no navigation; a "Leave the live view" link appears only on keyboard focus.

### Roadbook Box
The signature component. A ruled row per event: box number (25px 800) with caution marks in red and aria text "severity n of 3"; the clock time (00:03:12), total miles, and the partial distance since the previous box; the tulip; the note (20px 600) with citation chips. Performance notes are swiped marker green; synthetic events carry a Synthetic tag; resolved alerts print gray. The selected box is filled marker yellow, lands in 2 steps under 100 ms (clip from the left), and holds the roving tab stop; j, k, and the arrow keys move the selection, and `?box=N` deep-links it.

### Channel Strip
A 124px label cell (name and unit, the value at the cursor, then the source signal or a Synthetic tag), never shorter than those 3 lines, beside a uPlot canvas: a 1.6px ink trace, hairline grid, round ticks, a dashed stale form, struck gaps, the selected-time cursor in 2px ink, a dashed caution red threshold for the selected alert, and the cited window filled marker yellow under the trace.

### Tripmeter
Three fixed digit banks (speed 3 digits in mph, rpm 4 digits, coolant 3 digits in °C) with unlit segments behind and inline Overpass units, then the alert headline with red caution marks, its citation, and the safety line, all at glance size.

## Do's and Don'ts

### Do:
- **Do** keep the ground roll white (#F5F7F7) on every surface and state, including errors and the live view.
- **Do** cite evidence next to every number: source, signal, window, and stat.
- **Do** keep text to the verified pairs: tulip ink or box rule on roll white, tulip ink or caution red on marker yellow, only tulip ink on marker green.
- **Do** step motion (at most 2 frames, under 100 ms) and remove it under `prefers-reduced-motion`.
- **Do** theme the browser surfaces: marker yellow selection, ink caret, 3px ink focus ring, box rule scrollbars.
- **Do** print loading, empty, and error states as 1 plain line of text inside the shared frame.

### Don't:
- **Don't** add shadows, radius, gradients, or glow; this world is flat paper.
- **Don't** use caution red for anything but caution and anomaly marks.
- **Don't** put red or gray text on marker green.
- **Don't** use DSEG7 outside the live tripmeter.
- **Don't** use a spinner; loading is a sentence.
- **Don't** set any live-view text below 50.2px at 390 wide or 41.8px at 1440 wide, and never ask for input there.
- **Don't** build cards or card grids; use ruled rows and ruled tables.
