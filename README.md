# IVME trainer — a reverse-engineering writeup

An external movement trainer for **IVME v0.17.10.0** ("Never Stop Moving",
Godot 4.7, DDPWorks), and the story of how it was built.

It is pure Python (stdlib `ctypes` only). No game files are modified and
nothing is injected: it opens `ivme.exe` with `ReadProcessMemory` /
`WriteProcessMemory` and edits values in RAM.

**Features:** speed preset · jump preset · fly · noclip · adjustable
intensity · one-key restore.

> **Offline / practice only.** IVME re-simulates every public run from your
> recorded inputs with stock movement data. Anything this trainer changes
> will desync that check. See [Public runs](#public-runs-and-anti-cheat).

---

## Quick start

Start the game and load into a level, then:

```powershell
python ivme_trainer.py                      # interactive hotkey mode
```

| Key (number row or numpad) | Action |
| --- | --- |
| **1** | toggle speed preset |
| **2** | toggle jump preset |
| **3** | toggle fly (Space = up, C = down, release = hover) |
| **4** | toggle noclip (fly + no collision) |
| **+** / **-** | cheat intensity up / down (0.25x – 4x) |
| **9** | rescan memory (after a level load, if something stops working) |
| **0** | restore everything to stock |
| **END** | restore and quit |

IVME binds none of these keys (checked against the input map in
`project.binary`). The hotkeys still fire while you type in chat — press the
key again or press **0** if you hit one by accident.

One-shot commands:

```powershell
python ivme_trainer.py --scan-only          # find arrays + players, print values
python ivme_trainer.py --set max_speed=7.5  # patch one field, verify, exit
python ivme_trainer.py --preset speed --intensity 2
python ivme_trainer.py --restore            # stock values + collision back on
python ivme_trainer.py --fly                # interactive, fly starts ON
python ivme_trainer.py --noclip             # interactive, noclip starts ON
python test_trainer_logic.py                # offline tests, no game needed
```

Requirements: Windows x64, Python 3.10+, no third-party packages. Run as
administrator if `OpenProcess` fails.

---

## The writeup

### 1. Looking inside the game

IVME is a Godot 4.7 game. Everything lives in `ivme.pck`. The PCK format is
simple: a header, a file directory (path, offset, size), and the file data.
A few lines of Python list and extract any file from it.

The scripts are compiled GDScript (`.gdc`). [GDRE Tools](https://github.com/GDRETools/gdsdecomp)
decompiles them. The game's bytecode is not auto-detected, but forcing
`--bytecode=4.5.0` works. The interesting scripts were:

* `scripts/player/player_movement_data.tres` — every movement constant.
* `scripts/player/movement_model.gd` — the movement simulation
  (`pre_move` / `post_move`, `_enforce_speed_caps`, jumps, walljumps,
  slides) at a 120 Hz physics rate.
* `scripts/PlayerController.gd` and `scripts/player/player_body.gd` — the
  player node, its collision shape, and its movement state.
* `scripts/headless_verifier.gd` — the server-side replay check (see below).

### 2. The movement data

All tuning sits in one resource, `PlayerMovementData`, with 54 script
variables: `max_speed=5.0`, `accel=20`, `air_accel=20`, `friction=8`,
`jump_velocity=4`, `gravity_scale_base=1.2`, `air_speed_cap_multiplier=1.03`,
`per_bhop_cap_increase=1`, `max_air_cap_bonus=5`, `base_fov=95`, and so on.
The stock values were dumped with the Godot 4.7 editor into
[`movement_data.json`](movement_data.json), which also holds the presets.

Because `movement_model.gd` reads every constant from this one resource,
changing it changes speed caps, bhop scaling, jumps, walljumps and slides
all at once.

### 3. Finding it in memory

At runtime, a GDScript instance stores its variables as a contiguous array
of Godot `Variant`s. On x86_64 each `Variant` is 24 bytes: a 4-byte type id,
then the payload at `+8`. A `FLOAT` variant holds a `double`.

So the 54 movement variables are a `Variant[54]` with a very distinctive
pattern of doubles. The trainer:

1. Walks the private read/write heap pages (skipping images and mapped
   files cuts a multi-GB scan down to ~2–3 seconds).
2. Searches for `95.0` (`base_fov`, a value no preset touches) as an anchor.
3. Checks 24 slots around it: all must be `FLOAT`, finite, and sane, and at
   least 16 must still equal stock. That last rule means a rescan still
   works while a preset is active.

There are no static offsets, so ASLR and most game patches do not break it.
The game keeps several copies (cached resource, local player, replay ghost,
menu instances), so every write goes to **every** copy and is verified by
reading it back.

**Bug found on the way:** an early scan hit was later freed and reused by
the game for other data. Writing to it would have corrupted random memory.
Now every address is re-validated against the signature right before any
read or write.

### 4. Speed and jump presets

These are just writes into the movement array. Each preset in
`movement_data.json` is scaled by an **intensity** multiplier: at `1x` you get
the preset value; at `2x` the value goes twice as far past stock
(`max_speed 5.0 → 10.0` instead of `7.5`).

Fields the game sets itself at runtime (`respawn_position`, mouse and
controller sensitivity) are never touched by presets or restore.

### 5. From the movement array to the player

Some scan hits are not the bare resource but a `PlayerController` instance.
Its variable array is longer (94 slots): the 54 movement fields, then
runtime state. A fixed pattern of variant types identifies it, and two of
those slots are object references:

* slot 84 → `col_shape` (the `CollisionShape3D`)
* slot 87 → `_movement_state` (holds the player's `velocity`)

Following a Godot object reference to its script variables goes
`Object → script_instance (+0x68) → members array (+0x50)`. Those offsets,
plus `Node::parent (+0x100)` and `CharacterBody3D::velocity (+0x550)`, were
found with a probe: a tiny Godot project exported as a `.pck` and run under
a **copy** of `ivme.exe`, so the native layout is exactly the game's.
Toggling one property at a time and diffing object memory reveals each
offset.

Every pointer in the chain is sanity-checked (heap range, alignment, vtable
in the executable's range, expected variant types). If anything looks wrong,
the trainer skips that player instead of writing.

### 6. Noclip — the first idea did not work

The obvious approach is to set `CollisionShape3D.disabled = true` in memory.
The probe showed it does **nothing**: the physics server only learns about a
change through the property setter, and a raw memory write skips the setter.
The probe body kept resting on its floor with `disabled = true`.

The trick that works is in IVME's own code. Every physics tick,
`_handle_slide_crouch` runs:

```gdscript
col_shape.position.y = _original_col_pos_y
```

That is a real setter call. So the trainer writes `_original_col_pos_y`
(player slot 55) to `1000.0`, and the game itself moves the collision shape
1000 units up into empty sky. The body no longer touches anything. In the
probe, the body fell straight through the floor. On toggle-off the captured
original value (stock `0.0`) is written back.

### 7. Fly — fighting the engine for the velocity

Fly zeroes `gravity_scale_base` / `gravity_scale_max` and holds the
vertical velocity at a target (Space = `+8 × intensity`, C = `-8 × intensity`,
nothing = `0`, so you hover).

The first version wrote only `_movement_state.velocity.y`, and it barely
worked. The reason: at the start of each tick, the movement code copies the
**native** `CharacterBody3D.velocity` into the state, which wiped the write.
The fix follows the collision shape's parent pointer to the body and writes
the velocity in **both** places. In a live test the velocity then held at
the target on most 50 ms samples, which is enough for smooth flight.

**Picking the right player.** The game often has several
`PlayerController` instances that all look controllable (menus, leftover
scenes). Only the real one advances `state.physics_time` every frame. The
trainer samples it twice, 150 ms apart, and drives the one that moves. It
re-checks this every ~2 seconds, and re-acquires all pointers when a level
load replaces the player.

### 8. Testing

* `test_trainer_logic.py` builds fake players, shapes, bodies and movement
  states in a fake memory backend and tests the signature, player
  detection, noclip capture/restore, fly velocity writes (state **and**
  native), primary-player selection, stale-pointer refusal, and intensity
  scaling.
* Live checks were done against the running game: presets, intensity, fly
  up / hover, noclip on / off, and restore all reported `OK` and read back
  correctly, with the game responsive throughout.

---

## Public runs and anti-cheat

* **Runs are replay-verified.** `headless_verifier.gd` re-simulates your
  recorded inputs with stock movement data (`MAX_VALIDATE_H_SPEED = 24`,
  `TELEPORT_DISTANCE = 2.0`, `FINAL_POSITION_TOLERANCE = 2.0`, plus desync
  tolerances). Any change to your path desyncs it. Fly and noclip are the
  most obvious cases. Expect such runs to be rejected.
* **Certael attestation ships with the game**
  (`certael.windows.template_debug.x86_64.dll`, `certael_agent_probe.dll`):
  signed build manifest, launch through a "Certael Agent", session signing
  and revocation. This trainer does **not** bypass, hide from, or interfere
  with any of it.
* Keep the trainer folder **outside** the game install before any verified
  session — a signed build manifest may hash the install tree.

Use it to practice and to learn how the movement model behaves at other
tunings. Do not use it for public or verified runs.

## Limits

* Native offsets (`+0x68`, `+0x50`, `+0x100`, `+0x550`) belong to this exact
  Godot template build. If an update rebuilds it, fly and noclip will refuse
  to write (the sanity checks fail) until the offsets are found again.
* If the developers retune movement, update the defaults in
  `movement_data.json` so the signature still matches.

## Files

* `ivme_trainer.py` — the trainer (attach, scan, patch, fly / noclip, hotkeys).
* `movement_data.json` — field order, stock defaults, preset definitions.
* `test_trainer_logic.py` — offline tests against a fake memory backend.

## Disclaimer

Not affiliated with DDPWorks. No game code or assets are included in this
repository. For personal, offline use.
