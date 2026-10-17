#!/usr/bin/env python3
"""External trainer for IVME (Godot 4.7, v0.17.10.0) — movement stats.

How it works
------------
IVME stores all player movement tuning in a `PlayerMovementData` resource
(`res://scripts/player/player_movement_data.tres`). At runtime its 54 script
variables live in one contiguous Godot `Variant` array (24 bytes per Variant,
FLOAT variants hold a double at +8). The stock values are distinctive enough
(5.0, 20, 20, 8, 4, 1.2, 2.4, 0.12, 0.12, ... 45, 45, ... 95, 22) to find
every copy of that array in process memory with a signature scan — no static
offsets, so it survives ASLR and most game patches that don't retune movement.

The trainer patches *all* signature hits (the game keeps several copies:
cached resource, player instance, ghost, etc.) and verifies by reading back.

Some hits are `PlayerController` script instances: their member array
continues past the 54 movement fields with runtime state, including a
`col_shape` object reference (slot 84) and a `_movement_state` reference
(slot 87). Chasing those Godot object pointers
(Object -> script instance at +0x68 -> members array at +0x50) reaches the
live physics objects, which is what powers noclip and fly:

* Noclip displaces the collision shape 1000 units above the body by
  writing the script variable `_original_col_pos_y` (player slot 55). IVME's
  own movement code assigns `col_shape.position.y = _original_col_pos_y`
  through the API every physics tick, so the physics server faithfully
  moves the shape into empty sky and the body stops colliding with
  anything. (Writing `CollisionShape3D.disabled` directly does NOT work:
  the physics server never sees field writes that bypass the setter —
  verified experimentally under this exact `ivme.exe`.)
* Fly zeroes gravity (`gravity_scale_base/max`) and drives the movement
  state's `velocity.y` directly (Space = up, C = down).
* +/- adjusts an intensity multiplier that scales how far preset values
  push past stock, and the fly speed.

Usage
-----
  python ivme_trainer.py --scan-only           # find arrays, print values
  python ivme_trainer.py --set max_speed=7.5   # patch once, verify, exit
  python ivme_trainer.py --preset speed        # apply a preset once, exit
  python ivme_trainer.py --preset speed --intensity 2
  python ivme_trainer.py --restore             # stock defaults + collision on
  python ivme_trainer.py --noclip              # interactive, noclip starts ON
  python ivme_trainer.py --fly                 # interactive, fly starts ON
  python ivme_trainer.py                       # interactive hotkey mode:
      1   toggle "speed" preset      2    toggle "jump" preset
      3   toggle fly                 4    toggle noclip
      +/- cheat intensity up/down    9    rescan
      0   restore everything         END  quit (restores first)
      (number row or numpad; IVME binds none of these keys)
      While flying: Space = up, C = down (speed scales with intensity)

WARNING: IVME verifies public runs by re-simulating your recorded inputs
with stock movement data (`headless_verifier.gd`: max validated h-speed
24.0, teleport distance 2.0, final-position tolerance 2.0). Changed movement
values will desync that replay check even when they work locally, and the
Certael agent attests launch/session integrity. Expect public/verified runs
made with this active to be invalidated or flagged. Use offline.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import sys
import time

# --- Win32 constants ---------------------------------------------------------
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002

VARIANT_SIZE = 24          # sizeof(godot::Variant) on x86_64
VARIANT_FLOAT = 3          # Variant::FLOAT
VALUE_OFFSET = 8           # payload offset inside a Variant
OBJECT_PTR_OFFSET = 16     # Object* inside an OBJECT Variant (after ObjectID)

# Native-object offsets for IVME's exact template build (Godot 4.7
# template_debug), discovered by toggling properties in a probe project
# running under a copy of ivme.exe and diffing object memory. A game update
# that rebuilds the template can shift these; the trainer sanity-checks
# every pointer chain it follows and refuses implausible targets.
OBJECT_SCRIPT_INSTANCE_OFFSET = 0x68   # Object::script_instance
INSTANCE_MEMBERS_OFFSET = 0x50         # GDScriptInstance members data ptr
NODE_PARENT_OFFSET = 0x100             # Node parent pointer (shape -> body)
BODY_VELOCITY_OFFSET = 0x550           # CharacterBody3D.velocity (3xf32)

# PlayerController script-instance layout: the 54 movement fields, then
# player_body runtime fields, then PlayerController object references.
PLAYER_MEMBER_COUNT = 94
SLOT_ORIGINAL_COL_POS_Y = 55
SLOT_CAN_MOVE = 57
SLOT_INPUT_LOCKED = 59
SLOT_IS_REPLAY = 76
SLOT_COL_SHAPE = 84
SLOT_MOVEMENT_STATE = 87
# Displacing the collision shape this far above the body puts it in empty
# sky on IVME's maps; stock scene value of _original_col_pos_y is 0.0.
NOCLIP_SHAPE_OFFSET_Y = 1000.0
STOCK_COL_POS_Y = 0.0
# Variant types expected at slots 54..93 of a player instance.
PLAYER_TRAIL_TYPES = (3, 3, 3, 1, 1, 1, 1, 24, 24, 3, 1, 3, 3, 1, 2, 1, 2, 1, 1,
                      9, 3, 24, 1, 5, 1, 9, 1, 1, 1, 24, 24,
                      24, 24, 24, 24, 24, 24, 24, 24, 24)
# Object slots that may legitimately be NIL in a live player instance.
PLAYER_NIL_OK_SLOTS = (61, 62)     # _pause_menu, active_projectile
# player_movement_state member types (velocity is slot 0, a Vector3).
STATE_TYPES = (9, 3, 3, 3, 1, 1, 1, 1, 3, 1, 1, 9, 9, 1, 1, 3, 3, 3, 3, 1, 3)

INTENSITY_MIN, INTENSITY_MAX, INTENSITY_STEP = 0.25, 4.0, 0.25
BASE_FLY_SPEED = 8.0

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def find_pids(exe_name: str) -> list[int]:
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    pids = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe_name.lower():
                pids.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return pids


class GameMemory:
    def __init__(self, pid: int):
        self.pid = pid
        access = (PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
                  | PROCESS_QUERY_INFORMATION)
        self.handle = k32.OpenProcess(access, False, pid)
        if not self.handle:
            raise OSError(f"OpenProcess({pid}) failed, error {ctypes.get_last_error()} "
                          "(try running the trainer as administrator)")

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def read(self, addr: int, size: int) -> bytes | None:
        buf = (ctypes.c_ubyte * size)()
        got = ctypes.c_size_t(0)
        if k32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf, size,
                                 ctypes.byref(got)) and got.value == size:
            return bytes(bytearray(buf))
        return None

    def write(self, addr: int, data: bytes) -> bool:
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        put = ctypes.c_size_t(0)
        return bool(k32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), buf,
                                           len(data), ctypes.byref(put))
                    and put.value == len(data))

    def read_u64(self, addr: int) -> int:
        raw = self.read(addr, 8)
        return struct.unpack("<Q", raw)[0] if raw else 0

    def read_byte(self, addr: int) -> int | None:
        raw = self.read(addr, 1)
        return raw[0] if raw else None

    def write_byte(self, addr: int, value: int) -> bool:
        return self.write(addr, bytes([value & 0xFF]))

    @staticmethod
    def heapish(ptr: int) -> bool:
        return 0x10000000000 < ptr < 0x40000000000 and ptr % 8 == 0

    def is_alive(self) -> bool:
        code = wt.DWORD(0)
        return bool(self.handle and k32.GetExitCodeProcess(self.handle, ctypes.byref(code))
                    and code.value == STILL_ACTIVE)

    def regions(self, heap_only: bool = True):
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while addr < 0x7FFF_FFFF_FFFF:
            if not k32.VirtualQueryEx(self.handle, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if (mbi.State == MEM_COMMIT and size
                    and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))):
                # GDScript Variant arrays live on the default heap: private,
                # read/write pages. Skipping image/mapped/GPU regions cuts a
                # multi-GB scan down to the actual heap.
                if heap_only and not (mbi.Type == MEM_PRIVATE and mbi.Protect in
                                      (PAGE_READWRITE, PAGE_WRITECOPY, PAGE_EXECUTE_READWRITE)):
                    pass
                else:
                    yield base, size
            nxt = base + size
            if nxt <= addr:
                break
            addr = nxt


# --- Movement-data model -----------------------------------------------------

class MovementData:
    """Field table + signature for the PlayerMovementData Variant array."""

    # Indices used for signature matching: distinctive, spread across the
    # array, and all FLOAT-typed so one comparison path covers them.
    SIGNATURE_INDICES = (0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14,
                         15, 16, 17, 18, 20, 21, 22, 23, 24, 34, 35)

    def __init__(self, json_path: str):
        with open(json_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.fields = doc["fields"]
        self.presets = doc.get("presets", {})
        self.index = {f["name"]: i for i, f in enumerate(self.fields)}
        self.array_bytes = len(self.fields) * VARIANT_SIZE

    def field_addr(self, array_addr: int, name: str) -> int:
        return array_addr + self.index[name] * VARIANT_SIZE + VALUE_OFFSET

    # A candidate must have every signature slot typed FLOAT with a sane
    # finite value, and at least this many slots still at stock defaults.
    # That keeps rescans working after a preset changed a handful of fields.
    MIN_DEFAULT_MATCHES = 16

    def matches(self, blob: bytes, base: int = 0) -> bool:
        exact = 0
        for idx in self.SIGNATURE_INDICES:
            off = base + idx * VARIANT_SIZE
            if off + VARIANT_SIZE > len(blob):
                return False
            vtype, = struct.unpack_from("<I", blob, off)
            if vtype != VARIANT_FLOAT:
                return False
            value, = struct.unpack_from("<d", blob, off + VALUE_OFFSET)
            if value != value or abs(value) > 1000.0:  # NaN / implausible
                return False
            if abs(value - float(self.fields[idx]["default"])) <= 1e-9:
                exact += 1
        return exact >= self.MIN_DEFAULT_MATCHES

    def scan(self, mem: GameMemory, full_scan: bool = False) -> list[int]:
        # Anchor on base_fov (index 34, 95.0): distinctive, and no preset
        # touches it, so the anchor survives modified movement fields.
        anchor_idx = 34
        needle = struct.pack("<d", float(self.fields[anchor_idx]["default"]))
        hits: list[int] = []
        for base, size in mem.regions(heap_only=not full_scan):
            # Read in chunks so one unreadable page doesn't lose the region.
            data = b""
            chunk = 1 << 20
            pos = 0
            parts = []
            while pos < size:
                piece = mem.read(base + pos, min(chunk, size - pos))
                parts.append(piece if piece is not None else b"\x00" * min(chunk, size - pos))
                pos += min(chunk, size - pos)
            data = b"".join(parts)
            start = 0
            while True:
                i = data.find(needle, start)
                if i < 0:
                    break
                start = i + 1
                arr = i - (anchor_idx * VARIANT_SIZE + VALUE_OFFSET)
                if arr >= 0 and self.matches(data, arr):
                    hits.append(base + arr)
        return hits

    def validate(self, mem: GameMemory, array_addr: int) -> bool:
        """Re-read the whole array and re-check the signature.

        Copies of the resource are created and freed as levels/players load,
        so an address found by an earlier scan may have been recycled for
        unrelated data by the time we write to it. Always validate first.
        """
        blob = mem.read(array_addr, self.array_bytes)
        return blob is not None and self.matches(blob, 0)

    def read_field(self, mem: GameMemory, array_addr: int, name: str):
        field = self.fields[self.index[name]]
        addr = self.field_addr(array_addr, name)
        if field["type"] == "f":
            raw = mem.read(addr, 8)
            return struct.unpack("<d", raw)[0] if raw else None
        count = 2 if field["type"] == "v2" else 3
        raw = mem.read(addr, 4 * count)
        return list(struct.unpack(f"<{count}f", raw)) if raw else None

    def write_field(self, mem: GameMemory, array_addr: int, name: str, value) -> bool:
        field = self.fields[self.index[name]]
        addr = self.field_addr(array_addr, name)
        if field["type"] == "f":
            return mem.write(addr, struct.pack("<d", float(value)))
        return mem.write(addr, struct.pack(f"<{len(value)}f", *[float(v) for v in value]))

    def defaults(self) -> dict:
        return {f["name"]: f["default"] for f in self.fields}


def match_type_trail(types, expected, nil_ok_indices=()) -> bool:
    """True if Variant type ids match, allowing NIL (0) at nil-ok positions."""
    if len(types) < len(expected):
        return False
    for i, want in enumerate(expected):
        if types[i] == want:
            continue
        if want == 24 and types[i] == 0 and i in nil_ok_indices:
            continue
        return False
    return True


def scaled_preset_value(stock, preset, intensity: float):
    """Scale how far a preset value pushes past stock by `intensity`."""
    if isinstance(preset, list):
        return [float(s) + (float(p) - float(s)) * intensity
                for s, p in zip(stock, preset)]
    return float(stock) + (float(preset) - float(stock)) * intensity


def clamp_intensity(value: float) -> float:
    return max(INTENSITY_MIN, min(INTENSITY_MAX, round(float(value), 2)))


class PlayerRef:
    """Live pointers for one PlayerController script instance."""

    def __init__(self, segment: int, col_shape: int, state_members: int,
                 shape_vtable: int, can_move: bool, input_locked: bool,
                 is_replay: bool, body: int = 0, body_vtable: int = 0):
        self.segment = segment
        self.col_shape = col_shape
        self.state_members = state_members      # 0 when the chain is broken
        self.shape_vtable = shape_vtable
        self.body = body                        # CharacterBody3D Object*
        self.body_vtable = body_vtable
        self.can_move = can_move
        self.input_locked = input_locked
        self.is_replay = is_replay

    @property
    def velocity_addr(self) -> int:
        return self.state_members + VALUE_OFFSET if self.state_members else 0

    @property
    def native_velocity_addr(self) -> int:
        return self.body + BODY_VELOCITY_OFFSET if self.body else 0

    def describe(self) -> str:
        return (f"player @0x{self.segment:X} col_shape=0x{self.col_shape:X} "
                f"state={'0x%X' % self.state_members if self.state_members else 'none'} "
                f"can_move={self.can_move} input_locked={self.input_locked} "
                f"is_replay={self.is_replay}")


class Trainer:
    def __init__(self, mem: GameMemory, data: MovementData):
        self.mem = mem
        self.data = data
        self.arrays: list[int] = []
        self._noclip_saved: dict[int, float] = {}   # segment -> stock col pos y

    def rescan(self, full_scan: bool = False) -> list[int]:
        self.arrays = self.data.scan(self.mem, full_scan=full_scan)
        return self.arrays

    # -- player instances / native objects ------------------------------------

    def find_players(self) -> list[PlayerRef]:
        """Identify PlayerController instances among the movement arrays.

        A player instance's member array continues past the 54 movement
        fields with a fixed type trail; from it we read the CollisionShape3D
        pointer (noclip) and chase _movement_state to its members array
        (fly velocity). Pointers are sanity-checked so stale/freed instances
        are skipped instead of written to.
        """
        players: list[PlayerRef] = []
        for arr in self.arrays:
            blob = self.mem.read(arr, PLAYER_MEMBER_COUNT * VARIANT_SIZE)
            if blob is None:
                continue
            types = [struct.unpack_from("<I", blob, s * VARIANT_SIZE)[0]
                     for s in range(54, PLAYER_MEMBER_COUNT)]
            nil_ok = tuple(s - 54 for s in PLAYER_NIL_OK_SLOTS)
            if not match_type_trail(types, PLAYER_TRAIL_TYPES, nil_ok):
                continue

            def optr(slot: int) -> int:
                return struct.unpack_from(
                    "<Q", blob, slot * VARIANT_SIZE + OBJECT_PTR_OFFSET)[0]

            def bval(slot: int) -> bool:
                return bool(blob[slot * VARIANT_SIZE + VALUE_OFFSET])

            col_shape = optr(SLOT_COL_SHAPE)
            if not GameMemory.heapish(col_shape):
                continue
            vtable = self.mem.read_u64(col_shape)
            if not (0x7FF000000000 < vtable < 0x800000000000):
                continue

            state_members = 0
            state_obj = optr(SLOT_MOVEMENT_STATE)
            if GameMemory.heapish(state_obj):
                instance = self.mem.read_u64(state_obj + OBJECT_SCRIPT_INSTANCE_OFFSET)
                if GameMemory.heapish(instance):
                    members = self.mem.read_u64(instance + INSTANCE_MEMBERS_OFFSET)
                    if GameMemory.heapish(members):
                        state_blob = self.mem.read(members, len(STATE_TYPES) * VARIANT_SIZE)
                        if state_blob is not None:
                            stypes = [struct.unpack_from("<I", state_blob, s * VARIANT_SIZE)[0]
                                      for s in range(len(STATE_TYPES))]
                            if match_type_trail(stypes, STATE_TYPES):
                                state_members = members

            body_ptr = self.mem.read_u64(col_shape + NODE_PARENT_OFFSET)
            body_vtable = self.mem.read_u64(body_ptr) if GameMemory.heapish(body_ptr) else 0
            if not (GameMemory.heapish(body_ptr)
                    and 0x7FF000000000 < body_vtable < 0x800000000000):
                body_ptr, body_vtable = 0, 0

            players.append(PlayerRef(
                segment=arr, col_shape=col_shape, state_members=state_members,
                shape_vtable=vtable, can_move=bval(SLOT_CAN_MOVE),
                input_locked=bval(SLOT_INPUT_LOCKED), is_replay=bval(SLOT_IS_REPLAY),
                body=body_ptr, body_vtable=body_vtable))
        # Local player first: controllable and not a replay ghost.
        players.sort(key=lambda p: (p.is_replay, not (p.can_move and not p.input_locked)))
        return players

    def _player_still_valid(self, player: PlayerRef) -> bool:
        if not (self.data.validate(self.mem, player.segment)
                and self.mem.read_u64(player.col_shape) == player.shape_vtable):
            return False
        if player.body and self.mem.read_u64(player.body) != player.body_vtable:
            return False
        return True

    def col_pos_y_addr(self, player: PlayerRef) -> int:
        return (player.segment + SLOT_ORIGINAL_COL_POS_Y * VARIANT_SIZE
                + VALUE_OFFSET)

    def read_col_pos_y(self, player: PlayerRef):
        raw = self.mem.read(self.col_pos_y_addr(player), 8)
        return struct.unpack("<d", raw)[0] if raw else None

    def set_shape_displaced(self, players: list[PlayerRef], displaced: bool) -> bool:
        """Noclip: move the collision shape far above the body, or back.

        IVME's movement code assigns `col_shape.position.y =
        _original_col_pos_y` through the API every physics tick, so writing
        that script variable makes the engine itself relocate the physics
        shape. Originals are captured per instance and restored on disable;
        a fresh trainer process recognizes the displaced value and restores
        the stock scene offset (0.0).
        """
        if not self.mem.is_alive():
            print("  ! game process has exited; nothing to patch")
            return False
        all_ok, applied = True, False
        for p in players:
            if p.is_replay:
                continue
            if not self._player_still_valid(p):
                print(f"  - skipping stale player @0x{p.segment:X} "
                      "(signature/vtable check failed)")
                continue
            addr = self.col_pos_y_addr(p)
            current = self.read_col_pos_y(p)
            if current is None or current != current or abs(current) > 100000:
                print(f"  - skipping player @0x{p.segment:X} "
                      f"(implausible _original_col_pos_y {current})")
                continue
            if displaced:
                if p.segment not in self._noclip_saved and current != NOCLIP_SHAPE_OFFSET_Y:
                    self._noclip_saved[p.segment] = current
                want = NOCLIP_SHAPE_OFFSET_Y
            else:
                if p.segment in self._noclip_saved:
                    want = self._noclip_saved.pop(p.segment)
                elif current == NOCLIP_SHAPE_OFFSET_Y:
                    want = STOCK_COL_POS_Y
                else:
                    continue    # not displaced; nothing to restore
            applied = True
            if not self.mem.write(addr, struct.pack("<d", want)) \
                    or self.read_col_pos_y(p) != want:
                print(f"  ! shape displacement write failed @0x{addr:X}")
                all_ok = False
        if displaced and not applied:
            print("  ! no live player instances to displace collision shape on")
            return False
        return all_ok

    def read_physics_time(self, player: PlayerRef):
        if not player.state_members:
            return None
        raw = self.mem.read(player.state_members + VARIANT_SIZE + VALUE_OFFSET, 8)
        return struct.unpack("<d", raw)[0] if raw else None

    def select_primary(self, players: list[PlayerRef]):
        """Pick the actually-simulating local player among candidates.

        Menus and leftover scenes can hold extra PlayerController instances
        that look identical (can_move, not replay) but never tick. The real
        local player's state.physics_time advances every physics frame;
        frozen dummies' does not.
        """
        candidates = [p for p in players if not p.is_replay and p.state_members]
        if not candidates:
            locals_ = [p for p in players if not p.is_replay]
            return locals_[0] if locals_ else None
        if len(candidates) == 1:
            return candidates[0]
        first = {p.segment: self.read_physics_time(p) for p in candidates}
        time.sleep(0.15)
        best, best_delta = None, 0.001
        for p in candidates:
            now = self.read_physics_time(p)
            if now is None or first[p.segment] is None:
                continue
            delta = now - first[p.segment]
            if delta > best_delta:
                best, best_delta = p, delta
        return best if best is not None else candidates[0]

    def read_velocity(self, player: PlayerRef):
        if not player.velocity_addr:
            return None
        raw = self.mem.read(player.velocity_addr, 12)
        return struct.unpack("<3f", raw) if raw else None

    def write_velocity_y(self, player: PlayerRef, vel_y: float) -> bool:
        """Set vertical velocity in both the movement state and the body.

        do_movement copies native body velocity into the state at the start
        of every physics tick, so writing only the state is usually lost;
        writing both makes the value stick deterministically.
        """
        ok = False
        vel = self.read_velocity(player)
        if vel is not None:
            ok = self.mem.write(player.velocity_addr,
                                struct.pack("<3f", vel[0], vel_y, vel[2]))
        if player.native_velocity_addr:
            raw = self.mem.read(player.native_velocity_addr, 12)
            if raw:
                nx, _, nz = struct.unpack("<3f", raw)
                ok = self.mem.write(player.native_velocity_addr,
                                    struct.pack("<3f", nx, vel_y, nz)) or ok
        return ok

    # Fields the game itself rewrites at runtime (spawn placement, user
    # sensitivity settings). Bulk restore/preset applies must not stomp them;
    # they can still be set explicitly with --set.
    RUNTIME_OWNED = ("respawn_position", "mouse_sensitivity",
                      "controller_sensitivity")

    @staticmethod
    def _close(got, want) -> bool:
        if got is None:
            return False
        if isinstance(want, list):
            return (isinstance(got, list) and len(got) == len(want)
                    and all(abs(g - float(w)) <= 1e-6 for g, w in zip(got, want)))
        return abs(got - float(want)) <= 1e-9

    def apply(self, values: dict) -> bool:
        """Write values to every known array; verify each write by reading back."""
        if not self.mem.is_alive():
            print("  ! game process has exited; nothing to patch")
            return False
        all_ok = True
        failures = 0
        applied_any = False
        for arr in self.arrays:
            if not self.data.validate(self.mem, arr):
                print(f"  - skipping stale array @ 0x{arr:X} "
                      "(no longer matches the movement-data signature)")
                continue
            applied_any = True
            for name, value in values.items():
                if not self.data.write_field(self.mem, arr, name, value):
                    failures += 1
                    all_ok = False
                    if failures >= 3:
                        print(f"  ! writes keep failing (last: {name} @ 0x{arr:X}, "
                              f"Win32 error {ctypes.get_last_error()}); aborting — "
                              "the game may have closed or the array moved (rescan)")
                        return False
                    continue
                got = self.data.read_field(self.mem, arr, name)
                if not self._close(got, value):
                    print(f"  ! verify failed: {name} @ 0x{arr:X} read {got}, wanted {value}")
                    all_ok = False
        if not applied_any:
            print("  ! no live movement-data arrays (all scan hits were stale); rescan")
            return False
        return all_ok

    def restore(self) -> bool:
        values = {k: v for k, v in self.data.defaults().items()
                  if k not in self.RUNTIME_OWNED}
        ok = self.apply(values)
        # Also move displaced collision shapes back, so --restore recovers
        # from a trainer that was killed while noclip was on.
        players = self.find_players()
        if players:
            ok = self.set_shape_displaced(players, False) and ok
        return ok

    def dump(self):
        shown = None
        for arr in self.arrays:
            if not self.data.validate(self.mem, arr):
                print(f"  array @ 0x{arr:X} (stale — freed/reused since scan, skipped)")
                continue
            shown = arr
            print(f"  array @ 0x{arr:X}")
            for f in self.data.fields:
                v = self.data.read_field(self.mem, arr, f["name"])
                flag = "" if self._close(v, f["default"]) else "   <-- differs from stock"
                print(f"    {f['name']:<30} {v}{flag}")
            break  # values are identical across live copies in practice
        rest = [a for a in self.arrays if a != shown]
        if rest:
            print("  (other copies at " + ", ".join(f"0x{a:X}" for a in rest) + ")")


# --- CLI / hotkeys -----------------------------------------------------------

VK_END, VK_C, VK_SPACE = 0x23, 0x43, 0x20
# Number row and numpad digit, so either works.
KEYS_SPEED, KEYS_JUMP, KEYS_FLY, KEYS_NOCLIP = (0x31, 0x61), (0x32, 0x62), (0x33, 0x63), (0x34, 0x64)
KEYS_RESCAN, KEYS_RESTORE = (0x39, 0x69), (0x30, 0x60)
VK_PLUS_KEYS = (0xBB, 0x6B)    # =/+ main keyboard, numpad +
VK_MINUS_KEYS = (0xBD, 0x6D)   # - main keyboard, numpad -


def key_pressed(vk: int) -> bool:
    return bool(u32.GetAsyncKeyState(vk) & 0x8000)


def hotkey_loop(trainer: Trainer, intensity: float = 1.0,
                start_fly: bool = False, start_noclip: bool = False):
    data = trainer.data
    active: dict[str, bool] = {name: False for name in data.presets}
    state = {"fly": False, "noclip": False}
    players: list[PlayerRef] = []
    prev: dict[int, bool] = {}
    intensity = clamp_intensity(intensity)
    tick = 0

    def edge(vk: int) -> bool:
        now = key_pressed(vk)
        was = prev.get(vk, False)
        prev[vk] = now
        return now and not was

    def edge_any(vks) -> bool:
        return any(edge(vk) for vk in vks)

    def merged_values() -> dict:
        # Only fields some preset owns: active presets win (scaled by
        # intensity), inactive ones fall back to stock. While fly/noclip
        # is on, gravity is forced to zero. Runtime-owned fields untouched.
        defaults = data.defaults()
        out = {}
        for name in active:
            for field_name in data.presets[name]["values"]:
                out[field_name] = defaults[field_name]
        for name, on in active.items():
            if on:
                for field_name, value in data.presets[name]["values"].items():
                    out[field_name] = scaled_preset_value(
                        defaults[field_name], value, intensity)
        if state["fly"] or state["noclip"]:
            out["gravity_scale_base"] = 0.0
            out["gravity_scale_max"] = 0.0
        elif "gravity_scale_base" not in out:
            # Make sure turning fly/noclip off restores gravity even when
            # no preset owns those fields.
            out["gravity_scale_base"] = defaults["gravity_scale_base"]
            out["gravity_scale_max"] = defaults["gravity_scale_max"]
        return out

    primary: list = [None]   # selected local PlayerRef (boxed for closures)

    def refresh_players() -> list[PlayerRef]:
        nonlocal players
        players = trainer.find_players()
        primary[0] = trainer.select_primary(players)
        return players

    def local_players() -> list[PlayerRef]:
        return [p for p in players if not p.is_replay]

    def primary_player():
        # Fly velocity goes to the one simulating local player only;
        # other non-replay instances (menus, dummies) must not be driven.
        if primary[0] is not None:
            return primary[0]
        locals_ = local_players()
        return locals_[0] if locals_ else None

    def set_fly(on: bool, announce: bool = True) -> None:
        state["fly"] = on
        if not on:
            p = primary_player()
            if p:
                trainer.write_velocity_y(p, 0.0)   # don't launch on exit
        ok = trainer.apply(merged_values())
        if announce:
            print(f"Fly {'ON' if on else 'OFF'}: {'OK' if ok else 'FAILED'}",
                  flush=True)

    def set_noclip(on: bool, announce: bool = True) -> None:
        state["noclip"] = on
        refresh_players()
        ok = trainer.set_shape_displaced(players, displaced=on)
        ok = trainer.apply(merged_values()) and ok
        if announce:
            print(f"Noclip {'ON' if on else 'OFF'}: {'OK' if ok else 'FAILED'}",
                  flush=True)
        if on:
            for p in local_players():
                print(f"  {p.describe()}", flush=True)

    def restore_all() -> bool:
        if state["fly"] or state["noclip"]:
            p = primary_player()
            if p:
                trainer.write_velocity_y(p, 0.0)
        state["fly"] = state["noclip"] = False
        for k in active:
            active[k] = False
        return trainer.restore()

    print("Hotkeys: 1=speed  2=jump  3=fly  4=noclip  +/-=intensity  "
          "9=rescan  0=restore  END=quit | flying: Space=up, C=down",
          flush=True)
    refresh_players()
    for p in players:
        print(f"  {p.describe()}", flush=True)
    if primary[0] is not None:
        print(f"Fly target: player @0x{primary[0].segment:X}", flush=True)
    if start_fly:
        set_fly(True)
    if start_noclip:
        set_noclip(True)

    while True:
        tick += 1
        if edge(VK_END):
            print("Restoring defaults and exiting...", flush=True)
            restore_all()
            return
        if edge_any(KEYS_RESTORE):
            print("Restore:", "OK" if restore_all() else "FAILED", flush=True)
        if edge_any(KEYS_RESCAN):
            hits = trainer.rescan()
            refresh_players()
            print(f"Rescan: {len(hits)} array(s), {len(players)} player(s) found",
                  flush=True)
            if hits and (any(active.values()) or state["fly"] or state["noclip"]):
                trainer.apply(merged_values())
            if state["noclip"]:
                trainer.set_shape_displaced(players, displaced=True)
        for vks, pname in ((KEYS_SPEED, "speed"), (KEYS_JUMP, "jump")):
            if pname in data.presets and edge_any(vks):
                active[pname] = not active[pname]
                ok = trainer.apply(merged_values())
                print(f"Preset '{pname}' {'ON' if active[pname] else 'OFF'} "
                      f"(intensity {intensity:g}x): {'OK' if ok else 'FAILED'}",
                      flush=True)
        if edge_any(KEYS_FLY):
            set_fly(not state["fly"])
        if edge_any(KEYS_NOCLIP):
            set_noclip(not state["noclip"])
        plus_edge = edge_any(VK_PLUS_KEYS)
        minus_edge = edge_any(VK_MINUS_KEYS)
        if plus_edge or minus_edge:
            intensity = clamp_intensity(
                intensity + INTENSITY_STEP if plus_edge
                else intensity - INTENSITY_STEP)
            print(f"Intensity: {intensity:g}x (fly speed {BASE_FLY_SPEED * intensity:g})",
                  flush=True)
            if any(active.values()) or state["fly"] or state["noclip"]:
                trainer.apply(merged_values())

        # Fly/noclip vertical control: hold velocity.y at the target so the
        # movement model's gravity (zeroed) and jump logic can't drift it.
        if state["fly"] or state["noclip"]:
            speed = BASE_FLY_SPEED * intensity
            target_y = (speed if key_pressed(VK_SPACE) else 0.0) \
                + (-speed if key_pressed(VK_C) else 0.0)
            target = primary_player()
            if target:
                trainer.write_velocity_y(target, target_y)
            # Periodically re-validate: level loads recreate the player
            # instance, leaving our pointers stale. Also switch primary if
            # a different instance is the one actually simulating.
            if tick % 40 == 0:
                current = primary_player()
                if current is not None and len(local_players()) > 1:
                    before = trainer.read_physics_time(current)
                    time.sleep(0.1)
                    after = trainer.read_physics_time(current)
                    if before is not None and after is not None and after - before <= 0.001:
                        better = trainer.select_primary(players)
                        if better is not None and better.segment != current.segment:
                            primary[0] = better
                            print(f"Switched fly target to simulating player "
                                  f"@0x{better.segment:X}", flush=True)
                stale = [p for p in local_players()
                         if not trainer._player_still_valid(p)
                         or trainer.read_velocity(p) is None]
                if stale or not local_players():
                    trainer.rescan()
                    refresh_players()
                    trainer.apply(merged_values())
                    if state["noclip"]:
                        trainer.set_shape_displaced(players, displaced=True)
                    print(f"Re-acquired {len(players)} player(s) after stale pointers",
                          flush=True)
        time.sleep(0.05)


def parse_set_args(items: list[str], data: MovementData) -> dict:
    out = {}
    for item in items:
        name, sep, raw = item.partition("=")
        if not sep or name not in data.index:
            raise SystemExit(f"bad --set value {item!r}; field must be one of: "
                             + ", ".join(data.index))
        field = data.fields[data.index[name]]
        if field["type"] == "f":
            out[name] = float(raw)
        else:
            out[name] = [float(x) for x in raw.split(",")]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IVME external movement trainer")
    ap.add_argument("--exe", default="ivme.exe", help="game process name")
    ap.add_argument("--scan-only", action="store_true", help="find arrays and print values")
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                    help="set one field (repeatable), e.g. --set max_speed=7.5")
    ap.add_argument("--preset", choices=["speed", "jump"], help="apply a preset once")
    ap.add_argument("--intensity", type=float, default=1.0,
                    help="cheat intensity multiplier 0.25-4 (presets and fly speed)")
    ap.add_argument("--fly", action="store_true",
                    help="start interactive mode with fly ON")
    ap.add_argument("--noclip", action="store_true",
                    help="start interactive mode with noclip ON")
    ap.add_argument("--restore", action="store_true",
                    help="write stock defaults, re-enable collision, and exit")
    ap.add_argument("--full-scan", action="store_true",
                    help="scan all committed memory instead of heap pages only (slow)")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    data = MovementData(os.path.join(here, "movement_data.json"))

    pids = find_pids(args.exe)
    if not pids:
        print(f"{args.exe} is not running. Start the game first.")
        return 1
    print(f"Attaching to {args.exe} pid {pids[0]}" + (f" (also: {pids[1:]})" if len(pids) > 1 else ""))
    mem = GameMemory(pids[0])
    trainer = Trainer(mem, data)
    try:
        print("Scanning for PlayerMovementData arrays (a few seconds)...")
        hits = trainer.rescan(full_scan=args.full_scan)
        print(f"Found {len(hits)} array(s)")
        if not hits:
            print("No match. Load into a level first (the resource may not be "
                  "loaded at the main menu), or the game updated its tuning.")
            return 1
        if args.scan_only:
            trainer.dump()
            for p in trainer.find_players():
                print(f"  {p.describe()}")
            return 0
        if args.restore:
            print("Restore:", "OK" if trainer.restore() else "FAILED")
            return 0
        if args.preset:
            intensity = clamp_intensity(args.intensity)
            defaults = data.defaults()
            values = {k: scaled_preset_value(defaults[k], v, intensity)
                      for k, v in data.presets[args.preset]["values"].items()}
            print(f"Applying preset '{args.preset}' (intensity {intensity:g}x):",
                  "OK" if trainer.apply(values) else "FAILED")
            return 0
        if args.set:
            values = parse_set_args(args.set, data)
            print(f"Applying {values}:", "OK" if trainer.apply(values) else "FAILED")
            return 0
        trainer.dump()
        hotkey_loop(trainer, intensity=args.intensity,
                    start_fly=args.fly, start_noclip=args.noclip)
        return 0
    finally:
        mem.close()


if __name__ == "__main__":
    sys.exit(main())
