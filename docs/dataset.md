# Dataset notes

Photoncar develops and tests on recorded public data and on synthetic drives. This file records what was downloaded, its license, how the replay source reads it, and what the reachable data can and cannot show. Every number here was measured on 2026-09-23 against the pinned revisions below.

## Summary

- `make fetch-dataset` downloads 1 pinned comma2k19 segment: 37 array files, 7,206,664 bytes, plus the dataset LICENSE (1,065 bytes). Total 7,207,729 bytes into `data/public/comma2k19/` (gitignored). Each file is checked against its SHA-256 in `scripts/comma2k19_manifest.json`.
- A full multi-segment route can be fetched without downloading a multi-GB chunk: `scripts/fetch_comma2k19_route.py` reads the chunk zip's file list with HTTP range requests and downloads only the arrays. The default route is 15 consecutive segments (15 minutes): 27,695,255 bytes downloaded, 108,971,808 bytes on disk.
- The Hugging Face `demo` split cannot supply a full route. Its 64 segments come from 26 routes, and only 2 pairs are consecutive.
- Standing starts to 60 mph exist in the full dataset: 91 stretches go from under 0.5 m/s to at least 26.8224 m/s. All are commuter pace. The quickest takes 22.95 s (Civic); the quickest on the RAV4 takes 24.40 s. The demo split has none.
- comma2k19 has no OBD-II data. Coolant, intake air, trims, load, throttle, rpm by PID, and module voltage come from `SyntheticObdSource` and are listed as synthetic.

## What `make fetch-dataset` downloads

- Source: https://github.com/commaai/comma2k19 at commit `4c7f1a6e1957745beadc1def0e7225f559b09a2a`.
- Segment: `Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40` (2017 Toyota RAV4, 60 s). The `|` is URL-encoded as `%7C`.
- Destination: `data/public/comma2k19/Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40/` and `data/public/comma2k19/LICENSE`.
- Skipped: `video.hevc` (37,491,754 bytes), `raw_log.bz2` (6,883,752 bytes), `preview.png` (496,734 bytes). The video and preview show real plates and scenes.
- First download: every file was checked against the git blob SHA-1 in the pinned tree, then its SHA-256 was written to the manifest (`--write-manifest`). Later runs skip files already present with the pinned hash and replace any file that is missing or differs.
- Git LFS: the raw URLs returned the files themselves, not LFS pointers. The script still follows a pointer to `media.githubusercontent.com` and checks its size and SHA-256 if one appears.
- Python builds without a CA bundle (python.org installs on macOS) get the first system bundle that exists (`/etc/ssl/cert.pem`, Debian, or RHEL paths). Certificate checks stay on.

Paths below are relative to the segment directory, except `LICENSE`.

| File | Bytes | SHA-256 |
|---|---:|---|
| `global_pose/frame_gps_times` | 19,328 | `e275dc136ac742a58191ca58b103e5361246197a241647bc07b1f928395fc5b4` |
| `global_pose/frame_orientations` | 38,528 | `0eee212a8a1e6cc79978d84217aba068bd3ab9f441a6c1eebb51052ba698d1bb` |
| `global_pose/frame_positions` | 28,928 | `85864c9632030f5b6f0a6a8a78f2c9079e7555725d38a28ae029ed5dd4fc9057` |
| `global_pose/frame_times` | 9,728 | `2bec8428f9302b30a7edef3b86de0bc797c77e6964f404b3d982af1ce4dfcb5d` |
| `global_pose/frame_velocities` | 28,928 | `2e2a3b5d222153b440db70b34ea91e4f0eda661d4b139f16d7b057754cd978c2` |
| `processed_log/CAN/radar/t` | 80,928 | `b41517d6f7081b389b9726208d1b100f052f80ea28fd589562067c53fbb7d3db` |
| `processed_log/CAN/radar/value` | 565,728 | `4f4dddddf2a890aa4bb8acb83e3523327d3c9c1e6f327ac936754beabca4b6f9` |
| `processed_log/CAN/raw_can/address` | 1,083,872 | `581d1e63b955b1218b3c4d667878e2c30f67c979df2c5b046f76c4741be0dd5e` |
| `processed_log/CAN/raw_can/data` | 1,083,872 | `04842858174a318e441f6a1e3f38583d52be135327a43de1e51750ce5cea4938` |
| `processed_log/CAN/raw_can/src` | 1,083,872 | `c2ac62cdcb420cd416073f3c30eaaefb51307f2a6ab45e0e4f86eca09c14a935` |
| `processed_log/CAN/raw_can/t` | 1,083,872 | `57ee5a9875ac96c08e0e5be94a6e4529fa46bb675cbed1727837f98bcafbba79` |
| `processed_log/CAN/speed/t` | 39,920 | `53d2a88becfc45f9dcb804f2f78436f2bb6246d80386ff18ded10af679d5d6f3` |
| `processed_log/CAN/speed/value` | 39,920 | `ba40ded2c9d2a6d5f98801f5438d0cb5d811ccf1444e3a3f680d7addb707551f` |
| `processed_log/CAN/steering_angle/t` | 39,920 | `eb489c74b52cebccd30b803860d70aaafc44801a44b93e72585d602a982dbfc7` |
| `processed_log/CAN/steering_angle/value` | 39,920 | `6af86fe2fce790c6d3a51d512c625e4766699f473ec13207ea043c08d83a6bea` |
| `processed_log/CAN/wheel_speed/t` | 39,920 | `53d2a88becfc45f9dcb804f2f78436f2bb6246d80386ff18ded10af679d5d6f3` |
| `processed_log/CAN/wheel_speed/value` | 159,296 | `021dd8d0ac5029b34d3d2ddfc53c2654944baf57da4cb45d7ac4b0f995c69d2f` |
| `processed_log/GNSS/live_gnss_qcom/t` | 368 | `b6235345d9e056975660a6f8bba19281d2e3a3f40f614a3df9cd319bd7ab528b` |
| `processed_log/GNSS/live_gnss_qcom/value` | 1,568 | `1b544d6597976d494c40bddcbc36fc3020f7b00611dee7f4ba452200db0ea428` |
| `processed_log/GNSS/live_gnss_ublox/t` | 4,760 | `238794a8b336a9dc69e6787116d6e8fe4ce9013e4f454ca2bcde010b43011e95` |
| `processed_log/GNSS/live_gnss_ublox/value` | 27,920 | `1136d8819a640d482c06ed6460efdb84a625ddf765ed581b1cf28f030dabfff3` |
| `processed_log/GNSS/raw_gnss_qcom/t` | 4,928 | `b994638a16fc14c8b5561ce2e50a342490f722eab7c618eb2aec50f51522099e` |
| `processed_log/GNSS/raw_gnss_qcom/value` | 48,128 | `9c0b72b83ab20617fe774ac3dcf23fe24f597ab2112a3b908d6539149295fdf4` |
| `processed_log/GNSS/raw_gnss_ublox/t` | 73,952 | `1189929515072dbb3f2b87a4cca60fdf8d62f03ae20e448c6ed62aeb10438562` |
| `processed_log/GNSS/raw_gnss_ublox/value` | 738,368 | `369d0a467aad1a45b8d7910629c282d15f7d421dd8f33b34bc02ba97ac909576` |
| `processed_log/IMU/accelerometer/t` | 50,176 | `9df28fe965d5936bb91a020b688e35304ee4b823e388cb814d077b1f934a8558` |
| `processed_log/IMU/accelerometer/value` | 150,272 | `313949234c212c4f02b0a7c5f1c14173ac2b4dac50297782f50dc4e43fc9579b` |
| `processed_log/IMU/gyro/t` | 50,176 | `9df28fe965d5936bb91a020b688e35304ee4b823e388cb814d077b1f934a8558` |
| `processed_log/IMU/gyro/value` | 150,272 | `7af1d2f8b2ec8b78a0af59dde3a3e40bf8a8d425fb0c96a7fc71cadc0830b50c` |
| `processed_log/IMU/gyro_bias/t` | 50,176 | `9df28fe965d5936bb91a020b688e35304ee4b823e388cb814d077b1f934a8558` |
| `processed_log/IMU/gyro_bias/value` | 150,272 | `1c3dce954b63d437c16c76d7c1df91ea4c8c58fb8edcc06e5c3de5cee35d9859` |
| `processed_log/IMU/gyro_uncalibrated/t` | 50,176 | `9df28fe965d5936bb91a020b688e35304ee4b823e388cb814d077b1f934a8558` |
| `processed_log/IMU/gyro_uncalibrated/value` | 150,272 | `7f0020194f66c06922d3b34890407db3790da5a83a6e6c9c0b0f01d8f19e279c` |
| `processed_log/IMU/magnetometer/t` | 4,864 | `6dd128290581bbd86f6998eb4fc4590c7e1300dab9eafc938ccd72a100b34bbb` |
| `processed_log/IMU/magnetometer/value` | 14,336 | `3561b468184a4ae695f7fc1e11c96d1032e5bb725b85b29cc05fcbd943ffc901` |
| `processed_log/IMU/magnetometer_uncalibrated/t` | 4,864 | `6dd128290581bbd86f6998eb4fc4590c7e1300dab9eafc938ccd72a100b34bbb` |
| `processed_log/IMU/magnetometer_uncalibrated/value` | 14,336 | `93db4146db941251fc6c8c8cde4351140e7b8b053e906afc010e0830afc6cbc9` |
| `LICENSE` | 1,065 | `71381aa1c6d9e374bfa7f7797f745c0d09fdb52ce1ee9dcae6ca618217b248d4` |

## How `Comma2k19Source` reads a segment

`Comma2k19Source(segment_dir)` yields `CanFrame`, `GpsFix`, and `ImuSample` merged in non-decreasing `t_ns`. `meta.kind` is "replay" and `meta.dataset_ref` is the segment path relative to `data/public/comma2k19/`.

- Time: every `t` array is seconds of the recording device's boot clock. `t_ns = round(t * 1e9)`.
- Files are `.npy` arrays saved without the extension. `np.load` reads them directly.
- Raw CAN (`processed_log/CAN/raw_can/{t, src, address, data}`): `data` is `|S8` and numpy drops trailing null bytes, so payloads are padded back to 8 bytes. The true DLC is not stored, so every frame gets DLC 8. `src` 0 to 127 is the bus. `src` 128 and 129 are frames the logger sent itself; they are dropped and counted in `counts().dropped_can_by_src`. IDs above 0x7FF are marked extended (none in the pinned segment; the largest is 1779).
- GPS (`processed_log/GNSS/live_gnss_ublox`): columns are latitude (deg), longitude (deg), speed (m/s), UTC timestamp, altitude (m), bearing (deg). The dataset README says the UTC column is in seconds. The pinned data holds milliseconds: values near 1.533e12 with steps of 100 at 10 Hz. Values above 1e11 are read as milliseconds. Bearing is degrees clockwise from north; the replay converts it to radians. The array has no HDOP, satellite count, or fix type: `hdop` is NaN, `n_sats` is 0, and `fix_type` 3 is assumed for every row with finite, nonzero coordinates. Rows without them are dropped and counted.
- IMU (`processed_log/IMU/{accelerometer, gyro}`): m/s^2 and rad/s in [forward, right, down]. The replay rotates them to x forward, y left, z up (x, -y, -z). In the pinned segment the mean z reading is +9.661 m/s^2, so z up reads about +g. When the gyro has its own timestamps (some demo segments log it at 20 Hz), it is linearly interpolated onto the accelerometer's.
- Speed trace: `speed_trace()` returns `processed_log/CAN/speed` (vehicle speed from CAN, m/s), or the u-blox speed when a segment has no CAN speed. `SyntheticObdSource` uses it.

Measured on the pinned segment (also asserted by `tests/integration/test_comma2k19_replay.py`, marker `dataset`):

| Stream | Count | Rate |
|---|---:|---:|
| CAN bus 0 | 53,800 frames | 896.8 frames/s |
| CAN bus 1 | 45,000 frames | 750.4 frames/s |
| Dropped TX echoes (src 128, 129) | 12,434 and 24,234 frames | |
| u-blox fixes | 579 | 9.68 Hz |
| IMU samples | 6,256 | 104.26 Hz |
| CAN speed | 4,974 | 82.9 Hz, 7.974 to 19.841 m/s |

Rates differ between routes. On the default full route below, the u-blox logs about 5 Hz (4,505 fixes in 900 s), not 10 Hz.

## License

comma2k19 is released under the MIT License. The LICENSE file at the pinned commit, https://github.com/commaai/comma2k19/blob/4c7f1a6e1957745beadc1def0e7225f559b09a2a/LICENSE, reads exactly:

```text
MIT License

Copyright (c) 2018 comma.ai

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The fetch script stores this file next to the data (`data/public/comma2k19/LICENSE`, SHA-256 `71381aa1c6d9e374bfa7f7797f745c0d09fdb52ce1ee9dcae6ca618217b248d4`). The Hugging Face card (`commaai/comma2k19`) also says `license: mit`. MIT is written for software; Photoncar treats the data as MIT with attribution and keeps the notice.

## Attribution

Use this wording wherever replay data, figures, or numbers from it appear (README, case study, figure captions):

> Replay data: comma2k19 by comma.ai, MIT License. https://github.com/commaai/comma2k19. H. Schafer, E. Santana, A. Haden, R. Biasini, "A Commute in Data: The comma2k19 Dataset", arXiv:1812.05752, 2018.

Label synthetic signals as synthetic next to any figure that uses them (for example "OBD values: synthetic").

## Privacy note

comma2k19 is real public commuter data: 2 cars driving California's highway 280 between San Jose and San Francisco in 2018. It is published openly, but it is still real people's trips.

- Figures show local meters only (x, y from the first kept fix). No basemap, no map tiles, no latitude or longitude on screen or in exports.
- Never render `video.hevc` or `preview.png`; they are not downloaded.
- Route ids carry a device id and a start time (for example `b0c9d2329ad1606b|2018-08-02--08-34-47`). They are public in the dataset and appear only as `dataset_ref`.
- Nothing from `data/` enters the repo: no arrays, no GPS traces, no drive files. The manifest holds only paths, sizes, and hashes.
- The dataset never comes near the owner's home, so a privacy-zone test on it would pass even if trimming were broken. Privacy tests use a synthetic zone placed on a synthetic trace.

## Full routes, the demo split, and standing starts

Investigated with range requests only, against Hugging Face `commaai/comma2k19` at revision `4bff77c7254c654c28d4c2726186b4e825adccee`. No chunk was downloaded whole.

### The demo split (Parquet)

- 3 files, 227,930,673 bytes: `data/demo-00000-of-00003.parquet` (81,159,824), `demo-00001` (73,289,381), `demo-00002` (73,481,468). 64 rows in 6 row groups of 10 or 11 rows.
- Columns: `segment_id` (string), `preview` (image, the first video frame), and `log`, a struct of 37 list columns that mirror the segment arrays: `global_pose__*` (5), `processed_log__CAN__{radar, raw_can, speed, steering_angle, wheel_speed}__*` (raw CAN as `address`, `data` (binary), `src`, `t`), `processed_log__GNSS__{live_gnss_qcom, live_gnss_ublox, raw_gnss_qcom, raw_gnss_ublox}__*`, and `processed_log__IMU__{accelerometer, gyro, gyro_bias, gyro_uncalibrated, magnetometer, magnetometer_uncalibrated}__*`. So the demo has CAN, GNSS, and IMU. It has no video and no OBD-II.
- Segment ids: all 64 were read from the `segment_id` dictionary pages (112,627 bytes fetched). All are RAV4 (`b0c9d2329ad1606b`), from 26 routes, usually 3 segments apart. Only 2 pairs are consecutive: `2018-07-29--16-37-17` segments 4 and 5, and `2018-08-01--21-13-49` segments 9 and 10. The pinned example segment (`2018-08-02--08-34-47/40`) is one of the 64.
- So the demo split cannot supply a full multi-segment route. It also holds no standing start to 60 mph (checked on the CAN speed of all 64 segments, joining the 2 consecutive pairs). Its speeds range from 0.0 to 35.30 m/s, and 16 of its 62 segment groups contain a stop.
- Reading it needs a Parquet reader (duckdb from the optional `dataset` extra, plus its httpfs extension for remote files). The zip route below needs no extra dependency.

### The raw_data zips and full routes

- 10 zips, 94,622,767,664 bytes in total (`Chunk_1.zip` 8,731,252,405 to `Chunk_10.zip` 9,901,342,289). Chunks 1 and 2 are the RAV4; chunks 3 to 10 are the 2016 Honda Civic.
- Each zip is ZIP64. Its central directory sits at the end (Chunk_1: 1,725,354 bytes for 11,209 entries; the other chunks list 11,700 to 12,884 entries) and gives each file's offset, compressed size, and CRC-32, so single files can be read with HTTP range requests.
- Across all 10 chunks: 178 routes and 2,035 segments with processed logs (the README says 2019 segments). 154 routes have no missing segment numbers. A route's arrays (processed_log and global_pose) take 293,269 to 57,670,446 bytes to download (median 21,592,932), for 1 to 31 segments (median 12). Every route fits well under 400 MB.
- `scripts/fetch_comma2k19_route.py` does this with the standard library: it reads the central directory, downloads the route's array members, inflates them, checks each size and CRC-32, and checks SHA-256 against the "route" section of `scripts/comma2k19_manifest.json`. It refuses any reply that is not a 206 partial response, so it never pulls a whole chunk, and it refuses a plan over `--max-bytes` (400,000,000 by default). `--list Chunk_N.zip` lists a chunk's routes with segment ranges and download sizes.
- Default route: `Chunk_2/b0c9d2329ad1606b|2018-11-11--13-08-44`, segments 9 to 23 with none missing, 555 files, 27,695,255 bytes downloaded, 108,971,808 bytes on disk. The segments join with no gap over 0.031 s and span 900.0 s. 807,127 frames on bus 0, 675,000 on bus 1, 4,505 u-blox fixes, 93,828 IMU samples.
- To run it: `uv run python scripts/fetch_comma2k19_route.py`, then build 1 `Comma2k19Source` per segment directory (`data/public/comma2k19/Chunk_2/<route>/9` to `/23`) and merge them in segment order. Boot time continues across segments, so `t_ns` stays monotonic. `SyntheticObdSource` can overlay each segment or the joined speed trace.

### Standing starts and hard stops

Scanned on the CAN speed arrays of all 2,035 segments (59,229,639 bytes of range reads for the speed arrays, plus the 10 central directories), joining consecutive segments of each route.

- Standing start: the last sample under 0.5 m/s before speed reaches 26.8224 m/s (60 mph). 91 exist: 15 on the RAV4 and 76 on the Civic. The median takes 71.3 s. The quickest: 22.95 s (Civic, `Chunk_6/99c94dc769b5d96e|2018-07-12--16-42-23`, segment 9), 24.40 s (RAV4, `Chunk_2/b0c9d2329ad1606b|2018-11-11--13-08-44`, segment 14), 25.73 s (Civic, `Chunk_10`).
- In the default route, measured with `Comma2k19Source.speed_trace()`: under 0.5 m/s (0.485 m/s) at boot time 268,102.320 s, 60 mph (26.835 m/s) at 268,126.716 s: 24.396 s, a mean of 1.080 m/s^2 (0.110 g). This is a commuter merge, not a performance launch.
- Stops from 60 mph to under 0.5 m/s: 100 exist; the quickest takes 15.02 s over 255.6 m. The strongest 1 s mean deceleration in the whole dataset is 4.73 m/s^2 (0.48 g). No real stop comes near 1 g.
- For figure 1.4: a real standing start to 60 mph exists and can be shown, labeled as commuter pace. Performance numbers (0 to 60 at 0.5 g, 60 to 0 at 1.0 g) come only from the synthetic track day and must be labeled synthetic.

## Synthetic data

`photoncar.sources.synthetic` produces everything comma2k19 lacks. Nothing in it is measured, and every signal it produces is listed in `meta.synthetic_signals`.

- `SyntheticObdSource(base, seed)`: service 01 polls on the `config [obd]` schedule (10 requests/s: rpm, speed, and throttle at 2 Hz each, then the slow group round robin at 4 Hz; PID 0x10 is skipped for the E90 profile, which has no MAF sensor). rpm follows speed through an assumed 6-speed gear set (4.06, 2.40, 1.58, 1.19, 1.00, 0.87; final drive 3.85; tire radius 0.335 m). Throttle and load follow acceleration, coolant sits near 90 °C, trims stay within 3 %, module voltage near 14.1 V. Values go through J1979 byte encoding, so they are quantized as an ECU would send them, and `ObdPoll.raw` holds the data bytes.
- `synthetic_drive(seed, "track_day")`: 5 s at rest; 0.5 g to 70 mph (0 to 60 mph in 5.470247230 s); 4 s cruise; 1.0 g stop (60 to 0 mph in 36.68128983 m); a pull away at 0.3 g; 3 laps at 25 m/s of an oval with 300 m straights and 100 m radius half circles (lap 49.13274123 s, 6.25 m/s^2 lateral), timed by a 40 m start and finish line exposed as `start_finish` in local meters; coolant at 106 °C for exactly 20 s during the laps; a 0.5 g stop; 5 s at rest. GPS at 10 Hz, IMU at 104 Hz with fixed-seed noise. Positions sit around a fake origin at latitude 0.0, longitude 0.0 (open ocean), mapped through the WGS84 east/north tangent plane that a store rollup uses, with the first fix exactly on the origin.
- `synthetic_drive(seed, "commute")`: stop and go with 3 turns and a traffic light; the seed picks speeds and durations.
