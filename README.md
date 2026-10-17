# IVME-ADV-2026-001 — Client-side movement state is externally writable (speed, jump, fly, noclip)

| Field | Value |
| --- | --- |
| **Advisory ID** | IVME-ADV-2026-001 (unofficial) |
| **Product** | IVME ("Never Stop Moving"), DDPWorks |
| **Affected version** | 0.17.10.0 (Steam, Windows x64). Other versions not tested. |
| **Engine** | Godot 4.7 (`template_debug` build) |
| **Component** | `PlayerMovementData` resource, `PlayerController` / `PlayerBody` scripts, `MovementModel` |
| **Weakness** | [CWE-602](https://cwe.mitre.org/data/definitions/602.html) Client-Side Enforcement of Server-Side Security · [CWE-471](https://cwe.mitre.org/data/definitions/471.html) Modification of Assumed-Immutable Data · [CWE-345](https://cwe.mitre.org/data/definitions/345.html) Insufficient Verification of Data Authenticity (F-4) |
| **CVSS v3.1** | **3.3 Low** — `CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:N/I:L/A:N` |
| **Attack type** | Local, same-user process memory read/write (`ReadProcessMemory` / `WriteProcessMemory`) |
| **Mitigated by** | Headless replay verifier shipped in the game — partly; see [§7](#7-existing-mitigations) and [F-4](#f-4-replay-verifier-snaps-instead-of-failing-on-moderate-drift) |
| **Reported** | Not reported to the developer before publication. |
| **Date** | 2026-10-17 |
| **Researcher** | [@violetweather](https://github.com/violetweather) |

---

## Contents

1. [Summary](#1-summary)
2. [Severity rationale](#2-severity-rationale)
3. [Background](#3-background)
4. [Findings](#4-findings)
   - [F-1 Movement tuning is a mutable, signature-discoverable heap array](#f-1-movement-tuning-is-a-mutable-signature-discoverable-heap-array)
   - [F-2 Collision can be removed through a per-tick re-applied script variable (noclip)](#f-2-collision-can-be-removed-through-a-per-tick-re-applied-script-variable-noclip)
   - [F-3 Vertical velocity can be overridden in state and body (fly)](#f-3-vertical-velocity-can-be-overridden-in-state-and-body-fly)
   - [F-4 Replay verifier snaps instead of failing on moderate drift](#f-4-replay-verifier-snaps-instead-of-failing-on-moderate-drift)
   - [N-1 Negative result: writing `CollisionShape3D.disabled` has no effect](#n-1-negative-result-writing-collisionshape3ddisabled-has-no-effect)
5. [Proof of concept](#5-proof-of-concept)
6. [Impact](#6-impact)
7. [Existing mitigations](#7-existing-mitigations)
8. [Recommendations](#8-recommendations)
9. [Research method](#9-research-method)
10. [Test environment and evidence](#10-test-environment-and-evidence)
11. [Limitations](#11-limitations)
12. [Timeline](#12-timeline)
13. [Using the PoC trainer](#13-using-the-poc-trainer)
14. [Files](#14-files)

---

## 1. Summary

IVME keeps all player movement tuning, the collision-shape offset, and the
player velocity in ordinary writable heap memory, with no client-side
integrity check. A local process running as the same user can:

* find the 54-field movement-tuning array with a value signature (no static
  offsets), and change speed, acceleration, jump, gravity and bhop caps;
* move the player's collision shape 1000 units away by writing one script
  variable that the game itself re-applies every physics tick (**noclip**);
* hold the vertical velocity at any value by writing it in both the movement
  state and the native `CharacterBody3D` (**fly**).

All changes take effect on the next 120 Hz physics tick and are fully
reversible. No game files are changed and no code is injected.

The direct effect is **local**. The game already ships a headless replay
verifier that re-simulates recorded inputs with stock data. It rejects
large deviations. However, it corrects moderate drift by snapping the player
back onto the recording instead of failing (F-4), so it gives less
protection than a strict replay check would. This advisory does not test or
attempt any bypass of that verifier or of the Certael attestation layer. No
run was submitted.

## 2. Severity rationale

| Metric | Value | Why |
| --- | --- | --- |
| Attack Vector | Local (L) | Needs code running on the player's own PC. |
| Attack Complexity | Low (L) | Signature scan + plain memory writes. No race to win, no special conditions. |
| Privileges Required | Low (L) | Same user as the game. Administrator only if the process handle is restricted. |
| User Interaction | None (N) | — |
| Scope | Unchanged (U) | Only the game process is affected. |
| Confidentiality | None (N) | No data is disclosed. |
| Integrity | Low (L) | The player's own simulation is changed. Public results are partly protected by the replay verifier (F-4). |
| Availability | None (N) | The game kept running normally in every test. |

Base score **3.3 (Low)**. Large changes (noclip through walls, fast fly)
produce drift far beyond the verifier's abort limit. How much smaller
changes could pass under F-4 was **not** tested. It also depends on the
backend running the verifier at all, which is an assumption — see
[§11](#11-limitations).

## 3. Background

**Godot `Variant`.** GDScript stores every script variable as a `Variant`.
On x86_64 a `Variant` is 24 bytes: a 4-byte type id at `+0`, padding, then
the payload at `+8`. A `FLOAT` (type 3) holds a `double`. A `Vector3`
(type 9) holds three `float`s. An `OBJECT` (type 24) holds an `ObjectID` at
`+8` and an `Object*` at `+16`.

**Script instances.** A `GDScriptInstance` keeps its variables as one
contiguous `Variant[]`, in declaration order (inherited classes first). So a
script's variables are a predictable, typed block of memory.

**IVME's movement pipeline** (decompiled from `ivme.pck`):

* `res://scripts/player/player_movement_data.tres` — `PlayerMovementData`, 54
  tuning variables (`max_speed=5.0`, `accel=20`, `air_accel=20`,
  `friction=8`, `jump_velocity=4`, `gravity_scale_base=1.2`,
  `air_speed_cap_multiplier=1.03`, `per_bhop_cap_increase=1`,
  `max_air_cap_bonus=5`, `base_fov=95`, …). Full table:
  [`movement_data.json`](movement_data.json).
* `PlayerController.do_movement()` runs every physics tick at
  `BASE_PHYSICS_FPS = 120`:

  ```gdscript
  _movement_state.velocity = velocity          # native body -> state
  var pre := _movement_model.pre_move(_movement_state, self, cam, delta, _locomotion_fsm)
  ...
  velocity = _movement_state.velocity          # state -> native body
  move_and_slide()
  _movement_state.velocity = velocity
  _movement_model.post_move(_movement_state, self, cam, delta, now_on_floor, slide_held)
  velocity = _movement_state.velocity
  ```

* `MovementModel._handle_slide_crouch()` runs inside `pre_move` and, when the
  player is not sliding, does:

  ```gdscript
  body.col_shape.shape.height = body._original_col_height
  body.col_shape.position.y = body._original_col_pos_y
  ```

## 4. Findings

### F-1 Movement tuning is a mutable, signature-discoverable heap array

**Description.** The 54 `PlayerMovementData` variables live as a
`Variant[54]` on the default heap. The game keeps several live copies (the
cached resource, the local player's `PlayerController`, which inherits the
fields, replay ghosts, menu instances). Every copy is writable and none is
checked for changes. The movement model reads its constants from these
copies every tick.

**Locating it.** The stock values form a distinctive pattern of doubles:

1. Enumerate committed `MEM_PRIVATE` pages with `PAGE_READWRITE`,
   `PAGE_WRITECOPY` or `PAGE_EXECUTE_READWRITE` (the default heap). Skipping
   image and mapped regions brings a multi-GB scan down to about 2–3 s.
2. Search for the double `95.0` (`base_fov`, field 34) as an anchor. No
   preset changes this field.
3. Step back `34 × 24 + 8` bytes to the array start and check 24 signature
   slots: each must be type `FLOAT`, finite, and `|v| ≤ 1000`, and at least
   16 must equal stock. The "≥ 16" rule keeps rescans working while values
   are already changed.

**Write primitive.** Write a little-endian `double` to
`array + index × 24 + 8`, for every live copy, then read it back.

**Effect.** Changes take effect on the next tick: speed caps, ground and air
acceleration, bhop cap growth, jump height, gravity, walljump force, slide
behaviour.

**Hazard found during research.** Copies are freed and their memory reused
as levels and players load. A cached address can later hold unrelated data.
The PoC re-validates the full signature right before every read and write,
so it never writes into recycled memory.

### F-2 Collision can be removed through a per-tick re-applied script variable (noclip)

**Description.** `PlayerController` instances extend the movement fields with
runtime state. In 0.17.10.0 the instance holds 94 variables with a fixed
type layout (used as a second signature). Relevant slots:

| Slot | Variable | Type |
| --- | --- | --- |
| 55 | `_original_col_pos_y` | `FLOAT` (stock `0.0`) |
| 57 | `can_move` | `BOOL` |
| 59 | `input_locked` | `BOOL` |
| 76 | `is_replay` | `BOOL` |
| 84 | `col_shape` | `OBJECT` → `CollisionShape3D` |
| 87 | `_movement_state` | `OBJECT` → `PlayerMovementState` |

`_handle_slide_crouch()` assigns `col_shape.position.y =
_original_col_pos_y` **through the property setter** on every tick in which
the player is not sliding. The game treats `_original_col_pos_y` as a
constant captured at spawn, but it is never checked.

**Write primitive.** Write `1000.0` to slot 55 of the local, non-replay
`PlayerController`. On the next tick the game itself moves the collision
shape 1000 units above the body, into empty sky. The body then collides with
nothing. To undo it, write back the original value, which is captured before
the first write.

**Why this works when a direct write does not:** the setter notifies the
physics server. See N-1.

### F-3 Vertical velocity can be overridden in state and body (fly)

**Description.** `PlayerMovementState.velocity` (state variable slot 0,
`Vector3`) and the native `CharacterBody3D::velocity` are both plain memory.
They are copied into each other several times per tick (see
[§3](#3-background)).

**Pointer chain** (offsets for this exact `template_debug` build):

```
PlayerController Variant[87]  -> Object* movement_state   (+16 in the Variant)
movement_state + 0x68         -> GDScriptInstance*
instance       + 0x50         -> Variant[] members        (slot 0 = velocity)

PlayerController Variant[84]  -> Object* col_shape        (+16 in the Variant)
col_shape      + 0x100        -> Node* parent = CharacterBody3D
body           + 0x550        -> Vector3 velocity (3 × f32)
```

Every link is checked before use: heap range, 8-byte alignment, a vtable
pointer in the executable's range, and the expected variant type layout.

**Write primitive.** With gravity set to 0 (F-1:
`gravity_scale_base = gravity_scale_max = 0`), write the target Y velocity
as one `float` at `+4` in **both** the state velocity and the native body
velocity.

**Two problems found and fixed during research:**

1. *State-only writes are lost.* `do_movement()` starts with
   `_movement_state.velocity = velocity`, so the native value overwrites the
   state at the start of each tick. Writing both places fixes this.
2. *Timing.* Physics runs at 120 Hz (every ~8.3 ms), and the game sets the
   velocity again on each tick. At a 50 ms write interval most ticks used the
   game's own value. At about 4 ms almost every tick uses the target
   (measured below). Only the Y float is written, so a tick that lands
   between our read and write cannot have its horizontal velocity replaced
   with an old value.

| Write interval | Samples at target Y velocity (state **and** body, ±0.5) |
| --- | --- |
| 50 ms | 0/20 (↑), 1/20 (↓) — 0–5 % |
| 4 ms | 101/101 (↑), 100/101 (↓) — 99–100 % |

**Choosing the right instance.** Several non-replay `PlayerController`
instances can exist at once and look the same (`can_move = true`). Only the
simulating one advances `PlayerMovementState.physics_time` (state slot 1).
The PoC samples it twice and drives only the instance whose value moves. It
re-checks this every 2 s and re-acquires all pointers after a level load.

### F-4 Replay verifier snaps instead of failing on moderate drift

**Description.** In `--verify` mode, `headless_verifier.gd` compares the
re-simulated position to the recorded keyframe only once every 120 ticks
(1 s):

```gdscript
if current_frame_index > 0 and current_frame_index % 120 == 0:
    var kf = keyframes[current_frame_index / KEYFRAME_INTERVAL]
    var dist = player_node.global_position.distance_to(kf.pos)
    if dist > DESYNC_ABORT_TOLERANCE:          # 5.0
        printerr("VERIFICATION_FAILED: Desync ...")
        get_tree().quit(1)
    elif dist > DESYNC_SNAP_TOLERANCE:         # 0.05
        player_node.global_position = kf.pos   # accept the recording
        player_node.velocity = kf.vel
```

When the re-simulation drifts between 0.05 m and 5.0 m in a one-second
window, the verifier does not fail. It moves the simulated player to the
**recorded** position and velocity and goes on. The end check only needs the
final position within `FINAL_POSITION_TOLERANCE = 2.0` of the last keyframe.

**Why it matters.** The snap is there to absorb harmless float and
frame-timing differences between machines. But it also means the recorded
keyframes, which come from the client, are trusted after each snap. So the
verifier detects how far each second drifts, not whether the whole path
was possible with stock movement. The `--validate` bounds
(`MAX_VALIDATE_H_SPEED = 24`, `MAX_VALIDATE_V_SPEED = 30`,
`TELEPORT_DISTANCE = 2.0` per keyframe) are much looser than stock
movement (`max_speed = 5.0`).

**Status.** Found by reading the code only. Not tested against a real
submission. Whether specific F-1/F-3 changes stay under the limits was not
measured, on purpose.

### N-1 Negative result: writing `CollisionShape3D.disabled` has no effect

The obvious way to do noclip is to set the shape's native `disabled` flag
in memory. A controlled probe (a small Godot project running under a copy of
the same `ivme.exe`) showed that this changes the node's field but **not**
the physics server's copy. The body stayed on the floor with
`disabled = true`. Only setter calls (`set_disabled`, `set_position`, …)
reach the physics server. F-2 works because the game makes that setter call
itself.

## 5. Proof of concept

The PoC is [`ivme_trainer.py`](ivme_trainer.py): Python 3.10+, stdlib
`ctypes` only.

```text
1. OpenProcess(VM_READ | VM_WRITE | VM_OPERATION | QUERY_INFORMATION, ivme.exe)
2. Scan heap pages for the PlayerMovementData signature            (F-1)
3. Write tuning doubles to every live copy; read back to verify     (F-1)
4. Filter hits with the 94-slot PlayerController type layout
5. Follow col_shape / _movement_state / parent pointers, check each (F-2, F-3)
6. noclip: slot 55 := 1000.0                                        (F-2)
7. fly:    gravity := 0; every ~4 ms write vel.y to state + body    (F-3)
8. restore: stock values, slot 55 := captured original              (all)
```

Reproduce (game running, loaded into a level):

```powershell
python ivme_trainer.py --scan-only            # F-1: arrays + players found, values printed
python ivme_trainer.py --set max_speed=7.5    # F-1: "OK" after read-back verify
python ivme_trainer.py --noclip               # F-2: fall through the floor
python ivme_trainer.py --fly                  # F-3: hold Space to rise, C to sink
python ivme_trainer.py --restore              # everything back to stock
```

## 6. Impact

* **Local player:** full control over movement tuning, collision and
  vertical velocity. Useful for practice, route finding and learning the
  movement model at other settings.
* **Other players / leaderboards:** reduced but not zero. Large changes
  drift past the 5 m-per-second abort limit and fail. Moderate drift is
  snapped back and accepted (F-4). Whether this is enough for a run with
  changed tuning to pass was not tested.
* **Stability:** none observed. The game stayed responsive in every live
  test. Freed-memory reuse (F-1) would be a crash risk for a careless tool.
  The PoC guards against it.

## 7. Existing mitigations

The game ships checks meant to run on the backend. They catch large
changes. F-4 describes where they are weaker.

**`scripts/headless_verifier.gd`** (shipped in the client and run as
`ivme.exe --headless -- --verify|--validate <ghost>`):

| Constant | Value | Role |
| --- | --- | --- |
| `BASE_PHYSICS_FPS` | 120 | simulation rate |
| `KEYFRAME_INTERVAL` | 6 | ticks between recorded keyframes |
| `DESYNC_SNAP_TOLERANCE` | 0.05 | re-simulation vs recorded keyframe |
| `DESYNC_ABORT_TOLERANCE` | 5.0 | abort when re-simulation drifts this far |
| `FINAL_POSITION_TOLERANCE` | 2.0 | end position must match |
| `MAX_VALIDATE_H_SPEED` | 24.0 | horizontal speed bound |
| `MAX_VALIDATE_V_SPEED` | 30.0 | vertical speed bound |
| `TELEPORT_DISTANCE` | 2.0 | max movement between keyframes |
| `RACE_TIME_TOLERANCE_MS` | 2000 | reported time vs tick count |

* `--verify` replays the recorded **inputs** through the stock movement code
  and, every 120 ticks, compares the result to the recorded keyframe. Drift
  over 5.0 m fails the run. Drift between 0.05 m and 5.0 m is snapped back
  onto the recording (F-4). The final position must be within 2.0 m.
* `--validate` checks the ghost data alone for impossible values (teleports,
  speed bounds, time consistency).
* The ghost format carries a `certael_envelope`.

**Certael** (`certael.windows.template_debug.x86_64.dll`,
`certael_agent_probe.dll`): signed build manifest, policy and grant,
launch through a "Certael Agent", session signing and revocation. This
research did not test, bypass, or interfere with it.

## 8. Recommendations

For the developers, if they want to harden the client as well (optional —
the server-side replay already addresses competitive impact):

1. **Treat the replay verifier as the source of truth** (already the
   design) and make sure every leaderboard or public submission goes
   through `--verify`, not only `--validate`.
2. **Make the replay check strict (F-4).** Count the snaps and the total
   snapped distance, and fail or flag the run above a small budget. Check
   more often than every 120 ticks. Better still, never snap to client data:
   if stock re-simulation from inputs alone does not reach the finish, the
   run is not valid.
3. **Do not re-apply mutable "original" values each tick.** Capture
   `_original_col_pos_y` / `_original_col_height` once as constants, or check
   them against the scene's value before using them (F-2).
4. **Cheap client integrity check.** Hash the `PlayerMovementData` fields at
   load time and check the hash at random intervals or at run start/finish.
   Mark the run unverifiable on mismatch. This catches F-1 locally and gives
   the player clear feedback before they submit.
5. **Flag impossible tuning in the ghost metadata** (for example
   `gravity_scale_base == 0`) so the backend can reject a run before it
   re-simulates it.

Item 2 is the important one: it is the only one that does not trust the
client. Items 3–5 do not stop a determined local attacker on an open PC.
They move detection earlier and make the result clear to the player.

## 9. Research method

1. **Unpack.** `ivme.pck` is a standard Godot 4 pack: a header, a directory
   (path, offset, size), then the file data. A short Python script extracts
   single files.
2. **Decompile.** The `.gdc` files are compiled GDScript.
   [GDRE Tools](https://github.com/GDRETools/gdsdecomp) v2.7.4 decompiles them
   with `--bytecode=4.5.0`, because auto-detect fails on this build.
3. **Dump defaults.** The Godot 4.7 editor loaded
   `player_movement_data.tres` and exported all 54 fields in declaration
   order → `movement_data.json`.
4. **Find native offsets.** A minimal Godot project was exported to a
   `.pck` and run headless under a **copy** of `ivme.exe` in a temporary
   folder, so the native layout matches the game exactly. Changing one
   property at a time and diffing object memory gave
   `script_instance +0x68`, `members +0x50`, `parent +0x100` and
   `velocity +0x550`. The same probe confirmed N-1 (body stays on the floor)
   and F-2 (body falls through the floor).
5. **Map the input layer.** The input map in `project.binary` was parsed to
   pick PoC hotkeys the game does not bind (number keys, `C`; the game uses
   W/A/S/D, Space, Shift, Ctrl, E, Q, R, G, P, Y, Z, X, Esc, Alt, Home, F3, F5).
6. **Live testing.** Only against a local, offline session. No public run
   was submitted.

## 10. Test environment and evidence

* Windows 11 x64, IVME 0.17.10.0 (Steam), Python 3.x, Godot 4.7.2 editor,
  GDRE Tools 2.7.4.
* **Offline unit tests** — [`test_trainer_logic.py`](test_trainer_logic.py)
  builds fake players, shapes, bodies and movement states in a fake memory
  backend. It covers: the signature, player detection, ghost exclusion,
  noclip capture and restore, fly writes to both state and body (horizontal
  velocity unchanged), choosing the simulating instance, refusing stale
  pointers, and intensity scaling. Result: `ALL TRAINER LOGIC TESTS PASSED`.
* **Live checks** (game responsive throughout):
  * F-1: `--set max_speed=5.5` → `OK`; presets → `OK`; a rescan shows the
    changed values; `--restore` → all stock.
  * F-2: noclip on → slot 55 reads `1000.0`; off → `0.0`; `OK`.
  * F-3: the cadence table above.
  * Freed-memory hazard (F-1) reproduced with `--full-scan`, then fixed.

## 11. Limitations

* **Assumed, not observed:** that the backend runs `--verify` on every
  submitted run. Only the client-side code was examined. No run was
  submitted to test this or F-4.
* Native offsets belong to this exact Godot `template_debug` build. If an
  update rebuilds it, the PoC refuses to write (its checks fail) until the
  offsets are found again.
* If the developers change the movement values, update the defaults in
  `movement_data.json` so the signature still matches.
* Only 0.17.10.0 on Windows x64 was tested.

## 12. Timeline

| Date | Event |
| --- | --- |
| 2026-10-17 | PCK unpacked, scripts decompiled, movement data dumped |
| 2026-10-17 | F-1 confirmed live; freed-memory hazard found and fixed |
| 2026-10-17 | N-1 negative result; F-2 confirmed in probe and live |
| 2026-10-17 | F-3 state-only write lost; fixed by writing the body too |
| 2026-10-17 | F-3 timing fix (50 ms → 4 ms); measured 0–5 % → 99–100 % |
| 2026-10-17 | F-4 found by reading `headless_verifier.gd` (not tested) |
| 2026-10-17 | Advisory written and published |

## 13. Using the PoC trainer

> **Offline / practice only.** Do not submit runs made with any of this
> active. Noclip and fly in particular fail the replay check, and Certael
> may flag the session.

Start the game, load into a level, then:

```powershell
python ivme_trainer.py
```

| Key (number row or numpad) | Action |
| --- | --- |
| **1** | toggle speed preset |
| **2** | toggle jump preset |
| **3** | toggle fly (Space = up, C = down, release = hover) |
| **4** | toggle noclip (fly + no collision) |
| **+** / **-** | intensity up / down (0.25x – 4x; scales presets and fly speed) |
| **9** | rescan memory |
| **0** | restore everything to stock |
| **END** | restore and quit |

* Keys only work while the **IVME window is in front**. They still fire
  while you type in the in-game chat — press the key again or press **0**.
* **Ctrl+C** in the console also restores and quits.
* Intensity: at `1x` you get the preset value; at `2x` the value goes twice
  as far past stock (`max_speed 5.0 → 10.0` instead of `7.5`).
* Presets and restore never touch fields the game sets itself
  (`respawn_position`, mouse and controller sensitivity).
* Keep this folder **outside** the game install before any verified
  session. A signed build manifest may hash the install folder.

One-shot commands:

```powershell
python ivme_trainer.py --scan-only
python ivme_trainer.py --set max_speed=7.5 --set jump_velocity=5.2
python ivme_trainer.py --preset speed --intensity 2
python ivme_trainer.py --restore
python ivme_trainer.py --fly
python ivme_trainer.py --noclip
python test_trainer_logic.py
```

Windows x64, Python 3.10+, no third-party packages. Run as administrator if
`OpenProcess` fails.

## 14. Files

| File | Purpose |
| --- | --- |
| `ivme_trainer.py` | PoC: attach, scan, patch, fly / noclip, hotkeys |
| `movement_data.json` | field order, stock defaults, preset definitions |
| `test_trainer_logic.py` | offline tests against a fake memory backend |
